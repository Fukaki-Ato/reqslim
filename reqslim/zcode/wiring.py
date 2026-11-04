"""ZCode provider-table wiring: discovery, baseUrl rewriting, proxied-table generation.

One module so the table logic lives in exactly one place:

* ``tools/install_zcode.py`` calls it once, at install time;
* the proxy's table watcher (``--autostart``) calls it again whenever ZCode's
  bundled table changes on disk, which is what keeps reqslim working across
  ZCode updates without a reinstall.

Only ``anthropic-messages`` providers whose ``baseUrl`` matches a proxied
upstream or an explicitly owned local URL are touched; everything else
passes through verbatim.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

PROVIDER_TABLE = Path("config") / "provider" / "zcode-builtin.json"


# ------------------------------------------------------------------ discovery
def _windows_candidates() -> list[Path]:
    home = Path.home()
    roots = [
        Path(os.environ["ZCODE_HOME"]) if os.environ.get("ZCODE_HOME") else None,
        Path("D:/Zcode"),
        Path("C:/Zcode"),
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "ZCode",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "ZCode",
        Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "Programs" / "ZCode",
    ]
    return [root for root in roots if root]


def _posix_candidates() -> list[Path]:
    home = Path.home()
    roots = [
        Path(os.environ["ZCODE_HOME"]) if os.environ.get("ZCODE_HOME") else None,
        Path("/Applications/ZCode.app/Contents/Resources"),
        Path("/opt/ZCode/resources"),
        Path("/opt/zcode/resources"),
        Path("/usr/lib/zcode/resources"),
        home / "Applications" / "ZCode.app" / "Contents" / "Resources",
    ]
    return [root for root in roots if root]


def _as_table(root: Path) -> Path | None:
    """Accept an install root or a resources dir; return the table path."""
    if (root / PROVIDER_TABLE).exists():
        return root / PROVIDER_TABLE
    if (root / "resources" / PROVIDER_TABLE).exists():
        return root / "resources" / PROVIDER_TABLE
    return None


def find_table(explicit: str | None = None) -> Path | None:
    """Locate the shipped builtin provider table, or ``None``.

    Used by the watcher's rediscovery fallback: when the remembered path
    disappears (ZCode moved or was reinstalled elsewhere), the standard
    candidate list is searched again.
    """
    if explicit:
        found = _as_table(Path(explicit))
        return found
    candidates = _windows_candidates() if os.name == "nt" else _posix_candidates()
    for root in candidates:
        found = _as_table(root)
        if found:
            return found
    return None


# ------------------------------------------------------------------- rewriting
def _load_table(source: Path) -> dict[str, Any]:
    """Read and shape-check a provider table.

    A valid JSON document with the wrong shape must be reported as data we do
    not understand, not crash a watcher thread with ``AttributeError``.
    """
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("provider table root must be an object")
    config = data.setdefault("config", {})
    if not isinstance(config, dict):
        raise ValueError("provider table config must be an object")
    rules = config.setdefault("providerConfigRules", {})
    if not isinstance(rules, dict):
        raise ValueError("providerConfigRules must be an object")
    for key in ("providerRules", "templateRules"):
        entries = rules.get(key)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise ValueError(f"{key} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"{key} entries must be objects")
            if not isinstance(entry.get("config", {}), dict):
                raise ValueError(f"{key} entry config must be an object")
            api = entry.get("config", {}).get("api", {})
            if not isinstance(api, dict):
                raise ValueError(f"{key} entry api must be an object")
    return data


def _is_local(url: str) -> bool:
    try:
        return urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}
    except ValueError:
        return False


def _provider_apis(
    data: Mapping[str, Any],
) -> Iterator[tuple[str, dict[str, Any], dict[str, Any]]]:
    rules = data["config"]["providerConfigRules"]
    for key in ("providerRules", "templateRules"):
        for entry in rules.get(key) or []:
            api = (entry.get("config") or {}).get("api") or {}
            if api.get("type") == "anthropic-messages":
                yield key, entry, api


def _provider_identity(section: str, entry: Mapping[str, Any]) -> tuple[str, str]:
    ident = {key: entry[key] for key in ("providerId", "templateId") if entry.get(key)}
    if not ident:
        ident = json.loads(json.dumps(entry))
        ident.get("config", {}).get("api", {}).pop("baseUrl", None)
    return section, json.dumps(ident, sort_keys=True, ensure_ascii=True)


def _original_upstreams(data: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    originals: dict[tuple[str, str], str] = {}
    ambiguous: set[tuple[str, str]] = set()
    for section, entry, api in _provider_apis(data):
        identity = _provider_identity(section, entry)
        current = str(api.get("baseUrl", "")).rstrip("/")
        if identity in originals and originals[identity] != current:
            ambiguous.add(identity)
        originals[identity] = current
    return {key: value for key, value in originals.items() if key not in ambiguous}


def _make_writable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        os.chmod(path, mode | stat.S_IWUSR)
    except OSError:
        pass


def _mark_readonly(path: Path) -> None:
    """Keep ZCode's cloud refresher from overwriting a rewritten cache.

    The shipped client treats a failed refresh as "keep the local table", so a
    read-only cache makes the proxied table win deterministically. The
    installer/watcher clear the flag before their own rewrite.
    """
    try:
        mode = path.stat().st_mode
        os.chmod(path, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    except OSError:
        pass


def _atomic_write(path: Path, text: str, *, readonly: bool = False) -> None:
    """Write via a unique temp file so installer/watcher races cannot collide."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _make_writable(path)
    handle, name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp, path)
        if readonly:
            _mark_readonly(path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def rewrite_config(
    source: Path, mapping: dict[str, str]
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Rewrite provider baseUrls.

    ``mapping`` is original upstream URL -> local base URL. Returns the new
    table plus (providerId, local base) for every entry touched.
    """
    data = _load_table(source)
    touched: list[tuple[str, str]] = []
    for _, entry, api in _provider_apis(data):
        current = str(api.get("baseUrl", "")).rstrip("/")
        local = mapping.get(current)
        if local is not None:
            api["baseUrl"] = local
            ident = entry.get("providerId") or entry.get("templateId") or "?"
            touched.append((ident, local))
    return data, touched


def uncovered_upstreams(source: Path, mapping: dict[str, str]) -> list[str]:
    """Anthropic-protocol upstreams present in the table but not proxied.

    Read from the *original* table and printed as a hint: providers on those
    gateways keep bypassing reqslim until they are passed with
    ``--extra-upstream``. Loopback entries are skipped -- they only appear in
    already-rewritten tables.
    """
    data = _load_table(source)
    found: set[str] = set()
    for _, _, api in _provider_apis(data):
        current = str(api.get("baseUrl", "")).rstrip("/")
        if current and current not in mapping and not _is_local(current):
            found.add(current)
    return sorted(found)


def generate_proxied_table(
    source: Path, mapping: dict[str, str], out: Path
) -> list[tuple[str, str]]:
    """Rewrite ``source`` and write the result to ``out``, atomically.

    ZCode's CLI reads this file at process start; ``os.replace`` guarantees it
    never observes a half-written table. Returns the touched entries. Raises
    ``FileNotFoundError``/``OSError`` for unreadable source or unwritable
    output, and ``ValueError`` when nothing in the table matches ``mapping``
    (which means the mapping itself is stale and the installer must be
    re-run -- not something the watcher can fix by rewriting).
    """
    data, touched = rewrite_config(source, mapping)
    if not touched:
        raise ValueError(
            f"no provider in {source} uses any of: {', '.join(sorted(mapping))}"
        )
    _atomic_write(out, json.dumps(data, ensure_ascii=False, indent=2))
    return touched


# ----------------------------------------------------- runtime provider cache
#: ZCode's cloud-refreshed runtime provider cache:
#:     ~/.zcode/v2/runtime/provider/<platform>/<app version>/endpoint-<sha256(origin)[:32]>/zcode-builtin.json
#: The agent host reads the cache for the running version FIRST and only
#: falls back to the env-var table when that read fails (verified in the
#: 3.16/3.17 asar), so an unrewritten cache silently bypasses the proxy even
#: with ZCODE_BUILTIN_PROVIDER_CONFIG_FILE set. Every ZCode update unpacks a
#: new per-version directory, so the glob must span all of them.
RUNTIME_CACHE_GLOB = "*/*/endpoint-*/zcode-builtin.json"
CACHE_BACKUP_SUFFIX = ".bak-reqslim"
CACHE_STATE_SUFFIX = ".reqslim-state.json"

_CacheState = dict[tuple[str, str], tuple[str, dict[str, set[str]]]]


def _state_url(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("cache ownership URLs must be nonempty strings")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("cache ownership URLs must be HTTP(S) URLs")
    _ = parsed.port
    return value


def _load_cache_state(path: Path) -> _CacheState:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError(f"unsupported cache ownership state: {path}")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"cache ownership entries must be a list: {path}")
    state: _CacheState = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("cache ownership entries must be objects")
        section, identity = entry.get("section"), entry.get("identity")
        if section not in {"providerRules", "templateRules"} or not isinstance(identity, str):
            raise ValueError("invalid cache ownership identity")
        if not isinstance(json.loads(identity), dict):
            raise ValueError("cache ownership identity must encode an object")
        key = section, identity
        if key in state:
            raise ValueError("duplicate cache ownership identity")
        upstream = _state_url(entry.get("upstream"))
        groups = entry.get("claims")
        if not isinstance(groups, list) or not groups:
            raise ValueError("cache ownership claims must be a nonempty list")
        claims: dict[str, set[str]] = {}
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError("cache ownership claims must be objects")
            original = _state_url(group.get("upstream"))
            locals_ = group.get("locals")
            if original in claims or not isinstance(locals_, list) or not locals_:
                raise ValueError("invalid or duplicate cache ownership claim")
            claims[original] = {_state_url(url).rstrip("/") for url in locals_}
        if upstream not in claims:
            raise ValueError("current cache ownership upstream has no claims")
        state[key] = upstream, claims
    return state


def _cache_state_text(state: _CacheState) -> str:
    return json.dumps({
        "version": 1,
        "entries": [{
            "section": section,
            "identity": identity,
            "upstream": upstream,
            "claims": [{"upstream": original, "locals": sorted(locals_)}
                       for original, locals_ in sorted(claims.items())],
        } for (section, identity), (upstream, claims) in sorted(state.items())],
    }, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


def _owned_upstream(
    state: _CacheState, identity: tuple[str, str], current: str, *, historical: bool = False,
) -> str | None:
    record = state.get(identity)
    if record is None:
        return None
    upstream, claims = record
    if not historical and current in claims[upstream]:
        return upstream
    originals = [original for original, locals_ in claims.items() if current in locals_]
    if len({original.rstrip("/") for original in originals}) > 1:
        raise ValueError("ambiguous cache ownership for a provider")
    return originals[0] if originals else None


def _record_cache_claim(
    state: _CacheState, identity: tuple[str, str], upstream: str, local: str,
) -> None:
    _state_url(upstream)
    local = _state_url(local).rstrip("/")
    claims = state.get(identity, (upstream, {}))[1]
    locals_ = claims.setdefault(upstream, set())
    for original in list(claims):
        if original != upstream and original.rstrip("/") == upstream.rstrip("/"):
            locals_.update(claims.pop(original))
    locals_.add(local)
    state[identity] = upstream, claims


def runtime_cache_root(root: Path | None = None) -> Path:
    """The runtime provider-cache root; ``root`` overrides (tests)."""
    return root or (Path.home() / ".zcode" / "v2" / "runtime" / "provider")


def runtime_cache_files(root: Path | None = None) -> list[Path]:
    """Every runtime cache table on disk, across all ZCode versions."""
    base = runtime_cache_root(root)
    return sorted(base.glob(RUNTIME_CACHE_GLOB)) if base.is_dir() else []


def rewrite_runtime_cache_file(
    path: Path, mapping: dict[str, str], dry_run: bool = False,
    previous_mapping: dict[str, str] | None = None,
) -> str:
    """Rewrite one runtime cache table if it routes a mapped upstream.

    Returns the action: ``rewritten`` (also reported under ``dry_run``,
    which writes nothing), ``untouched`` (already points at the desired local
    ports), ``unrelated`` (no anthropic provider matches the mapping) or
    ``unreadable``. Errors from the write itself propagate as OSError so
    callers can retry on a mid-write clash with ZCode's cloud refresher.

    The ownership sidecar records original upstreams and historical local
    URLs per provider/template identity, including entries added after the
    first backup. It is persisted before the table. ``previous_mapping`` is
    old local URL -> original upstream URL, for migrating legacy installs.
    A backup alone does not prove that a later loopback value is ours.

    The one-time ``.bak-reqslim`` backup is created before the first real
    rewrite and never overwritten afterwards: the cloud refresher rewrites
    this file too, and the only pristine copy of the original must survive
    every re-run and refresh.
    """
    backup = path.with_name(path.name + CACHE_BACKUP_SUFFIX)
    state_path = path.with_name(path.name + CACHE_STATE_SUFFIX)
    try:
        data = _load_table(path)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return "unreadable"

    state = _load_cache_state(state_path)
    previous_state = _cache_state_text(state)
    originals: dict[tuple[str, str], str] = {}
    if backup.exists():
        try:
            originals = _original_upstreams(_load_table(backup))
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return "unreadable"
    forward = {upstream.rstrip("/"): local for upstream, local in mapping.items()}
    previous = {local.rstrip("/"): upstream for local, upstream in (previous_mapping or {}).items()}
    targets: dict[str, set[str]] = {}
    for upstream, local in mapping.items():
        targets.setdefault(local.rstrip("/"), set()).add(upstream)
    plans = []
    identities: dict[tuple[str, str], set[str]] = {}
    for section, entry, api in _provider_apis(data):
        raw_current = str(api.get("baseUrl", ""))
        current = raw_current.rstrip("/")
        identity = _provider_identity(section, entry)
        upstream = _owned_upstream(state, identity, current)
        owned = upstream is not None
        if upstream is None:
            upstream = previous.get(current)
            owned = upstream is not None
        if upstream is None and current in forward:
            upstream = raw_current
        if upstream is None and current in targets:
            candidates = targets[current]
            if len({url.rstrip("/") for url in candidates}) != 1:
                raise ValueError("ambiguous cache ownership for a provider; supply previous_mapping")
            upstream = next(iter(candidates))
            original = originals.get(identity)
            if original is not None and original not in {current, upstream.rstrip("/")}:
                raise ValueError("unconfirmed cache ownership for a provider; supply previous_mapping")
            owned = True
        if upstream is None:
            continue
        desired = forward.get(upstream.rstrip("/"), upstream)
        plans.append((identity, api, current, upstream, desired, owned))
        identities.setdefault(identity, set()).add(upstream.rstrip("/"))
    if any(len(upstreams) > 1 for upstreams in identities.values()):
        raise ValueError("ambiguous duplicate provider identity in cache ownership")
    changed = False
    for identity, api, current, upstream, desired, owned in plans:
        if owned:
            _record_cache_claim(state, identity, upstream, current)
        if upstream.rstrip("/") in forward:
            _record_cache_claim(state, identity, upstream, desired)
        if desired.rstrip("/") != current:
            api["baseUrl"] = desired
            changed = True
    action = "rewritten" if changed else "untouched" if plans else "unrelated"
    if dry_run:
        return action
    if changed and not backup.exists():
        shutil.copy2(path, backup)
    state_text = _cache_state_text(state)
    if state_text != previous_state:
        _atomic_write(state_path, state_text)
    if changed:
        _atomic_write(path, json.dumps(data, ensure_ascii=False), readonly=True)
    return action


def rewrite_runtime_caches(
    mapping: dict[str, str],
    root: Path | None = None,
    log=None,
    dry_run: bool = False,
    previous_mapping: dict[str, str] | None = None,
) -> list[tuple[Path, str]]:
    """Rewrite every runtime cache table; returns ``(path, action)`` pairs.

    Shared by the installer (one pass at install time) and by the proxy's
    cache watcher (whenever ZCode ships a new version directory), so both
    paths behave identically. ``log`` receives one ``[action] path`` line
    per file when given.
    """
    results: list[tuple[Path, str]] = []
    for path in runtime_cache_files(root):
        action = rewrite_runtime_cache_file(
            path, mapping, dry_run=dry_run, previous_mapping=previous_mapping,
        )
        results.append((path, action))
        if log:
            log(f"[{action}] {path}")
    return results


@dataclass(frozen=True)
class CacheRestorePlan:
    data: dict[str, Any] | None
    exists: bool
    changed: bool
    use_backup: bool
    managed: bool


def plan_runtime_cache_restore(
    path: Path, reverse_mapping: dict[str, str] | None = None,
) -> CacheRestorePlan:
    backup = path.with_name(path.name + CACHE_BACKUP_SUFFIX)
    state_path = path.with_name(path.name + CACHE_STATE_SUFFIX)
    state = _load_cache_state(state_path)
    backup_data = _load_table(backup) if backup.exists() else None
    exists = path.exists()
    data = _load_table(path) if exists else backup_data
    if data is None:
        return CacheRestorePlan(None, exists, False, False, state_path.exists())
    reverse = {key.rstrip("/"): value for key, value in (reverse_mapping or {}).items()}
    changed = False
    for section, entry, api in _provider_apis(data):
        current = str(api.get("baseUrl", "")).rstrip("/")
        remote = _owned_upstream(
            state, _provider_identity(section, entry), current, historical=not exists,
        )
        if remote is None:
            remote = reverse.get(current)
        if remote is not None and remote != api.get("baseUrl"):
            api["baseUrl"] = remote
            changed = True
    use_backup = backup_data is not None and data == backup_data and (exists or not changed)
    return CacheRestorePlan(data, exists, changed, use_backup,
                            changed or backup_data is not None or state_path.exists())


def restore_runtime_caches(
    root: Path | None = None,
    log=None,
    reverse_mapping: dict[str, str] | None = None,
    *,
    strict: bool = False,
    errors: list[str] | None = None,
    dry_run: bool = False,
) -> list[Path]:
    """Put every backed-up runtime cache back; ``--uninstall`` uses this.

    Ownership records take precedence over ``reverse_mapping`` (local URL ->
    upstream URL). Only exact owned values are undone; later providers,
    metadata and user proxy changes survive. An equivalent pristine backup
    is moved back to preserve its exact text; mixed backups are repaired
    before use. Backups and sidecars are consumed only after table repair.
    """
    log = log or (lambda message: None)
    restored: list[Path] = []
    failures: list[str] = []

    def failed(message: str) -> None:
        failures.append(message)
        log(message)

    reverse = {key.rstrip("/"): value for key, value in (reverse_mapping or {}).items()}
    base = runtime_cache_root(root)
    if base.is_dir():
        paths = set(runtime_cache_files(root))
        for suffix in (CACHE_BACKUP_SUFFIX, CACHE_STATE_SUFFIX):
            paths.update(item.with_name(item.name[: -len(suffix)])
                         for item in base.glob(RUNTIME_CACHE_GLOB + suffix))
        for path in sorted(paths):
            backup = path.with_name(path.name + CACHE_BACKUP_SUFFIX)
            state_path = path.with_name(path.name + CACHE_STATE_SUFFIX)
            previous_mode: int | None = None
            try:
                plan = plan_runtime_cache_restore(path, reverse)
                if plan.data is None:
                    if state_path.exists() and not dry_run:
                        _make_writable(state_path)
                        state_path.unlink()
                    continue
                if not plan.managed:
                    continue
                if not dry_run:
                    if plan.exists:
                        previous_mode = path.stat().st_mode
                    if plan.use_backup:
                        if plan.exists:
                            _make_writable(path)
                        os.replace(backup, path)
                        _make_writable(path)
                    elif plan.changed or not plan.exists:
                        _atomic_write(path, json.dumps(plan.data, ensure_ascii=False))
                    else:
                        _make_writable(path)
                    if backup.exists():
                        _make_writable(backup)
                        backup.unlink()
                    if state_path.exists():
                        _make_writable(state_path)
                        state_path.unlink()
                restored.append(path)
                log(f"restored runtime cache: {path}")
            except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
                if previous_mode is not None:
                    try:
                        os.chmod(path, previous_mode)
                    except OSError:
                        pass
                failed(f"could not restore {path}: {exc}")
    if errors is not None:
        errors.extend(failures)
    if strict and failures:
        raise OSError("runtime cache restoration failed: " + "; ".join(failures))
    return restored
