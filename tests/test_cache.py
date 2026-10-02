from concurrent.futures import ThreadPoolExecutor

import pytest

from speech_proxy.cache import AudioCache


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_cache_eviction_uses_byte_budget_and_lru():
    cache = AudioCache(max_bytes=10, ttl_seconds=60)
    cache.put("a", b"aaaa")
    cache.put("b", b"bbbb")
    assert cache.get("a") == b"aaaa"
    cache.put("c", b"cccc")
    assert cache.get("b") is None
    assert cache.get("a") == b"aaaa"
    assert cache.get("c") == b"cccc"


def test_hits_do_not_extend_absolute_ttl():
    clock = Clock()
    cache = AudioCache(100, 60, clock=clock)
    cache.put("audio", b"sound")
    clock.now += 59
    assert cache.get("audio") == b"sound"
    clock.now += 1
    assert cache.get("audio") is None


def test_expired_entries_are_removed_before_live_lru_eviction():
    clock = Clock()
    cache = AudioCache(8, 60, clock=clock)
    cache.put("expired", b"old!")
    clock.now += 30
    cache.put("live", b"live")
    # Accessing the old value makes it most recent, but not less expired.
    assert cache.get("expired") == b"old!"
    clock.now += 30
    cache.put("new", b"new!")
    assert cache.get("live") == b"live"
    assert cache.get("new") == b"new!"
    assert cache.get("expired") is None


def test_replacement_updates_size_and_resets_ttl():
    clock = Clock()
    cache = AudioCache(8, 60, clock=clock)
    cache.put("a", b"12345678")
    clock.now += 30
    cache.put("a", b"12")
    cache.put("b", b"345678")
    clock.now += 30
    assert cache.get("a") == b"12"
    assert cache.get("b") == b"345678"
    clock.now += 30
    assert cache.get("a") is None


def test_oversized_and_empty_values_are_not_cached():
    cache = AudioCache(5, 60)
    cache.put("a", b"abc")
    cache.put("huge", b"abcdef")
    cache.put("empty", b"")
    assert cache.get("a") == b"abc"
    assert cache.get("huge") is None
    assert cache.get("empty") is None
    cache.put("a", b"abcdef")
    assert cache.get("a") is None


@pytest.mark.parametrize("max_bytes,ttl_seconds", [(0, 60), (100, 0)])
def test_zero_capacity_or_ttl_disables_cache(max_bytes, ttl_seconds):
    cache = AudioCache(max_bytes, ttl_seconds)
    cache.put("a", b"audio")
    assert cache.get("a") is None


def test_concurrent_reads_and_writes_preserve_budget():
    cache = AudioCache(100, 60)

    def exchange(worker):
        key = str(worker)
        data = bytes([worker]) * 10
        for _ in range(50):
            cache.put(key, data)
            result = cache.get(key)
            assert result is None or result == data

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(exchange, range(16)))
    retained = [cache.get(str(worker)) for worker in range(16)]
    assert sum(len(value) for value in retained if value is not None) <= 100


@pytest.mark.parametrize(
    "max_bytes,ttl_seconds", [(-1, 10), (1.5, 10), (10, -1), (10, float("inf"))]
)
def test_invalid_configuration_is_rejected(max_bytes, ttl_seconds):
    with pytest.raises(ValueError):
        AudioCache(max_bytes, ttl_seconds)


def test_mutable_payload_is_rejected():
    cache = AudioCache(100, 60)
    with pytest.raises(TypeError):
        cache.put("a", bytearray(b"audio"))
