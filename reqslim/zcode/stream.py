"""Stream assembly and replay for ZCode's traffic.

ZCode runs with extended thinking enabled (``thinking`` appears 70 times in the
shipped runtime). That makes the naive "concatenate text deltas" assembler wrong
in a way that only shows up later: the ``thinking`` block and its ``signature``
must be echoed back verbatim on the next turn, or the provider rejects the
conversation. A cached response that loses them would break the session a few
turns after the cache hit -- the worst kind of bug, because the symptom appears
far from the cause.

So this assembler handles every block type Anthropic actually emits:

    text                 text_delta
    thinking             thinking_delta + signature_delta
    redacted_thinking    full payload in content_block_start, no deltas
    tool_use             input_json_delta (accumulated then parsed)

and the replay emits the same shapes back.
"""

from __future__ import annotations

import json
from typing import Any

#: Block types whose payload arrives entirely in content_block_start.
_WHOLE_BLOCK = frozenset({"redacted_thinking"})


class Assembler:
    def __init__(self) -> None:
        self.message: dict[str, Any] = {
            "id": "msg_reqslim",
            "type": "message",
            "role": "assistant",
            "model": "",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        self._blocks: dict[int, dict[str, Any]] = {}
        self._partial: dict[int, str] = {}
        self.events = 0
        #: Set when the upstream reported a failure *inside* the stream.
        self.error: dict[str, Any] | None = None
        #: Set when a tool payload did not parse -- the response is unusable.
        self.malformed = False
        #: Set by ``message_stop``, the stream's explicit terminator.
        self.stopped = False

    def feed(self, event: dict[str, Any]) -> None:
        self.events += 1
        kind = event.get("type")

        if kind == "message_start":
            started = event.get("message") or {}
            self.message.update(started)
            self.message.setdefault("content", [])
            self.message.setdefault("usage", {"input_tokens": 0, "output_tokens": 0})

        elif kind == "error":
            # Anthropic reports an overload mid-stream as HTTP 200 plus an
            # ``event: error``. Assembling that without a branch here produces
            # an empty message that looks like a successful answer, and the
            # proxy would then cache it and replay it for the whole TTL.
            self.error = event.get("error") or {"type": "error"}

        elif kind == "content_block_start":
            index = int(event.get("index", 0))
            block = dict(event.get("content_block") or {})
            if block.get("type") == "tool_use":
                block.setdefault("input", {})
                self._partial[index] = ""
            self._blocks[index] = block

        elif kind == "content_block_delta":
            index = int(event.get("index", 0))
            delta = event.get("delta") or {}
            block = self._blocks.setdefault(index, {"type": "text", "text": ""})
            if "text" in delta:
                block["text"] = block.get("text", "") + delta["text"]
            elif "thinking" in delta:
                block["thinking"] = block.get("thinking", "") + delta["thinking"]
            elif "signature" in delta:
                block["signature"] = delta["signature"]
            elif "partial_json" in delta:
                self._partial[index] = self._partial.get(index, "") + delta["partial_json"]

        elif kind == "content_block_stop":
            index = int(event.get("index", 0))
            if index in self._partial:
                raw = self._partial.pop(index)
                try:
                    self._blocks[index]["input"] = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    # A truncated tool input is not a usable response; keep the
                    # raw string so the failure is visible rather than silent,
                    # and flag the assembly so it is never cached.
                    self._blocks[index]["input"] = {}
                    self._blocks[index]["_malformed_input"] = raw
                    self.malformed = True

        elif kind == "message_delta":
            delta = event.get("delta") or {}
            for field in ("stop_reason", "stop_sequence"):
                if field in delta:
                    self.message[field] = delta[field]
            usage = event.get("usage") or {}
            if usage:
                merged = dict(self.message.get("usage") or {})
                merged.update(usage)
                self.message["usage"] = merged

        elif kind == "message_stop":
            self.stopped = True

    def finish(self) -> dict[str, Any]:
        self.message["content"] = [self._blocks[i] for i in sorted(self._blocks)]
        return self.message

    @property
    def complete(self) -> bool:
        """Whether this stream assembled into a message worth replaying.

        A 200 carrying an ``error`` event, a stream that stops before
        ``message_delta``, and a broken tool payload all look like success to a
        naive assembler. Each of them, if cached, would answer later requests
        with a message the upstream never sent -- the one failure mode the
        unmount guarantee cannot absorb. Checking before the store is cheaper
        than explaining the symptom a few turns later.

        Either terminator counts as finished: ``message_stop`` is the explicit
        one, and a ``stop_reason`` means the stream got past the content and
        only missed the marker. Requiring both would switch the cache off for
        any gateway that sends one but not the other.
        """
        return (
            self.error is None
            and not self.malformed
            and bool(self._blocks)
            and (self.stopped or self.message.get("stop_reason") is not None)
        )


def _blank(block: dict[str, Any]) -> dict[str, Any]:
    kind = block.get("type", "text")
    if kind == "text":
        return {"type": "text", "text": ""}
    if kind == "thinking":
        return {"type": "thinking", "thinking": ""}
    if kind == "tool_use":
        return {
            "type": "tool_use",
            "id": block.get("id", "toolu_reqslim"),
            "name": block.get("name", ""),
            "input": {},
        }
    return dict(block)


def _deltas(index: int, block: dict[str, Any]) -> list[dict[str, Any]]:
    kind = block.get("type", "text")
    if kind == "text":
        text = block.get("text", "")
        return (
            [{"type": "content_block_delta", "index": index,
              "delta": {"type": "text_delta", "text": text}}]
            if text
            else []
        )
    if kind == "thinking":
        out = []
        thinking = block.get("thinking", "")
        if thinking:
            out.append({"type": "content_block_delta", "index": index,
                        "delta": {"type": "thinking_delta", "thinking": thinking}})
        signature = block.get("signature")
        if signature:
            out.append({"type": "content_block_delta", "index": index,
                        "delta": {"type": "signature_delta", "signature": signature}})
        return out
    if kind == "tool_use":
        return [
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block.get("input") or {}, ensure_ascii=False),
                },
            }
        ]
    return []


