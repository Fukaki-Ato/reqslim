"""ZCode adaptation tests: normalisation, thinking-block fidelity, coalescing.

    python tests/test_zcode.py
"""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import ssl
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reqslim.cache import ExactCache
from reqslim.types import LLMResponse
from reqslim.zcode import (
    Assembler,
    Recorder,
    RequestComparison,
    ZCodeProxy,
    classify,
    compare_requests,
    fingerprint,
    normalize_request,
    replay_sse,
)
from reqslim.zcode.analytics import Event, load_events
from reqslim.zcode.__main__ import (
    CacheWatcher,
    health_on_port,
    load_autostart_config,
    main as zcode_main,
    reqslim_on_port,
    stop_running_proxy,
    TableWatcher,
)
from reqslim.zcode.tls import _openssl, ensure_tls
from reqslim.zcode.wiring import (
    generate_proxied_table,
    restore_runtime_caches,
    rewrite_runtime_cache_file,
    rewrite_runtime_caches,
)

TEXT = "条款抽取完成"

#: What Anthropic actually sends when the backend is overloaded mid-stream:
#: HTTP 200, a message_start, then an ``error`` event. A naive assembler turns
#: this into an empty message, which must never become a cached answer.
OVERLOADED_SSE = (
    "event: message_start\n"
    'data: {"type":"message_start","message":{"id":"msg_x","type":"message",'
    '"role":"assistant","model":"glm-5.3","content":[],"stop_reason":null,'
    '"usage":{"input_tokens":10,"output_tokens":0}}}\n\n'
    "event: error\n"
    'data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'
).encode()

#: A stream that died after its content but before any terminator -- a dropped
#: connection, a killed gateway. The text looks complete; the message is not.
TRUNCATED_STREAM_SSE = (
    "event: message_start\n"
    'data: {"type":"message_start","message":{"id":"msg_z","type":"message",'
    '"role":"assistant","model":"glm-5.3","content":[],"stop_reason":null,'
    '"usage":{"input_tokens":5,"output_tokens":0}}}\n\n'
    "event: content_block_start\n"
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    "event: content_block_delta\n"
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"半句话"}}\n\n'
    "event: content_block_stop\n"
    'data: {"type":"content_block_stop","index":0}\n\n'
).encode()

#: A tool call whose ``partial_json`` was cut off mid-string.
TRUNCATED_TOOL_SSE = (
    "event: message_start\n"
    'data: {"type":"message_start","message":{"id":"msg_y","type":"message",'
    '"role":"assistant","model":"glm-5.3","content":[],"stop_reason":null,'
    '"usage":{"input_tokens":5,"output_tokens":0}}}\n\n'
    "event: content_block_start\n"
    'data: {"type":"content_block_start","index":0,"content_block":'
    '{"type":"tool_use","id":"toolu_1","name":"read_file","input":{}}}\n\n'
    "event: content_block_delta\n"
    'data: {"type":"content_block_delta","index":0,"delta":'
    '{"type":"input_json_delta","partial_json":"{\\"path\\": \\"a.tx"}}\n\n'
    "event: content_block_stop\n"
    'data: {"type":"content_block_stop","index":0}\n\n'
    "event: message_delta\n"
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},'
    '"usage":{"output_tokens":9}}\n\n'
    "event: message_stop\n"
    'data: {"type":"message_stop"}\n\n'
).encode()

THINKING_MESSAGE = {
    "id": "msg_fake",
    "type": "message",
    "role": "assistant",
    "model": "glm-5.3",
    "content": [
        {"type": "thinking", "thinking": "先读合同再比对", "signature": "sig-abc123"},
        {"type": "text", "text": TEXT},
    ],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 20, "output_tokens": 10},
}


