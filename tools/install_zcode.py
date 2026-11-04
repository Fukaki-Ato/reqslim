"""Install the reqslim proxy in front of ZCode, without touching its install dir.

Portability notes
-----------------
Nothing here assumes a particular drive letter, user name or install path:

* the ZCode install is discovered from an ordered candidate list, or supplied
  with ``--zcode-dir``, or from the ``ZCODE_HOME`` environment variable;
* the generated launcher locates the module with ``%~dp0`` / ``$(dirname $0)``,
  so the whole folder can be moved or copied to another machine and still work;
* machine-specific state (config copy, request log, autostart entries) lives
  under the user's home directory, never next to the code.

How it works: ZCode has TWO provider tables, and the env var decides only
the second one (read order verified in the shipped 3.16/3.17 asar):

1. the cloud-refreshed runtime cache,
   ``~/.zcode/v2/runtime/provider/<platform>/<version>/endpoint-*/zcode-builtin.json``,
   read FIRST by the agent host -- an unrewritten cache silently bypasses
   the proxy, and every ZCode update unpacks a fresh one;
2. the ``ZCODE_BUILTIN_PROVIDER_CONFIG_FILE`` target (itself falling back
   to the bundled ``<resources>/config/provider/zcode-builtin.json``), read
   only when the cache is missing -- what a fresh machine or a freshly
   updated version relies on before its first cache exists.

So the installer does both: it copies the bundled table, rewrites the
``baseUrl`` of the providers you actually use to a local proxy, points the
env var at that rewritten copy, and rewrites every runtime cache already on
disk (one ``.bak-reqslim`` backup each, created once; ``--uninstall``
restores them).

ZCode 3.17 plan accounts also sign their model requests, and the signer
refuses ``http://`` base URLs. The installer therefore creates a private
loopback CA/certificate and sets ``ZCODE_AGENT_CA_CERT`` to a bundle that
ZCode trusts; the proxy serves HTTPS and forwards the signing handshake.
Providers of one account family can sit on different gateways (API-key
templates and plan accounts do), so each distinct upstream gets its own local
port inside one proxy process:

    python tools/install_zcode.py
    python tools/install_zcode.py --upstream bigmodel --port 8788
    python tools/install_zcode.py --extra-upstream https://zcode.z.ai/api/v1/zcode-plan/anthropic

With autostart (default, Windows), daily use needs no launcher at all:

* ``autostart.json`` records the listeners, the record path and the ZCode
  table path;
* ``proxy_autostart.pyw`` starts the proxy through ``pythonw.exe`` -- no
  console window -- and redirects diagnostics to ``proxy.log``;
* a shortcut in the user's Startup folder launches that at logon;
* the env vars above are set at user level, so ZCode routes through reqslim
  no matter how it is started (taskbar, Start menu, another tool), and trusts
  the loopback TLS certificate;
* the proxy watches ZCode's bundled provider table AND the runtime provider
  caches, regenerating both within a minute of a ZCode update, so new
  providers and template revisions keep flowing through reqslim without a
  reinstall.

``--no-autostart`` keeps the old explicit-launcher workflow;
``--uninstall`` removes every one of those artifacts (it never touches the
request log).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reqslim.zcode.__main__ import (  # noqa: E402
    autostart_health_matches,
    health_on_port,
    load_autostart_config,
    stop_running_proxy,
)
from reqslim.zcode.tls import TlsFiles, ensure_tls  # noqa: E402
from reqslim.zcode.wiring import (  # noqa: E402
    PROVIDER_TABLE,
    CACHE_BACKUP_SUFFIX,
    CACHE_STATE_SUFFIX,
    RUNTIME_CACHE_GLOB,
    _posix_candidates,
    _windows_candidates,
    generate_proxied_table,
    plan_runtime_cache_restore,
    restore_runtime_caches,
    rewrite_config,
    rewrite_runtime_caches,
    runtime_cache_files,
    runtime_cache_root,
    uncovered_upstreams,
)

UPSTREAM_PRESETS = {
    "bigmodel": "https://open.bigmodel.cn/api/anthropic",
    "zai": "https://api.z.ai/api/anthropic",
    "anthropic": "https://api.anthropic.com",
    "moonshot": "https://api.moonshot.cn/anthropic",
    "deepseek": "https://api.deepseek.com/anthropic",
}

#: The override ZCode's main process honours before its bundled table.
ENV_VAR = "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"
#: ZCode's agent reads this as ``network.caCertFile``.  It must contain the
#: private CA that signs the loopback listener (plus normal roots).
CA_ENV_VAR = "ZCODE_AGENT_CA_CERT"

#: Files the installer manages inside the config dir; uninstall removes them.
#: ``requests.jsonl`` is user data and survives an uninstall on purpose.
MANAGED_FILES = (
    "autostart.json",
    "proxy_autostart.pyw",
    "zcode-builtin.proxied.json",
    "zcode-builtin.original.json",
    "environment-backup.json",
    "proxy.log",
    "proxy.log.1",
)


@dataclass
class Install:
    resources: Path
    exe: Path | None
    label: str

    @property
    def table(self) -> Path:
        return self.resources / PROVIDER_TABLE


def _as_install(root: Path) -> Install | None:
    """Accept either an install root or a resources directory."""
    root = root.expanduser().resolve()
    if (root / PROVIDER_TABLE).exists():
        # macOS layout: <ZCode.app>/Contents/Resources -> binary is one level
        # up in Contents/MacOS (path arithmetic per the layout; not verified
        # on real macOS hardware).
        exe = root.parent / "ZCode.exe"
        if not exe.exists() and root.name == "Resources":
            exe = root.parent / "MacOS" / "ZCode"
        return Install(resources=root, exe=exe if exe and exe.exists() else None, label=str(root))
    resources = root / "resources"
    if (resources / PROVIDER_TABLE).exists():
        exe = root / "ZCode.exe"
        return Install(resources=resources, exe=exe if exe.exists() else None, label=str(root))
    return None


def find_zcode(explicit: str | None) -> Install:
    if explicit:
        found = _as_install(Path(explicit).expanduser().resolve())
        if found:
            return found
        raise SystemExit(f"no provider table under {explicit} (expected {PROVIDER_TABLE})")

    candidates = _windows_candidates() if os.name == "nt" else _posix_candidates()
    tried: list[str] = []
    for root in candidates:
        found = _as_install(root)
        if found:
            return found
        tried.append(str(root))

    raise SystemExit(
        "could not locate ZCode.\n"
        "  pass --zcode-dir <install dir, or the Resources dir>\n"
        "  or set ZCODE_HOME\n"
        "  tried:\n    " + "\n    ".join(tried)
    )


# ------------------------------------------------------------------ autostart
def write_autostart_config(
    config_dir: Path,
    listeners: list[tuple[int, str]],
    zcode_table: Path,
    record: Path,
    *,
    ttl: float = 3600.0,
    max_cache: int = 4096,
    cache: bool = True,
    coalesce: bool = True,
    coalesce_wait: float = 600.0,
    failure_ttl: float = 20.0,
    tls: TlsFiles | None = None,
) -> Path:
    path = config_dir / "autostart.json"
    data: dict[str, object] = {
        "listeners": [[port, url] for port, url in listeners],
        "record": str(record),
        "zcode_table": str(zcode_table),
        "config_dir": str(config_dir),
        "poll_seconds": 60,
        "ttl": ttl,
        "max_cache": max_cache,
        "cache": cache,
        "coalesce": coalesce,
        "coalesce_wait": coalesce_wait,
        "failure_ttl": failure_ttl,
    }
    if tls is not None:
        data["tls"] = {"cert": str(tls.cert), "key": str(tls.key)}
    _write_bytes(path, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
    return path


def configured_listeners(config_dir: Path) -> list[tuple[int, str]]:
    """Listener pairs from the previous autostart config, if readable."""
    return _listeners_from_file(config_dir / "autostart.json")


def _listeners_from_file(path: Path) -> list[tuple[int, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("listeners"), list):
        return []
    listeners: list[tuple[int, str]] = []
    for item in data["listeners"]:
        if not isinstance(item, (list, tuple)):
            continue
        try:
            port, url = item
        except (TypeError, ValueError):
            continue
        if (
            isinstance(port, int)
            and not isinstance(port, bool)
            and 0 < port <= 65535
            and isinstance(url, str)
            and url
        ):
            listeners.append((port, url.rstrip("/")))
    return listeners


def configured_ports(config_dir: Path) -> list[int]:
    """Ports from the previous autostart config, if it is readable."""
    return [port for port, _ in configured_listeners(config_dir)]


def configured_scheme(config_dir: Path) -> str | None:
    """Transport recorded in autostart.json, or None when it is unreadable."""
    try:
        data = json.loads((config_dir / "autostart.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tls = data.get("tls")
    if isinstance(tls, dict) and tls.get("cert") and tls.get("key"):
        return "https"
    return "http"


def configured_reverse_mapping(config_dir: Path) -> dict[str, str]:
    """local base URL -> original upstream URL, for backup-less cache repair.

    Include both schemes: an upgrade changes HTTP to HTTPS, and the old
    mapping is exactly what a backup-less cache may still contain.
    """
    reverse: dict[str, str] = {}
    for port, url in configured_listeners(config_dir):
        for scheme in ("http", "https"):
            reverse[f"{scheme}://127.0.0.1:{port}"] = url
    return reverse


def stop_configured_proxy(config_dir: Path, extra_ports: list[int] | None = None) -> bool:
    """Stop the currently running reqslim proxy before rewriting its files.

    A running CacheWatcher would undo restored runtime caches, and a running
    process on the new port would make the new instance exit. Returns False
    when something is still answering and the install/uninstall must stop.
    """
    ports = configured_ports(config_dir)
    ports.extend(extra_ports or [])
    if not ports:
        # A real install may have lost autostart.json while the proxy kept
        # running; only probe the default port for the default config dir, so
        # a temp/test install cannot kill an unrelated user proxy.
        default_dir = Path.home() / ".zcode" / "reqslim"
        try:
            is_default = config_dir.resolve() == default_dir.resolve()
        except OSError:
            is_default = False
        if is_default:
            ports.extend(range(8788, 8793))
    scheme = configured_scheme(config_dir)
    if scheme is None:
        # autostart.json was lost; discover the transport once, then use it
        # for both the liveness probe and the shutdown calls.
        for candidate in ("https", "http"):
            if any(health_on_port(port, scheme=candidate) for port in set(ports)):
                scheme = candidate
                break
    scheme = scheme or "http"
    if not ports or not any(health_on_port(port, scheme=scheme) for port in set(ports)):
        return True
    print("proxy       stopping the running reqslim instance")
    return stop_running_proxy(ports, scheme=scheme)


#: The machine-independent body of the pyw entry (braces are literal here;
#: paths are injected separately through the f-string header).
_PYW_BODY = '''

class _Log:
    """Append-only sink that rotates the log at LIMIT bytes.

    pythonw has no console, so this file is the only place diagnostics can
    land -- and the proxy runs for weeks, so a failure loop must not be able
    to grow it without bound.
    """

    LIMIT = 5 * 1024 * 1024

    def __init__(self, path):
        self.path = path
        self.count = 0
        self.handle = open(path, "a", buffering=1, encoding="utf-8")

    def write(self, text):
        self.count += len(text)
        if self.count > self.LIMIT:
            self.handle.close()
            try:
                os.replace(self.path, self.path + ".1")
            except OSError:
                pass
            self.handle = open(self.path, "a", buffering=1, encoding="utf-8")
            self.count = 0
        self.handle.write(text)

    def flush(self):
        try:
            self.handle.flush()
        except OSError:
            pass


def _run() -> int:
    log = _Log(LOG_PATH)
    sys.stdout = log
    sys.stderr = log
    log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] reqslim proxy starting\\n")
    sys.path.insert(0, MODULE_DIR)
    from reqslim.zcode.__main__ import main
    return main(["--autostart", "--autostart-config", sys.argv[1] if len(sys.argv) > 1 else CONFIG_PATH])


if __name__ == "__main__":
    try:
        _code = _run()
    except Exception:
        with open(LOG_PATH, "a", encoding="utf-8") as _handle:
            _handle.write(time.strftime("[%Y-%m-%d %H:%M:%S] proxy crashed\\n"))
            traceback.print_exc(file=_handle)
        _code = 1
    raise SystemExit(_code or 0)
'''


def write_autostart_pyw(config_dir: Path, module_dir: Path) -> Path:
    """The windowless proxy entry, launched by pythonw at logon.

    pythonw has no stdout, so every diagnostic would vanish silently; the
    first thing this wrapper does is redirect both streams into a rotating
    proxy.log. Import goes through an explicit sys.path entry rather than
    PYTHONPATH: the shortcut passes no environment, and the module dir is
    the one thing this file knows for certain.
    """
    path = config_dir / "proxy_autostart.pyw"
    log_path = str(config_dir / "proxy.log")
    header = f'''"""reqslim proxy logon entry -- run with pythonw.exe (no console)."""
import os
import sys
import time
import traceback

MODULE_DIR = r"{module_dir}"
LOG_PATH = r"{log_path}"
CONFIG_PATH = r"{config_dir / 'autostart.json'}"
'''
    path.write_text(header + _PYW_BODY, encoding="utf-8")
    return path


def _startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _ps_quote(text: str) -> str:
    return text.replace("'", "''")


def _powershell(script: str) -> subprocess.CompletedProcess:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    # Windows-native tools answer in the ANSI codepage (GBK on zh-CN), not
    # UTF-8; decode accordingly or the reader thread dies on a success message.
    return subprocess.run(
        ["powershell", "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True, text=True, encoding="mbcs", errors="replace",
    )


def _read_user_environment(name: str) -> dict[str, object] | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    if not isinstance(value, str) or kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
        raise ValueError(f"unsupported user environment value: {name}")
    return {"value": value, "type": kind}


def _set_user_environment(name: str, value: dict[str, object] | None) -> bool:
    if value is None:
        command = ["reg", "delete", r"HKCU\Environment", "/v", name, "/f"]
    else:
        kind = "REG_EXPAND_SZ" if value["type"] == 2 else "REG_SZ"
        command = [
            "reg", "add", r"HKCU\Environment", "/v", name,
            "/t", kind, "/d", str(value["value"]), "/f",
        ]
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="mbcs", errors="replace",
    )
    if result.returncode != 0:
        return False
    _notify_environment()
    return True


def _notify_environment() -> None:
    import ctypes

    try:
        result = ctypes.c_size_t()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, ctypes.c_wchar_p("Environment"), 2, 5000,
            ctypes.byref(result),
        )
    except (AttributeError, OSError):
        pass


def _load_environment_backup(config_dir: Path) -> dict[str, dict[str, object]]:
    path = config_dir / "environment-backup.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid environment backup: {path}")
    for name, entry in data.items():
        if name not in (ENV_VAR, CA_ENV_VAR) or not isinstance(entry, dict):
            raise ValueError(f"invalid environment backup entry: {name}")
        for field in ("previous", "installed"):
            value = entry.get(field)
            if field not in entry or (
                value is not None and (
                    not isinstance(value, dict) or not isinstance(value.get("value"), str)
                    or type(value.get("type")) is not int or value["type"] not in (1, 2)
                )
            ):
                raise ValueError(f"invalid environment backup value: {name}")
    return data


def _install_environment(
    config_dir: Path, proxied: Path, tls_bundle: Path | None
) -> tuple[bool, bool]:
    saved = _load_environment_backup(config_dir)
    current = {name: _read_user_environment(name) for name in (ENV_VAR, CA_ENV_VAR)}
    planned: dict[str, dict[str, object]] = {}
    for name, value in current.items():
        old = saved.get(name)
        previous = old["previous"] if old and value == old["installed"] else value
        if name == ENV_VAR:
            installed = {"value": str(proxied), "type": 1}
        elif tls_bundle is not None:
            installed = {"value": str(tls_bundle), "type": 1}
        else:
            installed = previous
        planned[name] = {"previous": previous, "installed": installed}
    path = config_dir / "environment-backup.json"
    text = json.dumps(planned, ensure_ascii=True, indent=1)
    _write_bytes(path, text.encode("utf-8"))
    results = []
    for name, entry in planned.items():
        desired = entry["installed"]
        ok = current[name] == desired or _set_user_environment(name, desired)
        ok = ok and _read_user_environment(name) == desired
        results.append(ok)
        if not ok:
            print(f"env         FAILED to update user environment variable {name}")
    return results[0], results[1]


def _restore_environment(config_dir: Path) -> list[str]:
    saved = _load_environment_backup(config_dir)
    errors: list[str] = []
    for name, path in (
        (ENV_VAR, config_dir / "zcode-builtin.proxied.json"),
        (CA_ENV_VAR, config_dir / "tls" / "ca-bundle.pem"),
    ):
        entry = saved.get(name, {"previous": None, "installed": {"value": str(path), "type": 1}})
        current = _read_user_environment(name)
        if current == entry["installed"] and current != entry["previous"]:
            if (
                not _set_user_environment(name, entry["previous"])
                or _read_user_environment(name) != entry["previous"]
            ):
                errors.append(f"could not restore user environment variable {name}")
    return errors


def _environment_rollback_errors(
    previous_environment: dict[str, dict[str, object] | None],
) -> list[str]:
    errors: list[str] = []
    for name, previous in previous_environment.items():
        try:
            if _read_user_environment(name) != previous:
                errors.append(f"user environment variable {name} has not been restored; current value kept")
        except (OSError, ValueError) as exc:
            errors.append(f"could not verify user environment variable {name}: {exc}")
    return errors


def _rollback_environment(
    config_dir: Path, previous_environment: dict[str, dict[str, object] | None],
) -> list[str]:
    if not _environment_rollback_errors(previous_environment):
        return []
    try:
        planned = _load_environment_backup(config_dir)
    except (OSError, ValueError) as exc:
        return [f"could not verify environment rollback: {exc}"]
    errors: list[str] = []
    for name, previous in previous_environment.items():
        try:
            current = _read_user_environment(name)
            if current == previous:
                continue
            if name not in planned or current != planned[name]["installed"]:
                errors.append(f"user environment variable {name} changed outside the installer; kept")
                continue
            _set_user_environment(name, previous)
            if _read_user_environment(name) != previous:
                errors.append(f"could not confirm restoration of user environment variable {name}")
        except (OSError, ValueError) as exc:
            errors.append(f"could not restore user environment variable {name}: {exc}")
    return errors or _environment_rollback_errors(previous_environment)


def install_autostart_windows(
    config_dir: Path,
    module_dir: Path,
    proxied: Path,
    python_exe: str,
    tls_bundle: Path | None = None,
) -> tuple[Path, Path | None, bool, bool]:
    """Register the logon entry and the user-level env overrides.

    The shortcut targets pythonw explicitly: .pyw file associations depend on
    how the interpreter was installed, and a dead association must not be the
    reason reqslim silently stops autoloading. The env var goes to the user
    environment (setx), so ZCode inherits it whichever way it is started.
    """
    pythonw = Path(python_exe).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(python_exe)  # console fallback; better than nothing
    pyw = write_autostart_pyw(config_dir, module_dir)

    lnk = _startup_dir() / "reqslim proxy.lnk"
    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{_ps_quote(str(lnk))}'); "
        f"$s.TargetPath = '{_ps_quote(str(pythonw))}'; "
        f"$s.Arguments = '\"{_ps_quote(str(pyw))}\"'; "
        f"$s.WorkingDirectory = '{_ps_quote(str(module_dir))}'; "
        "$s.WindowStyle = 7; "
        "$s.Description = 'reqslim proxy (auto-start, no window)'; "
        "$s.Save()"
    )
    result = _powershell(script)
    if result.returncode != 0 or not lnk.exists():
        detail = (result.stderr or result.stdout or "").strip()[:200]
        print(f"autostart   FAILED to create startup shortcut ({detail or 'unknown error'})")
        print("            fall back to start-zcode.bat; re-run the installer to retry")
        lnk_created = None
    else:
        lnk_created = lnk

    env_ok, ca_ok = _install_environment(config_dir, proxied, tls_bundle)
    return (lnk_created or lnk), lnk_created, env_ok, ca_ok


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass
class _FileState:
    content: bytes
    mode: int
    atime: int
    mtime: int


def _read_file_state(path: Path) -> _FileState | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return _FileState(path.read_bytes(), info.st_mode, info.st_atime_ns, info.st_mtime_ns)


class _InstallSnapshot:
    def __init__(self, config_dir: Path, module_dir: Path):
        self.files: dict[Path, _FileState | None] = {}
        self.tls_dir = config_dir / "tls"
        self.tls_existed = self.tls_dir.exists()
        for name in MANAGED_FILES:
            if not name.startswith("proxy.log"):
                self.remember(config_dir / name)
        for path in self.tls_dir.rglob("*"):
            if path.is_file():
                self.remember(path)
        for name in ("start-zcode.bat", "start-zcode.sh", "zcode-stats.bat", "zcode-report.bat"):
            self.remember(module_dir / name)
        if os.name == "nt":
            self.remember(_startup_dir() / "reqslim proxy.lnk")
        initial_caches = runtime_cache_files()
        self.initial_caches = set(initial_caches)
        self.late_caches: set[Path] = set()
        for path in initial_caches:
            self.remember(path)
            self.remember(path.with_name(path.name + CACHE_BACKUP_SUFFIX))
            self.remember(path.with_name(path.name + CACHE_STATE_SUFFIX))
        for suffix in (CACHE_BACKUP_SUFFIX, CACHE_STATE_SUFFIX):
            for artifact in runtime_cache_root().glob(RUNTIME_CACHE_GLOB + suffix):
                self.remember(artifact)
                path = artifact.with_name(artifact.name[:-len(suffix)])
                self.remember(path)
                self.remember(path.with_name(path.name + CACHE_BACKUP_SUFFIX))
                self.remember(path.with_name(path.name + CACHE_STATE_SUFFIX))
        self.initial_files = set(self.files)

    def remember(self, path: Path) -> None:
        if path in self.files:
            return
        self.files[path] = _read_file_state(path)

    def _capture_changes(self, reverse_mapping: dict[str, str]) -> list[str]:
        errors: list[str] = []
        self.late_caches.update(set(runtime_cache_files()) - self.initial_caches)
        for path in sorted(self.late_caches):
            backup = path.with_name(path.name + CACHE_BACKUP_SUFFIX)
            state_path = path.with_name(path.name + CACHE_STATE_SUFFIX)
            try:
                current = _read_file_state(path)
                plan = plan_runtime_cache_restore(path, reverse_mapping)
                if plan.data is None:
                    self.files[path] = None
                elif not plan.managed:
                    self.files.pop(path, None)
                else:
                    source = _read_file_state(backup) if plan.use_backup or current is None else current
                    if source is None:
                        raise OSError("runtime cache disappeared while planning restoration")
                    content = source.content if plan.use_backup else json.dumps(plan.data, ensure_ascii=False).encode("utf-8")
                    self.files[path] = _FileState(
                        content, source.mode | stat.S_IWUSR, source.atime, source.mtime,
                    )
                for artifact in (backup, state_path):
                    if artifact not in self.initial_files:
                        self.files[artifact] = None
            except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
                errors.append(f"{path}: {exc}")
        for sidecar in runtime_cache_root().glob(RUNTIME_CACHE_GLOB + CACHE_STATE_SUFFIX):
            if sidecar not in self.files:
                self.files[sidecar] = None
        for path in self.tls_dir.rglob("*"):
            if path.is_file() and path not in self.files:
                self.files[path] = None
        return errors

    def restore(self, reverse_mapping: dict[str, str]) -> list[str]:
        errors = self._capture_changes(reverse_mapping)
        if errors:
            return errors
        for path, state in self.files.items():
            if state is None:
                continue
            try:
                _write_bytes(path, state.content)
                path.chmod(state.mode)
                os.utime(path, ns=(state.atime, state.mtime))
            except OSError as exc:
                errors.append(f"{path}: {exc}")
        if errors:
            return errors
        for path, state in self.files.items():
            if state is None and path.exists():
                try:
                    path.chmod(path.stat().st_mode | stat.S_IWUSR)
                    path.unlink()
                except OSError as exc:
                    errors.append(f"{path}: {exc}")
        if not self.tls_existed and self.tls_dir.exists():
            for path in sorted(self.tls_dir.rglob("*"), reverse=True):
                if path.is_dir():
                    try:
                        path.rmdir()
                    except OSError:
                        pass
            try:
                self.tls_dir.rmdir()
            except OSError:
                pass
        return errors

    def save_recovery(
        self, config_dir: Path,
        previous_environment: dict[str, dict[str, object] | None] | None = None,
        reverse_mapping: dict[str, str] | None = None,
        path: Path | None = None,
        *, require_complete: bool = False,
    ) -> Path:
        errors = self._capture_changes(reverse_mapping or {})
        if require_complete and errors:
            raise OSError("runtime cache recovery planning failed: " + "; ".join(errors))
        config_dir.mkdir(parents=True, exist_ok=True)
        created = path is None
        if path is None:
            handle, name = tempfile.mkstemp(prefix="install-recovery-", suffix=".json", dir=str(config_dir))
            os.close(handle)
            path = Path(name)
        files = {
            str(candidate): None if state is None else {
                "content": base64.b64encode(state.content).decode("ascii"),
                "mode": state.mode, "atime": state.atime, "mtime": state.mtime,
            }
            for candidate, state in self.files.items()
        }
        data = {
            "version": 1, "files": files, "previous_environment": previous_environment,
            "tls_dir": str(self.tls_dir), "tls_existed": self.tls_existed,
            "capture_errors": errors,
        }
        try:
            _write_bytes(path, json.dumps(data, ensure_ascii=True).encode("utf-8"))
        except OSError:
            if created:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        return path


def _keep_recovery(
    snapshot: _InstallSnapshot, config_dir: Path,
    previous_environment: dict[str, dict[str, object] | None] | None,
    reverse_mapping: dict[str, str], path: Path | None,
) -> None:
    try:
        recovery = snapshot.save_recovery(config_dir, previous_environment, reverse_mapping, path)
        print(f"rollback    recovery snapshot: {recovery}", file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(f"rollback    could not save recovery snapshot: {exc}", file=sys.stderr)
        if path is not None and path.is_file():
            print(f"rollback    earlier recovery snapshot kept: {path}", file=sys.stderr)


def _remove_recovery(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink()
        except OSError as exc:
            print(f"note        recovery snapshot kept: {path} ({exc})", file=sys.stderr)


def _configured_running(config_dir: Path) -> bool:
    return any(
        health_on_port(port, scheme=scheme) is not None
        for port in configured_ports(config_dir)
        for scheme in ("https", "http")
    )


def _restart_previous(config_dir: Path, module_dir: Path) -> None:
    listeners = configured_listeners(config_dir)
    if not listeners:
        return
    try:
        ready = start_proxy_now(
            sys.executable, config_dir / "proxy_autostart.pyw", module_dir,
            listeners[0][0], configured_scheme(config_dir) or "http",
            config=config_dir / "autostart.json",
        )
    except OSError:
        ready = False
    print("proxy       previous instance restored" if ready else
          "proxy       could not restart the previous instance; its configuration was kept")


def start_proxy_now(
    python_exe: str, pyw: Path, module_dir: Path, port: int, scheme: str = "http",
    config: Path | None = None,
) -> bool:
    """Launch the proxy immediately, so "install, then open ZCode" works
    without waiting for the next logon.

    The Startup shortcut only fires at the next logon; without this, a fresh
    install leaves every ZCode request hitting a dead port until reboot.
    Idempotent by design: if an equivalent proxy already serves, the spawned
    process detects it and exits 0 on its own. Returns whether every configured
    listener answers ``/v1/health`` with the expected upstream mapping.
    """
    pythonw = Path(python_exe).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(python_exe)
    flags = 0
    if os.name == "nt":
        flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    config = config or pyw.parent / "autostart.json"
    spec = None
    if config.exists():
        try:
            spec = load_autostart_config(str(config))
        except (OSError, ValueError, SystemExit):
            return False
        listeners = spec.listeners
        scheme = "https" if spec.tls_cert and spec.tls_key else "http"
    else:
        listeners = []
    expected = {str(number): url for number, url in listeners}
    if len(expected) != len(listeners):
        return False
    ports = [number for number, _ in listeners] or [port]
    command = [str(pythonw), str(pyw), str(config)] if pyw.exists() else [
        str(pythonw), "-m", "reqslim.zcode", "--autostart", "--autostart-config", str(config),
    ]
    ready = False
    try:
        process = subprocess.Popen(
            command, cwd=str(module_dir), creationflags=flags, close_fds=True,
        )
    except OSError:
        return False
    try:
        for _ in range(10):
            time.sleep(1)
            health = [health_on_port(number, scheme=scheme) for number in ports]
            if all(
                autostart_health_matches(item, spec) if spec is not None else (
                    item is not None
                    and item.get("tls") is (scheme == "https")
                    and item.get("ready", True) is True
                    and (not expected or item.get("upstreams") == expected)
                )
                for item in health
            ):
                ready = True
                return True
            if process.poll() is not None:
                break
    finally:
        if not ready and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass
    return False


def uninstall(config_dir: Path, dry_run: bool = False) -> int:
    """Remove everything the installer created; keep the request log.

    The running proxy is stopped before runtime caches are restored: its
    CacheWatcher would otherwise rewrite the restored tables back to loopback
    on the next tick, leaving ZCode with permanent ECONNREFUSED after reboot.
    """
    removed: list[str] = []
    errors: list[str] = []
    reverse = configured_reverse_mapping(config_dir)
    if dry_run:
        for path in restore_runtime_caches(reverse_mapping=reverse, errors=errors, dry_run=True):
            print(f"would restore {path}")
        for name in MANAGED_FILES:
            if (config_dir / name).exists():
                print(f"would remove {config_dir / name}")
        print("dry run: nothing stopped, written or removed")
        for message in errors:
            print(message, file=sys.stderr)
        return 1 if errors else 0

    if os.name == "nt":
        try:
            _load_environment_backup(config_dir)
        except (OSError, ValueError) as exc:
            print(f"uninstall   aborted: {exc}", file=sys.stderr)
            return 1
    was_running = _configured_running(config_dir)

    if not stop_configured_proxy(config_dir):
        print(
            "uninstall   aborted: the reqslim proxy is still running and could "
            "not be stopped.\n"
            "            End it manually, then run --uninstall again.",
            file=sys.stderr,
        )
        return 1

    try:
        for path in restore_runtime_caches(
            log=lambda message: print(f"note        {message}"),
            reverse_mapping=reverse,
            errors=errors,
        ):
            removed.append(f"{path} (restored from .bak-reqslim)")
    except (OSError, ValueError) as exc:
        errors.append(str(exc))

    if not errors and os.name == "nt":
        try:
            errors.extend(_restore_environment(config_dir))
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    if errors:
        for message in errors:
            print(f"uninstall   aborted: {message}", file=sys.stderr)
        print("uninstall   recovery configuration, TLS files and startup entry were kept", file=sys.stderr)
        if was_running:
            _restart_previous(config_dir, Path(__file__).resolve().parent.parent)
        return 1

    if os.name == "nt":
        lnk = _startup_dir() / "reqslim proxy.lnk"
        if lnk.exists():
            try:
                lnk.unlink()
                removed.append(str(lnk))
            except OSError as exc:
                print(f"uninstall   aborted: {lnk}: {exc}", file=sys.stderr)
                if was_running:
                    _restart_previous(config_dir, Path(__file__).resolve().parent.parent)
                return 1

    for name in MANAGED_FILES:
        candidate = config_dir / name
        if candidate.exists():
            for attempt in range(3):
                try:
                    candidate.unlink()
                    removed.append(str(candidate))
                    break
                except OSError as exc:
                    if attempt == 2:
                        errors.append(f"{candidate}: {exc}")
                    else:
                        time.sleep(0.2)

    tls_dir = config_dir / "tls"
    if tls_dir.is_dir():
        try:
            shutil.rmtree(tls_dir)
            removed.append(str(tls_dir))
        except OSError as exc:
            errors.append(f"{tls_dir}: {exc}")

    if removed:
        for item in removed:
            print(f"removed     {item}")
    if errors:
        for item in errors:
            print(f"could not remove {item}", file=sys.stderr)
    if not removed and not errors:
        print("nothing to remove")

    log = config_dir / "requests.jsonl"
    if log.exists():
        print(f"kept        {log} (traffic history)")
    print("\nZCode returns to its direct connection the next time it starts.")
    print("Already-open apps still carry the old env var until they are restarted;")
    print("sign out/in (or reboot) to clear it from every future process.")
    return 1 if errors else 0


# ------------------------------------------------------------------- launchers
def _launcher_health_check(listeners: list[tuple[int, str]], scheme: str) -> str:
    return (
        "import sys;from reqslim.zcode.__main__ import autostart_ready;"
        "sys.exit(0 if autostart_ready(sys.argv[1]) else 1)"
    )


def write_windows_launcher(
    module_dir: Path,
    exe: Path | None,
    listeners: list[tuple[int, str]],
    tls: TlsFiles | None = None,
    config: Path | None = None,
) -> Path:
    launcher = module_dir / "start-zcode.bat"
    target = str(exe) if exe else "ZCode.exe"
    config_dir = str(config.parent) if config is not None else r"%USERPROFILE%\.zcode\reqslim"
    config_path = str(config) if config is not None else r"%CONFIG_DIR%\autostart.json"
    scheme = "https" if tls is not None else "http"
    primary = listeners[0][0]
    health_check = _launcher_health_check(listeners, scheme)
    ca_line = (
        f'set "ZCODE_AGENT_CA_CERT={tls.bundle}"\n' if tls is not None else ""
    )
    body = f"""@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "MODULE_DIR=%~dp0"
