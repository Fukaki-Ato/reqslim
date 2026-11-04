"""Observation layer: what ZCode actually sends, and what it costs you.

The point of recording every request is not dashboards. It is that "reduce
requests" is otherwise unfalsifiable: you cannot tell a cache that never hits
from one that hits constantly, and you cannot see that 30% of your traffic is
the same tool turn replayed after an edit.

Every event carries the fingerprint components, so the report can separate:

* **duplicates**  - identical normalised request, answered from cache
* **storms**      - same request repeated within 60s (retry loop, not new work)
* **near-dupes**  - same final turn, different history (context bloat, not
  cacheable, but it tells you the agent is re-asking in a fatter context)
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Iterable

from .comparison import RequestComparison, compare_requests, is_local_hit

STORM_WINDOW = 60.0


@dataclass
class Event:
    ts: float
    key: str
    short: str
    tail: str
    kind: str
    model: str
    status: int
    latency: float
    est_tokens: int
    tokens_in: int
    tokens_out: int
    hit: str
    stream: bool
    n_messages: int
    tools: tuple[str, ...] = ()
    upstream: str = ""
    scope: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @property
    def group(self) -> tuple[str, str, str]:
        """Scope for duplicate/storm analysis.

        The cache, coalescing and failure memo are per gateway and per
        credential; the report must group by the same scope or it calls two
        separate conversations duplicates.
        """
        return (self.upstream, self.scope, self.key)


class Recorder:
    def __init__(
        self,
        path: str | None = None,
        max_bytes: int = 32 * 1024 * 1024,
        max_events: int | None = 50_000,
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        #: Bounded on purpose. The JSONL is the durable record; the in-memory
        #: copy only has to cover a report's worth of recent traffic. Left
        #: unbounded, a proxy that stays up for weeks grows without limit
        #: (~200 bytes per event, and the file rotation never touches memory).
        #: Offline reports pass ``None`` to read the whole file.
        self.events: deque[Event] = deque(maxlen=max_events)
        #: UTF-8 bytes written to the current file since its last rotation.
        self._counter = 0
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._rotate(path, max_bytes)
            try:
                self._counter = os.path.getsize(path)
            except OSError:
                self._counter = 0

    @staticmethod
    def _rotate(path: str, max_bytes: int, *, force: bool = False) -> bool:
        """Keep one previous log and start a fresh file.

        Returns True only when a rotation actually happened. ``force`` rotates
        even when the current file has not exceeded the cap yet; that is what
        keeps the next write from crossing it. Rotation never raises, because
        logging must not prevent the proxy from running.
        """
        if not max_bytes:
            return False
        try:
            if not force and os.path.getsize(path) <= max_bytes:
                return False
            os.replace(path, path + ".1")
            return True
        except OSError:
            return False

    def add(self, event: Event) -> None:
        blob = event.to_json() + "\n"
        blob_bytes = len(blob.encode("utf-8"))
        with self._lock:
            self.events.append(event)
            if not self.path:
                return
            try:
                if (
                    self.max_bytes
                    and self._counter
                    and self._counter + blob_bytes > self.max_bytes
                ):
                    if self._rotate(self.path, self.max_bytes, force=True):
                        self._counter = 0
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(blob)
                self._counter += blob_bytes
            except OSError:
                self.path = None  # never let logging break the proxy

    def clear(self) -> None:
        with self._lock:
            self.events.clear()

    # ---------------------------------------------------------------- analysis
    def summary(self) -> dict[str, Any]:
        with self._lock:
            events = list(self.events)
        comparison = compare_requests(events)
        if not events:
            return {
                "requests": 0,
                "upstream": 0,
                "hits": {},
                "hit_rate": 0.0,
                "tokens": {"in": 0, "out": 0, "saved_estimate": 0},
                "by_kind": {},
                "top_tools": {},
                "duplicates": 0,
                "storms": 0,
                "near_duplicates": 0,
                "avg_latency": 0.0,
                "comparison": comparison.to_dict(),
            }

        hits = Counter(comparison.by_mechanism)
        hit_total = comparison.reduced_requests
        kinds = Counter(event.kind for event in events)
        tools = Counter(name for event in events for name in event.tools)
        saved = sum(event.est_tokens for event in events if is_local_hit(event))

        by_key: dict[tuple[str, str, str], list[Event]] = defaultdict(list)
        for event in events:
            by_key[event.group].append(event)
        duplicates = sum(len(group) - 1 for group in by_key.values() if len(group) > 1)

        storms = 0
        for group in by_key.values():
            ordered = sorted(group, key=lambda item: item.ts)
            for previous, current in zip(ordered, ordered[1:]):
                if current.ts - previous.ts <= STORM_WINDOW:
                    storms += 1

        by_tail: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for event in events:
            by_tail[(event.upstream, event.scope, event.tail)].add(event.key)
        near = sum(len(keys) - 1 for keys in by_tail.values() if len(keys) > 1)

        return {
            "requests": comparison.estimated_requests,
            "upstream": comparison.actual_requests,
            "hits": dict(hits),
            "hit_rate": comparison.reduction_rate,
            "tokens": {
                "in": sum(event.tokens_in for event in events),
                "out": sum(event.tokens_out for event in events),
                "saved_estimate": saved,
            },
            "by_kind": dict(kinds),
            "top_tools": dict(tools.most_common(10)),
            "duplicates": duplicates,
            "storms": storms,
            "near_duplicates": near,
            "avg_latency": round(
                sum(event.latency for event in events if not is_local_hit(event))
                / max(1, comparison.actual_requests),
                2,
            ),
            "comparison": comparison.to_dict(),
        }

    def clusters(self, limit: int = 8) -> list[dict[str, Any]]:
        """The repeated requests worth looking at, biggest waste first."""
        with self._lock:
            events = list(self.events)
        grouped: dict[tuple[str, str, str], list[Event]] = defaultdict(list)
        for event in events:
            grouped[event.group].append(event)
        rows = []
        for (upstream, _scope, key), group in grouped.items():
            if len(group) < 2:
                continue
            rows.append(
                {
                    "key": key[:12],
                    "upstream": upstream,
                    "count": len(group),
                    "kind": group[0].kind,
                    "messages": group[0].n_messages,
                    "tokens": sum(event.est_tokens for event in group),
                    "hit": sum(1 for event in group if is_local_hit(event)),
                }
            )
        rows.sort(key=lambda row: (row["count"], row["tokens"]), reverse=True)
        return rows[:limit]

    def format_report(self) -> str:
        s = self.summary()
        comparison = s["comparison"]
        lines = RequestComparison(
            estimated_requests=comparison["estimated_requests"],
            actual_requests=comparison["actual_requests"],
            reduced_requests=comparison["reduced_requests"],
            reduction_rate=comparison["reduction_rate"],
            by_mechanism=comparison["by_mechanism"],
        ).format_lines()
        lines.extend(
            [
                "",
                f"requests        {s['requests']}",
                f"upstream calls   {s['upstream']}",
                f"cache hits       {sum(s['hits'].values())}  ({s['hit_rate']}%)",
            ]
        )
        if s["hits"]:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(s["hits"].items()))
            lines.append(f"  by level       {detail}")
        lines.append(f"tokens in/out    {s['tokens']['in']} / {s['tokens']['out']}")
        lines.append(f"tokens avoided   ~{s['tokens']['saved_estimate']}")
        if s["by_kind"]:
            lines.append(f"request kinds    {s['by_kind']}")
        if s["top_tools"]:
            lines.append(f"tools called     {s['top_tools']}")
        lines.append(f"duplicates       {s['duplicates']}   storms {s['storms']}   near-dupes {s['near_duplicates']}")
        clusters = self.clusters(5)
        if clusters:
            lines.append("")
            lines.append("top repeated requests")
            for row in clusters:
                where = f" @ {row['upstream']}" if row["upstream"] else ""
                lines.append(
                    f"  {row['key']}  x{row['count']:<3} {row['kind']:<12} "
                    f"msgs={row['messages']:<3} hits={row['hit']}{where}"
                )
        return "\n".join(lines)


_EVENT_FIELDS = {item.name for item in fields(Event)}


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (TypeError, OverflowError):
        return False


def _event_from_payload(payload: dict[str, Any]) -> Event | None:
    """Validate/coerce one JSONL record, or skip it.

    The durable log is written by this process, but it can also be edited,
    truncated by a crash, or left over from an older version.  One bad record
    must not make ``--report`` unusable, and a record with the wrong types
    must not poison the counters (``sum(None)``, ``sorted`` on strings, ...).
    """
    data = {key: value for key, value in payload.items() if key in _EVENT_FIELDS}
    for name in ("key", "short", "tail", "kind", "model"):
        if not isinstance(data.get(name), str):
            return None
    if not all(_finite_number(data.get(name)) for name in ("ts", "latency")):
        return None
    for name in ("est_tokens", "tokens_in", "tokens_out", "n_messages", "status"):
        value = data.get(name)
        if not _finite_number(value):
            return None
        try:
            data[name] = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if not isinstance(data.get("stream"), bool):
        return None

    tools = data.get("tools", ())
    if tools is None:
        tools = ()
    if not isinstance(tools, (list, tuple)):
        return None
    if any(not isinstance(tool, str) for tool in tools):
        return None
    data["tools"] = tuple(tools)

    for name in ("hit", "upstream", "scope"):
        value = data.get(name, "")
        # A malformed hit must stay in the denominator as an upstream call,
        # not disappear from the report and inflate the reduction rate.
        data[name] = value if isinstance(value, str) else ""
    try:
        return Event(**data)
    except (TypeError, ValueError):
        return None


def load_events(path: str) -> Recorder:
    """Load a JSONL record for offline reporting, keeping every valid line."""
    recorder = Recorder(path=None, max_events=None)
    if not os.path.exists(path):
        return recorder
    # A crash can leave a truncated multibyte sequence at the tail; replace it
    # and let the JSON parser reject that line instead of aborting the report.
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            event = _event_from_payload(payload)
            if event is not None:
                recorder.events.append(event)
    return recorder