def _sse(message: dict) -> bytes:
    head = {**message, "content": [], "stop_reason": None,
            "usage": {"input_tokens": 20, "output_tokens": 0}}
    events: list[tuple[str, dict]] = [("message_start", {"type": "message_start", "message": head})]
    for index, block in enumerate(message["content"]):
        if block["type"] == "thinking":
            events.append(("content_block_start", {"type": "content_block_start", "index": index,
                                                   "content_block": {"type": "thinking", "thinking": ""}}))
            events.append(("content_block_delta", {"type": "content_block_delta", "index": index,
                                                   "delta": {"type": "thinking_delta", "thinking": block["thinking"]}}))
            events.append(("content_block_delta", {"type": "content_block_delta", "index": index,
                                                   "delta": {"type": "signature_delta", "signature": block["signature"]}}))
        else:
            events.append(("content_block_start", {"type": "content_block_start", "index": index,
                                                   "content_block": {"type": "text", "text": ""}}))
            events.append(("content_block_delta", {"type": "content_block_delta", "index": index,
                                                   "delta": {"type": "text_delta", "text": block["text"]}}))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    events.append(("message_delta", {"type": "message_delta",
                                     "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                     "usage": {"output_tokens": 10}}))
    events.append(("message_stop", {"type": "message_stop"}))
    return "".join(f"event: {n}\ndata: {json.dumps(p, ensure_ascii=False)}\n\n" for n, p in events).encode()


class FakeUpstream:
    def __init__(self, delay: float = 0.0, fail_status: int | None = None, message: dict | None = None,
                 raw_body: bytes | None = None, content_type: str = "text/event-stream"):
        self.calls = 0
        self.bodies: list[dict] = []
        self.delay = delay
        self.fail_status = fail_status
        self.message = message or THINKING_MESSAGE
        self.raw_body = raw_body
        self.content_type = content_type
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                outer.calls += 1
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                outer.bodies.append(payload)
                if self.path.split("?", 1)[0].endswith("/count_tokens"):
                    body = b'{"input_tokens":42}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if outer.delay:
                    time.sleep(outer.delay)
                if outer.fail_status:
                    body = json.dumps({"error": {"type": "overloaded"}}).encode()
                    self.send_response(outer.fail_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if outer.raw_body is not None:
                    self.send_response(200)
                    self.send_header("Content-Type", outer.content_type)
                    self.send_header("Content-Length", str(len(outer.raw_body)))
                    self.end_headers()
                    self.wfile.write(outer.raw_body)
                    return
                body = (
                    _sse(outer.message)
                    if payload.get("stream")
                    else json.dumps(outer.message, ensure_ascii=False).encode()
                )
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/event-stream" if payload.get("stream") else "application/json",
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class TrickleUpstream:
    """Sends an SSE response in two halves with a gap between them, so the
    proxy is provably still mid-stream when a client disconnects (a
    single-shot upstream would finish before the client could hang up)."""

    def __init__(self, sse: bytes, gap: float = 0.5) -> None:
        self.sse = sse
        self.gap = gap
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                half = len(outer.sse) // 2
                try:
                    for part in (outer.sse[:half], outer.sse[half:]):
                        self.wfile.write(f"{len(part):X}\r\n".encode() + part + b"\r\n")
                        self.wfile.flush()
                        time.sleep(outer.gap)
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def post(port: int, payload: dict) -> bytes:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def post_raw(
    port: int,
    path: str,
    body: bytes,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    merged = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    merged.update(headers or {})
    conn.request("POST", path, body=body, headers=merged)
    response = conn.getresponse()
    data = response.read()
    result = (response.status, dict(response.getheaders()), data)
    conn.close()
    return result


def _payload(text: str = "审查 doc-01", stream: bool = False, cache_control: int = 0,
             tool_id: str = "toolu_a", shuffle_tools: bool = False) -> dict:
    tools = [
        {"name": "read_file", "description": "read", "input_schema": {"type": "object"}},
        {"name": "bash", "description": "shell", "input_schema": {"type": "object"}},
    ]
    if shuffle_tools:
        tools.reverse()
    messages = [
        {"role": "user", "content": [{"type": "text", "text": text}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_id, "name": "read_file", "input": {"path": "a.txt"}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "文件内容"}],
        },
    ]
    if cache_control == 1:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
        messages[0]["content"][0]["cache_control"] = {"type": "ephemeral"}
    elif cache_control == 2:
        tools[0]["cache_control"] = {"type": "ephemeral"}
        messages[1]["content"][0]["cache_control"] = {"type": "ephemeral"}
    return {
        "model": "glm-5.3",
        "max_tokens": 4096,
        "stream": stream,
        "system": [{"type": "text", "text": "you are zcode"}],
        "tools": tools,
        "messages": messages,
        "metadata": {"user_id": "u1"},
    }


# ------------------------------------------------------------------ normalise
def test_cache_control_churn_does_not_change_the_key():
    a = fingerprint(_payload(cache_control=1))
    b = fingerprint(_payload(cache_control=2))
    assert a.key == b.key, "cache_control placement must not affect the cache key"


def test_tool_ids_do_not_change_the_key():
    a = fingerprint(_payload(tool_id="toolu_first"))
    b = fingerprint(_payload(tool_id="toolu_second"))
    assert a.key == b.key


def test_tool_order_does_not_change_the_key():
    assert fingerprint(_payload()).key == fingerprint(_payload(shuffle_tools=True)).key


def test_stream_and_metadata_do_not_change_the_key():
    assert fingerprint(_payload(stream=False)).key == fingerprint(_payload(stream=True)).key


def test_real_content_change_does_change_the_key():
    assert fingerprint(_payload("审查 doc-01")).key != fingerprint(_payload("审查 doc-02")).key


def test_normalize_preserves_the_original_payload():
    payload = _payload(cache_control=1)
    before = json.dumps(payload, sort_keys=True)
    normalize_request(payload)
    assert json.dumps(payload, sort_keys=True) == before, "normalisation must not mutate input"


def test_classify_shapes():
    assert classify(_payload()) == "tool_turn"
    first = _payload()
    first["messages"] = first["messages"][:1]
    assert classify(first) == "first_turn"
    compact = _payload()
    compact["system"] = [{"type": "text", "text": "Please summarize the conversation so far"}]
    assert classify(compact) == "compaction"


# -------------------------------------------------------------------- stream
def test_assembler_keeps_thinking_signature():
    assembler = Assembler()
    for line in _sse(THINKING_MESSAGE).decode().split("\n\n"):
        if not line.strip():
            continue
        data = line.split("data: ", 1)[1]
        assembler.feed(json.loads(data))
    message = assembler.finish()
    assert message["content"][0]["type"] == "thinking"
    assert message["content"][0]["signature"] == "sig-abc123"
    assert message["content"][1]["text"] == TEXT


def test_replay_roundtrip_preserves_blocks():
    first = Assembler()
    for line in _sse(THINKING_MESSAGE).decode().split("\n\n"):
        if line.strip():
            first.feed(json.loads(line.split("data: ", 1)[1]))
    original = first.finish()

    second = Assembler()
    for line in replay_sse(original).decode().split("\n\n"):
        if line.strip():
            second.feed(json.loads(line.split("data: ", 1)[1]))
    replayed = second.finish()

    assert replayed["content"] == original["content"], "replay must be lossless"
    assert replayed["stop_reason"] == "end_turn"


# --------------------------------------------------------------------- proxy
def test_proxy_hits_across_cache_control_churn():
    upstream = FakeUpstream()
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    try:
        port = server.server_address[1]
        post(port, _payload(cache_control=1))
        post(port, _payload(cache_control=2))
        assert upstream.calls == 1, f"expected 1 upstream call, got {upstream.calls}"
        stats = proxy.stats()
        assert stats["hits"]["normalized"] == 1
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_proxy_streaming_hit_keeps_thinking():
    upstream = FakeUpstream()
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    try:
        port = server.server_address[1]
        post(port, _payload(stream=True, tool_id="toolu_x"))
        body = post(port, _payload(stream=True, tool_id="toolu_y")).decode()
        assert "signature_delta" in body and "sig-abc123" in body, "thinking must survive replay"
        assert TEXT in body
        assert upstream.calls == 1
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_concurrent_identical_requests_are_coalesced():
    upstream = FakeUpstream(delay=0.4)
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    port = server.server_address[1]
    results: list[int] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            post(port, _payload(tool_id=f"toolu_{index}"))
            with lock:
                results.append(1)
        except Exception:
            with lock:
                results.append(0)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert len(results) == 6 and all(results)
        assert upstream.calls == 1, f"expected single-flight, got {upstream.calls} calls"
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_failure_memo_stops_a_retry_storm():
    upstream = FakeUpstream(fail_status=503)
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}", failure_ttl=30)
    server = proxy.serve(port=0)
    try:
        port = server.server_address[1]
        for _ in range(3):
            try:
                post(port, _payload())
            except urllib.error.HTTPError as exc:
                assert exc.code == 503
        assert upstream.calls == 1, f"expected memoised failure, got {upstream.calls} calls"
        assert proxy.stats()["hits"]["failure_memo"] == 2
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_failure_memo_relearns_hot_keys_after_reaching_the_bound():
    """Unique-key failure storms must not grow the memo without bound."""
    proxy = ZCodeProxy("http://127.0.0.1:9", failure_ttl=30)
    for i in range(6000):
        proxy._maybe_memo(f"key-{i}", 503, b"overloaded")
    assert len(proxy._failures) <= 4097, "memo must stay bounded"
    # the memo still works after a wipe: a retried key re-learns and replays
    proxy._maybe_memo("hot", 503, b"overloaded")
    assert proxy._failure_get("hot") is not None


def test_different_conversations_are_not_merged():
    upstream = FakeUpstream()
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    try:
        port = server.server_address[1]
        post(port, _payload("审查 doc-01"))
        post(port, _payload("审查 doc-02"))
        assert upstream.calls == 2
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


# ------------------------------------------------- upstream misbehaviour
def test_stream_error_is_not_cached_as_an_empty_answer():
    """A 200 carrying ``event: error`` is a failure, not an empty success.

    This is Anthropic's overload shape. Caching the assembled skeleton would
    answer the retry -- the exact request that follows an overload -- with a
    valid-looking message that has no content, for the whole TTL.
    """
    upstream = FakeUpstream(raw_body=OVERLOADED_SSE, content_type="text/event-stream")
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    try:
        port = server.server_address[1]
        first = post(port, _payload(stream=True)).decode()
        assert "overloaded_error" in first
        second = post(port, _payload(stream=True)).decode()
        assert "overloaded_error" in second, "the retry must reach the upstream, not a cached blank"
        assert upstream.calls == 2
        assert proxy.cache.size() == 0
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_non_json_200_is_passed_through_not_swallowed():
    html = b"<html><body>502 Bad Gateway from edge</body></html>"
    upstream = FakeUpstream(raw_body=html, content_type="text/html")
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    try:
        port = server.server_address[1]
        assert post(port, _payload()) == html, "the client must see what the upstream sent"
        assert proxy.cache.size() == 0
        post(port, _payload())
        assert upstream.calls == 2, "a non-message body must not be replayed"
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_truncated_tool_input_is_not_cached():
    upstream = FakeUpstream(raw_body=TRUNCATED_TOOL_SSE, content_type="text/event-stream")
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    try:
        port = server.server_address[1]
        first = post(port, _payload(stream=True)).decode()
        assert "_malformed_input" not in first, "diagnostics must not leak into the wire format"
        post(port, _payload(stream=True))
        assert upstream.calls == 2, "a half-parsed tool call is not a replayable answer"
        assert proxy.cache.size() == 0
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_message_without_type_is_still_cacheable():
    """Compatible gateways sometimes omit ``"type": "message"``.

    Treating that as "not a message" would silently switch the cache off for
    the whole provider, which is the project's entire purpose.
    """
    bare = {key: value for key, value in THINKING_MESSAGE.items() if key != "type"}
    upstream = FakeUpstream(message=bare)
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    try:
        port = server.server_address[1]
        post(port, _payload())
        post(port, _payload())
        assert upstream.calls == 1
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_truncated_stream_is_not_cached():
    """Content arrived, but the stream never terminated: not a finished answer."""
    upstream = FakeUpstream(raw_body=TRUNCATED_STREAM_SSE, content_type="text/event-stream")
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    try:
        port = server.server_address[1]
        post(port, _payload(stream=True))
        post(port, _payload(stream=True))
        assert upstream.calls == 2
        assert proxy.cache.size() == 0
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_cache_hit_does_not_double_count_tokens():
    upstream = FakeUpstream()
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    try:
        port = server.server_address[1]
        post(port, _payload())
        post(port, _payload())
        assert upstream.calls == 1
        tokens = proxy.stats()["tokens"]
        assert (tokens["in"], tokens["out"]) == (20, 10), (
            "tokens must report what the upstream billed, not what was replayed"
        )
        assert tokens["saved_estimate"] > 0
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_byte_identical_repeat_is_attributed_to_the_raw_hash():
    upstream = FakeUpstream()
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    try:
        port = server.server_address[1]
        post(port, _payload())
        post(port, _payload())
        assert proxy.stats()["hits"] == {"raw": 1}, (
            "an identical body is the cheapest mechanism and must be reported as such"
        )
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


# -------------------------------------------------------------- multi-upstream
def test_multi_listener_partitions_cache_by_upstream():
    upstream_a = FakeUpstream()
    upstream_b = FakeUpstream()
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream_a.port}")
    servers = proxy.serve_many(
        "127.0.0.1",
        [
            (0, f"http://127.0.0.1:{upstream_a.port}"),
            (0, f"http://127.0.0.1:{upstream_b.port}"),
        ],
    )
    try:
        port_a, port_b = (server.server_address[1] for server in servers)
        post(port_a, _payload("审查 doc-01"))
        post(port_b, _payload("审查 doc-01"))
        assert (upstream_a.calls, upstream_b.calls) == (1, 1), (
            "the same logical request on another gateway must not replay the first one's cache"
        )
        post(port_a, _payload("审查 doc-01"))
        assert upstream_a.calls == 1, "within one upstream the cache still hits"
        summary = proxy.stats()
        # The third request repeats the first byte for byte, so it is attributed
        # to the raw-hash path rather than to normalisation.
        assert sum(summary["hits"].values()) == 1
        assert summary["requests"] == 3, "one recorder serves every listener"
        assert len(summary["upstreams"]) == 2
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        upstream_a.stop()
        upstream_b.stop()


def test_parse_listeners():
    from reqslim.zcode.__main__ import parse_listeners

    assert parse_listeners(["8788=https://a.example/x/", "8789=https://b.example/y"]) == [
        (8788, "https://a.example/x"),
        (8789, "https://b.example/y"),
    ]
    for bad in (["8788=https://a", "8788=https://b"], ["nope"], ["8788="], ["=https://a"]):
        try:
            parse_listeners(bad)
            raise AssertionError(f"expected SystemExit for {bad}")
        except SystemExit:
            pass


# ----------------------------------------------------------------- analytics
def test_analytics_does_not_group_the_same_key_across_upstreams():
    recorder = Recorder()
    now = time.time()

    def add(upstream: str, ts: float) -> None:
        recorder.add(Event(
            ts=ts, key="same-key", short="same-key", tail="same-tail",
            kind="tool_turn", model="m", status=200, latency=0.1,
            est_tokens=10, tokens_in=1, tokens_out=1, hit="",
            stream=False, n_messages=2, tools=(), upstream=upstream,
        ))

    add("https://a.example", now)
    add("https://a.example", now + 1)
    add("https://b.example", now + 2)
    summary = recorder.summary()
    assert summary["duplicates"] == 1, "only the two A requests are duplicates"
    assert summary["storms"] == 1
    assert summary["near_duplicates"] == 0
    clusters = recorder.clusters()
    assert len(clusters) == 1 and clusters[0]["upstream"] == "https://a.example"


def test_analytics_counts_duplicates_and_storms():
    recorder = Recorder()
    now = time.time()
    for index in range(3):
        recorder.add(
            Event(
                ts=now + index * 2,
                key="k1", short="k1", tail="t1", kind="tool_turn", model="m",
                status=200, latency=0.1, est_tokens=1000, tokens_in=10, tokens_out=5,
                hit="normalized" if index else "", stream=True, n_messages=4,
            )
        )
    recorder.add(
        Event(
            ts=now + 100, key="k2", short="k2", tail="t2", kind="continuation", model="m",
            status=200, latency=0.2, est_tokens=500, tokens_in=8, tokens_out=4,
            hit="", stream=True, n_messages=2,
        )
    )
    summary = recorder.summary()
    assert summary["requests"] == 4
    assert summary["upstream"] == 2
    assert summary["duplicates"] == 2
    assert summary["storms"] == 2
    assert summary["comparison"] == {
        "estimated_requests": 4,
        "actual_requests": 2,
        "reduced_requests": 2,
        "reduction_rate": 50.0,
        "by_mechanism": {"normalized": 2},
    }
    report = recorder.format_report()
    assert "request reduction" in report
    assert "estimated requests  4" in report
    assert "actual requests     2" in report
    assert "reduced             2  (50.0%)" in report
    assert "requests        4" in report
    assert "upstream calls   2" in report
    assert "cache hits       2  (50.0%)" in report


def test_proxy_stats_exposes_the_request_comparison():
    upstream = FakeUpstream()
    proxy, server, port = _proxy_with(upstream)
    try:
        post(port, _payload("same"))
        post(port, _payload("same"))
        expected = {
            "estimated_requests": 2,
            "actual_requests": 1,
            "reduced_requests": 1,
            "reduction_rate": 50.0,
            "by_mechanism": {"raw": 1},
        }
        stats = proxy.stats()
        assert stats["requests"] == 2
        assert stats["upstream"] == 1
        assert stats["comparison"] == expected

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/stats", timeout=5) as response:
            http_stats = json.load(response)
        assert http_stats["comparison"] == expected

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/report", timeout=5) as response:
            report = response.read().decode("utf-8")
        assert "request reduction" in report
        assert "estimated requests  2" in report
        assert "actual requests     1" in report
        assert "reduced             1  (50.0%)" in report
    finally:
        proxy.close()
        upstream.stop()


def test_request_comparison_is_pure_and_conservation_holds():
    """Actual + every mechanism must add up to the logical attempts.

    This is the backend invariant the report relies on; an event with a
    malformed hit value must count as an upstream call, never as a saving.
    """
    now = time.time()

    def event(hit):
        return Event(
            ts=now, key="k", short="k", tail="t", kind="tool_turn", model="m",
            status=200, latency=0.1, est_tokens=10, tokens_in=0, tokens_out=0,
            hit=hit, stream=False, n_messages=1,
        )

    events = [
        event(""),
        event(""),
        event("raw"),
        event("normalized"),
        event("coalesced"),
        event("failure_memo"),
        event(None),
        event(1),
        event("bogus"),
        event(" "),
        event([]),
        event({}),
    ]
    comparison = compare_requests(events)
    assert isinstance(comparison, RequestComparison)
    assert comparison.estimated_requests == 12
    assert comparison.actual_requests == 8, "empty, unknown and malformed hits are upstream calls"
    assert comparison.reduced_requests == 4
    assert comparison.reduction_rate == 33.3
    assert comparison.by_mechanism == {
        "raw": 1,
        "normalized": 1,
        "coalesced": 1,
        "failure_memo": 1,
    }
    assert "bogus" not in comparison.by_mechanism
    assert sum(comparison.by_mechanism.values()) == comparison.reduced_requests
    assert comparison.to_dict()["by_mechanism"] == comparison.by_mechanism

    empty = compare_requests([])
    assert empty.estimated_requests == 0
    assert empty.actual_requests == 0
    assert empty.reduced_requests == 0
    assert empty.reduction_rate == 0.0
    assert empty.by_mechanism == {}


# ------------------------------------------------------- round-2 audit fixes
def test_real_zcode_compaction_request_is_classified():
    """The shipped ZCode compaction instruction (verified in zcode.cjs) says
    "create a detailed summary of the conversation so far", not
    "summarize the conversation". The old regex missed every real compaction
    request, polluting the hit-rate denominator classify exists to protect."""
    payload = _payload()
    payload["messages"] = [
        {"role": "user", "content": [{"type": "text", "text": "审查 doc-01"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "开始"}]},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
                        "Your task is to create a detailed summary of the conversation "
                        "so far, paying close attention to the user's explicit requests "
                        "and your previous actions."
                    ),
                }
            ],
        },
    ]
    assert classify(payload) == "compaction"