set "CONFIG_DIR={config_dir}"
set "AUTOSTART_CONFIG={config_path}"
set "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE=%CONFIG_DIR%\\zcode-builtin.proxied.json"
{ca_line}set "NO_PROXY=127.0.0.1,localhost,%NO_PROXY%"

rem Prefer the interpreter that ran the installer: it is known to exist and to
rem be able to import the package. PATH is only a fallback, because "python"
rem can resolve to a Store stub or an unrelated install on a fresh machine.
set "PY={sys.executable}"
if not exist "%PY%" set "PY=python"

rem Import the package from wherever this folder currently sits, rather than
rem relying on the working directory or on a pip install. Appended, not
rem replaced, so an existing PYTHONPATH keeps working.
set "PYTHONPATH=%MODULE_DIR%;%PYTHONPATH%"

rem Fail loudly here instead of letting ZCode start against a dead port.
rem goto, not a parenthesised block: %PY% can expand to a path containing
rem parentheses ("Program Files (x86)"), which would close the block early.
"%PY%" -c "import reqslim.zcode" 2>nul
if not errorlevel 1 goto :reqslim_ready
echo.
echo reqslim cannot be imported by the interpreter above.
echo   check that "%MODULE_DIR%reqslim" still exists, and that the
echo   interpreter is Python 3.9 or newer.
echo.
pause
exit /b 1
:reqslim_ready

