"""Small TTL+LRU caches used to avoid paying for identical work twice."""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Any, Generic, TypeVar

T = TypeVar("T")

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", (text or "").strip().casefold())


class TTLCache(Generic[T]):
    def __init__(self, maxsize: int = 512, ttl: float = 600.0) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self.hits = 0
        self.misses = 0
        self._items: OrderedDict[Any, tuple[float, T]] = OrderedDict()

    def get(self, key: Any) -> T | None:
        entry = self._items.get(key)
        if entry is None:
            self.misses += 1
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at > self.ttl:
            self._items.pop(key, None)
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return value

    def set(self, key: Any, value: T) -> None:
        if self.ttl <= 0 or self.maxsize <= 0:
            return
        self._items[key] = (time.monotonic(), value)
        self._items.move_to_end(key)
        while len(self._items) > self.maxsize:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 3) if total else 0.0

    def __len__(self) -> int:
        return len(self._items)
