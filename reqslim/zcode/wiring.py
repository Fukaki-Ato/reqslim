"""ZCode provider-table wiring: discovery, baseUrl rewriting, proxied-table generation.

One module so the table logic lives in exactly one place:

* ``tools/install_zcode.py`` calls it once, at install time;
* the proxy's table watcher (``--autostart``) calls it again whenever ZCode's
  bundled table changes on disk, which is what keeps reqslim working across
  ZCode updates without a reinstall.

The rewrite rules are the installer's original ones: only ``anthropic-messages``
providers whose ``baseUrl`` matches a proxied upstream are touched; loopback
entries are skipped; everything else passes through verbatim.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterator, Mapping

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


def _entries(data: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    if not isinstance(data, Mapping):
        return
    rules = (data.get("config") or {}).get("providerConfigRules") or {}
    for key in ("providerRules", "templateRules"):
        for entry in rules.get(key) or []:
            if isinstance(entry, Mapping):
                yield dict(entry)


def _local_bases(data: Mapping[str, Any]) -> set[str]:
    if not isinstance(data, Mapping):
        return set()
    found: set[str] = set()
    for entry in _entries(data):
        api = (entry.get("config") or {}).get("api") or {}
        if not isinstance(api, Mapping) or api.get("type") != "anthropic-messages":
            continue
        url = str(api.get("baseUrl", "")).rstrip("/")
        host = url.split("//", 1)[-1].split("/", 1)[0]
        if host.startswith(("127.0.0.1", "localhost")):
            found.add(url)
    return found


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
    rules = data["config"]["providerConfigRules"]
    touched: list[tuple[str, str]] = []
    for key in ("providerRules", "templateRules"):
        for entry in rules.get(key) or []:
            api = (entry.get("config") or {}).get("api") or {}
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
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("provider table root must be an object")
    rules = (data.get("config") or {}).get("providerConfigRules") or {}
    if not isinstance(rules, dict):
        raise ValueError("providerConfigRules must be an object")
    found: set[str] = set()
    for key in ("providerRules", "templateRules"):
        entries = rules.get(key) or []
        if not isinstance(entries, list):
            raise ValueError(f"{key} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"{key} entries must be objects")
            api = (entry.get("config") or {}).get("api") or {}
            if not isinstance(api, dict):
                raise ValueError(f"{key} entry api must be an object")
            if api.get("type") != "anthropic-messages":
                continue
            current = str(api.get("baseUrl", "")).rstrip("/")
            host = current.split("//", 1)[-1].split("/", 1)[0]
            if current and current not in mapping and not host.startswith(("127.0.0.1", "localhost")):
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


def runtime_cache_root(root: Path | None = None) -> Path:
    """The runtime provider-cache root; ``root`` overrides (tests)."""
    return root or (Path.home() / ".zcode" / "v2" / "runtime" / "provider")


def runtime_cache_files(root: Path | None = None) -> list[Path]:
    """Every runtime cache table on disk, across all ZCode versions."""
    base = runtime_cache_root(root)
    return sorted(base.glob(RUNTIME_CACHE_GLOB)) if base.is_dir() else []


def rewrite_runtime_cache_file(
    path: Path, mapping: dict[str, str], dry_run: bool = False
) -> str:
    """Rewrite one runtime cache table if it routes a mapped upstream.

    Returns the action: ``rewritten`` (also reported under ``dry_run``,
    which writes nothing), ``untouched`` (already points at the desired local
    ports), ``unrelated`` (no anthropic provider matches the mapping) or
    ``unreadable``. Errors from the write itself propagate as OSError so
    callers can retry on a mid-write clash with ZCode's cloud refresher.

    A table rewritten by an older install carries loopback URLs, not the
    original gateway URLs, so a plain re-run cannot see that the mapping
    changed. If the pristine ``.bak-reqslim`` exists, it is used as the source
    for the new mapping; this is what makes changing ``--port`` work.

    The one-time ``.bak-reqslim`` backup is created before the first real
    rewrite and never overwritten afterwards: the cloud refresher rewrites
    this file too, and the only pristine copy of the original must survive
    every re-run and refresh.
    """
    targets = {url.rstrip("/") for url in mapping.values()}
    backup = path.with_name(path.name + CACHE_BACKUP_SUFFIX)
    try:
        before = _load_table(path)
        local_before = _local_bases(before)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return "unreadable"

    try:
        data, touched = rewrite_config(path, mapping)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return "unreadable"

    # A table rewritten by an older install carries loopback URLs, not the
    # original gateway URLs. If the desired local ports are not all present,
    # the pristine backup is the only source that still has the originals.
    if local_before and not targets.issubset(local_before) and backup.exists():
        try:
            backup_data, backup_touched = rewrite_config(backup, mapping)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            backup_touched = []
            backup_data = {}
        if backup_touched:
            data, touched = backup_data, backup_touched

    # If every desired local base is already present, this file is current.
    # Extra loopback entries (another reverse proxy, a previous listener that
    # is no longer mapped) must not cause an endless rewrite.
    if local_before and targets.issubset(local_before) and not touched:
        return "untouched"

    if not touched:
        return "untouched" if local_before else "unrelated"
    if dry_run:
        return "rewritten"
    if not backup.exists():
        shutil.copy2(path, backup)
    _atomic_write(path, json.dumps(data, ensure_ascii=False), readonly=True)
    return "rewritten"


def rewrite_runtime_caches(
    mapping: dict[str, str],
    root: Path | None = None,
    log=None,
    dry_run: bool = False,
) -> list[tuple[Path, str]]:
    """Rewrite every runtime cache table; returns ``(path, action)`` pairs.

    Shared by the installer (one pass at install time) and by the proxy's
    cache watcher (whenever ZCode ships a new version directory), so both
    paths behave identically. ``log`` receives one ``[action] path`` line
    per file when given.
    """
    results: list[tuple[Path, str]] = []
    for path in runtime_cache_files(root):
        action = rewrite_runtime_cache_file(path, mapping, dry_run=dry_run)
        results.append((path, action))
        if log:
            log(f"[{action}] {path}")
    return results


def restore_runtime_caches(
    root: Path | None = None,
    log=None,
    reverse_mapping: dict[str, str] | None = None,
) -> list[Path]:
    """Put every backed-up runtime cache back; ``--uninstall`` uses this.

    A cache left pointing at reqslim's ports outlives the proxy and turns
    every model request into ECONNREFUSED, so removing reqslim must also
    remove the rewrites. Backups are moved back and consumed. A rewritten
    cache WITHOUT a backup is repaired by inverse-mapping the known local
    ports when ``reverse_mapping`` (local URL -> upstream URL) is supplied;
    otherwise it is reported and left alone.
    """
    log = log or (lambda message: None)
    restored: list[Path] = []
    reverse = {key.rstrip("/"): value for key, value in (reverse_mapping or {}).items()}
    base = runtime_cache_root(root)
    if base.is_dir():
        for backup in sorted(base.glob(RUNTIME_CACHE_GLOB + CACHE_BACKUP_SUFFIX)):
            target = backup.with_name(backup.name[: -len(CACHE_BACKUP_SUFFIX)])
            try:
                if target.exists():
                    _make_writable(target)
                os.replace(backup, target)
                _make_writable(target)
                restored.append(target)
                log(f"restored from backup: {target}")
            except OSError as exc:
                log(f"could not restore {backup}: {exc}")
        for path in runtime_cache_files(root):
            if path.with_name(path.name + CACHE_BACKUP_SUFFIX).exists():
                continue
            try:
                data = _load_table(path)
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                continue
            if not _local_bases(data):
                continue
            changed = 0
            rules = data["config"]["providerConfigRules"]
            for key in ("providerRules", "templateRules"):
                for entry in rules.get(key) or []:
                    api = (entry.get("config") or {}).get("api") or {}
                    current = str(api.get("baseUrl", "")).rstrip("/")
                    remote = reverse.get(current)
                    if remote is not None:
                        api["baseUrl"] = remote
                        changed += 1
            if changed:
                _atomic_write(path, json.dumps(data, ensure_ascii=False))
                restored.append(path)
                log(f"restored by inverse mapping: {path}")
            else:
                log(f"rewritten but no backup, left as-is: {path}")
    return restored
