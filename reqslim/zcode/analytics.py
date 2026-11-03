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
import os
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

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

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class Recorder:
    def __init__(
        self,
        path: str | None = None,
        max_bytes: int = 32 * 1024 * 1024,
        max_events: int = 50_000,
    ) -> None:
        self.path = path
        self._lock = threading.Lock()
        #: Bounded on purpose. The JSONL is the durable record; the in-memory
        #: copy only has to cover a report's worth of recent traffic. Left
        #: unbounded, a proxy that stays up for weeks grows without limit
        #: (~200 bytes per event, and the file rotation never touches memory).
        self.events: deque[Event] = deque(maxlen=max_events)
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._rotate(path, max_bytes)

    @staticmethod
    def _rotate(path: str, max_bytes: int) -> None:
        """Keep one previous log and start a fresh file.

        The proxy is meant to stay up for weeks, so an unbounded append-only
        file is a slow leak: it eventually costs minutes in ``--report`` and
        hundreds of MB on disk. Rotating at startup (never mid-session) keeps
        one run's events together and its report coherent.
        """
        if not max_bytes:
            return
        try:
            if os.path.getsize(path) <= max_bytes:
                return
            os.replace(path, path + ".1")
        except OSError:
            pass  # logging must never prevent the proxy from starting

    def add(self, event: Event) -> None:
        with self._lock:
            self.events.append(event)
            if self.path:
                try:
                    with open(self.path, "a", encoding="utf-8") as handle:
                        handle.write(event.to_json() + "\n")
                except OSError:
                    self.path = None  # never let logging break the proxy

    def clear(self) -> None:
        with self._lock:
            self.events.clear()

    # ---------------------------------------------------------------- analysis
    def summary(self) -> dict[str, Any]:
        with self._lock:
            events = list(self.events)
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
            }

        hits = Counter(event.hit for event in events if event.hit)
        hit_total = sum(hits.values())
        kinds = Counter(event.kind for event in events)
        tools = Counter(name for event in events for name in event.tools)
        saved = sum(event.est_tokens for event in events if event.hit)

        by_key: dict[str, list[Event]] = defaultdict(list)
        for event in events:
            by_key[event.key].append(event)
        duplicates = sum(len(group) - 1 for group in by_key.values() if len(group) > 1)

        storms = 0
        for group in by_key.values():
            ordered = sorted(group, key=lambda item: item.ts)
            for previous, current in zip(ordered, ordered[1:]):
                if current.ts - previous.ts <= STORM_WINDOW:
                    storms += 1

        by_tail: dict[str, set[str]] = defaultdict(set)
        for event in events:
            by_tail[event.tail].add(event.key)
        near = sum(len(keys) - 1 for keys in by_tail.values() if len(keys) > 1)

        return {
            "requests": len(events),
            "upstream": len(events) - hit_total,
            "hits": dict(hits),
            "hit_rate": round(hit_total / len(events) * 100, 1),
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
                sum(event.latency for event in events if not event.hit) / max(1, len(events) - hit_total),
                2,
            ),
        }

    def clusters(self, limit: int = 8) -> list[dict[str, Any]]:
        """The repeated requests worth looking at, biggest waste first."""
        with self._lock:
            events = list(self.events)
        grouped: dict[str, list[Event]] = defaultdict(list)
        for event in events:
            grouped[event.key].append(event)
        rows = []
        for key, group in grouped.items():
            if len(group) < 2:
                continue
            rows.append(
                {
                    "key": key[:12],
                    "count": len(group),
                    "kind": group[0].kind,
                    "messages": group[0].n_messages,
                    "tokens": sum(event.est_tokens for event in group),
                    "hit": sum(1 for event in group if event.hit),
                }
            )
        rows.sort(key=lambda row: (row["count"], row["tokens"]), reverse=True)
        return rows[:limit]

    def format_report(self) -> str:
        s = self.summary()
        lines = [
            f"requests        {s['requests']}",
            f"upstream calls   {s['upstream']}",
            f"cache hits       {sum(s['hits'].values())}  ({s['hit_rate']}%)",
        ]
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
                lines.append(
                    f"  {row['key']}  x{row['count']:<3} {row['kind']:<12} "
                    f"msgs={row['messages']:<3} hits={row['hit']}"
                )
        return "\n".join(lines)


def load_events(path: str) -> Recorder:
    recorder = Recorder(path=None)
    if not os.path.exists(path):
        return recorder
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload["tools"] = tuple(payload.get("tools") or ())
            try:
                recorder.events.append(Event(**payload))
            except TypeError:
                continue
    return recorder
