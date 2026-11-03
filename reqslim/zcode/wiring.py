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
from pathlib import Path
from typing import Any

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
def rewrite_config(
    source: Path, mapping: dict[str, str]
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Rewrite provider baseUrls.

    ``mapping`` is original upstream URL -> local base URL. Returns the new
    table plus (providerId, local base) for every entry touched.
    """
    data = json.loads(source.read_text(encoding="utf-8"))
    rules = data.setdefault("config", {}).setdefault("providerConfigRules", {})
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
    rules = data.get("config", {}).get("providerConfigRules", {})
    found: set[str] = set()
    for key in ("providerRules", "templateRules"):
        for entry in rules.get(key) or []:
            api = (entry.get("config") or {}).get("api") or {}
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
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, out)
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


def _loopback_anthropic_entries(data: dict[str, Any]) -> int:
    """How many anthropic providers already point at a loopback base URL."""
    rules = (data.get("config") or {}).get("providerConfigRules") or {}
    count = 0
    for key in ("providerRules", "templateRules"):
        for entry in rules.get(key) or []:
            api = (entry.get("config") or {}).get("api") or {}
            if api.get("type") != "anthropic-messages":
                continue
            url = str(api.get("baseUrl", ""))
            host = url.split("//", 1)[-1].split("/", 1)[0]
            if host.startswith(("127.0.0.1", "localhost")):
                count += 1
    return count


def rewrite_runtime_cache_file(
    path: Path, mapping: dict[str, str], dry_run: bool = False
) -> str:
    """Rewrite one runtime cache table if it routes a mapped upstream.

    Returns the action: ``rewritten`` (also reported under ``dry_run``,
    which writes nothing), ``untouched`` (already loopback), ``unrelated``
    (no anthropic provider matches the mapping) or ``unreadable``. Errors
    from the write itself propagate as OSError so callers can retry on a
    mid-write clash with ZCode's cloud refresher.

    The one-time ``.bak-reqslim`` backup is created before the first real
    rewrite and never overwritten afterwards: the cloud refresher rewrites
    this file too, and the only pristine copy of the original must survive
    every re-run and refresh.
    """
    try:
        data, touched = rewrite_config(path, mapping)
    except (OSError, ValueError):
        return "unreadable"
    if not touched:
        return "untouched" if _loopback_anthropic_entries(data) else "unrelated"
    if dry_run:
        return "rewritten"
    backup = path.with_name(path.name + CACHE_BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
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


def restore_runtime_caches(root: Path | None = None, log=None) -> list[Path]:
    """Put every backed-up runtime cache back; ``--uninstall`` uses this.

    A cache left pointing at reqslim's ports outlives the proxy and turns
    every model request into ECONNREFUSED, so removing reqslim must also
    remove the rewrites. Backups are moved back and consumed. A rewritten
    cache WITHOUT a backup is reported and left alone: its original content
    is gone, and inventing one would be worse than leaving a table that the
    next installer pass can rewrite again.
    """
    log = log or (lambda message: None)
    restored: list[Path] = []
    base = runtime_cache_root(root)
    if base.is_dir():
        for backup in sorted(base.glob(RUNTIME_CACHE_GLOB + CACHE_BACKUP_SUFFIX)):
            target = backup.with_name(backup.name[: -len(CACHE_BACKUP_SUFFIX)])
            try:
                os.replace(backup, target)
                restored.append(target)
                log(f"restored from backup: {target}")
            except OSError as exc:
                log(f"could not restore {backup}: {exc}")
        for path in runtime_cache_files(root):
            if path.with_name(path.name + CACHE_BACKUP_SUFFIX).exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if _loopback_anthropic_entries(data):
                log(f"rewritten but no backup, left as-is: {path}")
    return restored
