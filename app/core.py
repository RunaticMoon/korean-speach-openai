from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse

WINDOW_SECONDS = 32 * 24 * 60 * 60


class APIError(Exception):
    def __init__(self, status: int, message: str, code: str = "invalid_request", *,
                 param: str | None = None, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code
        self.param, self.headers = param, headers or {}

    def response(self) -> JSONResponse:
        kind = ("rate_limit_error" if self.status == 429 else
                "authentication_error" if self.status == 401 else
                "server_error" if self.status >= 500 else "invalid_request_error")
        return JSONResponse({"error": {"message": self.message, "type": kind,
                                      "param": self.param, "code": self.code}},
                            status_code=self.status, headers=self.headers)


@dataclass(frozen=True)
class Settings:
    proxy_api_key: str
    groq_api_key: str
    google_cloud_project: str
    data_dir: Path = Path("data")
    tts_limit: int = 3_500_000
    tts_daily_limit: int = 150_000
    cache_max_bytes: int = 16 * 1024 * 1024
    cache_ttl_seconds: int = 3600
    max_file_bytes: int = 25_000_000
    max_concurrent: int = 2
    google_default_voice: str = "ko-KR-Wavenet-A"
    default_asr_language: str = "ko"

    @classmethod
    def from_env(cls) -> Settings:
        settings = cls(
            proxy_api_key=os.getenv("PROXY_API_KEY", ""),
            groq_api_key=os.getenv("GROQ_API_KEY", ""),
            google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            tts_limit=int(os.getenv("TTS_ROLLING_32D_CHAR_LIMIT", "3500000")),
            tts_daily_limit=int(os.getenv("TTS_ROLLING_24H_CHAR_LIMIT", "150000")),
            cache_max_bytes=int(os.getenv("TTS_CACHE_MAX_BYTES", str(16 * 1024 * 1024))),
            cache_ttl_seconds=int(os.getenv("TTS_CACHE_TTL_SECONDS", "3600")),
            max_concurrent=int(os.getenv("MAX_CONCURRENT_REQUESTS", "2")),
            google_default_voice=os.getenv("GOOGLE_TTS_VOICE", "ko-KR-Wavenet-A"),
            default_asr_language=os.getenv("ASR_DEFAULT_LANGUAGE", "ko"),
        )
        if len(settings.proxy_api_key) < 32 or "CHANGE" in settings.proxy_api_key.upper():
            raise RuntimeError("Set PROXY_API_KEY to a random secret of at least 32 characters.")
        if not settings.groq_api_key or "CHANGE" in settings.groq_api_key.upper():
            raise RuntimeError("Set GROQ_API_KEY from a Groq Free-plan organization.")
        if not settings.google_cloud_project or "CHANGE" in settings.google_cloud_project.upper():
            raise RuntimeError("Set GOOGLE_CLOUD_PROJECT to the enabled/billed Cloud TTS project.")
        if not 1 <= settings.tts_limit <= 3_500_000:
            raise RuntimeError("TTS_ROLLING_32D_CHAR_LIMIT must be 1..3500000 (safety ceiling).")
        if not 1 <= settings.tts_daily_limit <= settings.tts_limit:
            raise RuntimeError("TTS_ROLLING_24H_CHAR_LIMIT must be positive and <= 32-day limit.")
        if not 1 <= settings.max_concurrent <= 8:
            raise RuntimeError("MAX_CONCURRENT_REQUESTS must be 1..8.")
        if settings.cache_max_bytes < 0 or settings.cache_ttl_seconds < 0:
            raise RuntimeError("Cache sizes/TTL cannot be negative.")
        if settings.google_default_voice not in {f"ko-KR-Wavenet-{v}" for v in "ABCD"}:
            raise RuntimeError("GOOGLE_TTS_VOICE must be a Korean WaveNet A/B/C/D voice.")
        return settings


def count_units(text: str) -> int:
    # Conservative for supplementary Unicode characters; ordinary Korean counts as 1.
    return len(text.encode("utf-16-le")) // 2


class UsageLedger:
    """Atomic reservations, shared by processes using the same local SQLite file.

    Deliberately never refunds: a timeout does not prove the upstream wasn't billed.
    No transcript, input text, audio or credentials are stored in this database.
    """
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = settings.data_dir / "usage.sqlite3"
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS reservations "
                       "(id INTEGER PRIMARY KEY, ts REAL NOT NULL, units INTEGER NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS reservations_ts ON reservations(ts)")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    def reserve(self, units: int, *, now: float | None = None) -> None:
        if units < 1:
            raise ValueError("Reservation must be positive.")
        now = time.time() if now is None else now
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            total, daily = db.execute(
                "SELECT COALESCE(SUM(units),0), "
                "COALESCE(SUM(CASE WHEN ts>=? THEN units ELSE 0 END),0) "
                "FROM reservations WHERE ts>=?", (now - 86400, now - WINDOW_SECONDS)
            ).fetchone()
            if total + units > self.settings.tts_limit or daily + units > self.settings.tts_daily_limit:
                raise APIError(429, "Local TTS safety limit reached; no upstream call was made. "
                               "Inspect /usage. There is no paid fallback.",
                               "local_tts_limit", headers={"Retry-After": "3600"})
            db.execute("INSERT INTO reservations(ts,units) VALUES (?,?)", (now, units))
            db.execute("DELETE FROM reservations WHERE ts<?", (now - WINDOW_SECONDS - 86400,))
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with closing(self._connect()) as db:
            total, daily = db.execute(
                "SELECT COALESCE(SUM(units),0), "
                "COALESCE(SUM(CASE WHEN ts>=? THEN units ELSE 0 END),0) "
                "FROM reservations WHERE ts>=?", (now - 86400, now - WINDOW_SECONDS)
            ).fetchone()
        return {
            "provider": "google", "voice_family": "ko-KR-Wavenet", "unit": "conservative_utf16_units",
            "rolling_32_days": {"reserved": total, "limit": self.settings.tts_limit,
                                "remaining": max(0, self.settings.tts_limit - total)},
            "rolling_24_hours": {"reserved": daily, "limit": self.settings.tts_daily_limit,
                                 "remaining": max(0, self.settings.tts_daily_limit - daily)},
            "warning": "Local reservations only; not Google billing/free-tier balance. "
                       "Failures are not refunded. External usage is not visible.",
        }


