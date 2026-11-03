"""ZCode adaptation tests: normalisation, thinking-block fidelity, coalescing.

    python tests/test_zcode.py
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reqslim.cache import ExactCache
from reqslim.types import LLMResponse
from reqslim.zcode import (
    Assembler,
    Recorder,
    ZCodeProxy,
    classify,
    fingerprint,
    normalize_request,
    replay_sse,
)
from reqslim.zcode.analytics import Event
from reqslim.zcode.__main__ import (
    load_autostart_config,
    reqslim_on_port,
    TableWatcher,
)
from reqslim.zcode.wiring import generate_proxied_table

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
                for part in (outer.sse[:half], outer.sse[half:]):
                    self.wfile.write(f"{len(part):X}\r\n".encode() + part + b"\r\n")
                    self.wfile.flush()
                    time.sleep(outer.gap)
                self.wfile.write(b"0\r\n\r\n")

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


def test_failure_memo_is_bounded():
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
    assert "requests" in recorder.format_report()


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
    message = {"type": "message", "content": [{"type": "text", "text": "hi"}]}
    fp = fingerprint(_payload())
    for index in range(16390):
        proxy._store(f"k{index}", f"raw{index}", fp, message, b"{}", "http://x")
    assert len(proxy._l1) == 16384
    assert ("http://x", "raw0") not in proxy._l1, "oldest entry must be evicted"
    assert ("http://x", "raw16389") in proxy._l1, "newest entry must survive"


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


if __name__ == "__main__":
    import traceback

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
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
