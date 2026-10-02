"""A bounded, process-local cache for successfully synthesized audio."""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable


class AudioCache:
    """Thread-safe LRU cache bounded by payload bytes and absolute TTL.

    Cache hits refresh recency, but never extend expiration. A zero capacity or
    TTL disables caching. Callers should only insert successful provider results.
    """

    def __init__(
        self,
        max_bytes: int,
        ttl_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("Cache capacity must be a nonnegative integer")
        if not math.isfinite(ttl_seconds) or ttl_seconds < 0:
            raise ValueError("Cache TTL must be nonnegative and finite")
        self._max_bytes = max_bytes
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._size = 0

    def get(self, key: str) -> bytes | None:
        if not self._max_bytes or not self._ttl_seconds:
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, data = entry
            if expires_at <= self._clock():
                self._remove(key)
                return None
            self._entries.move_to_end(key)
            return data

    def put(self, key: str, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("Audio cache values must be immutable bytes")
        if not self._max_bytes or not self._ttl_seconds:
            return
        with self._lock:
            # Remove an older value even if the replacement cannot be cached.
            self._remove(key)
            if not data or len(data) > self._max_bytes:
                return
            now = self._clock()
            for expired_key in [
                item_key for item_key, (expires_at, _) in self._entries.items() if expires_at <= now
            ]:
                self._remove(expired_key)
            while self._size + len(data) > self._max_bytes:
                _, (_, evicted_data) = self._entries.popitem(last=False)
                self._size -= len(evicted_data)
            self._entries[key] = (now + self._ttl_seconds, data)
            self._size += len(data)

    def _remove(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._size -= len(entry[1])
