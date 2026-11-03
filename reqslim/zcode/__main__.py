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
import os
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

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
        listeners.append((int(port), url.rstrip("/")))
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
    poll_seconds: float


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
    listeners: list[tuple[int, str]] = []
    for item in data.get("listeners") or []:
        try:
            port, url = item
        except (TypeError, ValueError):
            raise SystemExit(f"autostart listeners expect [port, url] pairs, got: {item!r}")
        if not isinstance(port, int) or port <= 0 or not isinstance(url, str) or not url:
            raise SystemExit(f"autostart listeners expect [port, url] pairs, got: {item!r}")
        listeners.append((port, url.rstrip("/")))
    if not listeners:
        raise SystemExit("autostart config has no listeners")
    ports = [port for port, _ in listeners]
    if len(set(ports)) != len(ports):
        raise SystemExit(f"autostart config has duplicate ports: {ports}")
    if not data.get("zcode_table"):
        raise SystemExit("autostart config is missing zcode_table")
    if not data.get("config_dir"):
        raise SystemExit("autostart config is missing config_dir")
    return AutostartSpec(
        listeners=listeners,
        record=data.get("record"),
        zcode_table=data["zcode_table"],
        config_dir=data["config_dir"],
        poll_seconds=float(data.get("poll_seconds") or DEFAULT_POLL_SECONDS),
    )