start "reqslim proxy" /min "%PY%" -m reqslim.zcode --autostart --autostart-config "%AUTOSTART_CONFIG%"

rem The proxy must answer on every listener before ZCode starts: a bind
rem failure (port taken, reserved range) must stop here, not become "every
rem request fails" inside ZCode with the cause hidden in a minimised window.
set /a PROBE=0
:wait_proxy
"%PY%" -c "{health_check}" "%AUTOSTART_CONFIG%" 2>nul
if not errorlevel 1 goto :proxy_ready
set /a PROBE+=1
if %PROBE% geq 10 goto :proxy_dead
timeout /t 1 /nobreak >nul
goto :wait_proxy
:proxy_dead
echo.
echo reqslim proxy did not come up with the expected listeners (first port {primary}).
echo   ZCode is NOT started: it would point at a dead port.
echo   look at the "reqslim proxy" window for the reason (port taken?).
echo.
pause
exit /b 1
:proxy_ready

start "" "{target}"
echo ZCode launched through reqslim ({len(listeners)} listener(s), first port {primary}).
echo   stats  {scheme}://127.0.0.1:{primary}/v1/stats
echo   report {scheme}://127.0.0.1:{primary}/v1/report
endlocal
"""
    launcher.write_text(body, encoding="utf-8")
    return launcher


def write_posix_launcher(
    module_dir: Path,
    exe: Path | None,
    listeners: list[tuple[int, str]],
    tls: TlsFiles | None = None,
    config: Path | None = None,
) -> Path:
    launcher = module_dir / "start-zcode.sh"
    config_dir = str(config.parent) if config is not None else "$HOME/.zcode/reqslim"
    config_path = str(config) if config is not None else "$CONFIG_DIR/autostart.json"
    scheme = "https" if tls is not None else "http"
    primary = listeners[0][0]
    health_check = _launcher_health_check(listeners, scheme)
    ca_line = (
        f'export ZCODE_AGENT_CA_CERT="{tls.bundle}"\n' if tls is not None else ""
    )
    body = f"""#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
