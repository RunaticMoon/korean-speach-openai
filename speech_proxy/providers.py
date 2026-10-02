"""Async Groq transcription and Google Cloud Text-to-Speech adapters."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import google.auth
import httpx
from google.auth.transport.requests import Request

from .audio import change_speed, split_text, wav_to_pcm
from .config import Settings
from .errors import APIError

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GOOGLE_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
_GOOGLE_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _upstream_error(response: httpx.Response, provider: str) -> APIError:
    status = response.status_code
    headers = None
    if status == 429:
        retry_after = response.headers.get("retry-after", "")
        # Forward a bounded delay, never arbitrary upstream response headers.
        if retry_after.isascii() and retry_after.isdigit() and len(retry_after) <= 6:
            headers = {"Retry-After": str(min(int(retry_after), 86400))}
        return APIError(
            429,
            f"{provider} rate limit exceeded.",
            "upstream_rate_limit",
            error_type="rate_limit_error",
            headers=headers,
        )
    if status in (401, 403):
        return APIError(
            502,
            f"{provider} authentication or permissions are misconfigured.",
            "upstream_authentication_error",
            error_type="server_error",
        )
    if status in (400, 413, 422):
        return APIError(
            413 if status == 413 else 400,
            f"{provider} rejected the request.",
            "upstream_invalid_request",
        )
    if status in (408, 504):
        return APIError(
            504, f"{provider} request timed out.", "upstream_timeout", error_type="server_error"
        )
    return APIError(502, f"{provider} is unavailable.", "upstream_error", error_type="server_error")


async def _post(
    client: httpx.AsyncClient,
    url: str,
    provider: str,
    timeout: float,  # noqa: ASYNC109
    max_bytes: int,
    **kwargs: Any,
) -> bytes:
    try:
        async with asyncio.timeout(timeout):
            async with client.stream(
                "POST", url, timeout=timeout, follow_redirects=False, **kwargs
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise _upstream_error(response, provider)
                content = bytearray()
                async for block in response.aiter_bytes():
                    if len(content) + len(block) > max_bytes:
                        raise APIError(
                            502,
                            f"{provider} returned an oversized response.",
                            "invalid_upstream_response",
                            error_type="server_error",
                        )
                    content.extend(block)
                return bytes(content)
    except (httpx.TimeoutException, TimeoutError):
        raise APIError(
            504, f"{provider} request timed out.", "upstream_timeout", error_type="server_error"
        ) from None
    except httpx.HTTPError:
        raise APIError(
            502,
            f"{provider} could not be reached.",
            "upstream_connection_error",
            error_type="server_error",
        ) from None


class GroqProvider:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient):
        self.settings = settings
        self.client = http_client

    async def transcribe(
        self,
        audio: bytes,
        filename: str,
        content_type: str,
        fields: dict[str, str | list[str]],
    ) -> dict | str:
        if not self.settings.groq_api_key:
            raise APIError(
                503,
                "Groq credentials are not configured.",
                "provider_not_configured",
                error_type="server_error",
            )
        model = fields.get("model", self.settings.groq_model)
        if model == "whisper-1":
            model = self.settings.groq_model
        data = {**fields, "model": model}
        payload = await _post(
            self.client,
            GROQ_URL,
            "Groq",
            self.settings.upstream_timeout_seconds,
            4 * 1024 * 1024,
            headers={"Authorization": f"Bearer {self.settings.groq_api_key.get_secret_value()}"},
            files={"file": (filename, audio, content_type)},
            data=data,
        )
        try:
            if fields.get("response_format") == "text":
                return payload.decode("utf-8")
            result = json.loads(payload)
            if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                raise ValueError("invalid transcription schema")
            return result
        except (ValueError, UnicodeDecodeError):
            raise APIError(
                502,
                "Groq returned an invalid transcription.",
                "invalid_upstream_response",
                error_type="server_error",
            ) from None


class _DeadlineRequest(Request):
    """Apply a total deadline to Google auth's synchronous HTTP transport."""

    def __init__(self, deadline: float):
        super().__init__()
        self.deadline = deadline

    def __call__(self, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("credential deadline exceeded")
        return super().__call__(*args, timeout=min(timeout or remaining, remaining), **kwargs)


class ADCAuth:
    """Load ADC lazily and share a single refresh across concurrent requests.

    Synchronous credential discovery/refresh runs in a worker thread. Shielding
    the shared task keeps a timed-out or cancelled caller from launching a second
    refresh while the original worker still owns the credentials.
    """

    def __init__(self, project: str | None, timeout: float, credentials_file: str | None = None):
        self.project = project
        self.timeout = timeout
        self.credentials_file = credentials_file
        self._credentials: Any = None
        self._pending: asyncio.Task[dict[str, str]] | None = None

    def _headers_sync(self) -> dict[str, str]:
        request = _DeadlineRequest(time.monotonic() + self.timeout)
        try:
            if self._credentials is None:
                if self.credentials_file:
                    self._credentials, _ = google.auth.load_credentials_from_file(
                        self.credentials_file,
                        scopes=[_GOOGLE_SCOPE],
                        request=request,
                        quota_project_id=self.project,
                    )
                else:
                    self._credentials, _ = google.auth.default(
                        scopes=[_GOOGLE_SCOPE],
                        request=request,
                        quota_project_id=self.project,
                    )
            if not self._credentials.valid:
                self._credentials.refresh(request)
            token = self._credentials.token
            if not isinstance(token, str) or not token:
                raise ValueError("no access token")
            headers = {"Authorization": f"Bearer {token}"}
            quota_project = self.project or getattr(self._credentials, "quota_project_id", None)
            if quota_project:
                headers["x-goog-user-project"] = quota_project
            return headers
        finally:
            request.session.close()

    async def headers(self) -> dict[str, str]:
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(asyncio.to_thread(self._headers_sync))
            # Retrieve late worker exceptions even when every caller has gone.
            self._pending.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
        try:
            async with asyncio.timeout(self.timeout):
                return await asyncio.shield(self._pending)
        except TimeoutError:
            raise APIError(
                504,
                "Google credential refresh timed out.",
                "upstream_timeout",
                error_type="server_error",
            ) from None
        except Exception:
            raise APIError(
                503,
                "Google Application Default Credentials are unavailable.",
                "provider_not_configured",
                error_type="server_error",
            ) from None


class GoogleProvider:
    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient,
        token_provider: Callable[[], Awaitable[dict[str, str]]] | None = None,
    ):
        self.settings = settings
        self.client = http_client
        self.auth = ADCAuth(
            settings.google_cloud_project,
            settings.upstream_timeout_seconds,
            settings.google_application_credentials,
        )
        self.token_provider = token_provider or self.auth.headers

    async def authorize(self) -> dict[str, str]:
        return await self.token_provider()

    async def synthesize(
        self,
        text: str,
        voice: str,
        speed: float,
        *,
        auth_headers: dict[str, str] | None = None,
    ) -> bytes:
        output = bytearray()
        for chunk in split_text(text):
            headers = auth_headers if auth_headers is not None else await self.authorize()
            payload = await _post(
                self.client,
                GOOGLE_URL,
                "Google",
                self.settings.upstream_timeout_seconds,
                (self.settings.max_output_audio_bytes + 4096) * 4 // 3 + 4096,
                headers=headers,
                json={
                    "input": {"text": chunk},
                    "voice": {"languageCode": "ko-KR", "name": voice},
                    "audioConfig": {
                        "audioEncoding": "LINEAR16",
                        "sampleRateHertz": 24_000,
                        "speakingRate": min(speed, 2.0),
                    },
                },
            )
            try:
                encoded = json.loads(payload)["audioContent"]
                if not isinstance(encoded, str):
                    raise ValueError("invalid audio content")
                wav = base64.b64decode(encoded, validate=True)
            except (ValueError, KeyError, TypeError, binascii.Error):
                raise APIError(
                    502,
                    "Google returned invalid audio.",
                    "invalid_upstream_response",
                    error_type="server_error",
                ) from None
            pcm = wav_to_pcm(wav)
            if len(output) + len(pcm) > self.settings.max_output_audio_bytes:
                raise APIError(
                    413,
                    "Synthesized audio exceeds the configured output limit.",
                    "audio_too_large",
                    "input",
                )
            output.extend(pcm)
        if not output:
            raise APIError(400, "Input text must not be empty.", param="input")
        pcm = bytes(output)
        if speed > 2:
            pcm = await change_speed(pcm, speed / 2, self.settings.ffmpeg_timeout_seconds)
        return pcm
