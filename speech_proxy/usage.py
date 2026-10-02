"""Durable, atomic reservations for locally enforced provider quotas.

A reservation is a charge against every configured rolling window. It is never
refunded: after a provider request fails, its billing outcome may be unknown.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import APIError

MIN_RETENTION_SECONDS = 32 * 24 * 60 * 60


@dataclass(frozen=True)
class UsageLimit:
    name: str
    window_seconds: int
    max_amount: int | None = None
    max_requests: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Usage limit names must not be empty")
        if type(self.window_seconds) is not int or self.window_seconds <= 0:
            raise ValueError("Usage windows must be positive integer seconds")
        for value in (self.max_amount, self.max_requests):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Usage limits must be nonnegative integers or None")


class UsageStore:
    """SQLite accounting shared safely by threads, processes, and restarts.

    ``amount`` is a provider-specific integer unit (characters or reserved audio
    seconds). A zero maximum disables that provider for the given window. A
    ``None`` maximum leaves that metric unlimited.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        if not math.isfinite(busy_timeout_seconds) or busy_timeout_seconds <= 0:
            raise ValueError("SQLite busy timeout must be positive and finite")
        database_path = str(path)
        if database_path != ":memory:":
            database = Path(database_path).expanduser()
            database.parent.mkdir(parents=True, exist_ok=True)
            database_path = str(database)
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            database_path,
            timeout=busy_timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS usage_events ("
                "id INTEGER PRIMARY KEY, provider TEXT NOT NULL, "
                "reserved_at REAL NOT NULL, amount INTEGER NOT NULL CHECK(amount >= 0))"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS usage_provider_time "
                "ON usage_events(provider, reserved_at)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS usage_time ON usage_events(reserved_at)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS usage_metadata "
                "(name TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO usage_metadata(name, value) VALUES (?, ?)",
                ("retention_seconds", MIN_RETENTION_SECONDS),
            )
            self._migrate_legacy_reservations()
        except BaseException:
            self._connection.close()
            raise

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def reserve(self, provider: str, amount: int, limits: Sequence[UsageLimit]) -> None:
        """Reserve atomically before calling a provider, or raise an API error."""
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("Provider must not be empty")
        if type(amount) is not int or amount < 0:
            raise ValueError("Reserved amount must be a nonnegative integer")
        limits = tuple(limits)
        self._validate_limits(limits)
        with self._lock:
            self._check_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                now = self._clock()
                self._maintain(now, limits)
                retry_after = 0
                exceeded: list[str] = []
                for limit in limits:
                    used_amount, requests = self._totals(provider, now, limit)
                    if self._exceeds(limit, used_amount + amount, requests + 1):
                        exceeded.append(limit.name)
                        retry_after = max(
                            retry_after,
                            self._retry_after(provider, amount, now, limit, used_amount, requests),
                        )
                if exceeded:
                    raise APIError(
                        429,
                        "Local quota exceeded for " + provider + ": " + ", ".join(exceeded),
                        code="local_quota_exceeded",
                        error_type="rate_limit_error",
                        headers={"Retry-After": str(retry_after)},
                    )
                self._connection.execute(
                    "INSERT INTO usage_events(provider, reserved_at, amount) VALUES (?, ?, ?)",
                    (provider, now, amount),
                )
                self._connection.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback()
                raise self._unavailable() from exc
            except BaseException:
                self._rollback()
                raise

    def snapshot(
        self, limits_by_provider: Mapping[str, Sequence[UsageLimit]]
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """Return committed reservation totals and remaining local allowances."""
        configured = {provider: tuple(limits) for provider, limits in limits_by_provider.items()}
        for limits in configured.values():
            self._validate_limits(limits)
        with self._lock:
            self._check_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                now = self._clock()
                self._maintain(
                    now, tuple(limit for limits in configured.values() for limit in limits)
                )
                result: dict[str, dict[str, dict[str, Any]]] = {}
                for provider, limits in configured.items():
                    result[provider] = {}
                    for limit in limits:
                        amount, requests = self._totals(provider, now, limit)
                        result[provider][limit.name] = {
                            "window_seconds": limit.window_seconds,
                            "amount": amount,
                            "requests": requests,
                            "max_amount": limit.max_amount,
                            "max_requests": limit.max_requests,
                            "remaining_amount": (
                                None
                                if limit.max_amount is None
                                else max(0, limit.max_amount - amount)
                            ),
                            "remaining_requests": (
                                None
                                if limit.max_requests is None
                                else max(0, limit.max_requests - requests)
                            ),
                        }
                self._connection.execute("COMMIT")
                return result
            except sqlite3.Error as exc:
                self._rollback()
                raise self._unavailable() from exc
            except BaseException:
                self._rollback()
                raise

    def _migrate_legacy_reservations(self) -> None:
        """Preserve the original gateway's TTS charges during an upgrade.

        Migration and its marker share a write transaction, so concurrently
        starting workers can never import a charge twice. Leave the old table
        intact for inspection; the original and updated gateway must not write
        to the same database concurrently.
        """
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            imported = self._connection.execute(
                "SELECT 1 FROM usage_metadata WHERE name = ?",
                ("legacy_reservations_imported",),
            ).fetchone()
            legacy_table = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reservations'"
            ).fetchone()
            if legacy_table and not imported:
                self._connection.execute(
                    "INSERT INTO usage_events(provider, reserved_at, amount) "
                    "SELECT 'google_tts', ts, units FROM reservations"
                )
                self._connection.execute(
                    "INSERT INTO usage_metadata(name, value) VALUES (?, 1)",
                    ("legacy_reservations_imported",),
                )
            self._connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise

    @staticmethod
    def _validate_limits(limits: Sequence[UsageLimit]) -> None:
        if any(not isinstance(limit, UsageLimit) for limit in limits):
            raise ValueError("Limits must contain UsageLimit instances")
        if len({limit.name for limit in limits}) != len(limits):
            raise ValueError("Usage limit names must be unique per provider")

    def _maintain(self, now: float, limits: Sequence[UsageLimit]) -> None:
        if not math.isfinite(now):
            raise ValueError("Usage clock must return a finite timestamp")
        retention = max((MIN_RETENTION_SECONDS, *(limit.window_seconds for limit in limits)))
        self._connection.execute(
            "UPDATE usage_metadata SET value = MAX(value, ?) WHERE name = ?",
            (retention, "retention_seconds"),
        )
        stored_retention = self._connection.execute(
            "SELECT value FROM usage_metadata WHERE name = ?", ("retention_seconds",)
        ).fetchone()[0]
        self._connection.execute(
            "DELETE FROM usage_events WHERE reserved_at <= ?", (now - stored_retention,)
        )

    def _totals(self, provider: str, now: float, limit: UsageLimit) -> tuple[int, int]:
        amount, requests = self._connection.execute(
            "SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM usage_events "
            "WHERE provider = ? AND reserved_at > ?",
            (provider, now - limit.window_seconds),
        ).fetchone()
        return amount, requests

    @staticmethod
    def _exceeds(limit: UsageLimit, amount: int, requests: int) -> bool:
        return (
            limit.max_amount == 0
            or limit.max_requests == 0
            or (limit.max_amount is not None and amount > limit.max_amount)
            or (limit.max_requests is not None and requests > limit.max_requests)
        )

    def _retry_after(
        self,
        provider: str,
        amount: int,
        now: float,
        limit: UsageLimit,
        used_amount: int,
        requests: int,
    ) -> int:
        # These requests cannot fit even in an empty window. Keep the header
        # bounded; a caller must change its request or configuration to succeed.
        if self._exceeds(limit, amount, 1):
            return limit.window_seconds
        rows = self._connection.execute(
            "SELECT reserved_at, amount FROM usage_events "
            "WHERE provider = ? AND reserved_at > ? ORDER BY reserved_at, id",
            (provider, now - limit.window_seconds),
        )
        for reserved_at, reserved_amount in rows:
            used_amount -= reserved_amount
            requests -= 1
            if not self._exceeds(limit, used_amount + amount, requests + 1):
                return max(1, math.ceil(reserved_at + limit.window_seconds - now))
        return limit.window_seconds

    def _check_open(self) -> None:
        if self._closed:
            raise self._unavailable()

    def _rollback(self) -> None:
        if self._connection.in_transaction:
            self._connection.execute("ROLLBACK")

    @staticmethod
    def _unavailable() -> APIError:
        return APIError(
            503,
            "Local usage accounting is temporarily unavailable; no provider request was sent.",
            code="usage_store_unavailable",
            error_type="server_error",
            headers={"Retry-After": "1"},
        )
