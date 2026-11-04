"""A bounded, TTL'd response cache.

One cache, not three. The proxy only ever needs "same key in, same answer out",
and it only ever stores a *finished* message keyed by a canonical request. A
semantic cache would have to guess whether two prompts mean the same thing;
a proxy that guesses is worse than a proxy that misses, because a wrong hit is
silent and expensive.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from copy import deepcopy

from .types import LLMResponse


class ExactCache:
    """LRU with a TTL and a byte budget.

    Hits are returned as copies tagged with their source. The budget bounds
    *response* bytes: the entry cap alone cannot, because one entry's size is
    whatever the model chose to answer with -- 4096 large thinking responses
    would otherwise sit in memory for the life of the process.
    """

    def __init__(
        self,
        maxsize: int = 2048,
        ttl: float | None = 3600.0,
        max_bytes: int | None = None,
    ) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[float, LLMResponse, int]] = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> LLMResponse | None:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if item is not None:
                ts, response, size = item
                if self.ttl is None or now - ts <= self.ttl:
                    self._data.move_to_end(key)
                    self.hits += 1
                    return _as_hit(response)
                self._data.pop(key, None)
                self._bytes -= size
            self.misses += 1
        return None

    def put(self, key: str, response: LLMResponse) -> bool:
        stored = deepcopy(response)
        size = len(stored.content.encode("utf-8"))
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._bytes -= old[2]
            if self.ttl is not None and self.ttl <= 0:
                return False
            self._data[key] = (time.time(), stored, size)
            self._bytes += size
            self._data.move_to_end(key)
            while self._data and (
                len(self._data) > self.maxsize
                or (self.max_bytes is not None and self._bytes > self.max_bytes)
            ):
                _, entry = self._data.popitem(last=False)
                self._bytes -= entry[2]
            return key in self._data

    def size(self) -> int:
        """Entry count, so callers do not have to reach into ``_data``."""
        with self._lock:
            return len(self._data)


def _as_hit(response: LLMResponse) -> LLMResponse:
    hit = deepcopy(response)
    hit.cached = True
    hit.source = "exact_cache"
    return hit
