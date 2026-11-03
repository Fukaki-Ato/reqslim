"""ZCode request canonicalisation.

Why this exists
---------------
A naive "hash the body" cache almost never hits inside a coding agent, because
ZCode rebuilds the request on every turn and three things churn without changing
the meaning:

1. **``cache_control`` markers.** ZCode places ephemeral-cache breakpoints on
   whichever blocks it wants cached. When a new turn arrives the breakpoints
   move, so the body changes byte-for-byte while the conversation is identical.
   Verified present in the shipped runtime (52 references in ``zcode.cjs``).
2. **``tool_use.id`` / ``tool_result.tool_use_id``.** Freshly minted ids per
   session. They are opaque handles; the model never reasons about them, and
   tool results are paired positionally, so they can be dropped from the key.
3. **Tool array order.** ZCode assembles its tool list from several registries
   (builtin, skills, MCP). The order is stable within a session but not across
   restarts or plugin reloads.

Stripping exactly these three makes two genuinely-equivalent requests collide,
which is what turns a useless cache into a useful one. Everything else -- text,
file contents, tool inputs, thinking budget -- stays in the key, because those
do change the answer.

This module never mutates the caller's payload: normalisation produces a
separate canonical copy that is only ever used for hashing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

#: Top-level keys that change between otherwise identical requests.
VOLATILE_TOP = frozenset(
    {
        "stream",
        "metadata",
        "client_metadata",
        "request_id",
        "user",
        "spend_logs_metadata",
    }
)

#: Fields that are transport or billing hints, not prompt content.
VOLATILE_ANYWHERE = frozenset({"cache_control", "cache_creation", "ephemeral_5m"})

#: Anchored to the wording the shipped ZCode runtime actually sends (verified
#: against ``zcode.cjs``): the compaction instruction is "Your task is to create
#: a detailed summary of the conversation so far" plus the answer-shape marker
#: "<analysis>", and both phrases appear nowhere else in the runtime. The old
#: bare-word "compact" alternative never matched a real compaction request and
#: would false-positive on any provider whose system prompt merely mentions
#: compaction.
_COMPACTION_HINT = re.compile(
    r"(summary of the conversation|summari[sz]e the conversation|"
    r"conversation summary|<analysis>)",
    re.IGNORECASE,
)


def _scrub(value: Any) -> Any:
    """Deep copy dropping volatile fields, so the caller's payload is untouched."""
    if isinstance(value, Mapping):
        return {
            key: _scrub(item)
            for key, item in value.items()
            if key not in VOLATILE_ANYWHERE
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def system_text(system: Any) -> str:
    """ZCode sometimes sends a string, sometimes a list of text blocks."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    parts: list[str] = []
    for block in system:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def canonical_tools(tools: Any) -> list[dict[str, Any]]:
    if not tools:
        return []
    cleaned = [_scrub(tool) for tool in tools if isinstance(tool, Mapping)]
    cleaned.sort(key=lambda tool: str(tool.get("name", "")))
    return cleaned


def canonical_messages(messages: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        content = message.get("content")
        blocks: list[Any]
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = []
            for block in content:
                if not isinstance(block, Mapping):
                    blocks.append(block)
                    continue
                clean = _scrub(block)
                kind = clean.get("type")
                if kind == "tool_use":
                    clean.pop("id", None)
                elif kind == "tool_result":
                    # ``_scrub`` above already recursed through the nested
                    # content, so cache_control inside it is gone too.
                    clean.pop("tool_use_id", None)
                blocks.append(clean)
        else:
            blocks = []
        out.append({"role": role, "content": blocks})
    return out


def normalize_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical form used for cache keys."""
    canonical: dict[str, Any] = {}
    for key, value in payload.items():
        if key in VOLATILE_TOP:
            continue
        canonical[key] = _scrub(value)
    canonical["system"] = system_text(payload.get("system"))
    canonical["tools"] = canonical_tools(payload.get("tools"))
    canonical["messages"] = canonical_messages(payload.get("messages"))
    return canonical


def _digest(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Fingerprint:
    """Cache key plus the components needed to explain a miss."""

    key: str
    system: str
    tools: str
    history: str
    tail: str
    n_messages: int
    kind: str

    @property
    def short(self) -> str:
        return self.key[:12]


def _compaction_text(payload: Mapping[str, Any]) -> str:
    """Where the compaction instruction can ride: the system prompt, or the
    final message. The shipped runtime injects its "create a detailed summary
    of the conversation" prompt through a request-shaping step that may land it
    in either place, so both are searched -- text blocks only, since a
    tool_result payload is content, not an instruction.
    """
    parts = [system_text(payload.get("system"))]
    messages = payload.get("messages") or []
    if messages:
        last = messages[-1]
        content = last.get("content") if isinstance(last, Mapping) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    return "\n".join(parts)


def classify(payload: Mapping[str, Any]) -> str:
    """Cheap shape-based label for analytics.

    ``compaction`` is a heuristic on the request's instruction text: ZCode
    compacts the conversation when it crosses ``autoCompactThresholdTokens``,
    and those calls are large, unique and impossible to cache -- worth counting
    separately so they do not pollute the hit-rate number.
    """
    messages = payload.get("messages") or []
    if _COMPACTION_HINT.search(_compaction_text(payload)):
        return "compaction"
    if not messages:
        return "empty"
    last = messages[-1]
    content = last.get("content") if isinstance(last, Mapping) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_result":
                return "tool_turn"
    if len(messages) == 1:
        return "first_turn"
    return "continuation"


def fingerprint(payload: Mapping[str, Any]) -> Fingerprint:
    canonical = normalize_request(payload)
    messages = canonical.get("messages") or []
    history = messages[:-1]
    tail = messages[-1] if messages else []
    return Fingerprint(
        key=_digest(canonical),
        system=_digest(canonical.get("system")),
        tools=_digest(canonical.get("tools")),
        history=_digest(history),
        tail=_digest(tail),
        n_messages=len(messages),
        kind=classify(payload),
    )


def tool_names(payload: Mapping[str, Any]) -> tuple[str, ...]:
    names = []
    for tool in payload.get("tools") or []:
        if isinstance(tool, Mapping) and isinstance(tool.get("name"), str):
            names.append(tool["name"])
    return tuple(sorted(names))