def test_context_management_note_is_not_compaction():
    """The shipped main system prompt explains auto-compaction ("the context is
    summarized"); that must not be labelled compaction -- only the compaction
    request itself is."""
    payload = _payload()
    payload["system"] = [
        {
            "type": "text",
            "text": (
                "# Context management\nWhen the conversation grows long, some or all "
                "of the current context is summarized; the summary, along with any "
                "remaining unsummarized context, is provided in the next context window."
            ),
        }
    ]
    assert classify(payload) != "compaction"


def test_failure_memo_expires_are_swept_on_insert():
    """An expired memo whose key is never retried again must not outlive its
    TTL: _maybe_memo sweeps on write, or the proxy leaks error bodies for
    weeks."""
    proxy = ZCodeProxy("http://127.0.0.1:1", failure_ttl=0.05)
    proxy._maybe_memo("k1", 503, b"boom")
    time.sleep(0.08)
    proxy._maybe_memo("k2", 503, b"boom")
    assert "k1" not in proxy._failures, "expired memo must be swept on insert"
    assert "k2" in proxy._failures


def test_failure_memo_is_bounded():
    proxy = ZCodeProxy("http://127.0.0.1:1", failure_ttl=60)
    for index in range(5000):
        proxy._maybe_memo(f"k{index}", 503, b"boom")
    assert len(proxy._failures) <= 4096, "memo table must have a hard cap"
    assert f"k{4999}" in proxy._failures, "cap drops the oldest, not the newest"