def replay_sse(message: dict[str, Any]) -> bytes:
    """Rebuild an SSE stream that is faithful to the stored message."""
    head = {
        **message,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {**(message.get("usage") or {}), "output_tokens": 0},
    }
    out: list[str] = []

    def emit(name: str, payload: dict[str, Any]) -> None:
        out.append(f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n")

    emit("message_start", {"type": "message_start", "message": head})
    for index, block in enumerate(message.get("content") or []):
        kind = block.get("type", "text")
        start_block = dict(block) if kind in _WHOLE_BLOCK else _blank(block)
        emit(
            "content_block_start",
            {"type": "content_block_start", "index": index, "content_block": start_block},
        )
        for delta in _deltas(index, block):
            emit(delta["type"], delta)
        emit("content_block_stop", {"type": "content_block_stop", "index": index})
    emit(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": message.get("stop_reason"),
                "stop_sequence": message.get("stop_sequence"),
            },
            "usage": {"output_tokens": (message.get("usage") or {}).get("output_tokens", 0)},
        },
    )
    emit("message_stop", {"type": "message_stop"})
    return "".join(out).encode("utf-8")


def usable(message: Any) -> bool:
    """Whether a finished message is safe to cache and replay.

    The upstream can answer 200 with something that is not a message at all: a
    gateway's HTML error page, ``{"error": ...}`` from an overloaded backend, an
    empty body. Those must be forwarded and forgotten. Caching one would answer
    a later request with a message that never existed, which is worse than
    missing the cache -- a wrong hit is silent and expensive.
    """
    if not isinstance(message, dict):
        return False
    # ``type`` is optional: Anthropic sends ``"message"``, but compatible
    # gateways sometimes omit it, and refusing those would silently disable the
    # cache for that provider. An explicit non-message type is still rejected.
    if message.get("type") not in (None, "message"):
        return False
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False
    # Private keys (``_malformed_input``) mark an assembly that failed to
    # parse; they are diagnostics, not protocol fields, and must not reach a
    # client or the cache.
    for block in content:
        if isinstance(block, dict) and any(str(key).startswith("_") for key in block):
            return False
    return True


def tool_uses(message: dict[str, Any]) -> list[str]:
    """Names of tools the model asked to run -- used for per-tool analytics."""
    names = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            names.append(str(block.get("name", "?")))
    return names


def has_thinking(message: dict[str, Any]) -> bool:
    return any(
        isinstance(block, dict) and block.get("type") in {"thinking", "redacted_thinking"}
        for block in message.get("content") or []
    )
