from __future__ import annotations

import asyncio
import json
import math
import shutil
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response

from .core import APIError, AudioCache, RequestGuard, Settings, UsageLedger, count_units
from .media import MIME, encode_audio, merge_wavs, split_text, subtitle_text
from .providers import Providers, TokenSource, upstream_headers

ASR_MODELS = {"whisper-1", "whisper-large-v3-turbo"}
TTS_MODELS = {"tts-1", "google-wavenet"}
KOREAN_VOICES = {f"ko-KR-Wavenet-{letter}" for letter in "ABCD"}
# Compatibility aliases only. These do not reproduce OpenAI voice identities.
VOICE_ALIASES = {
    "alloy": "ko-KR-Wavenet-A", "nova": "ko-KR-Wavenet-A",
    "shimmer": "ko-KR-Wavenet-B", "coral": "ko-KR-Wavenet-B",
    "sage": "ko-KR-Wavenet-B", "marin": "ko-KR-Wavenet-A",
    "echo": "ko-KR-Wavenet-C", "fable": "ko-KR-Wavenet-C",
    "ash": "ko-KR-Wavenet-C", "ballad": "ko-KR-Wavenet-C",
    "onyx": "ko-KR-Wavenet-D", "verse": "ko-KR-Wavenet-D",
    "cedar": "ko-KR-Wavenet-D",
}


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    model: str
    input: str = Field(min_length=1, max_length=4096)
    voice: str = "alloy"
    response_format: Literal["mp3", "opus", "aac", "flac", "wav", "pcm"] = "mp3"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    instructions: str | None = None
    stream_format: Literal["audio", "sse"] = "audio"