def test_cache_evicts_by_byte_budget():
    """Entries are bounded by total response bytes, not just count: 4096 large
    thinking responses would otherwise sit in memory indefinitely."""
    cache = ExactCache(maxsize=10, ttl=None, max_bytes=120)
    for index in range(4):
        cache.put(f"k{index}", LLMResponse(content="x" * 50))
    assert cache.size() == 2, "over-budget entries must be evicted oldest-first"
    assert cache.get("k3") is not None
    assert cache.get("k0") is None


def test_l1_is_bounded_by_lru_not_cleared():
    """The raw-hash map must degrade to an LRU, not drop every mapping at once
    (a clear loses the raw attribution for all still-hot keys)."""
    proxy = ZCodeProxy("http://127.0.0.1:1")
    message = {
        "type": "message",
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
    }
    fp = fingerprint(_payload())
    for index in range(16390):
        proxy._store(f"k{index}", f"raw{index}", fp, message, b"{}", "http://x")
    assert len(proxy._l1) == 16384
    assert ("http://x", "/v1/messages", "", "", "raw0") not in proxy._l1, "oldest entry must be evicted"
    assert ("http://x", "/v1/messages", "", "", "raw16389") in proxy._l1, "newest entry must survive"


def test_oversized_body_is_rejected_without_reading():
    """A Content-Length far above the cap is refused before the body is read,
    so a runaway client cannot hang the proxy on a read."""
    upstream = FakeUpstream()
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.putrequest("POST", "/v1/messages")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(65 * 1024 * 1024))
        conn.putheader("Connection", "close")
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == 413
        assert upstream.calls == 0
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_client_abort_is_recorded_not_cached():
    """A stream the client walked away from is upstream spend (status 499),
    not a 200, and never a cached answer."""
    upstream = TrickleUpstream(_sse(THINKING_MESSAGE), gap=0.5)
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    try:
        conn = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=10
        )
        conn.request(
            "POST",
            "/v1/messages",
            body=json.dumps(_payload(stream=True)).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        response.read(16)
        conn.close()  # mid-stream: the upstream's second half is 0.5s away
        time.sleep(1.2)  # let the proxy hit the broken pipe and finish
        assert proxy.cache.size() == 0, "an aborted stream must not be cached"
        statuses = [event.status for event in proxy.recorder.events]
        assert 499 in statuses, f"abort must be visible in the record, got {statuses}"
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


# ------------------------------------------------------- autostart & wiring
def _table_entry(ident: str, base_url: str, api_type: str = "anthropic-messages") -> dict:
    return {"providerId": ident, "config": {"api": {"type": api_type, "baseUrl": base_url}}}


def _write_table(path: Path, entries: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {"config": {"providerConfigRules": {"providerRules": entries, "templateRules": []}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _read_base_urls(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rules = data["config"]["providerConfigRules"]["providerRules"]
    return {entry["providerId"]: entry["config"]["api"]["baseUrl"] for entry in rules}


def test_wiring_generate_proxied_table_rewrites_matching_providers():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / "builtin.json"
        out = tmp_path / "proxied.json"
        _write_table(source, [
            _table_entry("anthro", "https://up.example/api/anthropic"),
            _table_entry("openai", "https://up.example/v4", api_type="openai-chat-completions"),
            _table_entry("loopback", "http://127.0.0.1:9999"),
        ])
        touched = generate_proxied_table(
            source, {"https://up.example/api/anthropic": "http://127.0.0.1:8788"}, out
        )
        assert touched == [("anthro", "http://127.0.0.1:8788")]
        base_urls = _read_base_urls(out)
        assert base_urls["anthro"] == "http://127.0.0.1:8788"
        assert base_urls["openai"] == "https://up.example/v4", "non-anthropic must pass through"
        assert base_urls["loopback"] == "http://127.0.0.1:9999", "loopback entries pass through"
        assert not list(tmp_path.glob("*.tmp")), "atomic write must clean up"


def test_wiring_regenerates_after_zcode_update():
    """ZCode update adds a provider to its bundled table; the regenerated
    proxied copy must pick it up without losing the first mapping."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / "builtin.json"
        out = tmp_path / "proxied.json"
        mapping = {
            "https://up.example/api/anthropic": "http://127.0.0.1:8788",
            "https://new.example/plan/anthropic": "http://127.0.0.1:8789",
        }
        _write_table(source, [_table_entry("p1", "https://up.example/api/anthropic")])
        generate_proxied_table(source, mapping, out)
        assert _read_base_urls(out) == {"p1": "http://127.0.0.1:8788"}

        # the update: a new gateway with new providers lands in the bundled table
        _write_table(source, [
            _table_entry("p1", "https://up.example/api/anthropic"),
            _table_entry("p2", "https://new.example/plan/anthropic"),
        ])
        touched = generate_proxied_table(source, mapping, out)
        assert ("p2", "http://127.0.0.1:8789") in touched
        assert _read_base_urls(out) == {
            "p1": "http://127.0.0.1:8788",
            "p2": "http://127.0.0.1:8789",
        }


def test_wiring_generate_rejects_mapping_that_matches_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / "builtin.json"
        out = tmp_path / "proxied.json"
        _write_table(source, [_table_entry("p1", "https://up.example/api/anthropic")])
        try:
            generate_proxied_table(source, {"https://other.example/x": "http://127.0.0.1:1"}, out)
            raise AssertionError("expected ValueError for a mapping with no matches")
        except ValueError:
            pass
        assert not out.exists(), "nothing may be written when nothing matches"


def test_autostart_config_loads_or_rejects():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        good = tmp_path / "autostart.json"
        good.write_text(json.dumps({
            "listeners": [[8788, "https://a.example/x/"], [8789, "https://b.example/y"]],
            "record": "r.jsonl",
            "zcode_table": "t.json",
            "config_dir": str(tmp_path),
            "poll_seconds": 5,
        }), encoding="utf-8")
        spec = load_autostart_config(str(good))
        assert spec.listeners == [(8788, "https://a.example/x"), (8789, "https://b.example/y")]
        assert spec.record == "r.jsonl"
        assert spec.poll_seconds == 5

        missing = tmp_path / "nope.json"
        try:
            load_autostart_config(str(missing))
            raise AssertionError("expected SystemExit for a missing config")
        except SystemExit:
            pass

        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"listeners": [[0, "https://x"]], "zcode_table": "t", "config_dir": "c"}), encoding="utf-8")
        try:
            load_autostart_config(str(bad))
            raise AssertionError("expected SystemExit for a malformed listener")
        except SystemExit:
            pass


def test_reqslim_on_port_detects_the_proxy():
    upstream = FakeUpstream()
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    try:
        port = server.server_address[1]
        assert reqslim_on_port(port) is True
        # a bound-but-closed port: nothing answers, so it is not reqslim
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        assert reqslim_on_port(free_port) is False
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_table_watcher_regenerates_on_change():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / "builtin.json"
        out = tmp_path / "proxied.json"
        mapping = {"https://up.example/api/anthropic": "http://127.0.0.1:8788"}
        _write_table(source, [_table_entry("p1", "https://up.example/api/anthropic")])
        watcher = TableWatcher(source, mapping, out)

        assert watcher.tick() is True, "first tick must generate the table"
        assert _read_base_urls(out) == {"p1": "http://127.0.0.1:8788"}

        assert watcher.tick() is False, "unchanged table must not regenerate"

        # ZCode update: same size, newer mtime
        stamp = time.time() + 120
        os.utime(source, (stamp, stamp))
        assert watcher.tick() is True, "an mtime bump is a new table"
        assert watcher.tick() is False

        # updater holds the file mid-swap: keep serving, count misses
        os.unlink(source)
        assert watcher.tick() is False
        assert watcher.misses == 1


# ------------------------------------------------- 0.2.1: remaining audit
def test_beta_tag_normalization():
    """Only the *set* of beta switches matters: order and spacing are client
    churn, presence is semantics."""
    from reqslim.zcode.normalize import beta_tag

    assert beta_tag({}) == ""
    assert beta_tag({"Anthropic-Beta": "  x  "}) == "x"
    ordered = {"anthropic-beta": "b-feature,c-feature"}
    shuffled = {"anthropic-beta": " c-feature , b-feature "}
    assert beta_tag(ordered) == beta_tag(shuffled) == "b-feature,c-feature"
    assert beta_tag({"anthropic-beta": "a,b"}) != beta_tag({"anthropic-beta": "a"})


def test_fingerprint_includes_beta():
    fp_a = fingerprint(_payload(), beta="feat-a")
    fp_b = fingerprint(_payload(), beta="feat-b")
    fp_a_again = fingerprint(_payload(), beta="feat-a")
    assert fp_a.key != fp_b.key, "different beta switches change the answer"
    assert fp_a.key == fp_a_again.key, "same inputs must digest identically"
    assert fp_a.beta == "feat-a"


def test_proxy_partitions_cache_by_beta_header():
    """Two byte-identical requests under different beta switches must both
    reach the upstream; a repeat under the same beta hits the raw cache."""
    upstream = FakeUpstream()
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    try:
        port = server.server_address[1]

        def post_beta(beta):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/messages",
                data=json.dumps(_payload()).encode(),
                headers={
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": beta,
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()

        post_beta("feat-a")
        post_beta("feat-b")
        assert upstream.calls == 2, "different beta sets must not share a cache entry"
        post_beta("feat-a")
        assert upstream.calls == 2, "identical beta must hit the raw cache"
        assert proxy.stats()["hits"] == {"raw": 1}
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_follower_coalesces_while_leader_is_slow():
    """A leader streaming for a long time keeps followers coalesced: the
    follower budget is a deadline, not the old fixed 3x180s count (which a
    slow drip stream could outlast)."""
    upstream = FakeUpstream(delay=2.0)
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}", coalesce_wait=60.0)
    server = proxy.serve(port=0)
    port = server.server_address[1]
    outcomes = []

    def worker():
        try:
            post(port, _payload())
            outcomes.append(1)
        except Exception:
            outcomes.append(0)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    try:
        t1.start()
        time.sleep(0.15)  # the first request becomes the leader
        t2.start()
        t1.join(30)
        t2.join(30)
        assert outcomes == [1, 1]
        assert upstream.calls == 1, f"slow leader must keep coalescing: {upstream.calls}"
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_coalesce_budget_expiry_forwards():
    """Past its deadline a follower forwards concurrently: progress wins over
    deduplication (and both clients still get an answer)."""
    upstream = FakeUpstream(delay=1.0)
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}", coalesce_wait=0.3)
    server = proxy.serve(port=0)
    port = server.server_address[1]
    outcomes = []

    def worker():
        try:
            post(port, _payload())
            outcomes.append(1)
        except Exception:
            outcomes.append(0)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    try:
        t1.start()
        time.sleep(0.15)
        t2.start()
        t1.join(30)
        t2.join(30)
        assert outcomes == [1, 1]
        assert upstream.calls == 2, f"expired follower must forward: {upstream.calls}"
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_recorder_rotates_on_write_when_over_budget():
    """The autostart proxy runs for weeks: rotation must happen on write, not
    only at startup, or the JSONL grows without bound."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        recorder = Recorder(path=path, max_bytes=700)
        for _ in range(6):
            recorder.add(Event(
                ts=time.time(), key="k", short="k", tail="t", kind="tool_turn",
                model="m", status=200, latency=0.01, est_tokens=100,
                tokens_in=0, tokens_out=0, hit="", stream=False,
                n_messages=1, tools=(),
            ))
        assert os.path.exists(path + ".1"), "over-budget log must rotate on write"
        assert os.path.getsize(path) <= 700 + 900, "current file stays near budget"
        recorder.add(Event(
            ts=time.time(), key="after-rotation", short="a", tail="t",
            kind="tool_turn", model="m", status=200, latency=0.01,
            est_tokens=1, tokens_in=0, tokens_out=0, hit="", stream=False,
            n_messages=1, tools=(),
        ))
        with open(path, encoding="utf-8") as handle:
            assert '"after-rotation"' in handle.read(), \
                "post-rotation events land in the fresh file"


# ---------------------------------------------- runtime provider cache layer
MAPPING_BIGMODEL = {"https://open.bigmodel.cn/api/anthropic": "http://127.0.0.1:8788"}


def _runtime_cache_dir(root: Path, version: str) -> Path:
    """One ZCode version's cache dir: <root>/<platform>/<version>/endpoint-<hash>."""
    cache_dir = root / "windows-x86_64" / version / "endpoint-a1b2c3"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def test_runtime_cache_rewrite_backs_up_once_and_is_idempotent():
    """ZCode reads the runtime cache FIRST (asar-verified): the rewrite must
    land there, keep exactly one pristine backup, and stay idempotent across
    installer re-runs and watcher ticks."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        table = _runtime_cache_dir(root, "3.17.2") / "zcode-builtin.json"
        _write_table(table, [_table_entry("p1", "https://open.bigmodel.cn/api/anthropic")])
        original = table.read_text(encoding="utf-8")

        assert rewrite_runtime_caches(MAPPING_BIGMODEL, root=root) == [(table, "rewritten")]
        assert _read_base_urls(table) == {"p1": "http://127.0.0.1:8788"}
        backup = table.with_name(table.name + ".bak-reqslim")
        assert backup.exists() and backup.read_text(encoding="utf-8") == original, \
            "the first rewrite must keep a pristine .bak-reqslim"

        for _ in range(2):  # installer re-run, then a watcher tick
            assert rewrite_runtime_caches(MAPPING_BIGMODEL, root=root) == [(table, "untouched")]
        assert backup.read_text(encoding="utf-8") == original, \
            "re-runs must never overwrite the only pristine backup"


def test_runtime_cache_skips_unrelated_and_unreadable_tables():
    """Caches of other gateways (or a table caught mid-write) must be skipped
    without breaking the batch -- the autostart watcher runs this forever."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        other = _runtime_cache_dir(root, "3.16.3") / "zcode-builtin.json"
        _write_table(other, [_table_entry("kimi", "https://api.kimi.com/coding/v1")])
        broken = _runtime_cache_dir(root, "3.17.1") / "zcode-builtin.json"
        broken.write_text('{"config": {"providerConfig', encoding="utf-8")
        ours = _runtime_cache_dir(root, "3.17.2") / "zcode-builtin.json"
        _write_table(ours, [_table_entry("p1", "https://open.bigmodel.cn/api/anthropic")])

        results = dict(rewrite_runtime_caches(MAPPING_BIGMODEL, root=root))
        assert results[other] == "unrelated", "another gateway's cache must pass through"
        assert "127.0.0.1" not in other.read_text(encoding="utf-8")
        assert results[broken] == "unreadable", "a truncated table must be skipped, not crash"
        assert results[ours] == "rewritten"


def test_runtime_cache_dry_run_decides_but_writes_nothing():
    """--dry-run must report what WOULD change without touching anything."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        table = _runtime_cache_dir(root, "3.17.2") / "zcode-builtin.json"
        _write_table(table, [_table_entry("p1", "https://open.bigmodel.cn/api/anthropic")])
        before = table.read_text(encoding="utf-8")

        assert rewrite_runtime_caches(MAPPING_BIGMODEL, root=root, dry_run=True) == \
            [(table, "rewritten")]
        assert table.read_text(encoding="utf-8") == before, "dry run must not write"
        assert not table.with_name(table.name + ".bak-reqslim").exists(), \
            "dry run must not create the backup either"


def test_restore_runtime_caches_puts_the_originals_back():
    """--uninstall must undo the rewrites: a cache left pointing at reqslim's
    dead ports turns into a permanent ECONNREFUSED."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        table = _runtime_cache_dir(root, "3.17.2") / "zcode-builtin.json"
        _write_table(table, [_table_entry("p1", "https://open.bigmodel.cn/api/anthropic")])
        original = table.read_text(encoding="utf-8")
        rewrite_runtime_caches(MAPPING_BIGMODEL, root=root)
        assert "127.0.0.1" in table.read_text(encoding="utf-8")

        assert restore_runtime_caches(root=root) == [table]
        assert table.read_text(encoding="utf-8") == original, "the pristine table must be back"
        assert not table.with_name(table.name + ".bak-reqslim").exists(), "backup is consumed"


def test_runtime_cache_rewrite_is_read_only_and_uninstall_makes_it_writable():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        table = _runtime_cache_dir(root, "3.17.2") / "zcode-builtin.json"
        _write_table(table, [_table_entry("p1", "https://open.bigmodel.cn/api/anthropic")])
        assert rewrite_runtime_cache_file(table, MAPPING_BIGMODEL) == "rewritten"
        assert not (table.stat().st_mode & stat.S_IWUSR), "cloud refresher must not overwrite it"
        restore_runtime_caches(root=root)
        assert table.stat().st_mode & stat.S_IWUSR, "uninstall restores a writable file"


def test_restore_runtime_caches_uses_inverse_mapping_without_a_backup():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        table = _runtime_cache_dir(root, "3.17.2") / "zcode-builtin.json"
        _write_table(table, [_table_entry("p1", "http://127.0.0.1:8788")])
        restored = restore_runtime_caches(
            root=root,
            reverse_mapping={"http://127.0.0.1:8788": "https://open.bigmodel.cn/api/anthropic"},
        )
        assert restored == [table]
        assert _read_base_urls(table) == {"p1": "https://open.bigmodel.cn/api/anthropic"}


def test_cache_watcher_rewrites_caches_that_zcode_updates_ship():
    """A ZCode update unpacks a NEW version directory with original baseUrls;
    the autostart proxy must rewrite it within one tick (the 3.17.2 lesson)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        watcher = CacheWatcher(MAPPING_BIGMODEL, root=root)
        assert watcher.tick() == [], "an empty root is a clean no-op"

        _write_table(
            _runtime_cache_dir(root, "3.17.1") / "zcode-builtin.json",
            [_table_entry("p1", "https://open.bigmodel.cn/api/anthropic")],
        )
        assert len(watcher.tick()) == 1
        assert watcher.tick() == [], "unchanged caches must not rewrite again"

        _write_table(
            _runtime_cache_dir(root, "3.17.2") / "zcode-builtin.json",
            [_table_entry("p1", "https://open.bigmodel.cn/api/anthropic")],
        )
        assert len(watcher.tick()) == 1, "the freshly shipped version must be rewritten"
        table = _runtime_cache_dir(root, "3.17.2") / "zcode-builtin.json"
        assert _read_base_urls(table) == {"p1": "http://127.0.0.1:8788"}


# ------------------------------------------------ third-round correctness fixes
def _proxy_with(upstream: FakeUpstream) -> tuple[ZCodeProxy, ThreadingHTTPServer, int]:
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.port}")
    server = proxy.serve(port=0)
    return proxy, server, server.server_address[1]


def test_authorization_and_api_key_are_forwarded():
    seen: dict[str, str | None] = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            return

        def do_POST(self):
            seen["authorization"] = self.headers.get("Authorization")
            seen["x-api-key"] = self.headers.get("x-api-key")
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps(THINKING_MESSAGE, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.server_address[1]}")
    server = proxy.serve(port=0)
    try:
        status, _headers, _body = post_raw(
            server.server_address[1],
            "/v1/messages",
            json.dumps(_payload()).encode(),
            {"Authorization": "Bearer test-token", "x-api-key": "test-key"},
        )
        assert status == 200
        assert seen["authorization"] == "Bearer test-token"
        assert seen["x-api-key"] == "test-key"
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_chunked_request_body_is_accepted():
    upstream = FakeUpstream()
    proxy, server, port = _proxy_with(upstream)
    try:
        body = json.dumps(_payload()).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/v1/messages",
            body=iter([body]),
            headers={"Content-Type": "application/json"},
            encode_chunked=True,
        )
        response = conn.getresponse()
        assert response.status == 200
        assert b"msg_fake" in response.read()
        conn.close()
        assert upstream.calls == 1
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_non_object_request_body_is_a_400_not_a_disconnect():
    upstream = FakeUpstream()
    proxy, server, port = _proxy_with(upstream)
    try:
        status, _headers, body = post_raw(port, "/v1/messages", b"[]")
        assert status == 400
        assert b"JSON object" in body
        assert upstream.calls == 0
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_non_object_200_is_forwarded_and_recorded_not_crashing():
    upstream = FakeUpstream(raw_body=b"[]", content_type="application/json")
    proxy, server, port = _proxy_with(upstream)
    try:
        for _ in range(2):
            status, _headers, body = post_raw(port, "/v1/messages", json.dumps(_payload()).encode())
            assert status == 200 and body == b"[]"
        assert upstream.calls == 2, "an unusable 200 must not be cached"
        assert proxy.cache.size() == 0
        assert [event.status for event in proxy.recorder.events] == [200, 200]
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_unsupported_path_closes_instead_of_poisoning_keepalive():
    upstream = FakeUpstream()
    proxy, server, port = _proxy_with(upstream)
    try:
        status, _headers, _body = post_raw(port, "/not-messages", b'{"padding":"' + b"x" * 200 + b'"}')
        assert status == 404
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/not-messages",
            body=b'{"padding":"' + b"x" * 200 + b'"}',
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        response.read()
        try:
            conn.request(
                "POST",
                "/v1/messages",
                body=json.dumps(_payload()).encode(),
                headers={"Content-Type": "application/json"},
            )
            second = conn.getresponse()
            assert second.status != 501, "the unread body must not become a request line"
        except (ConnectionError, http.client.HTTPException):
            pass
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_malformed_sse_is_recorded_as_502_and_does_not_kill_the_client():
    sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"id":"m","type":"message",'
        b'"role":"assistant","model":"x","content":[],"stop_reason":null,'
        b'"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":null,'
        b'"content_block":{"type":"text","text":""}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    upstream = FakeUpstream(raw_body=sse, content_type="text/event-stream")
    proxy, server, port = _proxy_with(upstream)
    try:
        body = post(port, _payload(stream=True))
        assert b"proxy_error" in body, "the client must see an explicit error event"
        assert [event.status for event in proxy.recorder.events] == [502]
        assert proxy.cache.size() == 0
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_invalid_content_block_is_never_cached():
    message = {
        "id": "m", "type": "message", "role": "assistant", "model": "x",
        "content": ["not-a-block"], "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    upstream = FakeUpstream(message=message)
    proxy, server, port = _proxy_with(upstream)
    try:
        status, _headers, _body = post_raw(port, "/v1/messages", json.dumps(_payload()).encode())
        assert status == 200
        assert proxy.cache.size() == 0, "a non-dict content block must not poison the cache"
        assert upstream.calls == 1
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_stream_without_stop_reason_is_not_cached():
    sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"id":"m","type":"message",'
        b'"role":"assistant","model":"x","content":[],"stop_reason":null,'
        b'"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"text","text":""}}\n\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"done"}}\n\n'
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":0}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    upstream = FakeUpstream(raw_body=sse, content_type="text/event-stream")
    proxy, server, port = _proxy_with(upstream)
    try:
        post(port, _payload(stream=True))
        post(port, _payload(stream=True))
        assert upstream.calls == 2, "a null stop_reason must not be replayed"
        assert proxy.cache.size() == 0
        assert all(event.status == 502 for event in proxy.recorder.events)
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_stream_request_receiving_json_200_is_a_controlled_error():
    upstream = FakeUpstream(
        raw_body=b'{"error":{"type":"overloaded_error"}}',
        content_type="application/json",
    )
    proxy, server, port = _proxy_with(upstream)
    try:
        status, headers, _body = post_raw(
            port, "/v1/messages", json.dumps(_payload(stream=True)).encode()
        )
        assert status == 502
        assert headers.get("Content-Type", "").startswith("application/json"), headers
        assert [event.status for event in proxy.recorder.events] == [502]
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_upstream_content_type_and_retry_after_are_forwarded():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            return

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = b"gateway HTML"
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Retry-After", "37")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    proxy = ZCodeProxy(f"http://127.0.0.1:{upstream.server_address[1]}")
    server = proxy.serve(port=0)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/v1/messages",
            data=json.dumps(_payload()).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=10)
            raise AssertionError("expected 502")
        except urllib.error.HTTPError as exc:
            assert exc.code == 502
            assert exc.headers.get("Content-Type") == "text/plain"
            assert exc.headers.get("Retry-After") == "37"
            assert exc.read() == b"gateway HTML"
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_normalization_keeps_cache_control_inside_tool_inputs_and_schemas():
    first = _payload()
    second = _payload()
    first["messages"][1]["content"][0]["input"] = {"cache_control": {"ttl": 60}}
    second["messages"][1]["content"][0]["input"] = {"cache_control": {"ttl": 999}}
    assert fingerprint(first).key != fingerprint(second).key, "tool input is content"

    first = _payload()
    second = _payload()
    first["tools"][0]["input_schema"] = {"type": "object", "properties": {"cache_control": {"type": "string"}}}
    second["tools"][0]["input_schema"] = {"type": "object", "properties": {"cache_control": {"type": "integer"}}}
    assert fingerprint(first).key != fingerprint(second).key, "tool schema is content"

    upstream = FakeUpstream()
    proxy, server, port = _proxy_with(upstream)
    try:
        post(port, first)
        post(port, second)
        assert upstream.calls == 2, "different tool schemas must not share a cache entry"
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_credentials_partition_cache_and_coalescing():
    upstream = FakeUpstream()
    proxy, server, port = _proxy_with(upstream)
    body = json.dumps(_payload()).encode()
    try:
        for key in ("account-a", "account-b"):
            post_raw(port, "/v1/messages", body, {"x-api-key": key})
        assert upstream.calls == 2, "different accounts must not share a reply"
        post_raw(port, "/v1/messages", body, {"x-api-key": "account-a"})
        assert upstream.calls == 2, "the same account still hits its own cache"
        assert proxy.stats()["hits"] == {"raw": 1}
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_coalesced_followers_are_attributed_as_coalesced():
    upstream = FakeUpstream(delay=0.4)
    proxy, server, port = _proxy_with(upstream)
    outcomes: list[int] = []
    lock = threading.Lock()

    def worker():
        try:
            post(port, _payload())
            with lock:
                outcomes.append(200)
        except Exception as exc:  # noqa: BLE001
            with lock:
                outcomes.append(getattr(exc, "code", type(exc).__name__))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        assert outcomes == [200] * 4
        assert upstream.calls == 1
        assert proxy.stats()["hits"] == {"coalesced": 3}
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_non_retryable_errors_are_shared_by_coalescing():
    upstream = FakeUpstream(delay=0.15, fail_status=400)
    proxy, server, port = _proxy_with(upstream)
    outcomes: list[int] = []
    lock = threading.Lock()

    def worker():
        try:
            post(port, _payload())
            with lock:
                outcomes.append(200)
        except urllib.error.HTTPError as exc:
            with lock:
                outcomes.append(exc.code)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        assert outcomes == [400, 400, 400]
        assert upstream.calls == 1, "a deterministic client error should not be re-sent"
        assert proxy.stats()["hits"] == {"coalesced": 2}
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_oversized_response_is_still_shared_by_coalescing():
    message = {**THINKING_MESSAGE, "content": [{"type": "text", "text": "x" * 2048}]}
    upstream = FakeUpstream(delay=0.4, message=message)
    proxy, server, port = _proxy_with(upstream)
    proxy.cache.max_bytes = 128
    payload = _payload()
    outcomes: list[int] = []
    lock = threading.Lock()

    def worker():
        try:
            post(port, payload)
            with lock:
                outcomes.append(200)
        except Exception as exc:  # noqa: BLE001
            with lock:
                outcomes.append(getattr(exc, "code", type(exc).__name__))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        assert outcomes == [200, 200]
        assert upstream.calls == 1, "over the cache limit, handoff must still coalesce"
        assert proxy.stats()["hits"] == {"coalesced": 1}
        assert proxy.cache.size() == 0
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_count_tokens_path_is_forwarded():
    upstream = FakeUpstream()
    proxy, server, port = _proxy_with(upstream)
    try:
        status, _headers, body = post_raw(
            port, "/v1/messages/count_tokens", json.dumps(_payload()).encode()
        )
        assert status == 200
        assert json.loads(body) == {"input_tokens": 42}
        assert upstream.calls == 1
    finally:
        server.shutdown()
        server.server_close()
        upstream.stop()


def test_runtime_cache_reinstall_rebases_from_the_pristine_backup():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        table = _runtime_cache_dir(root, "3.17.2") / "zcode-builtin.json"
        _write_table(table, [_table_entry("p1", "https://open.bigmodel.cn/api/anthropic")])
        original = table.read_text(encoding="utf-8")
        first = {"https://open.bigmodel.cn/api/anthropic": "http://127.0.0.1:8788"}
        second = {"https://open.bigmodel.cn/api/anthropic": "http://127.0.0.1:8799"}
        assert rewrite_runtime_cache_file(table, first) == "rewritten"
        assert _read_base_urls(table) == {"p1": "http://127.0.0.1:8788"}
        assert rewrite_runtime_cache_file(table, second) == "rewritten"
        assert _read_base_urls(table) == {"p1": "http://127.0.0.1:8799"}
        assert rewrite_runtime_cache_file(table, second) == "untouched"
        assert table.with_name(table.name + ".bak-reqslim").read_text(encoding="utf-8") == original


def test_malformed_provider_table_is_unreadable_not_a_crash():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        table = _runtime_cache_dir(root, "3.17.2") / "zcode-builtin.json"
        table.write_text("[]", encoding="utf-8")
        assert rewrite_runtime_cache_file(table, MAPPING_BIGMODEL) == "unreadable"
        assert CacheWatcher(MAPPING_BIGMODEL, root=root).tick() == []
        assert TableWatcher(table, MAPPING_BIGMODEL, root / "out.json").tick() is False


def test_recorder_rotation_stays_under_the_byte_budget():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        recorder = Recorder(path=path, max_bytes=1000)
        event = Event(
            ts=time.time(), key="k" * 100, short="k", tail="t", kind="tool_turn",
            model="m", status=200, latency=0.01, est_tokens=100,
            tokens_in=0, tokens_out=0, hit="", stream=False,
            n_messages=1, tools=(),
        )
        peak = 0
        for _ in range(25):
            recorder.add(event)
            peak = max(peak, os.path.getsize(path))
        assert peak <= 1000, f"rotation must keep the current file bounded, peak={peak}"
        assert os.path.exists(path + ".1")


def test_offline_report_loads_more_than_the_live_window():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "requests.jsonl"
        event = Event(
            ts=time.time(), key="k", short="k", tail="t", kind="tool_turn",
            model="m", status=200, latency=0.01, est_tokens=1,
            tokens_in=0, tokens_out=0, hit="", stream=False,
            n_messages=1, tools=(),
        )
        line = event.to_json() + "\n"
        with open(path, "w", encoding="utf-8") as handle:
            for _ in range(50005):
                handle.write(line)
        recorder = load_events(str(path))
        assert len(recorder.events) == 50005
        assert recorder.summary()["requests"] == 50005


def test_offline_report_skips_malformed_records_without_losing_valid_ones():
    """A crash-truncated line or a hand-edited field must not kill --report."""
    now = time.time()
    valid = Event(
        ts=now, key="k", short="k", tail="t", kind="tool_turn", model="m",
        status=200, latency=0.1, est_tokens=10, tokens_in=1, tokens_out=1,
        hit="", stream=False, n_messages=1,
    )
    payload = json.loads(valid.to_json())
    cache_hit = {**payload, "hit": "raw"}
    unknown_hit = {**payload, "hit": "bogus"}
    null_hit = {**payload, "hit": None}
    list_hit = {**payload, "hit": []}
    bad_tools = {**payload, "tools": 5}
    bad_tokens = {**payload, "tokens_in": None}
    bad_stream = {**payload, "stream": "yes"}
    bad_number = {**payload, "tokens_in": 1e999}
    bad_ts = {**payload, "ts": float("nan")}

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "requests.jsonl"
        lines = [
            valid.to_json(),
            json.dumps(cache_hit),
            json.dumps(unknown_hit),
            json.dumps(null_hit),
            json.dumps(list_hit),
            json.dumps(bad_tools),
            json.dumps(bad_tokens),
            json.dumps(bad_stream),
            json.dumps(bad_number),
            json.dumps(bad_ts),
            "[]",
        ]
        with open(path, "wb") as handle:
            handle.write(("\n".join(lines) + "\n").encode("utf-8"))
            handle.write(b"\xff\xfe\n")  # truncated multibyte tail
        recorder = load_events(str(path))
        assert len(recorder.events) == 5, "valid, unknown and non-string-hit records survive"
        summary = recorder.summary()
        assert summary["requests"] == 5
        assert summary["upstream"] == 4, "non-string hit labels are upstream calls"
        assert summary["comparison"] == {
            "estimated_requests": 5,
            "actual_requests": 4,
            "reduced_requests": 1,
            "reduction_rate": 20.0,
            "by_mechanism": {"raw": 1},
        }
        assert "request reduction" in recorder.format_report()


def test_missing_report_file_is_an_error():
    missing = str(Path(tempfile.gettempdir()) / "reqslim-no-such-report.jsonl")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = zcode_main(["--report", missing])
    assert code == 2
    assert "not found" in err.getvalue()


def test_autostart_config_carries_proxy_options():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "autostart.json"
        path.write_text(json.dumps({
            "listeners": [[8788, "https://a.example/x"]],
            "record": "r.jsonl",
            "zcode_table": "t.json",
            "config_dir": tmp,
            "poll_seconds": 5,
            "ttl": 30,
            "max_cache": 12,
            "cache": False,
            "coalesce": False,
            "coalesce_wait": 2,
            "failure_ttl": 0,
        }), encoding="utf-8")
        spec = load_autostart_config(str(path))
        assert spec.ttl == 30 and spec.max_cache == 12
        assert spec.cache_enabled is False and spec.coalesce is False
        assert spec.coalesce_wait == 2 and spec.failure_ttl == 0


def test_stop_running_proxy_closes_the_listeners():
    upstream = FakeUpstream()
    proxy, server, port = _proxy_with(upstream)
    try:
        assert health_on_port(port) is not None
        assert stop_running_proxy([port], timeout=5) is True
        time.sleep(0.2)
        assert health_on_port(port) is None
    finally:
        proxy.close()
        upstream.stop()


def test_signing_handshake_is_forwarded_to_provider_origin():
    """ZCode's signer posts to /api/paas/... on the provider *origin*.

    The upstream URL carries ``/api/anthropic``; blindly appending the
    handshake path would produce ``/api/anthropic/api/paas/...`` and the
    gateway would answer 404 before any signed request could start.
    """
    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            seen["path"] = self.path
            seen["authorization"] = self.headers.get("Authorization", "")
            body = b'{"code":200,"data":{"privateCipher":"cipher"}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}/api/anthropic"
    proxy = ZCodeProxy(upstream_url)
    servers = proxy.serve_many("127.0.0.1", [(0, upstream_url)])
    port = servers[0].server_address[1]
    try:
        status, _, body = post_raw(
            port,
            "/api/paas/c1f3a7e2/v2/client",
            b'{"nonce":"n","sig":"s","ts":"1"}',
            {"Authorization": "Bearer account-token"},
        )
        assert status == 200, status
        assert body == b'{"code":200,"data":{"privateCipher":"cipher"}}'
        assert seen["path"] == "/api/paas/c1f3a7e2/v2/client", seen
        assert seen["authorization"] == "Bearer account-token", seen
        assert proxy.recorder.summary()["requests"] == 0, "handshake must not be cached/recorded"
    finally:
        proxy.close()
        upstream.shutdown()
        upstream.server_close()


def test_tls_listener_serves_https_health():
    """The installer's HTTPS listener must be usable by a TLS client.

    ZCode's signer refuses HTTP; this exercises the exact transport the
    provider table will point at, including a certificate with 127.0.0.1 in
    its SAN.
    """
    if _openssl() is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tls = ensure_tls(Path(tmp))
        proxy = ZCodeProxy("http://127.0.0.1:1")
        servers = proxy.serve_many(
            "127.0.0.1",
            [(0, "http://127.0.0.1:1")],
            tls=(str(tls.cert), str(tls.key)),
        )
        port = servers[0].server_address[1]
        try:
            context = ssl._create_unverified_context()
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
            conn.request("GET", "/v1/health")
            response = conn.getresponse()
            data = json.loads(response.read())
            conn.close()
            assert response.status == 200
            assert data["ok"] is True
            assert data["tls"] is True
            assert health_on_port(port, scheme="https") is not None
        finally:
            proxy.close()


if __name__ == "__main__":
    import traceback

    thread_errors = []
    original_hook = threading.excepthook

    def thread_error(args):
        thread_errors.append((name, args))
        print(f"FAIL  background thread during {name}: {args.exc_type.__name__}", flush=True)
        original_hook(args)

    threading.excepthook = thread_error
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception:
            failures += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    threading.excepthook = original_hook
    for test_name, error in thread_errors:
        print(f"FAIL  background thread {error.thread.name} during {test_name}: {error.exc_type.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures or thread_errors else 0)
