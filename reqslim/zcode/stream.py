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
from copy import deepcopy
from typing import Any, Mapping

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
        self._partial: dict[int, str | None] = {}
        self.events = 0
        #: Set when the upstream reported a failure *inside* the stream.
        self.error: dict[str, Any] | None = None
        #: Set when a tool payload did not parse -- the response is unusable.
        self.malformed = False
        #: Set by ``message_stop``, the stream's explicit terminator.
        self.stopped = False

    @staticmethod
    def _index(event: Mapping[str, Any]) -> int:
        value = event.get("index", 0)
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, str) and value.strip().isdigit():
                value = int(value)
            else:
                raise ValueError(f"invalid content index: {value!r}")
        if value < 0:
            raise ValueError(f"negative content index: {value}")
        return value

    def feed(self, event: Any) -> None:
        """Consume one SSE event, never letting malformed input escape.

        Upstream JSON is untrusted input. A gateway sending ``null`` for an
        index or a string in ``delta`` must not kill the client's stream; it
        marks the assembly unusable instead.
        """
        self.events += 1
        try:
            self._feed(event)
        except (TypeError, ValueError, AttributeError, KeyError, json.JSONDecodeError):
            self.malformed = True

    def _feed(self, event: Any) -> None:
        if not isinstance(event, Mapping):
            raise ValueError("event must be an object")
        kind = event.get("type")

        if kind == "message_start":
            started = event.get("message")
            if not isinstance(started, Mapping):
                raise ValueError("message_start.message must be an object")
            self.message.update(deepcopy(dict(started)))
            self.message.setdefault("content", [])
            self.message.setdefault("usage", {"input_tokens": 0, "output_tokens": 0})

        elif kind == "error":
            # Anthropic reports an overload mid-stream as HTTP 200 plus an
            # ``event: error``. Assembling that without a branch here produces
            # an empty message that looks like a successful answer, and the
            # proxy would then cache it and replay it for the whole TTL.
            self.error = event.get("error") or {"type": "error"}

        elif kind == "content_block_start":
            index = self._index(event)
            content_block = event.get("content_block")
            if not isinstance(content_block, Mapping):
                raise ValueError("content_block must be an object")
            block = deepcopy(dict(content_block))
            if block.get("type") == "tool_use":
                block.setdefault("input", {})
                self._partial[index] = None
            self._blocks[index] = block

        elif kind == "content_block_delta":
            index = self._index(event)
            delta = event.get("delta")
            if not isinstance(delta, Mapping):
                raise ValueError("delta must be an object")
            block = self._blocks.setdefault(index, {"type": "text", "text": ""})
            if delta.get("type") == "citations_delta" or "citation" in delta:
                citation = delta.get("citation")
                if not isinstance(citation, Mapping):
                    raise ValueError("citations_delta.citation must be an object")
                citations = block.get("citations")
                if citations is None:
                    citations = []
                if not isinstance(citations, list):
                    raise ValueError("content_block.citations must be an array")
                citations.append(deepcopy(dict(citation)))
                block["citations"] = citations
            elif "text" in delta:
                text = delta["text"]
                if not isinstance(text, str):
                    raise ValueError("text_delta.text must be a string")
                block["text"] = block.get("text", "") + text
            elif "thinking" in delta:
                thinking = delta["thinking"]
                if not isinstance(thinking, str):
                    raise ValueError("thinking_delta.thinking must be a string")
                block["thinking"] = block.get("thinking", "") + thinking
            elif "signature" in delta:
                signature = delta["signature"]
                if not isinstance(signature, str):
                    raise ValueError("signature_delta.signature must be a string")
                block["signature"] = block.get("signature", "") + signature
            elif "partial_json" in delta:
                partial = delta["partial_json"]
                if not isinstance(partial, str):
                    raise ValueError("input_json_delta.partial_json must be a string")
                self._partial[index] = (self._partial.get(index) or "") + partial

        elif kind == "content_block_stop":
            index = self._index(event)
            if index in self._partial:
                raw = self._partial.pop(index)
                try:
                    if raw is None:
                        parsed = self._blocks[index].get("input")
                    else:
                        parsed = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    parsed = None
                if not isinstance(parsed, dict):
                    # A truncated tool input is not a usable response; keep the
                    # raw string so the failure is visible rather than silent,
                    # and flag the assembly so it is never cached.
                    self._blocks.setdefault(index, {"type": "tool_use", "input": {}})
                    self._blocks[index]["input"] = {}
                    self._blocks[index]["_malformed_input"] = raw
                    self.malformed = True
                else:
                    self._blocks[index]["input"] = parsed

        elif kind == "message_delta":
            delta = event.get("delta")
            if not isinstance(delta, Mapping):
                raise ValueError("message_delta.delta must be an object")
            for field in ("stop_reason", "stop_sequence"):
                if field in delta:
                    self.message[field] = delta[field]
            usage = event.get("usage")
            if usage is not None and not isinstance(usage, Mapping):
                raise ValueError("message_delta.usage must be an object")
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
        any gateway that sends one but not the other. ``stop_reason`` itself is
        mandatory: ``message_stop`` alone is a stream that stopped before
        ``message_delta``, and replaying it would hand clients a null stop
        reason. Every tool input must also have been closed.
        """
        return (
            self.error is None
            and not self.malformed
            and bool(self._blocks)
            and not self._partial
            and self.message.get("stop_reason") is not None
        )


def _blank(block: Mapping[str, Any]) -> dict[str, Any]:
    kind = block.get("type", "text")
    blank = dict(block)
    if kind == "text":
        blank.setdefault("type", kind)
        blank["text"] = ""
        citations = block.get("citations")
        if isinstance(citations, list) and all(isinstance(item, Mapping) for item in citations):
            blank["citations"] = []
    elif kind == "thinking":
        blank["thinking"] = ""
        if isinstance(block.get("signature"), str):
            blank["signature"] = ""
    elif kind == "tool_use":
        blank.setdefault("id", "toolu_reqslim")
        blank.setdefault("name", "")
        blank["input"] = {}
    return blank


def _deltas(index: int, block: Mapping[str, Any]) -> list[dict[str, Any]]:
    kind = block.get("type", "text")
    if kind == "text":
        text = block.get("text", "")
        out = []
        if isinstance(text, str) and text:
            out.append({"type": "content_block_delta", "index": index,
                        "delta": {"type": "text_delta", "text": text}})
        citations = block.get("citations")
        if isinstance(citations, list) and all(isinstance(item, Mapping) for item in citations):
            for citation in citations:
                out.append({"type": "content_block_delta", "index": index,
                            "delta": {"type": "citations_delta", "citation": dict(citation)}})
        return out
    if kind == "thinking":
        out = []
        thinking = block.get("thinking", "")
        if isinstance(thinking, str) and thinking:
            out.append({"type": "content_block_delta", "index": index,
                        "delta": {"type": "thinking_delta", "thinking": thinking}})
        signature = block.get("signature")
        if isinstance(signature, str) and signature:
            out.append({"type": "content_block_delta", "index": index,
                        "delta": {"type": "signature_delta", "signature": signature}})
        return out
    if kind == "tool_use":
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        return [
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(tool_input, ensure_ascii=False),
                },
            }
        ]
    return []


def replay_sse(message: Any) -> bytes:
    """Rebuild an SSE stream that is faithful to the stored message."""
    if not isinstance(message, Mapping):
        message = {}
    content = message.get("content")
    if not isinstance(content, list):
        content = []
    usage = message.get("usage")
    if not isinstance(usage, Mapping):
        usage = {}
    head = {
        **message,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {**usage, "output_tokens": 0},
    }
    out: list[str] = []

    def emit(name: str, payload: dict[str, Any]) -> None:
        out.append(f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n")

    emit("message_start", {"type": "message_start", "message": head})
    for index, block in enumerate(content):
        if not isinstance(block, Mapping):
            continue
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
            "usage": {"output_tokens": usage.get("output_tokens", 0)},
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
    if message.get("stop_reason") is None:
        return False
    usage = message.get("usage")
    if usage is not None and not isinstance(usage, Mapping):
        return False
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False
    # Private keys (``_malformed_input``) mark an assembly that failed to
    # parse; they are diagnostics, not protocol fields, and must not reach a
    # client or the cache.
    for block in content:
        if not isinstance(block, Mapping):
            return False
        if any(str(key).startswith("_") for key in block):
            return False
        kind = block.get("type")
        if not isinstance(kind, str) or not kind:
            return False
        if kind == "text" and not isinstance(block.get("text"), str):
            return False
        if kind == "thinking":
            if not isinstance(block.get("thinking"), str):
                return False
            signature = block.get("signature")
            if signature is not None and not isinstance(signature, str):
                return False
        if kind == "tool_use":
            if not isinstance(block.get("id"), str) or not isinstance(block.get("name"), str):
                return False
            if not isinstance(block.get("input"), dict):
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
