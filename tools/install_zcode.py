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

How it works: ZCode resolves its provider table from
``<resources>/config/provider/zcode-builtin.json``, but the main process honours
``ZCODE_BUILTIN_PROVIDER_CONFIG_FILE`` first (verified in the shipped asar).
So the installer copies that table, rewrites the ``baseUrl`` of the providers
you actually use to a local proxy, and points ZCode at the rewritten copy.
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
* the env var above is set at user level, so ZCode routes through reqslim no
  matter how it is started (taskbar, Start menu, another tool);
* the proxy watches ZCode's bundled provider table and regenerates the
  rewritten copy within a minute of a ZCode update changing it, so new
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
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reqslim.zcode.wiring import (  # noqa: E402
    PROVIDER_TABLE,
    _posix_candidates,
    _windows_candidates,
    generate_proxied_table,
    rewrite_config,
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

#: Files the installer manages inside the config dir; uninstall removes them.
#: ``requests.jsonl`` is user data and survives an uninstall on purpose.
MANAGED_FILES = (
    "autostart.json",
    "proxy_autostart.pyw",
    "zcode-builtin.proxied.json",
    "zcode-builtin.original.json",
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
    if (root / PROVIDER_TABLE).exists():
        # macOS layout: <ZCode.app>/Contents/Resources -> binary is one level
        # up in Contents/MacOS (path arithmetic per the layout; not verified
        # on real macOS hardware).
        exe = root.parent / "MacOS" / "ZCode" if root.name == "Resources" else None
        return Install(resources=root, exe=exe if exe and exe.exists() else None, label=str(root))
    resources = root / "resources"
    if (resources / PROVIDER_TABLE).exists():
        exe = root / "ZCode.exe"
        return Install(resources=resources, exe=exe if exe.exists() else None, label=str(root))
    return None


def find_zcode(explicit: str | None) -> Install:
    if explicit:
        found = _as_install(Path(explicit))
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
    config_dir: Path, listeners: list[tuple[int, str]], zcode_table: Path, record: Path
) -> Path:
    path = config_dir / "autostart.json"
    path.write_text(
        json.dumps(
            {
                "listeners": [[port, url] for port, url in listeners],
                "record": str(record),
                "zcode_table": str(zcode_table),
                "config_dir": str(config_dir),
                "poll_seconds": 60,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    return path


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
    return main(["--autostart"])


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


def install_autostart_windows(
    config_dir: Path, module_dir: Path, proxied: Path, python_exe: str
) -> tuple[Path, Path | None]:
    """Register the logon entry and the user-level env override.

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

    env = subprocess.run(
        ["setx", ENV_VAR, str(proxied)],
        capture_output=True, text=True, encoding="mbcs", errors="replace",
    )
    if env.returncode != 0:
        detail = (env.stderr or env.stdout or "").strip()[:200]
        print(f"env         FAILED to set user environment variable ({detail})")
        print("            ZCode will still route through reqslim when started via start-zcode.bat")
    return (lnk_created or lnk), lnk_created


def start_proxy_now(python_exe: str, pyw: Path, module_dir: Path, port: int) -> bool:
    """Launch the proxy immediately, so "install, then open ZCode" works
    without waiting for the next logon.

    The Startup shortcut only fires at the next logon; without this, a fresh
    install leaves every ZCode request hitting a dead port until reboot.
    Idempotent by design: if an equivalent proxy already serves, the spawned
    process detects it and exits 0 on its own. Returns whether the primary
    port answers ``/v1/health`` afterwards.
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
    subprocess.Popen(
        [str(pythonw), str(pyw)],
        cwd=str(module_dir),
        creationflags=flags,
        close_fds=True,
    )
    for _ in range(10):
        time.sleep(1)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/health", timeout=2
            ) as response:
                if json.loads(response.read() or b"{}").get("ok") is True:
                    return True
        except Exception:  # noqa: BLE001 - not up yet; keep polling
            continue
    return False


def uninstall(config_dir: Path) -> int:
    """Remove everything the installer created; keep the request log."""
    removed: list[str] = []

    if os.name == "nt":
        lnk = _startup_dir() / "reqslim proxy.lnk"
        if lnk.exists():
            lnk.unlink()
            removed.append(str(lnk))
        result = subprocess.run(
            ["reg", "delete", r"HKCU\Environment", "/v", ENV_VAR, "/f"],
            capture_output=True, text=True, encoding="mbcs", errors="replace",
        )
        if result.returncode == 0:
            removed.append(f"user env var {ENV_VAR}")

    for name in MANAGED_FILES:
        candidate = config_dir / name
        if candidate.exists():
            candidate.unlink()
            removed.append(str(candidate))

    if removed:
        for item in removed:
            print(f"removed     {item}")
    else:
        print("nothing to remove")

    log = config_dir / "requests.jsonl"
    if log.exists():
        print(f"kept        {log} (traffic history)")
    print("\nZCode returns to its direct connection the next time it starts.")
    print("Already-open apps still carry the old env var until they are restarted;")
    print("sign out/in (or reboot) to clear it from every future process.")
    return 0


# ------------------------------------------------------------------- launchers
def write_windows_launcher(
    module_dir: Path, exe: Path | None, listeners: list[tuple[int, str]]
) -> Path:
    launcher = module_dir / "start-zcode.bat"
    target = str(exe) if exe else "ZCode.exe"
    listen_args = " ".join(f'--listen "{port}={url}"' for port, url in listeners)
    primary = listeners[0][0]
    body = f"""@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "MODULE_DIR=%~dp0"
set "CONFIG_DIR=%USERPROFILE%\\.zcode\\reqslim"
set "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE=%CONFIG_DIR%\\zcode-builtin.proxied.json"

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

start "reqslim proxy" /min "%PY%" -m reqslim.zcode --record "%CONFIG_DIR%\\requests.jsonl" {listen_args}

rem The proxy must answer before ZCode starts: a bind failure (port taken,
rem reserved range) must stop here, not become "every request fails" inside
rem ZCode with the cause hidden in a minimised window. %PY% passed the import
rem check above, so urllib is guaranteed to exist for the probe. An already
rem running proxy instance also passes: same listeners, same behaviour.
set /a PROBE=0
:wait_proxy
"%PY%" -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:{primary}/v1/health',timeout=2)" 2>nul
if not errorlevel 1 goto :proxy_ready
set /a PROBE+=1
if %PROBE% geq 10 goto :proxy_dead
timeout /t 1 /nobreak >nul
goto :wait_proxy
:proxy_dead
echo.
echo reqslim proxy did not come up on port {primary}.
echo   ZCode is NOT started: it would point at a dead port.
echo   look at the "reqslim proxy" window for the reason (port taken?).
echo.
pause
exit /b 1
:proxy_ready

start "" "{target}"
echo ZCode launched through reqslim ({len(listeners)} listener(s), first port {primary}).
echo   stats  http://127.0.0.1:{primary}/v1/stats
echo   report http://127.0.0.1:{primary}/v1/report
endlocal
"""
    launcher.write_text(body, encoding="utf-8")
    return launcher


def write_posix_launcher(
    module_dir: Path, exe: Path | None, listeners: list[tuple[int, str]]
) -> Path:
    launcher = module_dir / "start-zcode.sh"
    listen_args = " ".join(f"--listen {port}={url}" for port, url in listeners)
    primary = listeners[0][0]
    body = f"""#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
CONFIG_DIR="$HOME/.zcode/reqslim"
export ZCODE_BUILTIN_PROVIDER_CONFIG_FILE="$CONFIG_DIR/zcode-builtin.proxied.json"
export PYTHONPATH="$HERE:${{PYTHONPATH:-}}"

if ! python3 -c "import reqslim.zcode" 2>/dev/null; then
  echo "reqslim cannot be imported by $(command -v python3 || echo python3)." >&2
  echo "  check that \\"$HERE/reqslim\\" still exists, and that python3 is 3.9 or newer." >&2
  exit 1
fi

python3 -m reqslim.zcode --record "$CONFIG_DIR/requests.jsonl" {listen_args} &
PROXY_PID=$!

# The proxy must answer before ZCode starts: a bind failure must abort here,
# not leave ZCode pointing at a dead port. An already running proxy instance
# also passes: same listeners, same behaviour.
proxy_up=false
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:{primary}/v1/health',timeout=2)" 2>/dev/null; then
    proxy_up=true
    break
  fi
  kill -0 "$PROXY_PID" 2>/dev/null || break
  sleep 1
done
if [ "$proxy_up" != true ]; then
  echo "reqslim proxy did not come up on port {primary} -- ZCode NOT started." >&2
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
echo "  stats  http://127.0.0.1:{primary}/v1/stats"
"""
    launcher.write_text(body, encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


def write_helpers(module_dir: Path, port: int, py: str | None = None) -> tuple[Path, Path]:
    stats = module_dir / "zcode-stats.bat"
    report = module_dir / "zcode-report.bat"
    stats.write_text(
        "@echo off\nchcp 65001 >nul\n"
        f"curl -s http://127.0.0.1:{port}/v1/report 2>nul\n"
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
    parser.add_argument(
        "--no-autostart", action="store_true",
        help="only write the config and the manual launcher; no logon entry, no user env var",
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help="remove the logon entry, the user env var and the generated files; keeps requests.jsonl",
    )
    parser.add_argument("--package-dir", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    config_dir = Path.home() / ".zcode" / "reqslim"
    if args.uninstall:
        return uninstall(config_dir)

    install = find_zcode(args.zcode_dir)
    primary = UPSTREAM_PRESETS.get(args.upstream or "", args.upstream or UPSTREAM_PRESETS["bigmodel"])

    # One local port per distinct upstream; the extras follow the primary port.
    listeners: list[tuple[int, str]] = [(args.port, primary.rstrip("/"))]
    for extra in args.extra_upstream:
        url = UPSTREAM_PRESETS.get(extra, extra).rstrip("/")
        if url not in {u for _, u in listeners}:
            listeners.append((args.port + len(listeners), url))
    mapping = {url: f"http://127.0.0.1:{port}" for port, url in listeners}

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
        names = [ident for ident, local in touched if local == f"http://127.0.0.1:{port}"]
        print(f"listen      127.0.0.1:{port}  ->  {url}")
        print(f"            {', '.join(names)}")
    remaining = uncovered_upstreams(install.table, mapping)
    if remaining:
        print("uncovered   " + ", ".join(remaining))
        print("            pass --extra-upstream <url> to route these through reqslim too")

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    config_dir.mkdir(parents=True, exist_ok=True)
    generate_proxied_table(install.table, mapping, proxied)
    shutil.copy2(install.table, config_dir / "zcode-builtin.original.json")
    write_autostart_config(config_dir, listeners, install.table, record)

    if os.name == "nt":
        launcher = write_windows_launcher(module_dir, install.exe, listeners)
        stats, report = write_helpers(module_dir, args.port, sys.executable)
        helpers = f"{stats.name}, {report.name}"
    else:
        launcher = write_posix_launcher(module_dir, install.exe, listeners)
        helpers = "-"

    print(f"launcher    {launcher} (manual fallback)")
    if helpers != "-":
        print(f"helpers     {helpers}")
    if os.name == "nt" and not args.no_autostart:
        lnk, ok = install_autostart_windows(config_dir, module_dir, proxied, sys.executable)
        if ok:
            print(f"autostart   {lnk}")
            print(f"env         {ENV_VAR} -> {proxied} (user level; new processes inherit it)")
            print("watch       proxy rewrites the provider table within a minute of ZCode updates")
        else:
            print("autostart   incomplete -- start-zcode.bat remains the working path")
        # Start it now: the logon entry would otherwise leave every ZCode
        # request on a dead port until the next reboot.
        if start_proxy_now(sys.executable, config_dir / "proxy_autostart.pyw", module_dir, args.port):
            print(f"proxy       serving on http://127.0.0.1:{args.port} -- you can open ZCode now")
        else:
            print("proxy       did not come up -- see proxy.log in the config dir")
    elif os.name == "nt":
        print("autostart   skipped (--no-autostart); use start-zcode.bat to launch")
    else:
        print("autostart   not configured on this OS; add start-zcode.sh to your session's startup")
    print(f"\nZCode routes through reqslim on its next start. "
          f"Open http://127.0.0.1:{args.port}/v1/stats to watch traffic.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
