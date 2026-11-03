"""ZCode-specific proxy.

Three request-reduction mechanisms, in the order they are tried:

1. **L1 raw hash** - the exact same bytes we already answered. Costs one sha256
   of the body and lets a repeat skip canonicalisation entirely, which is why it
   is tried first and reported as its own hit level.
2. **L2 normalised key** - same conversation modulo ``cache_control`` markers,
   tool ids and tool ordering. This is the one that catches ZCode's natural
   churn; without it the hit rate is close to zero.
3. **In-flight coalescing** - two identical requests arriving while the first is
   still streaming. ZCode fans out sub-agents and retries on timeout, so this is
   a real pattern, not a theoretical one.

Plus a **failure memo** with a short TTL: when the upstream returns 429/5xx, the
identical retry that follows is answered from the memo instead of hammering the
provider. It does not make the agent succeed, it stops the retry storm from
multiplying it.

**Upstreams**: ZCode picks an upstream through each provider's ``baseUrl``, and
different providers of the same account family can sit on different gateways
(e.g. ``open.bigmodel.cn`` for API keys vs ``zcode.z.ai`` for plan accounts).
``serve_many`` binds one local port per upstream inside one process, so the
provider table can route every provider through reqslim while the traffic
report stays unified. Cache, coalescing and failure memo are partitioned by
upstream: replaying an answer fetched under another gateway would change the
answer the client would have received, which violates the unmount guarantee.

Deliberately *not* included: response rewriting, context trimming, and model
routing. Trimming an Anthropic conversation requires preserving strict
``tool_use`` / ``tool_result`` pairing and thinking signatures; getting it
subtly wrong corrupts a session in ways that are painful to debug. The proxy
reduces requests and stays invisible.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..cache import ExactCache
from ..types import LLMResponse
from .analytics import Event, Recorder
from .normalize import fingerprint
from .stream import Assembler, replay_sse, tool_uses, usable

HOP_BY_HOP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "accept-encoding",
}

RETRYABLE = {429, 500, 502, 503, 504}


class ZCodeProxy:
    def __init__(
        self,
        upstream: str,
        *,
        ttl: float = 3600.0,
        max_cache: int = 4096,
        max_cache_bytes: int = 1_000_000,
        cache_bytes: int = 128 * 1024 * 1024,
        cache_enabled: bool = True,
        coalesce: bool = True,
        failure_ttl: float = 20.0,
        record_path: str | None = None,
        timeout: float = 300.0,
        max_body: int = 64 * 1024 * 1024,
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self.cache = ExactCache(maxsize=max_cache, ttl=ttl, max_bytes=cache_bytes)
        self.cache_enabled = cache_enabled
        self.coalesce = coalesce
        self.failure_ttl = failure_ttl
        self.max_cache_bytes = max_cache_bytes
        self.timeout = timeout
        self.max_body = max_body
        self.recorder = Recorder(record_path)

        self._lock = threading.Lock()
        self._l1: OrderedDict[tuple[str, str], tuple[str, Any]] = OrderedDict()
        self._inflight: dict[str, threading.Event] = {}
        self._failures: dict[str, tuple[float, int, bytes]] = {}
        #: port -> upstream URL, filled in by serve/serve_many
        self.listeners: dict[int, str] = {}

    # ------------------------------------------------------------------ server
    def serve(self, host: str = "127.0.0.1", port: int = 8788) -> ThreadingHTTPServer:
        """Single-upstream convenience wrapper around :meth:`serve_many`."""
        return self.serve_many(host, [(port, self.upstream)])[0]

    def serve_many(
        self, host: str, listeners: list[tuple[int, str]]
    ) -> list[ThreadingHTTPServer]:
        """Bind one local port per upstream, all sharing this proxy's state.

        ZCode routes through each provider's ``baseUrl``, and different
        providers of one account family can sit on different gateways. Giving
        every gateway its own port lets the provider table route them all
        through reqslim while recorder and report stay unified. Cache keys,
        coalescing and the failure memo are scoped per upstream -- see the
        module docstring for why sharing them across gateways would be wrong.
        """
        ports = [port for port, _ in listeners]
        # port 0 means "let the OS pick" (tests use it), so it may repeat.
        fixed = [port for port in ports if port]
        if len(set(fixed)) != len(fixed):
            raise ValueError(f"duplicate listen ports: {fixed}")
        self.listeners = {port: upstream for port, upstream in listeners}
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "reqslim-zcode/0.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0].rstrip("/")
                if path in {"/v1/stats", "/stats"}:
                    self._json(200, proxy.stats())
                elif path in {"/v1/report", "/report"}:
                    body = proxy.recorder.format_report().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path in {"/health", "/v1/health"}:
                    self._json(
                        200,
                        {
                            "ok": True,
                            "upstream": proxy.upstream,
                            "upstreams": {
                                str(port): url
                                for port, url in sorted(proxy.listeners.items())
                            },
                            "cache": proxy.cache_enabled,
                            "coalesce": proxy.coalesce,
                        },
                    )
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = self.path.split("?")[0].rstrip("/")
                if not (path.endswith("/v1/messages") or path.endswith("/messages")):
                    self._json(404, {"error": f"unsupported path {self.path}"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self._json(400, {"error": "invalid content-length"})
                    return
                if length > proxy.max_body:
                    # Refuse before reading: a runaway body must not hold a
                    # reader thread hostage. The connection is closed because
                    # an unread body would poison the next request on it.
                    self.close_connection = True
                    self._json(413, {"error": f"request body too large ({length} bytes)"})
                    return
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._json(400, {"error": "invalid json"})
                    return
                proxy.handle(self, payload, raw, dict(self.headers.items()), path)

            def _json(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def write_chunk(self, data: bytes) -> None:
                self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
                self.wfile.flush()

        class Server(ThreadingHTTPServer):
            # On Windows SO_REUSEADDR lets a *second* process bind a port that
            # is already listening, so the second instance silently steals
            # traffic instead of failing. POSIX needs the flag for a fast
            # restart while the old socket is in TIME_WAIT.
            allow_reuse_address = os.name != "nt"

        servers: list[ThreadingHTTPServer] = []
        for port, upstream in listeners:
            try:
                server = Server((host, port), Handler)
            except OSError as exc:
                # A half-bound set would leave the caller with a proxy that
                # answers on some ports and not others, which is harder to
                # diagnose than a clean refusal.
                for bound in servers:
                    bound.shutdown()
                    bound.server_close()
                reason = exc.strerror or str(exc)
                hint = "another reqslim instance may be running, or the port is taken"
                if isinstance(exc, PermissionError):
                    hint = (
                        "on Windows this usually means the port is in use or sits in "
                        "a reserved range (Hyper-V / WSL dynamic ports)"
                    )
                raise OSError(
                    f"cannot bind {host}:{port} - {hint} [{reason}]. "
                    "Pick a different --port and re-run the installer."
                ) from exc
            server.upstream = upstream  # type: ignore[attr-defined]
            threading.Thread(
                target=server.serve_forever, daemon=True, name=f"reqslim-zcode:{port}"
            ).start()
            servers.append(server)
        # Key by the actually-bound port: with port=0 the OS assigns and the
        # requested port is useless as a dict key.
        self.listeners = {
            server.server_address[1]: server.upstream  # type: ignore[attr-defined]
            for server in servers
        }
        return servers

    # ------------------------------------------------------------------- core
    def handle(
        self,
        handler: Any,
        payload: dict[str, Any],
        raw: bytes,
        headers: dict[str, str],
        path: str = "/v1/messages",
    ) -> None:
        started = time.time()
        upstream = getattr(handler.server, "upstream", None) or self.upstream
        streaming = bool(payload.get("stream"))
        suffix = "/v1/messages" if path.endswith("/v1/messages") else "/messages"
        raw_hash = hashlib.sha256(raw).hexdigest()

        # Cache scopes are per upstream: the same logical request to two
        # gateways is two different conversations as far as replay is concerned.
        with self._lock:
            known = self._l1.get((upstream, raw_hash))
            if known is not None:
                # A repeat is exactly the entry worth keeping under memory
                # pressure; eviction below must see it as recently used.
                self._l1.move_to_end((upstream, raw_hash))
        # A raw-hash hit means these exact bytes were answered before, so the
        # saving belongs to the cheapest mechanism. Reporting it as
        # "normalized" would hide whether the raw path ever fires.
        level = "normalized" if known is None else "raw"
        if known is None:
            fp = fingerprint(payload)
            key = f"{upstream}|{fp.key}"
        else:
            key, fp = known

        is_leader = False
        for _ in range(3):
            hit = self._cache_get(key)
            if hit is not None:
                # ``billed=False``: the tokens were paid for when this answer
                # was first fetched. Counting them again would make the report
                # show more input tokens than the upstream ever saw.
                self._record(fp, payload, raw, started, level, 200, hit, billed=False)
                self._replay(handler, hit, streaming)
                return

            memo = self._failure_get(key)
            if memo is not None:
                status, body = memo
                self._record(fp, payload, raw, started, "failure_memo", status)
                self._raw(handler, status, body)
                return

            if not self.coalesce:
                break
            leader, event = self._acquire(key)
            if leader:
                is_leader = True
                break
            event.wait(timeout=180)
            # leader finished: loop and re-check the cache

        try:
            if streaming:
                self._forward_stream(
                    handler, payload, headers, key, raw_hash, fp, raw, started, suffix, upstream
                )
            else:
                self._forward_block(
                    handler, payload, headers, key, raw_hash, fp, raw, started, suffix, upstream
                )
        finally:
            if is_leader:
                self._release(key)

    # ---------------------------------------------------------------- forward
    def _forward_block(
        self,
        handler: Any,
        payload: dict[str, Any],
        headers: dict[str, str],
        key: str,
        raw_hash: str,
        fp: Any,
        raw: bytes,
        started: float,
        suffix: str,
        upstream: str,
    ) -> None:
        try:
            status, body = self._request(payload, headers, suffix, upstream)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self._maybe_memo(key, exc.code, body)
            self._record(fp, payload, raw, started, "", exc.code)
            self._raw(handler, exc.code, body)
            return
        except Exception as exc:  # noqa: BLE001
            self._record(fp, payload, raw, started, "", 502)
            self._send_error(handler, 502, str(exc))
            return

        if status != 200:
            self._record(fp, payload, raw, started, "", status)
            self._raw(handler, status, body)
            return

        try:
            message = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A 200 that is not JSON -- a gateway HTML page, a WAF block, an
            # empty body. Forward it untouched: the client can still show the
            # real error, and nothing is cached under a key it does not own.
            self._record(fp, payload, raw, started, "", status)
            self._raw(handler, status, body)
            return

        self._store(key, raw_hash, fp, message, raw, upstream)
        self._record(fp, payload, raw, started, "", 200, message)
        self._raw(handler, 200, body)

    def _forward_stream(
        self,
        handler: Any,
        payload: dict[str, Any],
        headers: dict[str, str],
        key: str,
        raw_hash: str,
        fp: Any,
        raw: bytes,
        started: float,
        suffix: str,
        upstream: str,
    ) -> None:
        try:
            upstream_conn = self._open(payload, headers, suffix, upstream)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self._maybe_memo(key, exc.code, body)
            self._raw(handler, exc.code, body)
            self._record(fp, payload, raw, started, "", exc.code)
            return
        except Exception as exc:  # noqa: BLE001
            self._send_error(handler, 502, str(exc))
            self._record(fp, payload, raw, started, "", 502)
            return

        with upstream_conn:
            if upstream_conn.status != 200:
                body = upstream_conn.read()
                self._raw(handler, upstream_conn.status, body)
                self._record(fp, payload, raw, started, "", upstream_conn.status)
                return

            try:
                handler.send_response(200)
                handler.send_header("Content-Type", "text/event-stream")
                handler.send_header("Cache-Control", "no-cache")
                handler.send_header("Connection", "keep-alive")
                handler.send_header("Transfer-Encoding", "chunked")
                handler.end_headers()
            except OSError:
                # The client vanished before the response even started; there
                # is nothing to forward to and nothing to replay.
                self._record(fp, payload, raw, started, "", 499)
                return

            assembler = Assembler()
            aborted = False       # client hung up mid-stream
            upstream_died = False  # upstream connection broke mid-stream
            try:
                for line in upstream_conn:
                    try:
                        handler.write_chunk(line)
                    except OSError:
                        # Client-side writes are wrapped tight so an upstream
                        # read failure is attributed to the right side.
                        aborted = True
                        break
                    stripped = line.decode("utf-8", "replace").strip()
                    if not stripped.startswith("data:"):
                        continue
                    data = stripped[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        assembler.feed(json.loads(data))
                    except json.JSONDecodeError:
                        continue
            except (OSError, http.client.HTTPException):
                # A read from the upstream failed mid-stream (drop,
                # IncompleteRead). The client keeps the events already
                # forwarded and sees the truncation for what it is.
                upstream_died = True

            message = assembler.finish()
            if not aborted and not upstream_died and assembler.complete:
                self._store(key, raw_hash, fp, message, raw, upstream)
            # Record before the terminal chunk: once the client sees the end of
            # the stream it may immediately ask for stats, and accounting that
            # lags the response looks like lost requests.
            # 529 is Anthropic's overloaded code: an error carried inside a 200
            # stream is that same failure, and logging the wire status (200)
            # would hide it. 499 marks a client abort as upstream spend that
            # still happened; 502 marks an upstream that died mid-answer.
            self._record(
                fp, payload, raw, started, "",
                499 if aborted
                else 502 if upstream_died
                else 529 if assembler.error
                else 200,
                message,
            )
            try:
                handler.wfile.write(b"0\r\n\r\n")
                handler.wfile.flush()
            except OSError:
                pass

    # ------------------------------------------------------------------ cache
    def _cache_get(self, key: str) -> dict[str, Any] | None:
        if not self.cache_enabled:
            return None
        hit = self.cache.get(key)
        if hit is None:
            return None
        try:
            message = json.loads(hit.content)
        except json.JSONDecodeError:
            return None
        return message

    def _store(
        self,
        key: str,
        raw_hash: str,
        fp: Any,
        message: dict[str, Any],
        raw: bytes,
        upstream: str,
    ) -> None:
        # The single gate every cached message passes through. ``usable`` is
        # the invariant, not a redundant check: a 200 with an error body or a
        # half-assembled stream must never become a replayable answer.
        if not self.cache_enabled or len(raw) > self.max_cache_bytes or not usable(message):
            return
        with self._lock:
            self._l1[(upstream, raw_hash)] = (key, fp)
            self._l1.move_to_end((upstream, raw_hash))
            # LRU eviction, not a wipe: clearing the whole map would drop the
            # raw attribution for every still-hot key at once.
            while len(self._l1) > 16384:
                self._l1.popitem(last=False)
        self.cache.put(key, LLMResponse(content=json.dumps(message, ensure_ascii=False)))

    # -------------------------------------------------------------- coalescing
    def _acquire(self, key: str) -> tuple[bool, threading.Event]:
        with self._lock:
            event = self._inflight.get(key)
            if event is None:
                event = threading.Event()
                self._inflight[key] = event
                return True, event
            return False, event

    def _release(self, key: str) -> None:
        with self._lock:
            event = self._inflight.pop(key, None)
        if event is not None:
            event.set()

    # ------------------------------------------------------------ failure memo
    def _maybe_memo(self, key: str, status: int, body: bytes) -> None:
        if not self.failure_ttl or status not in RETRYABLE:
            return
        now = time.time()
        with self._lock:
            # Expired entries are dropped on write as well as on lookup: a memo
            # whose key is never retried again must not outlive its TTL.
            stale = [k for k, (expiry, _, _) in self._failures.items() if expiry < now]
            for k in stale:
                del self._failures[k]
            # Bounded on top: a retry storm over unique keys must not grow this
            # dict for the life of the process. Wiping instead of pruning is
            # deliberate -- past 4096 the upstream is clearly unhealthy, and a
            # hot key re-learns its memo on the next upstream rejection.
            if len(self._failures) >= 4096:
                self._failures.clear()
            self._failures[key] = (now + self.failure_ttl, status, body)

    def _failure_get(self, key: str) -> tuple[int, bytes] | None:
        if not self.failure_ttl:
            return None
        with self._lock:
            entry = self._failures.get(key)
            if entry is None:
                return None
            expiry, status, body = entry
            if expiry < time.time():
                self._failures.pop(key, None)
                return None
            return status, body

    # ---------------------------------------------------------------- upstream
    def _open(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        suffix: str,
        upstream: str,
    ) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        forwarded = {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}
        forwarded["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{upstream}{suffix}", data=body, headers=forwarded, method="POST"
        )
        return urllib.request.urlopen(request, timeout=self.timeout)

    def _request(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        suffix: str,
        upstream: str,
    ) -> tuple[int, bytes]:
        with self._open(payload, headers, suffix, upstream) as response:
            return response.status, response.read()

    # ------------------------------------------------------------------ output
    def _replay(self, handler: Any, message: dict[str, Any], streaming: bool) -> None:
        clean = message
        if streaming:
            data = replay_sse(clean)
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
        else:
            data = json.dumps(clean, ensure_ascii=False).encode("utf-8")
            self._raw(handler, 200, data)

    def _raw(self, handler: Any, status: int, body: bytes) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _send_error(self, handler: Any, status: int, message: str) -> None:
        self._raw(
            handler,
            status,
            json.dumps({"error": {"type": "proxy_error", "message": message}}).encode(),
        )

    # --------------------------------------------------------------- recording
    def _record(
        self,
        fp: Any,
        payload: dict[str, Any],
        raw: bytes,
        started: float,
        hit_level: str,
        status: int,
        message: dict[str, Any] | None = None,
        *,
        billed: bool = True,
    ) -> None:
        usage = (message or {}).get("usage") or {}
        self.recorder.add(
            Event(
                ts=time.time(),
                key=fp.key,
                short=fp.short,
                tail=fp.tail,
                kind=fp.kind,
                model=str(payload.get("model") or ""),
                status=status,
                latency=round(time.time() - started, 3),
                est_tokens=max(1, len(raw) // 3),
                tokens_in=int(usage.get("input_tokens", 0) or 0) if billed else 0,
                tokens_out=int(usage.get("output_tokens", 0) or 0) if billed else 0,
                hit=hit_level,
                stream=bool(payload.get("stream")),
                n_messages=fp.n_messages,
                tools=tuple(tool_uses(message)) if message else (),
            )
        )

    def stats(self) -> dict[str, Any]:
        summary = self.recorder.summary()
        summary["upstream_url"] = self.upstream
        if len(self.listeners) > 1:
            summary["upstreams"] = {
                str(port): url for port, url in sorted(self.listeners.items())
            }
        summary["cache_entries"] = self.cache.size()
        summary["inflight"] = len(self._inflight)
        summary["memoized_failures"] = len(self._failures)
        return summary
