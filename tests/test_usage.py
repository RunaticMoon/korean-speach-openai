import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from speech_proxy.errors import APIError
from speech_proxy.usage import MIN_RETENTION_SECONDS, UsageLimit, UsageStore


class Clock:
    def __init__(self, now=10_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_reservation_survives_restart_and_provider_failure(tmp_path):
    path = tmp_path / "nested" / "usage.sqlite3"
    clock = Clock()
    limit = UsageLimit("daily_characters", 86400, max_amount=10)
    store = UsageStore(path, clock=clock)
    store.reserve("google", 7, [limit])
    # A provider timeout after reservation has no refund operation.
    store.close()
    reopened = UsageStore(path, clock=clock)
    try:
        with pytest.raises(APIError) as denied:
            reopened.reserve("google", 4, [limit])
        assert denied.value.status == 429
        assert denied.value.code == "local_quota_exceeded"
        totals = reopened.snapshot({"google": [limit]})["google"][limit.name]
        assert totals["amount"] == 7
        assert totals["requests"] == 1
        assert totals["remaining_amount"] == 3
        assert totals["remaining_requests"] is None
    finally:
        reopened.close()


def test_rolling_window_expires_at_boundary_and_retry_after_is_precise(tmp_path):
    clock = Clock()
    store = UsageStore(tmp_path / "usage.sqlite3", clock=clock)
    limit = UsageLimit("minute", 60, max_amount=10, max_requests=2)
    try:
        store.reserve("groq", 3, [limit])
        clock.now += 10
        store.reserve("groq", 5, [limit])
        clock.now += 10.25
        with pytest.raises(APIError) as denied:
            store.reserve("groq", 8, [limit])
        # Both existing amounts must expire, so expiry of the second record wins.
        assert denied.value.headers["Retry-After"] == "50"
        clock.now += 39.75
        totals = store.snapshot({"groq": [limit]})["groq"]["minute"]
        assert totals["amount"] == 5
        assert totals["requests"] == 1
        store.reserve("groq", 5, [limit])
    finally:
        store.close()


def test_all_windows_are_checked_atomically(tmp_path):
    clock = Clock()
    store = UsageStore(tmp_path / "usage.sqlite3", clock=clock)
    limits = [
        UsageLimit("minute", 60, max_requests=2),
        UsageLimit("day", 86400, max_amount=10),
    ]
    try:
        store.reserve("groq", 8, limits)
        with pytest.raises(APIError) as denied:
            store.reserve("groq", 3, limits)
        assert denied.value.headers["Retry-After"] == "86400"
        store.reserve("groq", 2, limits)
        snapshot = store.snapshot({"groq": limits})["groq"]
        assert snapshot["minute"]["requests"] == 2
        assert snapshot["day"]["amount"] == 10
        store.reserve("google", 10, limits)
        assert store.snapshot({"google": limits})["google"]["day"]["amount"] == 10
    finally:
        store.close()


def test_retry_after_honors_longest_blocking_window(tmp_path):
    clock = Clock()
    store = UsageStore(tmp_path / "usage.sqlite3", clock=clock)
    limits = [
        UsageLimit("minute", 60, max_requests=1),
        UsageLimit("day", 86400, max_amount=10),
    ]
    try:
        store.reserve("groq", 10, limits)
        clock.now += 15.5
        with pytest.raises(APIError) as denied:
            store.reserve("groq", 1, limits)
        assert denied.value.headers["Retry-After"] == "86385"
    finally:
        store.close()


@pytest.mark.parametrize(
    "limit",
    [
        UsageLimit("disabled_amount", 60, max_amount=0),
        UsageLimit("disabled_requests", 60, max_requests=0),
    ],
)
def test_zero_limit_disables_provider_even_for_zero_amount(tmp_path, limit):
    store = UsageStore(tmp_path / "usage.sqlite3")
    try:
        with pytest.raises(APIError) as denied:
            store.reserve("google", 0, [limit])
        assert denied.value.status == 429
        assert store.snapshot({"google": [limit]})["google"][limit.name]["requests"] == 0
    finally:
        store.close()


def test_concurrent_store_instances_cannot_overshoot(tmp_path):
    path = tmp_path / "usage.sqlite3"
    clock = Clock()
    limit = UsageLimit("day", 86400, max_amount=75, max_requests=10)
    stores = [UsageStore(path, clock=clock) for _ in range(8)]
    barrier = threading.Barrier(len(stores))

    def reserve_many(store):
        accepted = 0
        barrier.wait()
        for _ in range(10):
            try:
                store.reserve("google", 10, [limit])
                accepted += 1
            except APIError as exc:
                assert exc.status == 429
        return accepted

    try:
        with ThreadPoolExecutor(max_workers=len(stores)) as pool:
            accepted = sum(pool.map(reserve_many, stores))
        assert accepted == 7
        totals = stores[0].snapshot({"google": [limit]})["google"]["day"]
        assert totals["amount"] == 70
        assert totals["requests"] == 7
    finally:
        for store in stores:
            store.close()


def test_retention_preserves_other_provider_long_window(tmp_path):
    clock = Clock()
    path = tmp_path / "usage.sqlite3"
    store = UsageStore(path, clock=clock)
    month = UsageLimit("month", MIN_RETENTION_SECONDS, max_amount=100)
    minute = UsageLimit("minute", 60, max_requests=20)
    try:
        store.reserve("google", 80, [month])
        clock.now += 31 * 86400
        store.reserve("groq", 10, [minute])
        assert store.snapshot({"google": [month]})["google"]["month"]["amount"] == 80
        clock.now += 86400
        assert store.snapshot({"google": [month]})["google"]["month"]["amount"] == 0
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 1
    finally:
        store.close()


def test_registered_retention_longer_than_minimum_survives_other_instance(tmp_path):
    clock = Clock()
    path = tmp_path / "usage.sqlite3"
    long = UsageLimit("long", 60 * 86400, max_amount=100)
    short = UsageLimit("short", 60, max_requests=20)
    first = UsageStore(path, clock=clock)
    second = UsageStore(path, clock=clock)
    try:
        first.reserve("google", 80, [long])
        clock.now += 40 * 86400
        second.reserve("groq", 10, [short])
        assert first.snapshot({"google": [long]})["google"]["long"]["amount"] == 80
    finally:
        first.close()
        second.close()


def test_clock_rollback_counts_future_reservations_conservatively(tmp_path):
    clock = Clock()
    store = UsageStore(tmp_path / "usage.sqlite3", clock=clock)
    limit = UsageLimit("minute", 60, max_requests=1)
    try:
        store.reserve("groq", 10, [limit])
        clock.now -= 5
        with pytest.raises(APIError) as denied:
            store.reserve("groq", 10, [limit])
        assert denied.value.headers["Retry-After"] == "65"
    finally:
        store.close()


def test_database_lock_fails_closed_and_recovers(tmp_path):
    path = tmp_path / "usage.sqlite3"
    store = UsageStore(path, busy_timeout_seconds=0.01)
    limit = UsageLimit("minute", 60, max_requests=10)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(APIError) as denied:
            store.reserve("groq", 10, [limit])
        assert denied.value.status == 503
        assert denied.value.code == "usage_store_unavailable"
        connection.execute("ROLLBACK")
        store.reserve("groq", 10, [limit])
        assert store.snapshot({"groq": [limit]})["groq"]["minute"]["requests"] == 1
    finally:
        connection.close()
        store.close()


def test_in_memory_store_and_unlimited_windows():
    store = UsageStore(":memory:")
    limit = UsageLimit("unlimited", 60)
    try:
        store.reserve("google", 10, [])
        result = store.snapshot({"google": [limit]})["google"]["unlimited"]
        assert result["amount"] == 10
        assert result["remaining_amount"] is None
        assert result["remaining_requests"] is None
    finally:
        store.close()


def test_upgrade_imports_original_tts_reservations_once(tmp_path):
    clock = Clock()
    path = tmp_path / "usage.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE reservations (id INTEGER PRIMARY KEY, ts REAL, units INTEGER)"
        )
        connection.executemany(
            "INSERT INTO reservations(ts, units) VALUES (?, ?)",
            [(clock.now, 80), (clock.now - MIN_RETENTION_SECONDS - 1, 20)],
        )
    limit = UsageLimit("month", MIN_RETENTION_SECONDS, max_amount=100)
    first = UsageStore(path, clock=clock)
    second = UsageStore(path, clock=clock)
    try:
        with pytest.raises(APIError) as denied:
            first.reserve("google_tts", 30, [limit])
        assert denied.value.status == 429
        first.reserve("google_tts", 10, [limit])
        totals = second.snapshot({"google_tts": [limit]})["google_tts"]["month"]
        assert totals["amount"] == 90
        assert totals["requests"] == 2
    finally:
        first.close()
        second.close()
    reopened = UsageStore(path, clock=clock)
    try:
        assert reopened.snapshot({"google_tts": [limit]})["google_tts"]["month"]["amount"] == 90
    finally:
        reopened.close()


def test_concurrent_startup_imports_legacy_records_once(tmp_path):
    clock = Clock()
    path = tmp_path / "usage.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE reservations (id INTEGER PRIMARY KEY, ts REAL, units INTEGER)"
        )
        connection.execute("INSERT INTO reservations(ts, units) VALUES (?, ?)", (clock.now, 80))
    barrier = threading.Barrier(8)
    limit = UsageLimit("month", MIN_RETENTION_SECONDS, max_amount=100)

    def start_worker(_):
        barrier.wait()
        store = UsageStore(path, clock=clock)
        try:
            return store.snapshot({"google_tts": [limit]})["google_tts"]["month"]["amount"]
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(start_worker, range(8))) == [80] * 8


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "", "window_seconds": 60},
        {"name": "bad", "window_seconds": 0},
        {"name": "bad", "window_seconds": 1.5},
        {"name": "bad", "window_seconds": 60, "max_amount": -1},
        {"name": "bad", "window_seconds": 60, "max_requests": True},
    ],
)
def test_invalid_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        UsageLimit(**kwargs)
