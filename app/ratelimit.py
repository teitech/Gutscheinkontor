"""Einfaches Rate-Limiting im Arbeitsspeicher.

Gilt nur für einen Prozess. Die App läuft bewusst mit genau einem Worker.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict

_MAX_KEYS = 10_000


class RateLimiter:
    def __init__(self) -> None:
        self._events: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, window: float, now: float) -> Deque[float]:
        events = self._events[key]
        while events and events[0] <= now - window:
            events.popleft()
        return events

    def blocked(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._prune(key, window, now)
            if not events:
                self._events.pop(key, None)
            return len(events) >= limit

    def add(self, key: str, window: float) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._events) > _MAX_KEYS:
                for k in list(self._events):
                    self._prune(k, window, now)
                    if not self._events[k]:
                        del self._events[k]
            self._events[key].append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def reset_all(self) -> None:
        with self._lock:
            self._events.clear()


limiter = RateLimiter()
