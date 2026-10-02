import asyncio
import hashlib
import json
import logging
import math
import shutil
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, PlainTextResponse, Response

from . import __version__
from .audio import audio_duration, encode_audio
from .cache import AudioCache
from .config import VOICE_PATTERN, Settings
from .errors import APIError
from .middleware import RequestGuard
from .providers import GoogleProvider, GroqProvider
from .schemas import SpeechRequest, TranscriptionRequest
from .subtitles import render_subtitles
from .usage import UsageLimit, UsageStore

logger = logging.getLogger(__name__)

VOICE_ALIASES = {
    "alloy": "ko-KR-Wavenet-A",
    "nova": "ko-KR-Wavenet-A",
    "marin": "ko-KR-Wavenet-A",
    "shimmer": "ko-KR-Wavenet-B",
    "coral": "ko-KR-Wavenet-B",
    "sage": "ko-KR-Wavenet-B",
    "echo": "ko-KR-Wavenet-C",
    "fable": "ko-KR-Wavenet-C",
    "ash": "ko-KR-Wavenet-C",
    "ballad": "ko-KR-Wavenet-C",
    "onyx": "ko-KR-Wavenet-D",
    "verse": "ko-KR-Wavenet-D",
    "cedar": "ko-KR-Wavenet-D",
}


def usage_limits(settings: Settings) -> dict[str, list[UsageLimit]]:
    return {
        "google_tts": [
            UsageLimit("32_days", 32 * 86400, max_amount=settings.tts_32day_char_limit),
            UsageLimit("24_hours", 86400, max_amount=settings.tts_daily_char_limit),
        ],
        "groq_stt": [
            UsageLimit("minute", 60, max_requests=settings.groq_minute_request_limit),
            UsageLimit("day", 86400, max_requests=settings.groq_daily_request_limit),
            UsageLimit("audio_hour", 3600, max_amount=settings.groq_hourly_audio_seconds_limit),
            UsageLimit("audio_day", 86400, max_amount=settings.groq_daily_audio_seconds_limit),
        ],
    }


def resolve_voice(voice: str, settings: Settings) -> str:
    aliases = {**VOICE_ALIASES, "alloy": settings.google_tts_voice}
    resolved = aliases.get(voice, voice)
    if not VOICE_PATTERN.fullmatch(resolved):
        raise APIError(
            400,
            "Use an OpenAI voice alias or a Korean Standard/Wavenet voice A–D",
            "unsupported_voice",
            "voice",
        )
    return resolved


def validation_error(exc: ValidationError | RequestValidationError) -> APIError:
    error = exc.errors()[0]
    location = [str(part) for part in error["loc"] if part != "body"]
    param = ".".join(location) or "body"
    # Do not reflect inputs (audio, text, credentials) from validation errors.
    return APIError(400, f"Invalid or missing parameter: {param}", "invalid_parameter", param)