CONFIG_DIR="{config_dir}"
AUTOSTART_CONFIG="{config_path}"
export ZCODE_BUILTIN_PROVIDER_CONFIG_FILE="$CONFIG_DIR/zcode-builtin.proxied.json"
export NO_PROXY="127.0.0.1,localhost,${{NO_PROXY:-}}"
{ca_line}export PYTHONPATH="$HERE:${{PYTHONPATH:-}}"

if ! python3 -c "import reqslim.zcode" 2>/dev/null; then
  echo "reqslim cannot be imported by $(command -v python3 || echo python3)." >&2
  echo "  check that \\"$HERE/reqslim\\" still exists, and that python3 is 3.9 or newer." >&2
  exit 1
fi

PROXY_PID=""
python3 -m reqslim.zcode --autostart --autostart-config "$AUTOSTART_CONFIG" &
PROXY_PID=$!

# The proxy must answer on every listener before ZCode starts: a bind failure
# must abort here, not leave ZCode pointing at a dead port. An already running
# proxy instance with the same mapping also passes.
proxy_up=false
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if python3 -c "{health_check}" "$AUTOSTART_CONFIG" 2>/dev/null; then
    proxy_up=true
    break
  fi
  kill -0 "$PROXY_PID" 2>/dev/null || break
  sleep 1