def create_app(settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
               tokens: TokenSource | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    aliases = {**VOICE_ALIASES, "alloy": settings.google_default_voice}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is required. Install it or run the supplied Docker image.")
        app.state.ledger = UsageLedger(settings)
        app.state.cache = AudioCache(settings.cache_max_bytes, settings.cache_ttl_seconds)
        app.state.semaphore = asyncio.Semaphore(settings.max_concurrent)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(90.0, connect=10.0, pool=10.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            follow_redirects=False, transport=transport, trust_env=False,
        ) as client:
            app.state.providers = Providers(settings, client, tokens)
            yield

    app = FastAPI(title="Korean Speech OpenAI Gateway", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestGuard, api_key=settings.proxy_api_key, max_file_bytes=settings.max_file_bytes)

    @app.exception_handler(APIError)
    async def api_error(_: Request, exc: APIError):
        return exc.response()

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        first = exc.errors()[0]
        # Do not return input text or audio in validation errors.
        param = ".".join(str(v) for v in first.get("loc", ())[1:]) or None
        return APIError(400, "Invalid or unsupported request field. Check the compatibility table in README.",
                        "validation_error", param=param).response()

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return APIError(exc.status_code, "Request could not be processed.", "http_error").response()

    @app.exception_handler(httpx.TimeoutException)
    async def upstream_timeout(_: Request, exc: httpx.TimeoutException):
        return APIError(504, "Upstream timed out; no automatic retry. Reserved TTS usage is retained.",
                        "upstream_timeout").response()

    @app.exception_handler(httpx.RequestError)
    async def upstream_network_error(_: Request, exc: httpx.RequestError):
        return APIError(502, "Could not connect to upstream. No automatic retry was attempted.",
                        "upstream_connection_error").response()

    @app.exception_handler(sqlite3.Error)
    async def usage_storage_error(_: Request, exc: sqlite3.Error):
        return APIError(503, "Usage database unavailable; request blocked to protect the quota.",
                        "usage_storage_error").response()

    @app.exception_handler(TimeoutError)
    async def encode_timeout(_: Request, exc: TimeoutError):
        return APIError(504, "Audio encoding timed out.", "audio_encode_timeout").response()

    @app.get("/health")
    async def health():
        return {"status": "ok", "upstreams_verified": False}

    @app.get("/usage")
    async def usage(request: Request):
        return await asyncio.to_thread(request.app.state.ledger.snapshot)

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [
            {"id": model, "object": "model", "created": 0, "owned_by": "local-speech-gateway"}
            for model in sorted(ASR_MODELS | TTS_MODELS)
        ]}

    @app.get("/v1/voices")
    async def voices():
        return {"object": "list", "data": [
            {"id": name, "provider_voice": provider, "language": "ko-KR"}
            for name, provider in sorted(aliases.items())
        ], "google_voice_ids": sorted(KOREAN_VOICES),
            "note": "Gateway extension, not a standard OpenAI endpoint. Aliases are not OpenAI voices."}

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request):
        allowed = {"file", "model", "language", "prompt", "response_format", "temperature",
                   "timestamp_granularities[]", "stream"}
        async with request.form(max_files=1, max_fields=16, max_part_size=32768) as form:
            unsupported = set(form.keys()) - allowed
            if unsupported:
                raise APIError(400, "Unsupported transcription field.", "unsupported_parameter",
                               param=sorted(unsupported)[0])
            for key in allowed - {"timestamp_granularities[]"}:
                if len(form.getlist(key)) > 1:
                    raise APIError(400, "Duplicate scalar field.", param=key)
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                raise APIError(400, "An uploaded audio file is required.", param="file")
            model = form.get("model")
            if model not in ASR_MODELS:
                raise APIError(400, "Use whisper-1 or whisper-large-v3-turbo.", "model_not_found", param="model")
            fmt = str(form.get("response_format", "json"))
            if fmt not in {"json", "text", "verbose_json", "srt", "vtt"}:
                raise APIError(400, "Unsupported transcript format.", param="response_format")
            if str(form.get("stream", "false")).lower() not in {"false", "0"}:
                raise APIError(400, "Live transcription/SSE is not implemented.", "unsupported_parameter", param="stream")
            try:
                temperature = float(str(form.get("temperature", "0")))
                if not math.isfinite(temperature) or not 0 <= temperature <= 1:
                    raise ValueError
            except ValueError:
                raise APIError(400, "temperature must be between 0 and 1.", param="temperature")
            granularities = form.getlist("timestamp_granularities[]")
            if any(g not in {"word", "segment"} for g in granularities):
                raise APIError(400, "Timestamp granularity must be word or segment.", param="timestamp_granularities")
            if granularities and fmt != "verbose_json":
                raise APIError(400, "timestamp_granularities requires verbose_json.", param="timestamp_granularities")
            fields: dict[str, Any] = {"model": "whisper-large-v3-turbo", "temperature": str(temperature),
                                      "response_format": "verbose_json" if fmt in {"srt", "vtt"} else fmt}
            language = str(form.get("language", settings.default_asr_language))
            if language and language != "auto":
                if len(language) != 2 or not language.isascii() or not language.isalpha():
                    raise APIError(400, "Use a two-letter language code, e.g. ko, or auto.", param="language")
                fields["language"] = language.lower()
            if form.get("prompt"):
                fields["prompt"] = str(form["prompt"])
            if fmt in {"srt", "vtt"}:
                fields["timestamp_granularities[]"] = ["segment"]
            elif granularities:
                fields["timestamp_granularities[]"] = granularities
            filename = Path((upload.filename or "audio.wav").replace("\\", "/")).name
            if Path(filename).suffix.lower() not in {".flac", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".ogg", ".wav", ".webm"}:
                raise APIError(400, "Unsupported audio file extension.", param="file")
            audio = await upload.read(settings.max_file_bytes + 1)
            if not audio:
                raise APIError(400, "Audio file is empty.", param="file")
            if len(audio) > settings.max_file_bytes:
                raise APIError(413, "Free-tier upload limit is 25,000,000 bytes in this gateway.", "payload_too_large")
        async with request.app.state.semaphore:
            result = await request.app.state.providers.transcribe(audio=audio, filename=filename, fields=fields)
        headers = {**upstream_headers(result), "X-Speech-Provider": "groq",
                   "X-Speech-Model": "whisper-large-v3-turbo", "Cache-Control": "no-store"}
        if fmt == "text":
            return Response(result.content, media_type="text/plain", headers=headers)
        try:
            data = result.json()
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            raise APIError(502, "Groq returned invalid transcription JSON.", "invalid_upstream_response")
        if fmt in {"srt", "vtt"}:
            return Response(subtitle_text(data, fmt),
                            media_type="text/vtt" if fmt == "vtt" else "application/x-subrip", headers=headers)
        # Groq metadata is not required by OpenAI consumers.
        data.pop("x_groq", None)
        return JSONResponse(data, headers=headers)

    @app.post("/v1/audio/speech")
    async def speech(body: SpeechRequest, request: Request):
        if body.model not in TTS_MODELS:
            raise APIError(400, "Use tts-1 or google-wavenet; other voice families are blocked.",
                           "model_not_found", param="model")
        if body.instructions:
            raise APIError(400, "WaveNet cannot honor natural-language voice instructions.",
                           "unsupported_parameter", param="instructions")
        if body.stream_format != "audio":
            raise APIError(400, "SSE/live speech synthesis is not implemented.", "unsupported_parameter", param="stream_format")
        if not body.input.strip():
            raise APIError(400, "input cannot be blank.", param="input")
        try:
            body.input.encode("utf-8")
        except UnicodeEncodeError:
            raise APIError(400, "input must contain valid Unicode.", param="input")
        voice = aliases.get(body.voice, body.voice)
        if voice not in KOREAN_VOICES:
            raise APIError(400, "Use an advertised alias or ko-KR-Wavenet-A/B/C/D.", "invalid_voice", param="voice")
        state = request.app.state
        cache_key = state.cache.key({"text": body.input, "voice": voice, "speed": body.speed,
                                     "format": body.response_format, "version": 1})
        result_headers = {"X-Speech-Provider": "google", "X-Speech-Voice": voice, "Cache-Control": "no-store"}
        async with state.semaphore:
            cached = state.cache.get(cache_key)
            if cached is not None:
                result_headers["X-TTS-Cache"] = "HIT"
                return Response(cached, media_type=MIME[body.response_format], headers=result_headers)
            # Authenticate before reserving, but reserve the ENTIRE text before any billable call.
            auth_headers = await state.providers.tokens.headers()
            await asyncio.to_thread(state.ledger.reserve, count_units(body.input))
            google_speed = min(body.speed, 2.0)
            chunks = []
            for text in split_text(body.input):
                chunks.append(await state.providers.synthesize(text, voice, google_speed, auth_headers))
            wav = merge_wavs(chunks)
            result = await encode_audio(wav, body.response_format, body.speed / google_speed)
            state.cache.put(cache_key, result)
        result_headers["X-TTS-Cache"] = "MISS"
        # Complete synthesis first; binary SDK streaming readers work, but this is NOT low-latency
        # streaming generation and does not implement Realtime sessions or WebSockets.
        return Response(result, media_type=MIME[body.response_format], headers=result_headers)

    return app
