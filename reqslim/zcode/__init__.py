"""ZCode-specific request reduction.

Import surface:

    from reqslim.zcode import ZCodeProxy, fingerprint, normalize_request

Run the proxy:

    python -m reqslim.zcode --upstream https://open.bigmodel.cn/api/anthropic

Read the traffic report:

    python -m reqslim.zcode --report %USERPROFILE%\\.zcode\\reqslim\\requests.jsonl
"""

from __future__ import annotations

from .analytics import Event, Recorder, load_events
from .normalize import (
    Fingerprint,
    classify,
    fingerprint,
    normalize_request,
    system_text,
    tool_names,
)
from .proxy import ZCodeProxy
from .stream import Assembler, has_thinking, replay_sse, tool_uses

__all__ = [
    "Assembler",
    "Event",
    "Fingerprint",
    "Recorder",
    "ZCodeProxy",
    "classify",
    "fingerprint",
    "has_thinking",
    "load_events",
    "normalize_request",
    "replay_sse",
    "system_text",
    "tool_names",
    "tool_uses",
]