def reqslim_on_port(port: int, timeout: float = 2.0) -> bool:
    """Whether a reqslim proxy (not some other app) is serving this port."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/v1/health", timeout=timeout
        ) as response:
            data = json.loads(response.read() or b"{}")
    except Exception:  # noqa: BLE001 - anything answering is not reqslim enough
        return False
    return data.get("ok") is True and isinstance(data.get("upstreams"), dict)


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
            if self.misses == REDISCOVER_AFTER:
                self._rediscover()
            return False
        self.misses = 0
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature == self.last:
            return False
        try:
            generate_proxied_table(self.source, self.mapping, self.out)
        except (OSError, ValueError) as exc:
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
            self.tick()


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
            except (OSError, ValueError) as exc:
                # Most likely the cloud refresher holds the file mid-write:
                # drop the signature and try again next tick.
                self.last.pop(path, None)
                self.log(f"runtime cache rewrite deferred: {path} ({exc})")
                continue
            if action == "rewritten":
                rewritten.append(path)
                self.log(f"runtime cache rewritten: {path}")
            if action != "unreadable":
                self.last[path] = signature
        for gone in set(self.last) - current:
            self.last.pop(gone, None)
        return rewritten

    def loop(self, stop: threading.Event, poll_seconds: float) -> None:
        while not stop.wait(poll_seconds):
            self.tick()


# ----------------------------------------------------------------------- main
def run_autostart(args: argparse.Namespace) -> int:
    spec = load_autostart_config(args.autostart_config or default_autostart_config_path())
    record_path = None
    if not args.no_record:
        record_path = args.record or spec.record or default_record_path()

    mapping = {url: f"http://127.0.0.1:{port}" for port, url in spec.listeners}
    proxied = Path(spec.config_dir) / "zcode-builtin.proxied.json"

    # Refresh the table before serving: a request that arrives in the first
    # seconds must not read a copy older than this process's own knowledge.
    watcher = TableWatcher(Path(spec.zcode_table), mapping, proxied)
    try:
        if watcher.tick():
            print(f"provider table regenerated -> {proxied}", flush=True)
    except Exception as exc:  # noqa: BLE001 - never block startup on the table
        print(f"provider table refresh failed, keeping previous copy: {exc}", flush=True)

    cache_watcher = CacheWatcher(mapping, log=print)
    try:
        cache_watcher.tick()
    except Exception as exc:  # noqa: BLE001 - never block startup on the cache
        print(f"runtime cache rewrite failed: {exc}", flush=True)

    served = [port for port, _ in spec.listeners if reqslim_on_port(port)]
    if len(served) == len(spec.listeners):
        print("reqslim proxy already running on all listeners; nothing to do", flush=True)
        return 0

    proxy = ZCodeProxy(
        spec.listeners[0][1],
        coalesce_wait=args.coalesce_wait,
        record_path=record_path,
    )
    try:
        proxy.serve_many(args.host, list(spec.listeners))
    except OSError as exc:
        partial = any(reqslim_on_port(port) for port, _ in spec.listeners)
        print(f"\nreqslim could not start: {exc}", file=sys.stderr, flush=True)
        if partial:
            print(
                "  an older reqslim instance with different listeners is running;\n"
                "  end it (or reboot) so the new listener set can bind.",
                file=sys.stderr, flush=True,
            )
        return 1

    stop = threading.Event()
    threading.Thread(
        target=watcher.loop, args=(stop, spec.poll_seconds),
        daemon=True, name="reqslim-table-watch",
    ).start()
    threading.Thread(
        target=cache_watcher.loop, args=(stop, spec.poll_seconds),
        daemon=True, name="reqslim-cache-watch",
    ).start()

    print("reqslim zcode proxy (autostart)", flush=True)
    for port, url in spec.listeners:
        print(f"  http://{args.host}:{port}  ->  {url}", flush=True)
    print(f"  watching  {spec.zcode_table} (every {spec.poll_seconds:.0f}s)", flush=True)
    print(f"  table ->  {proxied}", flush=True)
    print(f"  caches    {runtime_cache_root()} (every {spec.poll_seconds:.0f}s)", flush=True)
    if record_path:
        print(f"  recording {record_path}", flush=True)
    print(f"  stats     http://{args.host}:{spec.listeners[0][0]}/v1/stats", flush=True)
    print(f"  report    http://{args.host}:{spec.listeners[0][0]}/v1/report", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
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
    parser.add_argument("--ttl", type=float, default=3600.0, help="cache TTL in seconds")
    parser.add_argument("--max-cache", type=int, default=4096)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-coalesce", action="store_true")
    parser.add_argument(
        "--coalesce-wait", type=float, default=600.0, metavar="SECONDS",
        help="follower budget waiting for an in-flight leader; past it the "
             "follower forwards concurrently (0 waits forever; default 600)",
    )
    parser.add_argument("--failure-ttl", type=float, default=20.0, help="0 disables the failure memo")
    parser.add_argument("--record", default=None, help="JSONL path (default: ~/.zcode/reqslim/requests.jsonl)")
    parser.add_argument("--no-record", action="store_true")
    parser.add_argument("--report", default=None, help="print a report for a JSONL file and exit")
    args = parser.parse_args(argv)

    if args.report:
        recorder = load_events(args.report)
        print(recorder.format_report())
        return 0

    if args.autostart:
        if args.listen or args.upstream != DEFAULT_UPSTREAM or args.port != 8788:
            raise SystemExit("--autostart reads its listeners from autostart.json; "
                             "drop --listen/--upstream/--port")
        if args.record:
            raise SystemExit("--autostart takes its record path from autostart.json; use --no-record to disable")
        return run_autostart(args)

    record_path = None
    if not args.no_record:
        record_path = args.record or default_record_path()

    listeners = parse_listeners(args.listen)
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
    try:
        if listeners:
            proxy.serve_many(args.host, listeners)
        else:
            proxy.serve(args.host, args.port)
    except OSError as exc:
        print(f"\nreqslim could not start: {exc}", file=sys.stderr, flush=True)
        return 1

    if listeners:
        print("reqslim zcode proxy", flush=True)
        for port, url in listeners:
            print(f"  http://{args.host}:{port}  ->  {url}", flush=True)
    else:
        print(f"reqslim zcode proxy  http://{args.host}:{args.port}  ->  {args.upstream}", flush=True)
    print(f"  cache     {'on' if not args.no_cache else 'off'} (ttl {args.ttl:.0f}s)", flush=True)
    print(f"  coalesce  {'on' if not args.no_coalesce else 'off'}", flush=True)
    print(f"  memo      {'off' if not args.failure_ttl else f'{args.failure_ttl:.0f}s'}", flush=True)
    if record_path:
        print(f"  recording {record_path}", flush=True)
    stats_port = listeners[0][0] if listeners else args.port
    print(f"  stats     http://{args.host}:{stats_port}/v1/stats", flush=True)
    print(f"  report    http://{args.host}:{stats_port}/v1/report", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
