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
