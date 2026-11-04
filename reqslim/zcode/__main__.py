"""CLI: ``python -m reqslim.zcode``.

    # run the proxy in front of ZCode
    python -m reqslim.zcode --upstream https://open.bigmodel.cn/api/anthropic

    # one local port per upstream, one process, one traffic report
    python -m reqslim.zcode --listen 8788=https://open.bigmodel.cn/api/anthropic \
                            --listen 8789=https://zcode.z.ai/api/v1/zcode-plan/anthropic

    # headless autostart used by the logon entry (tools/install_zcode.py writes
    # ~/.zcode/reqslim/autostart.json): also watches ZCode's provider table and
    # regenerates the proxied copy when ZCode updates it. Exits 0 without
    # starting a second instance if an equivalent proxy is already serving.
    python -m reqslim.zcode --autostart

    # read the recorded traffic
    python -m reqslim.zcode --report %USERPROFILE%\\.zcode\\reqslim\\requests.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import os
import ssl
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import __version__
from .analytics import load_events
from .proxy import ZCodeProxy
from .wiring import (
    find_table,
    generate_proxied_table,
    rewrite_runtime_cache_file,
    runtime_cache_files,
    runtime_cache_root,
)

DEFAULT_UPSTREAM = "https://open.bigmodel.cn/api/anthropic"

#: Local health/shutdown calls must never go through an environment proxy.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
#: The listener may be HTTPS (ZCode's signer requires it).  The certificate is
#: a loopback-only one made by this installer, so hostname verification against
#: the machine's root store is deliberately not part of the local probe.
_LOCAL_TLS_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
)

#: How often the autostart watcher re-checks ZCode's bundled provider table.
#: ZCode updates land between app launches; a minute of staleness after an
#: update is invisible next to the restart the update itself requires.
DEFAULT_POLL_SECONDS = 60.0

#: Consecutive watcher ticks with the source table missing before rediscovery
#: runs. One missing sample is an in-place update; ten is a moved install.
REDISCOVER_AFTER = 10


def default_record_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".zcode", "reqslim", "requests.jsonl")


def default_autostart_config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".zcode", "reqslim", "autostart.json")


def parse_listeners(specs: list[str]) -> list[tuple[int, str]]:
    listeners: list[tuple[int, str]] = []
    for spec in specs:
        port, sep, url = spec.partition("=")
        if not sep or not port.isdigit() or not url:
            raise SystemExit(f"--listen expects PORT=URL, got: {spec}")
        number = int(port)
        if not 0 < number <= 65535:
            raise SystemExit(f"--listen port must be 1..65535, got: {spec}")
        listeners.append((number, url.rstrip("/")))
    ports = [port for port, _ in listeners]
    if len(set(ports)) != len(ports):
        raise SystemExit(f"--listen: duplicate ports: {ports}")
    return listeners


# ------------------------------------------------------------------ autostart
@dataclass
class AutostartSpec:
    listeners: list[tuple[int, str]]
    record: str | None
    zcode_table: str
    config_dir: str
    poll_seconds: float = DEFAULT_POLL_SECONDS
    ttl: float = 3600.0
    max_cache: int = 4096
    cache_enabled: bool = True
    coalesce: bool = True
    coalesce_wait: float = 600.0
    failure_ttl: float = 20.0
    tls_cert: str | None = None
    tls_key: str | None = None


def _float_value(value: Any, default: float, name: str, minimum: float | None = None) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise SystemExit(f"autostart config {name} must be a number, got: {value!r}")
    if not math.isfinite(parsed):
        raise SystemExit(f"autostart config {name} must be finite, got: {value!r}")
    if minimum is not None and parsed < minimum:
        raise SystemExit(f"autostart config {name} must be >= {minimum}, got: {value!r}")
    return parsed


def _int_value(value: Any, default: int, name: str) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise SystemExit(f"autostart config {name} must be an integer, got: {value!r}")
    if parsed < 0:
        raise SystemExit(f"autostart config {name} must be >= 0, got: {value!r}")
    return parsed


def _bool_value(value: Any, default: bool, name: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SystemExit(f"autostart config {name} must be true or false, got: {value!r}")
    return value


def load_autostart_config(path: str) -> AutostartSpec:
    """Read what the installer wrote; the proxy's machine-specific state."""
    config = Path(path)
    if not config.exists():
        raise SystemExit(
            f"autostart config not found: {path}\n  run tools/install_zcode.py first"
        )
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"autostart config unreadable: {path} ({exc})")
    if not isinstance(data, dict):
        raise SystemExit(f"autostart config must be a JSON object: {path}")
    listeners: list[tuple[int, str]] = []
    raw_listeners = data.get("listeners")
    if not isinstance(raw_listeners, list):
        raise SystemExit("autostart config listeners must be a list")
    for item in raw_listeners:
        try:
            port, url = item
        except (TypeError, ValueError):
            raise SystemExit(f"autostart listeners expect [port, url] pairs, got: {item!r}")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port <= 65535:
            raise SystemExit(f"autostart listeners expect [port, url] pairs, got: {item!r}")
        if not isinstance(url, str) or not url:
            raise SystemExit(f"autostart listeners expect [port, url] pairs, got: {item!r}")
        listeners.append((port, url.rstrip("/")))
    if not listeners:
        raise SystemExit("autostart config has no listeners")
    ports = [port for port, _ in listeners]
    if len(set(ports)) != len(ports):
        raise SystemExit(f"autostart config has duplicate ports: {ports}")
    if not isinstance(data.get("zcode_table"), str) or not data.get("zcode_table"):
        raise SystemExit("autostart config is missing zcode_table")
    if not isinstance(data.get("config_dir"), str) or not data.get("config_dir"):
        raise SystemExit("autostart config is missing config_dir")
    tls_cert = tls_key = None
    raw_tls = data.get("tls")
    if raw_tls is not None:
        if not isinstance(raw_tls, dict):
            raise SystemExit("autostart config tls must be an object")
        tls_cert = raw_tls.get("cert")
        tls_key = raw_tls.get("key")
        if bool(tls_cert) != bool(tls_key):
            raise SystemExit("autostart config tls needs both cert and key")
        if tls_cert is not None and (not isinstance(tls_cert, str) or not tls_cert):
            raise SystemExit("autostart config tls.cert must be a path")
        if tls_key is not None and (not isinstance(tls_key, str) or not tls_key):
            raise SystemExit("autostart config tls.key must be a path")
    return AutostartSpec(
        listeners=listeners,
        record=data.get("record") if isinstance(data.get("record"), str) else None,
        zcode_table=data["zcode_table"],
        config_dir=data["config_dir"],
        poll_seconds=_float_value(
            data.get("poll_seconds"), DEFAULT_POLL_SECONDS, "poll_seconds", minimum=0.1
        ),
        ttl=_float_value(data.get("ttl"), 3600.0, "ttl", minimum=0.0),
        max_cache=_int_value(data.get("max_cache"), 4096, "max_cache"),
        cache_enabled=_bool_value(data.get("cache"), True, "cache"),
        coalesce=_bool_value(data.get("coalesce"), True, "coalesce"),
        coalesce_wait=_float_value(
            data.get("coalesce_wait"), 600.0, "coalesce_wait", minimum=0.0
        ),
        failure_ttl=_float_value(data.get("failure_ttl"), 20.0, "failure_ttl", minimum=0.0),
        tls_cert=tls_cert,
        tls_key=tls_key,
    )


