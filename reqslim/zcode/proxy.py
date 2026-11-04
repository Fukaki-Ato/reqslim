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

ZCode 3.17 plan accounts require HTTPS for client request signing. Listeners
can therefore be TLS-wrapped with the installer's loopback certificate; the
signing handshake (``/api/paas/...``) is forwarded to the gateway origin
without entering the cache.

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
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, NamedTuple
from urllib.parse import urlsplit

from .. import __version__
from ..cache import ExactCache
from ..types import LLMResponse
from .analytics import Event, Recorder
from .normalize import beta_tag, fingerprint, header_scope
from .stream import Assembler, replay_sse, tool_uses, usable
from .tls import make_context

HOP_BY_HOP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "http2-settings",
    "content-length",
    "accept-encoding",
    "expect",
}

#: Response headers we always own. Everything else from upstream is forwarded
#: so clients still see request ids, rate limits and Retry-After.
OWNED_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "server",
    "date",
}

RETRYABLE = {429, 500, 502, 503, 504}
MESSAGE_SUFFIXES = (
    "/v1/messages/count_tokens",
    "/messages/count_tokens",
    "/v1/messages",
    "/messages",
)


def message_suffix(path: str) -> str | None:
    """Return the upstream path for a supported Anthropic endpoint."""
    route = path.split("?", 1)[0].rstrip("/")
    for suffix in MESSAGE_SUFFIXES:
        if route.endswith(suffix):
            return suffix
    return None


def _is_sse(content_type: str | None) -> bool:
    if not content_type:
        return True
    return content_type.lower().split(";", 1)[0].strip() == "text/event-stream"