def create_app(
    settings: Settings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    google_token_provider: Callable[[], Awaitable[dict[str, str]]] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        config = settings or Settings()
        for binary in ("ffmpeg", "ffprobe"):
            if shutil.which(binary) is None:
                raise RuntimeError(f"{binary} is required; install FFmpeg before starting")
        application.state.settings = config
        async with AsyncExitStack() as resources:
            client = http_client or await resources.enter_async_context(
                httpx.AsyncClient(
                    timeout=httpx.Timeout(config.upstream_timeout_seconds, connect=10),
                    follow_redirects=False,
                    trust_env=False,
                )
            )
            application.state.usage = UsageStore(config.usage_db_path)
            resources.callback(application.state.usage.close)
            application.state.limits = usage_limits(config)
            application.state.cache = AudioCache(config.cache_max_bytes, config.cache_ttl_seconds)
            application.state.groq = GroqProvider(config, client)
            application.state.google = GoogleProvider(
                config, client, token_provider=google_token_provider
            )
            # A lock exists only while requests for this exact synthesis are active.
            application.state.synthesis_locks = {}
            yield

    application = FastAPI(
        title="Korean Speech OpenAI Bridge",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.add_middleware(RequestGuard, settings=lambda: application.state.settings)

    @application.exception_handler(APIError)
    async def handle_api_error(_: Request, exc: APIError) -> JSONResponse:
        return exc.response()

    @application.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return validation_error(exc).response()

    @application.exception_handler(HTTPException)
    async def handle_http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return APIError(exc.status_code, "Invalid request or route", "invalid_request").response()

    @application.exception_handler(sqlite3.Error)
    async def handle_storage_error(_: Request, exc: sqlite3.Error) -> JSONResponse:
        logger.error("Usage store failed: %s", type(exc).__name__)
        return APIError(
            503, "Usage accounting is unavailable", "usage_unavailable", error_type="server_error"
        ).response()

    @application.exception_handler(Exception)
    async def handle_unknown_error(_: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled request failure: %s", type(exc).__name__)
        return APIError(
            500, "Internal server error", "server_error", error_type="server_error"
        ).response()

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @application.get("/ready")
    async def ready() -> dict[str, Any]:
        config = application.state.settings
        # This is a local configuration check, never a paid provider health probe.
        await asyncio.to_thread(application.state.usage.snapshot, application.state.limits)
        return {
            "status": "ok",
            "groq_key_configured": config.groq_api_key is not None,
            "groq_free_tier_confirmed": config.groq_free_tier_confirmed,
            "google_auth": "ADC resolved on first synthesis",
            "upstream_verified": False,
        }

    @application.get("/usage")
    async def usage() -> dict[str, Any]:
        return {
            "units": {"google_tts": "utf16_units", "groq_stt": "reserved_audio_seconds"},
            "warning": "Local reservations only; external usage and balances are unknown.",
            "usage": await asyncio.to_thread(
                application.state.usage.snapshot, application.state.limits
            ),
        }

    @application.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "created": 0, "owned_by": owner}
                for name, owner in [
                    ("whisper-1", "groq"),
                    ("whisper-large-v3-turbo", "groq"),
                    ("whisper-large-v3", "groq"),
                    ("tts-1", "google"),
                    ("tts-1-hd", "google"),
                    ("google-wavenet", "google"),
                ]
            ],
        }

    @application.get("/v1/voices")
    async def voices() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": name,
                    "provider_voice": resolve_voice(name, application.state.settings),
                    "language": "ko-KR",
                }
                for name in sorted(VOICE_ALIASES)
            ],
            "google_voice_ids": [
                f"ko-KR-{family}-{letter}"
                for family in ("Wavenet", "Standard")
                for letter in "ABCD"
            ],
            "note": "Bridge extension; aliases map to Google voice identities.",
        }

    @application.post("/v1/audio/speech")
    async def speech(body: SpeechRequest) -> Response:
        config = application.state.settings
        if body.instructions:
            raise APIError(
                400,
                "WaveNet does not support voice instructions",
                "unsupported_parameter",
                "instructions",
            )
        if len(body.input) > config.max_tts_input_chars:
            raise APIError(400, "Text exceeds MAX_TTS_INPUT_CHARS", "text_too_long", "input")
        voice = resolve_voice(body.voice, config)
        key = hashlib.sha256(
            json.dumps([voice, body.speed, body.input], ensure_ascii=True).encode()
        ).hexdigest()
        locks = application.state.synthesis_locks
        entry = locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                pcm = application.state.cache.get(key)
                cache_status = "HIT" if pcm is not None else "MISS"
                if pcm is None:
                    auth_headers = await application.state.google.authorize()
                    await asyncio.to_thread(
                        application.state.usage.reserve,
                        "google_tts",
                        len(body.input.encode("utf-16-le")) // 2,
                        application.state.limits["google_tts"],
                    )
                    pcm = await application.state.google.synthesize(
                        body.input, voice, body.speed, auth_headers=auth_headers
                    )
                    application.state.cache.put(key, pcm)
            output, media_type = await encode_audio(
                pcm, body.response_format, config.ffmpeg_timeout_seconds
            )
            return Response(
                output,
                media_type=media_type,
                headers={
                    "X-Speech-Provider": "google",
                    "X-Speech-Voice": voice,
                    "X-TTS-Cache": cache_status,
                },
            )
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                locks.pop(key, None)

    @application.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request) -> Response:
        config = application.state.settings
        if config.groq_api_key is None:
            raise APIError(503, "Groq is not configured", "provider_not_configured")
        if not config.groq_free_tier_confirmed:
            raise APIError(
                503,
                "Confirm your Groq Free plan and set GROQ_FREE_TIER_CONFIRMED=true",
                "free_tier_not_confirmed",
            )
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            raise APIError(400, "Use multipart/form-data", "invalid_content_type")
        async with request.form(max_files=1, max_fields=20, max_part_size=16_384) as form:
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                raise APIError(400, "An audio file is required", "invalid_parameter", "file")
            fields: dict[str, Any] = {}
            if len(form.getlist("file")) != 1:
                raise APIError(400, "Duplicate file field", "invalid_parameter", "file")
            for key, value in form.multi_items():
                if key == "file":
                    continue
                if not isinstance(value, str):
                    raise APIError(400, "Expected a text field", "invalid_parameter", key)
                if key in {"timestamp_granularities[]", "timestamp_granularities"}:
                    fields.setdefault("timestamp_granularities", []).append(value)
                elif key in fields:
                    raise APIError(400, "Duplicate form field", "invalid_parameter", key)
                else:
                    fields[key] = value
            if "stream" in fields:
                if fields["stream"].lower() not in {"false", "0"}:
                    raise APIError(
                        400, "Streaming transcription is unsupported", "unsupported_stream"
                    )
                fields["stream"] = False
            fields.setdefault("language", config.asr_default_language)
            if isinstance(fields["language"], str):
                fields["language"] = fields["language"].lower() or "auto"
            try:
                options = TranscriptionRequest.model_validate(fields)
            except ValidationError as exc:
                raise validation_error(exc) from exc
            if options.timestamp_granularities and options.response_format != "verbose_json":
                raise APIError(
                    400,
                    "Timestamps require response_format=verbose_json",
                    "invalid_parameter",
                    "timestamp_granularities",
                )
            audio = await upload.read(config.max_upload_bytes + 1)
            if not audio:
                raise APIError(400, "Audio file is empty", "invalid_audio", "file")
            if len(audio) > config.max_upload_bytes:
                raise APIError(413, "Audio file is too large", "request_too_large", "file")
            # Use only the basename and bound multipart metadata forwarded upstream.
            filename = (upload.filename or "audio.wav").replace("\\", "/").rsplit("/", 1)[-1]
            if len(filename) > 200 or any(ord(char) < 32 for char in filename):
                raise APIError(400, "Invalid filename", "invalid_parameter", "file")
            if filename.rsplit(".", 1)[-1].lower() not in {
                "flac",
                "mp3",
                "mp4",
                "mpeg",
                "mpga",
                "m4a",
                "ogg",
                "wav",
                "webm",
            }:
                raise APIError(400, "Unsupported audio file extension", "invalid_parameter", "file")
            duration = await audio_duration(
                audio, filename, config.max_audio_seconds, config.ffmpeg_timeout_seconds
            )
            provider_fields: dict[str, str | list[str]] = {
                "model": config.groq_model if options.model == "whisper-1" else options.model,
                "temperature": str(options.temperature),
                "response_format": (
                    "verbose_json"
                    if options.response_format in {"srt", "vtt"}
                    else options.response_format
                ),
            }
            if options.language != "auto":
                provider_fields["language"] = options.language
            if options.prompt is not None:
                provider_fields["prompt"] = options.prompt
            if options.timestamp_granularities:
                provider_fields["timestamp_granularities[]"] = options.timestamp_granularities
            elif options.response_format in {"srt", "vtt"}:
                provider_fields["timestamp_granularities[]"] = ["segment"]
            await asyncio.to_thread(
                application.state.usage.reserve,
                "groq_stt",
                max(10, math.ceil(duration)),
                application.state.limits["groq_stt"],
            )
            result = await application.state.groq.transcribe(
                audio, filename, "application/octet-stream", provider_fields
            )
        response_headers = {"X-Speech-Provider": "groq", "X-Speech-Model": provider_fields["model"]}
        if options.response_format == "text":
            return PlainTextResponse(result, headers=response_headers)
        if options.response_format in {"srt", "vtt"}:
            return PlainTextResponse(
                render_subtitles(result, options.response_format),
                media_type="text/vtt"
                if options.response_format == "vtt"
                else "application/x-subrip",
                headers=response_headers,
            )
        result.pop("x_groq", None)
        return JSONResponse(result, headers=response_headers)

    return application


app = create_app()
