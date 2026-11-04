"""Request-count comparison: what would have been sent vs what was sent.

ZCode can emit the same logical request several times (retries, fan-out,
sub-agents, a cache breakpoint moving).  Every such attempt is recorded as an
``Event``.  An event with a known ``hit`` level was answered by reqslim
(raw/normalized cache, in-flight coalescing, or the failure memo); an empty,
unknown or malformed level reached the upstream.

The comparison therefore has one unambiguous meaning:

* ``estimated_requests`` - all logical attempts ZCode made, i.e. what the
  upstream would have received without the proxy;
* ``actual_requests`` - attempts that consumed an upstream call;
* ``reduced_requests`` - the difference, broken down by mechanism.

This module is deliberately pure: it receives any iterable of event-like
objects with a ``hit`` attribute, so the accounting can be tested without a
server, a recorder, or a live upstream.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

#: Every mechanism the proxy currently records.  A historical/corrupt record
#: with any other label is an upstream call, not a saving.
MECHANISMS = frozenset({"raw", "normalized", "coalesced", "failure_memo"})


@dataclass(frozen=True)
class RequestComparison:
    """The request-count comparison shown in stats and reports."""

    estimated_requests: int
    actual_requests: int
    reduced_requests: int
    reduction_rate: float
    by_mechanism: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "estimated_requests": self.estimated_requests,
            "actual_requests": self.actual_requests,
            "reduced_requests": self.reduced_requests,
            "reduction_rate": self.reduction_rate,
            "by_mechanism": dict(self.by_mechanism),
        }

    def format_lines(self) -> list[str]:
        lines = [
            "request reduction",
            f"  estimated requests  {self.estimated_requests}",
            f"  actual requests     {self.actual_requests}",
            f"  reduced             {self.reduced_requests}  ({self.reduction_rate}%)",
        ]
        if self.by_mechanism:
            detail = ", ".join(
                f"{name}={count}" for name, count in sorted(self.by_mechanism.items())
            )
            lines.append(f"  by mechanism        {detail}")
        return lines


def is_local_hit(event: Any) -> bool:
    """Whether an event was answered locally with a known mechanism label."""
    hit = getattr(event, "hit", "")
    return isinstance(hit, str) and hit in MECHANISMS


def compare_requests(events: Iterable[Any]) -> RequestComparison:
    """Compare logical attempts with upstream calls.

    A known mechanism in ``hit`` means reqslim answered the attempt locally.
    Empty, unknown or malformed hit values are treated as upstream calls
    rather than silently claiming a saving: under-reporting a reduction is
    safer than inflating it.
    """
    estimated = 0
    actual = 0
    mechanisms: Counter[str] = Counter()
    for event in events:
        estimated += 1
        if is_local_hit(event):
            mechanisms[event.hit] += 1
        else:
            actual += 1
    reduced = estimated - actual
    rate = round(reduced / estimated * 100, 1) if estimated else 0.0
    return RequestComparison(
        estimated_requests=estimated,
        actual_requests=actual,
        reduced_requests=reduced,
        reduction_rate=rate,
        by_mechanism=dict(mechanisms),
    )