def health_on_port(
    port: int, timeout: float = 2.0, *, scheme: str = "http"
) -> dict[str, Any] | None:
    """Return the health document for a reqslim instance, or None.

    Listeners can be HTTP (older installs, ``--no-tls``) or HTTPS (the
    installer's default for ZCode 3.17's signer).  Callers know which one
    they configured; probing the wrong scheme would make the other listener
    log a protocol error on every poll.
    """
    opener = _LOCAL_TLS_OPENER if scheme == "https" else _LOCAL_OPENER
    try:
        with opener.open(
            f"{scheme}://127.0.0.1:{port}/v1/health", timeout=timeout
        ) as response:
            data = json.loads(response.read() or b"{}")
    except Exception:  # noqa: BLE001 - anything answering is not reqslim enough
        return None
    if not isinstance(data, dict) or data.get("ok") is not True:
        return None
    if not isinstance(data.get("upstreams"), dict):
        return None
    return data


def health_on_port_any(port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    """Health probe when the listener's scheme is not known yet."""
    return (
        health_on_port(port, timeout=timeout, scheme="https")
        or health_on_port(port, timeout=timeout, scheme="http")
    )


def reqslim_on_port(
    port: int, timeout: float = 2.0, *, scheme: str = "http"
) -> bool:
    """Whether a reqslim proxy (not some other app) is serving this port."""
    return health_on_port(port, timeout=timeout, scheme=scheme) is not None


def _local_request(
    port: int,
    path: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
    timeout: float = 2.0,
    scheme: str = "http",
):
    """Open a local listener using the configured scheme."""
    opener = _LOCAL_TLS_OPENER if scheme == "https" else _LOCAL_OPENER
    request = urllib.request.Request(
        f"{scheme}://127.0.0.1:{port}{path}",
        data=data,
        headers=headers or {},
        method=method,
    )
    return opener.open(request, timeout=timeout)


def stop_running_proxy(
    ports: list[int] | tuple[int, ...],
    timeout: float = 10.0,
    *,
    scheme: str = "http",
) -> bool:
    """Ask every reqslim listener on ``ports`` to shut down.

    Returns True when no reqslim health endpoint is left. Used by the installer
    and uninstaller so a running watcher cannot rewrite restored runtime
    caches and so a reinstall cannot leave the old upstream mapping in place.
    """
    found = False
    for port in sorted(set(ports)):
        health = health_on_port(port, scheme=scheme)
        if health is None:
            continue
        found = True
        try:
            with _local_request(
                port,
                "/v1/shutdown",
                data=b"",
                headers={"Content-Length": "0"},
                method="POST",
                timeout=2,
                scheme=scheme,
            ) as response:
                response.read()
        except Exception:  # noqa: BLE001 - an old proxy may not have /shutdown
            continue
    if not found:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(
            reqslim_on_port(port, timeout=0.5, scheme=scheme) for port in set(ports)
        ):
            return True
        time.sleep(0.2)
    return not any(
        reqslim_on_port(port, timeout=0.5, scheme=scheme) for port in set(ports)
    )


class TableWatcher:
    """Keeps the proxied provider table in sync with ZCode's bundled one.

    ZCode updates rewrite its builtin table (new providers, new template
    revisions); without this loop the proxied copy drifts and freshly added
    providers bypass reqslim. Each ``tick`` compares a cheap (mtime, size)
    signature and regenerates the proxied table through the same rewrite the
    installer uses, so install-time and update-time behaviour cannot diverge.

    A missing source table is normal for the seconds an updater has the file
    mid-swap -- the last good proxied table keeps serving and the tick just
    counts misses. ``REDISCOVER_AFTER`` misses in a row mean the install moved;
    the standard candidate list is searched once and adopted if it finds a
    table, which is the one recovery an unattended watcher can do safely.
    """

    def __init__(
        self,
        source: Path,
        mapping: dict[str, str],
        out: Path,
        log=None,
    ) -> None:
        self.source = Path(source)
        self.mapping = dict(mapping)
        self.out = Path(out)
        self.log = log or (lambda message: None)
        self.last: tuple[int, int] | None = None
        self.misses = 0

    def tick(self) -> bool:
        """Run one check. Returns True when the proxied table was regenerated."""
        try:
            stat = self.source.stat()
        except OSError:
            self.misses += 1
            if self.misses % REDISCOVER_AFTER == 0:
                self._rediscover()
            return False
        self.misses = 0
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature == self.last:
            return False
        try:
            generate_proxied_table(self.source, self.mapping, self.out)
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            # ``last`` stays unset so the next tick retries; a persistent
            # failure (mapping stale after a big provider-table shuffle)
            # degrades to one log line per poll, not a broken proxy.
            self.log(f"provider table refresh failed, keeping previous copy: {exc}")
            return False
        self.last = signature
        self.log("provider table refreshed (ZCode table changed)")
        return True

    def _rediscover(self) -> None:
        found = find_table(None)
        if found and found != self.source:
            self.log(f"provider table moved, now watching {found}")
            self.source = found
            self.last = None
            self.misses = 0

    def loop(self, stop: threading.Event, poll_seconds: float) -> None:
        while not stop.wait(poll_seconds):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - never kill the watcher
                self.log(f"provider table watcher error: {exc}")


class CacheWatcher:
    """Keeps ZCode's runtime provider caches pointed at reqslim.

    A ZCode update unpacks a new per-version directory under
    ``~/.zcode/v2/runtime/provider`` whose cloud-refreshed table carries the
    original baseUrls -- and the agent host reads that table BEFORE the
    env-var file, so the new version silently bypasses the proxy (observed
    live on 3.17.2 right after an upgrade). The installer rewrites the
    caches that exist at install time; this loop catches the ones that
    appear later, through the same ``rewrite_runtime_cache_file``. Each tick
    stats every cache file and reparses only the changed ones; rewrites are
    idempotent, so the steady state is stat-only.
    """

    def __init__(self, mapping: dict[str, str], root: Path | None = None, log=None) -> None:
        self.mapping = dict(mapping)
        self.root = root
        self.log = log or (lambda message: None)
        self.last: dict[Path, tuple[int, int]] = {}

    def tick(self) -> list[Path]:
        """Run one check; returns the cache files rewritten by it."""
        rewritten: list[Path] = []
        current: set[Path] = set()
        for path in runtime_cache_files(self.root):
            current.add(path)
            try:
                stat = path.stat()
            except OSError:
                self.last.pop(path, None)
                continue
            signature = (stat.st_mtime_ns, stat.st_size)
            if self.last.get(path) == signature:
                continue
            try:
                action = rewrite_runtime_cache_file(path, self.mapping)
            except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
                # Most likely the cloud refresher holds the file mid-write:
                # drop the signature and try again next tick.
                self.last.pop(path, None)
                self.log(f"runtime cache rewrite deferred: {path} ({exc})")
                continue
            if action == "rewritten":
                rewritten.append(path)
                self.log(f"runtime cache rewritten: {path}")
            if action != "unreadable":
                try:
                    stat = path.stat()
                except OSError:
                    self.last.pop(path, None)
                else:
                    self.last[path] = (stat.st_mtime_ns, stat.st_size)
        for gone in set(self.last) - current:
            self.last.pop(gone, None)
        return rewritten

    def loop(self, stop: threading.Event, poll_seconds: float) -> None:
        while not stop.wait(poll_seconds):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - never kill the watcher
                self.log(f"runtime cache watcher error: {exc}")


# ----------------------------------------------------------------------- main
def _autostart_settings(
    spec: AutostartSpec, host: str, record_path: str | None, *,
    ttl: float | None = None, max_cache: int | None = None,
    cache_enabled: bool | None = None, coalesce: bool | None = None,
    coalesce_wait: float | None = None, failure_ttl: float | None = None,
) -> dict[str, Any]:
    return {
        "ttl": spec.ttl if ttl is None else ttl,
        "max_cache": spec.max_cache if max_cache is None else max_cache,
        "cache": spec.cache_enabled if cache_enabled is None else cache_enabled,
        "coalesce": spec.coalesce if coalesce is None else coalesce,
        "coalesce_wait": spec.coalesce_wait if coalesce_wait is None else coalesce_wait,
        "failure_ttl": spec.failure_ttl if failure_ttl is None else failure_ttl,
        "record": os.path.abspath(record_path) if record_path else None,
        "watchers": {
            "table": os.path.abspath(spec.zcode_table),
            "config_dir": os.path.abspath(spec.config_dir),
            "poll_seconds": spec.poll_seconds,
            "host": host,
            "tls_cert": os.path.abspath(spec.tls_cert) if spec.tls_cert else None,
            "tls_key": os.path.abspath(spec.tls_key) if spec.tls_key else None,
        },
    }


def _matches_autostart(
    health: dict[str, Any] | None, spec: AutostartSpec, settings: dict[str, Any],
) -> bool:
    return (
        health is not None
        and health.get("upstreams") == {str(port): url for port, url in spec.listeners}
        and health.get("tls") is bool(spec.tls_cert and spec.tls_key)
        and health.get("ready", True) is True
        and health.get("version") == __version__
        and health.get("settings") == settings
    )


def autostart_health_matches(
    health: dict[str, Any] | None, spec: AutostartSpec, host: str = "127.0.0.1",
) -> bool:
    settings = _autostart_settings(spec, host, spec.record or default_record_path())
    return _matches_autostart(health, spec, settings)


def autostart_ready(config_path: str, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    try:
        spec = load_autostart_config(config_path)
    except (OSError, ValueError, SystemExit):
        return False
    scheme = "https" if spec.tls_cert and spec.tls_key else "http"
    return all(
        autostart_health_matches(health_on_port(port, timeout=timeout, scheme=scheme), spec, host)
        for port, _ in spec.listeners
    )


def run_autostart(args: argparse.Namespace) -> int:
    spec = load_autostart_config(args.autostart_config or default_autostart_config_path())
    record_path = None
    if not args.no_record:
        record_path = spec.record or default_record_path()
    expected_tls = bool(spec.tls_cert and spec.tls_key)
    scheme = "https" if expected_tls else "http"
    settings = _autostart_settings(
        spec, args.host, record_path, ttl=args.ttl, max_cache=args.max_cache,
        cache_enabled=spec.cache_enabled and not args.no_cache,
        coalesce=spec.coalesce and not args.no_coalesce,
        coalesce_wait=args.coalesce_wait, failure_ttl=args.failure_ttl,
    )
    ports = [port for port, _ in spec.listeners]
    health = {port: health_on_port(port, scheme=scheme) for port in ports}
    if all(item is not None for item in health.values()):
        if all(
            _matches_autostart(item, spec, settings)
            for item in health.values()
        ):
            print("reqslim proxy already running with the same listeners; nothing to do", flush=True)
            return 0
        print("reqslim proxy is running with different settings; restarting it", flush=True)
        if not stop_running_proxy(ports, scheme=scheme):
            print(
                "could not stop the running reqslim proxy; end it manually and try again",
                file=sys.stderr,
                flush=True,
            )
            return 1
    else:
        # An upgrade changes the scheme; an old proxy is still healthy on the
        # other scheme and would keep the new listeners from binding.
        stale_scheme = "http" if scheme == "https" else "https"
        stale = {
            port: health_on_port(port, scheme=stale_scheme) for port in ports
        }
        if any(item is not None for item in stale.values()):
            print("reqslim is running with the previous transport; restarting it", flush=True)
            if not stop_running_proxy(ports, scheme=stale_scheme):
                print(
                    "could not stop the previous reqslim proxy; end it manually and try again",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
        if any(item is not None for item in health.values()):
            print("reqslim is running on only part of the listener set; restarting it", flush=True)
            if not stop_running_proxy(ports, scheme=scheme):
                print(
                    "could not stop the partial reqslim proxy; end it manually and try again",
                    file=sys.stderr,
                    flush=True,
                )
                return 1

    bind_host = args.host
    advertised = "127.0.0.1" if bind_host in {"0.0.0.0", "::"} else bind_host
    mapping = {url: f"{scheme}://{advertised}:{port}" for port, url in spec.listeners}
    proxied = Path(spec.config_dir) / "zcode-builtin.proxied.json"

    watcher = TableWatcher(Path(spec.zcode_table), mapping, proxied)
    cache_watcher = CacheWatcher(mapping, log=print)
    ttl = settings["ttl"]
    max_cache = settings["max_cache"]
    coalesce_wait = settings["coalesce_wait"]
    failure_ttl = settings["failure_ttl"]
    proxy = ZCodeProxy(
        spec.listeners[0][1],
        ttl=ttl,
        max_cache=max_cache,
        cache_enabled=settings["cache"],
        coalesce=settings["coalesce"],
        coalesce_wait=coalesce_wait,
        failure_ttl=failure_ttl,
        record_path=record_path,
    )
    proxy.watchers = settings["watchers"]
    proxy.ready = False
    proxy.managed_shutdown = True
    try:
        tls = (spec.tls_cert, spec.tls_key) if scheme == "https" else None
        proxy.serve_many(args.host, list(spec.listeners), tls=tls)
    except OSError as exc:
        partial = any(
            reqslim_on_port(port, scheme=scheme) for port, _ in spec.listeners
        )
        print(f"\nreqslim could not start: {exc}", file=sys.stderr, flush=True)
        if partial:
            print(
                "  an older reqslim instance with different listeners is running;\n"
                "  end it (or reboot) so the new listener set can bind.",
                file=sys.stderr, flush=True,
            )
        return 1

    try:
        if watcher.tick():
            print(f"provider table regenerated -> {proxied}", flush=True)
    except Exception as exc:
        print(f"provider table refresh failed, keeping previous copy: {exc}", flush=True)
    try:
        cache_watcher.tick()
    except Exception as exc:
        print(f"runtime cache rewrite failed: {exc}", flush=True)

    stop = threading.Event()
    workers = [
        threading.Thread(
            target=watcher.loop, args=(stop, spec.poll_seconds),
            daemon=True, name="reqslim-table-watch",
        ),
        threading.Thread(
            target=cache_watcher.loop, args=(stop, spec.poll_seconds),
            daemon=True, name="reqslim-cache-watch",
        ),
    ]
    for worker in workers:
        worker.start()
    proxy.ready = True

    print("reqslim zcode proxy (autostart)", flush=True)
    for port, url in spec.listeners:
        print(f"  {scheme}://{args.host}:{port}  ->  {url}", flush=True)
    print(f"  watching  {spec.zcode_table} (every {spec.poll_seconds:.0f}s)", flush=True)
    print(f"  table ->  {proxied}", flush=True)
    print(f"  caches    {runtime_cache_root()} (every {spec.poll_seconds:.0f}s)", flush=True)
    print(
        f"  cache     {'on' if proxy.cache_enabled else 'off'} (ttl {ttl:.0f}s), "
        f"coalesce {'on' if proxy.coalesce else 'off'}, memo {failure_ttl:.0f}s",
        flush=True,
    )
    if record_path:
        print(f"  recording {record_path}", flush=True)
    print(f"  stats     {scheme}://{args.host}:{spec.listeners[0][0]}/v1/stats", flush=True)
    print(f"  report    {scheme}://{args.host}:{spec.listeners[0][0]}/v1/report", flush=True)
    try:
        while not proxy.shutdown_event.wait(3600):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for worker in workers:
            worker.join()
        proxy.close()
    print("\nstopped", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reqslim.zcode", description="ZCode request-reduction proxy")
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument(
        "--listen",
        action="append",
        default=[],
        metavar="PORT=URL",
        help="bind one local port per upstream (repeatable); overrides --upstream/--port",
    )
    parser.add_argument(
        "--autostart", action="store_true",
        help="read listeners from the installer's autostart.json, refresh the proxied "
             "provider table on ZCode updates, and exit 0 if an equivalent proxy already runs",
    )
    parser.add_argument("--autostart-config", default=None, metavar="PATH")
    parser.add_argument("--ttl", type=float, default=None, help="cache TTL in seconds (default: 3600)")
    parser.add_argument("--max-cache", type=int, default=None)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-coalesce", action="store_true")
    parser.add_argument(
        "--coalesce-wait", type=float, default=None, metavar="SECONDS",
        help="follower budget waiting for an in-flight leader; past it the "
             "follower forwards concurrently (0 waits forever; default 600)",
    )
    parser.add_argument("--failure-ttl", type=float, default=None, help="0 disables the failure memo (default: 20)")
    parser.add_argument("--record", default=None, help="JSONL path (default: ~/.zcode/reqslim/requests.jsonl)")
    parser.add_argument("--no-record", action="store_true")
    parser.add_argument("--tls-cert", default=None, metavar="PATH", help="serve HTTPS with this certificate")
    parser.add_argument("--tls-key", default=None, metavar="PATH", help="private key for --tls-cert")
    parser.add_argument("--report", default=None, help="print a report for a JSONL file and exit")
    args = parser.parse_args(argv)

    for name in ("ttl", "coalesce_wait", "failure_ttl"):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value < 0):
            raise SystemExit(f"--{name.replace('_', '-')} must be finite and >= 0")
    if args.max_cache is not None and args.max_cache < 0:
        raise SystemExit("--max-cache must be >= 0")

    if args.report:
        if not os.path.isfile(args.report):
            print(f"report file not found: {args.report}", file=sys.stderr)
            return 2
        recorder = load_events(args.report)
        print(recorder.format_report())
        return 0

    if args.autostart:
        if args.listen or args.upstream != DEFAULT_UPSTREAM or args.port != 8788:
            raise SystemExit("--autostart reads its listeners from autostart.json; "
                             "drop --listen/--upstream/--port")
        if args.record:
            raise SystemExit("--autostart takes its record path from autostart.json; use --no-record to disable")
        if args.tls_cert or args.tls_key:
            raise SystemExit("--autostart reads TLS paths from autostart.json; drop --tls-cert/--tls-key")
        return run_autostart(args)

    for name, default in (("ttl", 3600.0), ("max_cache", 4096), ("coalesce_wait", 600.0), ("failure_ttl", 20.0)):
        if getattr(args, name) is None:
            setattr(args, name, default)

    if not 0 < args.port <= 65535:
        raise SystemExit(f"--port must be 1..65535, got: {args.port}")

    record_path = None
    if not args.no_record:
        record_path = args.record or default_record_path()

    listeners = parse_listeners(args.listen)
    if bool(args.tls_cert) != bool(args.tls_key):
        raise SystemExit("--tls-cert and --tls-key must be given together")
    tls = (args.tls_cert, args.tls_key) if args.tls_cert else None
    proxy = ZCodeProxy(
        args.upstream,
        ttl=args.ttl,
        max_cache=args.max_cache,
        cache_enabled=not args.no_cache,
        coalesce=not args.no_coalesce,
        coalesce_wait=args.coalesce_wait,
        failure_ttl=args.failure_ttl,
        record_path=record_path,
    )
    proxy.managed_shutdown = True
    try:
        if listeners:
            proxy.serve_many(args.host, listeners, tls=tls)
        else:
            proxy.serve(args.host, args.port, tls=tls)
    except OSError as exc:
        print(f"\nreqslim could not start: {exc}", file=sys.stderr, flush=True)
        return 1

    scheme = "https" if tls else "http"
    if listeners:
        print("reqslim zcode proxy", flush=True)
        for port, url in listeners:
            print(f"  {scheme}://{args.host}:{port}  ->  {url}", flush=True)
    else:
        print(f"reqslim zcode proxy  {scheme}://{args.host}:{args.port}  ->  {args.upstream}", flush=True)
    print(f"  cache     {'on' if not args.no_cache else 'off'} (ttl {args.ttl:.0f}s)", flush=True)
    print(f"  coalesce  {'on' if not args.no_coalesce else 'off'}", flush=True)
    print(f"  memo      {'off' if not args.failure_ttl else f'{args.failure_ttl:.0f}s'}", flush=True)
    if record_path:
        print(f"  recording {record_path}", flush=True)
    stats_port = listeners[0][0] if listeners else args.port
    print(f"  stats     {scheme}://{args.host}:{stats_port}/v1/stats", flush=True)
    print(f"  report    {scheme}://{args.host}:{stats_port}/v1/report", flush=True)
    try:
        while not proxy.shutdown_event.wait(3600):
            pass
        print("\nstopped", flush=True)
        proxy.close()
        return 0
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
        proxy.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