done
if [ "$proxy_up" != true ]; then
  echo "reqslim proxy did not come up with the expected listeners (first port {primary}) -- ZCode NOT started." >&2
  echo "  run the python3 -m reqslim.zcode line above in the foreground to see why." >&2
  kill "$PROXY_PID" 2>/dev/null || true
  exit 1
fi

# Start ZCode itself. macOS uses `open -a`; on Linux look for a binary next
# to the launcher or on PATH (POSIX layouts are untested on real hardware).
BIN=""
OPEN_APP=false
case "$(uname -s)" in
  Darwin) OPEN_APP=true ;;
  *)
    for candidate in "$HERE/ZCode" "$HERE/zcode"; do
      if [ -x "$candidate" ]; then BIN="$candidate"; break; fi
    done
    if [ -z "$BIN" ]; then BIN="$(command -v zcode 2>/dev/null || true)"; fi
    ;;
esac
if [ "$OPEN_APP" = true ]; then
  open -a ZCode
elif [ -n "$BIN" ]; then
  "$BIN"
else
  echo "could not locate the ZCode binary -- edit BIN= detection in this launcher." >&2
  exit 1
fi
echo "ZCode launched through reqslim ({len(listeners)} listener(s), first port {primary})."
echo "  stats  {scheme}://127.0.0.1:{primary}/v1/stats"
"""
    launcher.write_text(body, encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


def write_helpers(module_dir: Path, port: int, py: str | None = None) -> tuple[Path, Path]:
    stats = module_dir / "zcode-stats.bat"
    report = module_dir / "zcode-report.bat"
    stats.write_text(
        "@echo off\nchcp 65001 >nul\n"
        f"curl -sk https://127.0.0.1:{port}/v1/report 2>nul\n"
        f"if errorlevel 1 curl -s http://127.0.0.1:{port}/v1/report 2>nul\n"
        f'if errorlevel 1 powershell -NoProfile -Command "try {{ irm http://127.0.0.1:{port}/v1/report }}'
        ' catch { \'proxy is not running\' }"\npause\n',
        encoding="utf-8",
    )
    # Same interpreter rule as the launcher: the interpreter that ran the
    # installer is known to exist and to import the package; bare "python" can
    # resolve to a Store stub that opens the store and exits 0.
    py_line = f'set "PY={py or "python"}"\nif not exist "%PY%" set "PY=python"\n'
    report.write_text(
        "@echo off\nchcp 65001 >nul\n"
        'set "PYTHONPATH=%~dp0;%PYTHONPATH%"\n'
        f"{py_line}"
        '"%PY%" -m reqslim.zcode --report "%USERPROFILE%\\.zcode\\reqslim\\requests.jsonl"\n'
        "pause\n",
        encoding="utf-8",
    )
    return stats, report


# ----------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wire reqslim in front of ZCode")
    parser.add_argument("--zcode-dir", default=None, help="ZCode install dir, or its Resources dir")
    parser.add_argument("--upstream", default=None, help="URL, or one of: " + ", ".join(UPSTREAM_PRESETS))
    parser.add_argument(
        "--extra-upstream",
        action="append",
        default=[],
        metavar="URL",
        help="also rewrite providers pointing at this upstream (URL or preset name); "
        "each gets the next local port. Repeatable.",
    )
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--ttl", type=float, default=3600.0, help="cache TTL seconds for autostart")
    parser.add_argument("--max-cache", type=int, default=4096)
    parser.add_argument("--no-cache", action="store_true", help="disable response caching in autostart")
    parser.add_argument("--no-coalesce", action="store_true", help="disable single-flight in autostart")
    parser.add_argument("--coalesce-wait", type=float, default=600.0, metavar="SECONDS")
    parser.add_argument("--failure-ttl", type=float, default=20.0, help="failure memo seconds, 0 disables")
    parser.add_argument(
        "--no-tls", action="store_true",
        help="serve plain HTTP instead of the local HTTPS required by ZCode's "
             "client request signing (3.17+ plan accounts)",
    )
    parser.add_argument(
        "--no-autostart", action="store_true",
        help="only write the config and the manual launcher; no logon entry, no user env vars",
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help="remove the logon entry, the user env vars and the generated files; keeps requests.jsonl",
    )
    parser.add_argument("--package-dir", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    config_dir = Path.home() / ".zcode" / "reqslim"
    if args.uninstall:
        return uninstall(config_dir, dry_run=args.dry_run)

    scheme = "http" if args.no_tls else "https"
    install = find_zcode(args.zcode_dir)
    primary = UPSTREAM_PRESETS.get(args.upstream or "", args.upstream or UPSTREAM_PRESETS["bigmodel"])

    if not 0 < args.port <= 65535:
        raise SystemExit(f"--port must be 1..65535, got: {args.port}")

    # One local port per distinct upstream; the extras follow the primary port.
    listeners: list[tuple[int, str]] = [(args.port, primary.rstrip("/"))]
    for extra in args.extra_upstream:
        url = UPSTREAM_PRESETS.get(extra, extra).rstrip("/")
        if url not in {u for _, u in listeners}:
            port = args.port + len(listeners)
            if port > 65535:
                raise SystemExit(f"--port leaves no room for extra upstream {url}")
            listeners.append((port, url))
    mapping = {url: f"{scheme}://127.0.0.1:{port}" for port, url in listeners}

    if not install.table.exists():
        raise SystemExit(f"provider table not found: {install.table}")

    module_dir = Path(args.package_dir).resolve()
    proxied = config_dir / "zcode-builtin.proxied.json"
    record = config_dir / "requests.jsonl"

    try:
        _, touched = rewrite_config(install.table, mapping)
    except (OSError, ValueError):
        raise SystemExit(f"provider table unreadable: {install.table}")
    if not touched:
        raise SystemExit(
            f"no provider in the table uses any of: {', '.join(url for _, url in listeners)}\n"
            "  pass --upstream / --extra-upstream with the base URLs you actually use"
        )

    print(f"zcode       {install.label}")
    print(f"module      {module_dir}")
    print(f"config      {config_dir}")
    for port, url in listeners:
        names = [ident for ident, local in touched if local == f"{scheme}://127.0.0.1:{port}"]
        print(f"listen      {scheme}://127.0.0.1:{port}  ->  {url}")
        print(f"            {', '.join(names)}")
    remaining = uncovered_upstreams(install.table, mapping)
    if remaining:
        print("uncovered   " + ", ".join(remaining))
        print("            pass --extra-upstream <url> to route these through reqslim too")

    previous_mapping = configured_reverse_mapping(config_dir)
    if args.dry_run:
        for path, action in rewrite_runtime_caches(mapping, dry_run=True, previous_mapping=previous_mapping):
            if action == "rewritten":
                print(f"            would rewrite {path}")
            elif action == "unreadable":
                print(f"            unreadable, skipped {path}")
        print("dry run: nothing written (runtime provider caches included)")
        return 0

    # Stop the previous proxy before touching caches: its watcher would
    # otherwise rewrite them back to the old mapping mid-install. Doing this
    # before any file writes also keeps a failed stop from leaving a mixed
    # configuration behind.
    was_running = _configured_running(config_dir)
    if not stop_configured_proxy(config_dir, [port for port, _ in listeners]):
        raise SystemExit(
            "the reqslim proxy is still running and could not be stopped; "
            "end it manually and re-run the installer"
        )
    try:
        snapshot = _InstallSnapshot(config_dir, module_dir)
    except OSError as exc:
        print(f"install     could not snapshot the previous installation: {exc}", file=sys.stderr)
        if was_running:
            _restart_previous(config_dir, module_dir)
        return 1

    previous_environment: dict[str, dict[str, object] | None] | None = None
    recovery: Path | None = None
    reverse_mapping = {local: remote for remote, local in mapping.items()}
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        tls: TlsFiles | None = None
        if scheme == "https":
            tls = ensure_tls(config_dir)
            print(f"tls         {tls.cert}")
            print(f"            CA bundle for ZCode: {tls.bundle}")
        generate_proxied_table(install.table, mapping, proxied)
        shutil.copy2(install.table, config_dir / "zcode-builtin.original.json")
        autostart_config = write_autostart_config(
            config_dir, listeners, install.table, record,
            ttl=args.ttl, max_cache=args.max_cache, cache=not args.no_cache,
            coalesce=not args.no_coalesce, coalesce_wait=args.coalesce_wait,
            failure_ttl=args.failure_ttl, tls=tls,
        )

        cache_actions = rewrite_runtime_caches(mapping, previous_mapping=previous_mapping)
        if cache_actions:
            counts: dict[str, int] = {}
            for _, action in cache_actions:
                counts[action] = counts.get(action, 0) + 1
            summary = ", ".join(f"{count} {action}" for action, count in sorted(counts.items()))
            print(f"cache       runtime provider caches: {summary}")
            for path, action in cache_actions:
                if action == "rewritten":
                    print(f"            rewritten {path} (.bak-reqslim backup kept once)")
                elif action == "unreadable":
                    print(f"            unreadable, skipped {path}")

        if os.name == "nt" and not args.no_autostart:
            pyw = write_autostart_pyw(config_dir, module_dir)
            if not start_proxy_now(
                sys.executable, pyw, module_dir, args.port, scheme, config=autostart_config,
            ):
                raise RuntimeError("proxy did not come up with every configured listener; see proxy.log")
            print(f"proxy       serving on {scheme}://127.0.0.1:{args.port} -- you can open ZCode now")

        if os.name == "nt":
            launcher = write_windows_launcher(module_dir, install.exe, listeners, tls, config=autostart_config)
            stats, report = write_helpers(module_dir, args.port, sys.executable)
            helpers = f"{stats.name}, {report.name}"
        else:
            launcher = write_posix_launcher(module_dir, install.exe, listeners, tls, config=autostart_config)
            helpers = "-"

        print(f"launcher    {launcher} (manual fallback)")
        if helpers != "-":
            print(f"helpers     {helpers}")
        if os.name == "nt" and not args.no_autostart:
            previous_environment = {
                name: _read_user_environment(name) for name in (ENV_VAR, CA_ENV_VAR)
            }
            recovery = snapshot.save_recovery(
                config_dir, previous_environment, reverse_mapping, require_complete=True,
            )
            lnk, ok, env_ok, ca_ok = install_autostart_windows(
                config_dir, module_dir, proxied, sys.executable,
                tls.bundle if tls is not None else None,
            )
            if not env_ok or not ca_ok:
                failed = [name for name, ready in ((ENV_VAR, env_ok), (CA_ENV_VAR, ca_ok)) if not ready]
                raise RuntimeError("required user environment registration failed: " + ", ".join(failed))
            if ok:
                print(f"autostart   {lnk}")
                if env_ok:
                    print(f"env         {ENV_VAR} -> {proxied} (user level; new processes inherit it)")
                if tls is not None and ca_ok:
                    print(f"env         {CA_ENV_VAR} -> {tls.bundle} (local CA trusted by ZCode)")
                print("watch       proxy rewrites the provider table within a minute of ZCode updates")
            else:
                print("autostart   incomplete -- start-zcode.bat remains the working path")
        elif os.name == "nt":
            print(
                "autostart   skipped (--no-autostart); runtime caches are still wired, "
                "so start the proxy with start-zcode.bat before opening ZCode"
            )
        else:
            print("autostart   not configured on this OS; add start-zcode.sh to your session's startup")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"install     failed: {exc}", file=sys.stderr)
        environment_errors: list[str] = []
        if previous_environment is not None:
            environment_errors = _rollback_environment(config_dir, previous_environment)
        if not environment_errors:
            if not stop_configured_proxy(config_dir, [port for port, _ in listeners]):
                print("rollback    blocked by a running proxy; current files kept", file=sys.stderr)
                _keep_recovery(snapshot, config_dir, previous_environment, reverse_mapping, recovery)
                return 1
            if previous_environment is not None:
                environment_errors = _environment_rollback_errors(previous_environment)
        if environment_errors:
            for error in environment_errors:
                print(f"rollback    {error}", file=sys.stderr)
            _keep_recovery(snapshot, config_dir, previous_environment, reverse_mapping, recovery)
            print("rollback    current configuration, TLS, environment backup and startup materials kept", file=sys.stderr)
            return 1
        rollback_errors = snapshot.restore(reverse_mapping)
        if rollback_errors:
            for error in rollback_errors:
                print(f"rollback    {error}", file=sys.stderr)
            _keep_recovery(snapshot, config_dir, previous_environment, reverse_mapping, recovery)
        else:
            _remove_recovery(recovery)
            print("install     previous configuration and runtime caches restored", file=sys.stderr)
            if was_running:
                _restart_previous(config_dir, module_dir)
        return 1
    _remove_recovery(recovery)
    if args.no_autostart:
        print(
            f"\nManual mode: start the proxy (start-zcode.bat) before opening ZCode. "
            f"Open {scheme}://127.0.0.1:{args.port}/v1/stats to watch traffic."
        )
    else:
        print(f"\nZCode routes through reqslim on its next start. "
              f"Open {scheme}://127.0.0.1:{args.port}/v1/stats to watch traffic.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