class AudioCache:
    """Process-local bounded LRU; successful TTS output only, disabled with max_bytes=0."""
    def __init__(self, max_bytes: int, ttl: int):
        self.max_bytes, self.ttl, self.size = max_bytes, ttl, 0
        self.items: OrderedDict[str, tuple[float, bytes]] = OrderedDict()

    @staticmethod
    def key(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _purge(self) -> None:
        now = time.monotonic()
        for key in list(self.items):
            if self.items[key][0] <= now:
                _, body = self.items.pop(key)
                self.size -= len(body)

    def get(self, key: str) -> bytes | None:
        self._purge()
        item = self.items.get(key)
        if item is None:
            return None
        self.items.move_to_end(key)
        return item[1]

    def put(self, key: str, body: bytes) -> None:
        self._purge()
        if not self.ttl or not self.max_bytes or len(body) > self.max_bytes:
            return
        old = self.items.pop(key, None)
        if old:
            self.size -= len(old[1])
        while self.items and self.size + len(body) > self.max_bytes:
            _, (_, evicted) = self.items.popitem(last=False)
            self.size -= len(evicted)
        self.items[key] = (time.monotonic() + self.ttl, body)
        self.size += len(body)


class RequestGuard:
    """Authenticate before parsing uploads; cap bodies even without Content-Length.

    Designed for a private, low-concurrency gateway. Bodies are buffered in memory.
    Put a TLS/private-network ingress in front, not a public unauthenticated port.
    """
    def __init__(self, app, api_key: str, max_file_bytes: int):
        self.app, self.api_key, self.max_file_bytes = app, api_key, max_file_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        if path != "/health":
            authorization = headers.get(b"authorization", b"").decode("latin-1")
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), self.api_key.encode()):
                await APIError(401, "Invalid gateway API key.", "invalid_api_key",
                               headers={"WWW-Authenticate": "Bearer"}).response()(scope, receive, send)
                return
        if scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        cap = self.max_file_bytes + 128_000 if path == "/v1/audio/transcriptions" else 128_000
        try:
            content_length = int(headers.get(b"content-length", b"0"))
            if content_length < 0:
                raise ValueError
        except ValueError:
            await APIError(400, "Invalid Content-Length.").response()(scope, receive, send)
            return
        if content_length > cap:
            await APIError(413, "Request body exceeds gateway limit.", "payload_too_large").response()(scope, receive, send)
            return
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > cap:
                await APIError(413, "Request body exceeds gateway limit.", "payload_too_large").response()(scope, receive, send)
                return
            if not event.get("more_body", False):
                break
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
