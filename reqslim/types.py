"""Core value objects: a response and its token usage.

Plain data, no framework dependency. There is deliberately no request type on
the ZCode path: requests arrive as raw JSON from the client and are
canonicalised in ``zcode.normalize``, so a half-modelled request object would
only invite drift between the two representations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMResponse:
    """A model answer plus provenance, which is what makes savings auditable."""

    content: str
    raw: Any = None
    usage: Usage = field(default_factory=Usage)
    cached: bool = False
    source: str = "llm"
    meta: dict[str, Any] = field(default_factory=dict)