def _message_bytes(message: Any) -> int:
    if message is None:
        return 0
    try:
        return len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never turn a POST into a GET behind the client's back.

    A provider that returns 301/302 is passed through to the client instead of
    silently losing the request body.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class _Handoff(NamedTuple):
    expiry: float
    generation: int
    status: int
    body: bytes
    headers: dict[str, str]
    message: Any


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
        coalesce_wait: float = 600.0,
        failure_ttl: float = 20.0,
        record_path: str | None = None,
        timeout: float = 300.0,
        max_body: int = 64 * 1024 * 1024,
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self.cache = ExactCache(maxsize=max_cache, ttl=ttl, max_bytes=cache_bytes)
        self.cache_enabled = cache_enabled
        self.coalesce = coalesce
        self.coalesce_wait = coalesce_wait
        self.failure_ttl = failure_ttl
        self.max_cache_bytes = max_cache_bytes
        self.timeout = timeout
        self.max_body = max_body
        self.recorder = Recorder(record_path)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._l1: OrderedDict[tuple[str, str, str, str, str], tuple[str, Any]] = OrderedDict()
        self._inflight: dict[str, threading.Event] = {}
        #: key -> (expiry, status, body, headers)
        self._failures: dict[str, tuple[float, int, bytes, dict[str, str]]] = {}
        #: Short-lived leader result for followers that waited, so a response
        #: too large to cache still serves the whole single-flight group.
        self._handoff: OrderedDict[str, _Handoff] = OrderedDict()
        self._handoff_generation = 0
        self._handoff_bytes = 0
        self._handoff_limit = 32 * 1024 * 1024
        self._servers: list[ThreadingHTTPServer] = []
        self.shutdown_event = threading.Event()
        #: port -> upstream URL, filled in by serve/serve_many
        self.listeners: dict[int, str] = {}
        self.tls_enabled = False
        self.ready = True
        self.managed_shutdown = False
        self.record_path = os.path.abspath(record_path) if record_path else None
        self.watchers: dict[str, Any] | None = None

    def runtime_settings(self) -> dict[str, Any]:
        return {
            "ttl": self.cache.ttl,
            "max_cache": self.cache.maxsize,
            "cache": self.cache_enabled,
            "coalesce": self.coalesce,
            "coalesce_wait": self.coalesce_wait,
            "failure_ttl": self.failure_ttl,
            "record": self.record_path,
            "watchers": self.watchers,
        }

    # ------------------------------------------------------------------ server
    def serve(
        self,
        host: str = "127.0.0.1",
        port: int = 8788,
        *,
        tls: tuple[str, str] | None = None,
    ) -> ThreadingHTTPServer:
        """Single-upstream convenience wrapper around :meth:`serve_many`."""
        return self.serve_many(host, [(port, self.upstream)], tls=tls)[0]

    def serve_many(
        self,
        host: str,
        listeners: list[tuple[int, str]],
        *,
        tls: tuple[str, str] | None = None,
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
            server_version = f"reqslim-zcode/{__version__}"

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def handle(self) -> None:
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, ssl.SSLError):
                    self.close_connection = True

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
                            "ready": proxy.ready,
                            "version": __version__,
                            "pid": os.getpid(),
                            "upstream": proxy.upstream,
                            "upstreams": {
                                str(port): url
                                for port, url in sorted(proxy.listeners.items())
                            },
                            "cache": proxy.cache_enabled,
                            "coalesce": proxy.coalesce,
                            "tls": proxy.tls_enabled,
                            "settings": proxy.runtime_settings(),
                        },
                    )
                elif path.startswith("/api/"):
                    proxy.pass_through(
                        self, "GET", self.path, b"", dict(self.headers.items())
                    )
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = self.path.split("?")[0].rstrip("/")
                if path in {"/v1/shutdown", "/shutdown"}:
                    if self.client_address[0] not in {"127.0.0.1", "::1"}:
                        self._json(403, {"error": "shutdown is loopback-only"})
                        return
                    self._json(200, {"ok": True, "stopping": True})
                    proxy.request_shutdown()
                    return

                is_message = message_suffix(path) is not None
                if not is_message and not path.startswith("/api/"):
                    # Do not read a body we are not going to use. Closing is
                    # safe and avoids buffering an arbitrary upload.
                    self.close_connection = True
                    self._json(404, {"error": f"unsupported path {self.path}"})
                    return
                raw = self._read_body()
                if raw is None:
                    return
                if not is_message:
                    # ZCode's client-signing handshake lives at
                    # /api/paas/.../client on the provider origin. It must
                    # reach the real gateway unchanged; it is not a model
                    # request and must not enter the cache.
                    proxy.pass_through(
                        self, "POST", self.path, raw, dict(self.headers.items())
                    )
                    return
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._json(400, {"error": "invalid json"})
                    return
                if not isinstance(payload, dict):
                    self._json(400, {"error": "request body must be a JSON object"})
                    return
                try:
                    proxy.handle(self, payload, raw, dict(self.headers.items()), self.path)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            def _read_body(self) -> bytes | None:
                """Read a bounded request body, or send an error and return None."""
                transfer = (self.headers.get("Transfer-Encoding") or "").lower()
                if "chunked" in transfer:
                    try:
                        return self._read_chunked()
                    except OverflowError:
                        self.close_connection = True
                        self._json(413, {"error": f"request body too large (limit {proxy.max_body} bytes)"})
                        return None
                    except ValueError:
                        self.close_connection = True
                        self._json(400, {"error": "invalid chunked request body"})
                        return None
                length_header = self.headers.get("Content-Length")
                if length_header is None:
                    return b""
                try:
                    length = int(length_header)
                except ValueError:
                    self.close_connection = True
                    self._json(400, {"error": "invalid content-length"})
                    return None
                if length < 0:
                    self.close_connection = True
                    self._json(400, {"error": "invalid content-length"})
                    return None
                if length > proxy.max_body:
                    # Refuse before reading: a runaway body must not hold a
                    # reader thread hostage. The connection is closed because
                    # an unread body would poison the next request on it.
                    self.close_connection = True
                    self._json(413, {"error": f"request body too large ({length} bytes)"})
                    return None
                raw = self.rfile.read(length)
                if len(raw) != length:
                    self.close_connection = True
                    self._json(400, {"error": "incomplete request body"})
                    return None
                return raw

            def _read_chunked(self) -> bytes:
                """Decode a chunked request body, bounded by max_body."""
                chunks: list[bytes] = []
                total = 0
                while True:
                    line = self.rfile.readline(65537)
                    if not line:
                        raise ValueError("truncated chunk header")
                    size_text = line.split(b";", 1)[0].strip()
                    try:
                        size = int(size_text, 16)
                    except ValueError:
                        raise ValueError("invalid chunk size")
                    if size < 0:
                        raise ValueError("negative chunk size")
                    if size == 0:
                        while True:
                            trailer = self.rfile.readline(65537)
                            if trailer in (b"\r\n", b"\n", b""):
                                break
                        return b"".join(chunks)
                    total += size
                    if total > proxy.max_body:
                        raise OverflowError
                    chunk = self.rfile.read(size)
                    if len(chunk) != size:
                        raise ValueError("truncated chunk")
                    chunks.append(chunk)
                    if self.rfile.read(2) != b"\r\n":
                        raise ValueError("missing chunk terminator")

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

            def finish_request(self, request: Any, client_address: Any) -> None:
                if context is None:
                    super().finish_request(request, client_address)
                    return
                try:
                    request.settimeout(min(proxy.timeout, 10.0))
                    wrapped = context.wrap_socket(request, server_side=True)
                except (OSError, ssl.SSLError):
                    request.close()
                    return
                try:
                    wrapped.settimeout(proxy.timeout)
                    super().finish_request(wrapped, client_address)
                finally:
                    self.shutdown_request(wrapped)

        context = make_context(*tls) if tls else None
        self.tls_enabled = context is not None
        servers: list[ThreadingHTTPServer] = []
        for port, upstream in listeners:
            server: ThreadingHTTPServer | None = None
            try:
                server = Server((host, port), Handler)
            except OSError as exc:
                # A half-bound set would leave the caller with a proxy that
                # answers on some ports and not others, which is harder to
                # diagnose than a clean refusal.
                if server is not None:
                    try:
                        server.server_close()
                    except Exception:  # noqa: BLE001
                        pass
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
                target=server.serve_forever, daemon=True,
                name=f"reqslim-zcode:{server.server_address[1]}"
            ).start()
            servers.append(server)
        # Key by the actually-bound port: with port=0 the OS assigns and the
        # requested port is useless as a dict key.
        self.listeners = {
            server.server_address[1]: server.upstream  # type: ignore[attr-defined]
            for server in servers
        }
        self._servers = servers
        return servers

    def request_shutdown(self) -> None:
        """Ask the owning process loop to close every listener."""
        self.shutdown_event.set()
        # Also close without relying on a main loop. /v1/shutdown is used by
        # the uninstaller and by tests that embed the proxy directly.
        if not self.managed_shutdown:
            threading.Thread(target=self._delayed_close, daemon=True).start()

    def _delayed_close(self) -> None:
        time.sleep(0.05)
        self.close()

    def close(self) -> None:
        with self._close_lock:
            with self._lock:
                servers, self._servers = self._servers, []
            for server in servers:
                try:
                    server.shutdown()
                except Exception:
                    pass
                finally:
                    server.server_close()

    # ------------------------------------------------------------------- core
    def handle(
        self,
        handler: Any,
        payload: dict[str, Any],
        raw: bytes,
        headers: dict[str, str],
        path: str = "/v1/messages",
    ) -> None:
        if not isinstance(payload, dict):
            self._send_error(handler, 400, "request body must be a JSON object")
            return
        with self._lock:
            observed_generation = self._handoff_generation
        started = time.time()
        upstream = getattr(handler.server, "upstream", None) or self.upstream
        streaming = bool(payload.get("stream"))
        route, separator, query = path.partition("?")
        suffix = message_suffix(route) or "/v1/messages"
        if separator:
            suffix += "?" + query
        raw_hash = hashlib.sha256(raw).hexdigest()
        beta = beta_tag(headers)
        scope = header_scope(headers)

        # Cache scopes are per upstream, beta switch and credential. The same
        # logical request to two gateways or two accounts is not the same
        # conversation as far as replay is concerned.
        l1_key = (upstream, suffix, beta, scope, raw_hash)
        with self._lock:
            known = self._l1.get(l1_key)
            if known is not None:
                # A repeat is exactly the entry worth keeping under memory
                # pressure; eviction below must see it as recently used.
                self._l1.move_to_end(l1_key)
        # A raw-hash hit means these exact bytes were answered before, so the
        # saving belongs to the cheapest mechanism. Reporting it as
        # "normalized" would hide whether the raw path ever fires.
        level = "normalized" if known is None else "raw"
        if known is None:
            try:
                fp = fingerprint(payload, beta, upstream, scope, endpoint=suffix)
            except (TypeError, ValueError, OverflowError) as exc:
                self._send_error(handler, 400, str(exc))
                return
            key = f"{upstream}|{suffix}|{beta}|{scope}|{fp.key}"
        else:
            key, fp = known

        def replay_handoff(entry: tuple[int, bytes, dict[str, str], Any]) -> None:
            status, body, hand_headers, message = entry
            self._record(fp, payload, raw, started, "coalesced", status, message, billed=False)
            if message is not None and status == 200:
                self._replay(handler, message, streaming)
            else:
                self._raw(handler, status, body, headers=hand_headers)

        is_leader = False
        # Follower budget: a deadline, not a retry count. The old fixed
        # 3x180s starved coalescing exactly where it matters -- slow long
        # streams (the socket timeout bounds per-read gaps, not total
        # duration, so a drip stream can legitimately run far longer).
        # Past the deadline a follower forwards concurrently: progress wins
        # over deduplication.
        deadline = time.monotonic() + self.coalesce_wait if self.coalesce_wait > 0 else None
        waited = False
        while True:
            hit = self._cache_get(key)
            if hit is not None:
                # ``billed=False``: the tokens were paid for when this answer
                # was first fetched. Counting them again would make the report
                # show more input tokens than the upstream ever saw.
                shown_level = "coalesced" if waited else level
                self._record(fp, payload, raw, started, shown_level, 200, hit, billed=False)
                self._replay(handler, hit, streaming)
                return

            memo = self._failure_get(key)
            if memo is not None:
                status, body, memo_headers = memo
                self._record(fp, payload, raw, started, "failure_memo", status)
                self._raw(handler, status, body, headers=memo_headers)
                return

            if self.coalesce:
                handed = self._handoff_get(key, after_generation=observed_generation)
                if handed is not None:
                    replay_handoff(handed)
                    return

            if not self.coalesce:
                break
            leader, event = self._acquire(key)
            if leader:
                is_leader = True
                break
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            event.wait(timeout=30 if remaining is None else min(30.0, remaining))
            waited = True
            # leader finished (or the 30s tick): loop and re-check the result

        try:
            if is_leader:
                hit = self._cache_get(key)
                if hit is not None:
                    self._record(fp, payload, raw, started, "coalesced", 200, hit, billed=False)
                    self._replay(handler, hit, streaming)
                    return
                memo = self._failure_get(key)
                if memo is not None:
                    status, body, memo_headers = memo
                    self._record(fp, payload, raw, started, "failure_memo", status)
                    self._raw(handler, status, body, headers=memo_headers)
                    return
                handed = self._handoff_get(key, after_generation=observed_generation)
                if handed is not None:
                    replay_handoff(handed)
                    return
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
            status, body, resp_headers = self._request(payload, headers, suffix, upstream)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            resp_headers = dict(exc.headers.items()) if exc.headers else {}
            self._maybe_memo(key, exc.code, body, resp_headers)
            self._record(fp, payload, raw, started, "", exc.code)
            self._publish(key, exc.code, body, resp_headers, None)
            self._raw(handler, exc.code, body, headers=resp_headers)
            return
        except Exception as exc:  # noqa: BLE001
            self._record(fp, payload, raw, started, "", 502)
            self._send_error(handler, 502, str(exc))
            return

        if status != 200:
            self._maybe_memo(key, status, body, resp_headers)
            self._record(fp, payload, raw, started, "", status)
            self._publish(key, status, body, resp_headers, None)
            self._raw(handler, status, body, headers=resp_headers)
            return

        try:
            message = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A 200 that is not JSON -- a gateway HTML page, a WAF block, an
            # empty body. Forward it untouched with the upstream content type:
            # the client can still show the real error, and nothing is cached
            # under a key it does not own.
            self._record(fp, payload, raw, started, "", status)
            self._publish(key, status, body, resp_headers, None)
            self._raw(handler, status, body, headers=resp_headers)
            return

        self._store(key, raw_hash, fp, message, raw, upstream)
        self._record(fp, payload, raw, started, "", 200, message)
        self._publish(key, 200, body, resp_headers, message if usable(message) else None)
        self._raw(handler, 200, body, headers=resp_headers)

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
            resp_headers = dict(exc.headers.items()) if exc.headers else {}
            self._maybe_memo(key, exc.code, body, resp_headers)
            self._record(fp, payload, raw, started, "", exc.code)
            self._publish(key, exc.code, body, resp_headers, None)
            self._raw(handler, exc.code, body, headers=resp_headers)
            return
        except Exception as exc:  # noqa: BLE001
            self._send_error(handler, 502, str(exc))
            self._record(fp, payload, raw, started, "", 502)
            return

        with upstream_conn:
            resp_headers = dict(upstream_conn.headers.items())
            if upstream_conn.status != 200:
                body = upstream_conn.read()
                self._maybe_memo(key, upstream_conn.status, body, resp_headers)
                self._raw(handler, upstream_conn.status, body, headers=resp_headers)
                self._record(fp, payload, raw, started, "", upstream_conn.status)
                self._publish(key, upstream_conn.status, body, resp_headers, None)
                return

            content_type = upstream_conn.headers.get("Content-Type")
            if not _is_sse(content_type):
                # A stream request answered with JSON/HTML is not an SSE
                # success. Forward it as a controlled proxy error instead of
                # inventing an empty event stream and recording a 200.
                body = upstream_conn.read()
                if not body:
                    body = json.dumps(
                        {"error": {"type": "proxy_error", "message": "upstream returned an empty non-SSE body"}}
                    ).encode("utf-8")
                self._record(fp, payload, raw, started, "", 502)
                self._publish(key, 502, body, resp_headers, None)
                self._raw(handler, 502, body, content_type=content_type, headers=resp_headers)
                return

            try:
                handler.send_response(200)
                self._send_stream_headers(handler, resp_headers)
                handler.end_headers()
            except OSError:
                # The client vanished before the response even started; there
                # is nothing to forward to and nothing to replay.
                self._record(fp, payload, raw, started, "", 499)
                return

            assembler = Assembler()
            aborted = False       # client hung up mid-stream
            upstream_died = False  # upstream connection broke mid-stream
            data_lines: list[str] = []

            def feed_event() -> None:
                data = "\n".join(data_lines)
                data_lines.clear()
                if not data.strip() or data.strip() == "[DONE]":
                    return
                try:
                    assembler.feed(json.loads(data))
                except json.JSONDecodeError:
                    assembler.malformed = True

            try:
                for line in upstream_conn:
                    try:
                        handler.write_chunk(line)
                    except OSError:
                        # Client-side writes are wrapped tight so an upstream
                        # read failure is attributed to the right side.
                        aborted = True
                        break
                    stripped = line.decode("utf-8", "replace").rstrip("\r\n")
                    if not stripped:
                        feed_event()
                    elif stripped.startswith("data:"):
                        value = stripped[5:]
                        data_lines.append(value[1:] if value.startswith(" ") else value)
                if data_lines and not aborted:
                    feed_event()
            except (OSError, http.client.HTTPException):
                # A read from the upstream failed mid-stream (drop,
                # IncompleteRead). The client gets an explicit SSE error so a
                # partial answer cannot masquerade as a completed stream.
                upstream_died = True

            message = assembler.finish()
            if not aborted and not upstream_died and assembler.complete:
                self._store(key, raw_hash, fp, message, raw, upstream)
                if usable(message):
                    self._publish(key, 200, b"", resp_headers, message)
            # Record before the terminal chunk: once the client sees the end of
            # the stream it may immediately ask for stats, and accounting that
            # lags the response looks like lost requests.
            # 529 is Anthropic's overloaded code: an error carried inside a 200
            # stream is that same failure, and logging the wire status (200)
            # would hide it. 499 marks a client abort as upstream spend that
            # still happened; 502 marks a stream that never became a message.
            status = (
                499 if aborted
                else 502 if upstream_died
                else 529 if assembler.error
                else 200 if assembler.complete
                else 502
            )
            self._record(
                fp, payload, raw, started, "",
                status,
                message if status == 200 else None,
            )
            if not aborted and status != 200 and not assembler.error:
                try:
                    handler.write_chunk(
                        b'event: error\n'
                        b'data: {"type":"error","error":{"type":"proxy_error",'
                        b'"message":"upstream stream ended before completion"}}\n\n'
                    )
                except OSError:
                    aborted = True
            if not aborted:
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
        message: Any,
        raw: bytes,
        upstream: str,
    ) -> bool:
        # The single gate every cached message passes through. ``usable`` is
        # the invariant, not a redundant check: a 200 with an error body or a
        # half-assembled stream must never become a replayable answer.
        if not self.cache_enabled or len(raw) > self.max_cache_bytes or not usable(message):
            return False
        stored = self.cache.put(key, LLMResponse(content=json.dumps(message, ensure_ascii=False)))
        if not stored:
            return False
        l1_key = (upstream, fp.endpoint or "/v1/messages", fp.beta, fp.scope, raw_hash)
        with self._lock:
            self._l1[l1_key] = (key, fp)
            self._l1.move_to_end(l1_key)
            # LRU eviction, not a wipe: clearing the whole map would drop the
            # raw attribution for every still-hot key at once.
            while len(self._l1) > 16384:
                self._l1.popitem(last=False)
        return True

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

    # ----------------------------------------------------------------- handoff
    def _handoff_get(
        self, key: str, *, after_generation: int | None = None,
    ) -> tuple[int, bytes, dict[str, str], Any] | None:
        now = time.monotonic()
        with self._lock:
            entry = self._handoff.get(key)
            if entry is None:
                return None
            if entry.expiry < now:
                self._handoff.pop(key, None)
                self._handoff_bytes -= len(entry.body) + _message_bytes(entry.message)
                return None
            if after_generation is not None and entry.generation <= after_generation:
                return None
            return entry.status, entry.body, entry.headers, entry.message

    def _handoff_put(
        self,
        key: str,
        status: int,
        body: bytes,
        headers: dict[str, str],
        message: Any,
    ) -> None:
        if not self.coalesce:
            return
        size = len(body) + _message_bytes(message)
        if size > self._handoff_limit:
            return
        with self._lock:
            old = self._handoff.pop(key, None)
            if old is not None:
                self._handoff_bytes -= len(old.body) + _message_bytes(old.message)
            self._handoff_generation += 1
            entry = _Handoff(time.monotonic() + 30.0, self._handoff_generation,
                             status, body, dict(headers), message)
            self._handoff[key] = entry
            self._handoff_bytes += size
            while self._handoff and (
                len(self._handoff) > 128 or self._handoff_bytes > self._handoff_limit
            ):
                _, dropped = self._handoff.popitem(last=False)
                self._handoff_bytes -= len(dropped.body) + _message_bytes(dropped.message)

    def _publish(
        self,
        key: str,
        status: int,
        body: bytes,
        headers: dict[str, str],
        message: Any,
    ) -> None:
        self._handoff_put(key, status, body, headers, message)

    # ------------------------------------------------------------ failure memo
    def _maybe_memo(
        self,
        key: str,
        status: int,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not self.failure_ttl or status not in RETRYABLE:
            return
        now = time.time()
        with self._lock:
            # Expired entries are dropped on write as well as on lookup: a memo
            # whose key is never retried again must not outlive its TTL.
            stale = [k for k, entry in self._failures.items() if entry[0] < now]
            for k in stale:
                del self._failures[k]
            # Bounded on top: a retry storm over unique keys must not grow this
            # dict for the life of the process. Wiping instead of pruning is
            # deliberate -- past 4096 the upstream is clearly unhealthy, and a
            # hot key re-learns its memo on the next upstream rejection.
            if len(self._failures) >= 4096:
                self._failures.clear()
            self._failures[key] = (now + self.failure_ttl, status, body, dict(headers or {}))

    def _failure_get(self, key: str) -> tuple[int, bytes, dict[str, str]] | None:
        if not self.failure_ttl:
            return None
        with self._lock:
            entry = self._failures.get(key)
            if entry is None:
                return None
            expiry, status, body, headers = entry
            if expiry < time.time():
                self._failures.pop(key, None)
                return None
            return status, body, headers

    # ---------------------------------------------------------------- upstream
    def pass_through(
        self,
        handler: Any,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        """Forward a non-model request to the provider origin.

        ZCode's client-signing handshake is a POST to
        ``/api/paas/.../client`` on the same origin as the provider base URL.
        It must see the real gateway and the original ``Authorization``; the
        proxy only changes the transport from loopback TLS to the upstream.
        """
        upstream = getattr(handler.server, "upstream", None) or self.upstream
        parts = urlsplit(upstream)
        url = f"{parts.scheme}://{parts.netloc}{path}"
        forwarded = {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}
        data = body if method not in {"GET", "HEAD"} else None
        request = urllib.request.Request(url, data=data, headers=forwarded, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                response_body = response.read()
                response_headers = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read()
            response_headers = dict(exc.headers.items()) if exc.headers else {}
        except Exception as exc:  # noqa: BLE001
            self._send_error(handler, 502, str(exc))
            return
        self._raw(handler, status, response_body, headers=response_headers)

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
        return self._opener.open(request, timeout=self.timeout)

    def _request(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        suffix: str,
        upstream: str,
    ) -> tuple[int, bytes, dict[str, str]]:
        with self._open(payload, headers, suffix, upstream) as response:
            return (
                response.status,
                response.read(),
                dict(response.headers.items()),
            )

    # ------------------------------------------------------------------ output
    def _send_stream_headers(self, handler: Any, headers: dict[str, str] | None = None) -> None:
        for name, value in (headers or {}).items():
            if name.lower() in OWNED_RESPONSE_HEADERS or name.lower() == "content-type":
                continue
            try:
                handler.send_header(name, value)
            except (ValueError, OSError):
                continue
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.send_header("Transfer-Encoding", "chunked")

    def _replay(self, handler: Any, message: Any, streaming: bool) -> None:
        clean = message if isinstance(message, Mapping) else {}
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

    def _raw(
        self,
        handler: Any,
        status: int,
        body: bytes,
        *,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        handler.send_response(status)
        upstream_type = None
        for name, value in (headers or {}).items():
            if name.lower() == "content-type":
                upstream_type = value
                continue
            if name.lower() in OWNED_RESPONSE_HEADERS:
                continue
            try:
                handler.send_header(name, value)
            except (ValueError, OSError):
                continue
        handler.send_header(
            "Content-Type",
            content_type or upstream_type or "application/json; charset=utf-8",
        )
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
        message: Any = None,
        *,
        billed: bool = True,
    ) -> None:
        if not isinstance(message, Mapping):
            message = {}
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            usage = {}

        def _tokens(name: str) -> int:
            try:
                return int(usage.get(name, 0) or 0)
            except (TypeError, ValueError, OverflowError):
                return 0

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
                tokens_in=_tokens("input_tokens") if billed else 0,
                tokens_out=_tokens("output_tokens") if billed else 0,
                hit=hit_level,
                stream=bool(payload.get("stream")),
                n_messages=fp.n_messages,
                tools=tuple(tool_uses(message)),
                upstream=fp.upstream,
                scope=fp.scope,
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
        summary["handoff_entries"] = len(self._handoff)
        return summary
