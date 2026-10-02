from __future__ import annotations

import asyncio
import base64
import binascii
from typing import Any, Protocol

import httpx

from .core import APIError, Settings

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GOOGLE_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"


class TokenSource(Protocol):
    async def headers(self) -> dict[str, str]: ...


class GoogleADC:
    def __init__(self, project: str):
        self.project = project
        self._credentials: Any = None
        self._lock = asyncio.Lock()

    def _headers_sync(self) -> dict[str, str]:
        # Lazy import keeps unit tests independent of Google credentials/SDK installation.
        import google.auth
        from google.auth.transport.requests import Request
        if self._credentials is None:
            self._credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
                quota_project_id=self.project,
            )
        request = Request()
        # google-auth refresh is blocking, but this method runs in a worker thread.
        # Bound refresh transport timeout; synthesize POST itself is never retried.
        def bounded_request(*args, **kwargs):
            kwargs.setdefault("timeout", 30)
            return request(*args, **kwargs)
        result: dict[str, str] = {}
        try:
            self._credentials.before_request(bounded_request, "POST", GOOGLE_URL, result)
        finally:
            request.session.close()
        result["x-goog-user-project"] = self.project
        return result

    async def headers(self) -> dict[str, str]:
        async with self._lock:
            try:
                return await asyncio.to_thread(self._headers_sync)
            except Exception as exc:
                raise APIError(503, "Google ADC authentication failed. Check credential file, project, "
                               "API enablement and IAM permissions.", "google_auth_failed") from exc


def upstream_headers(response: httpx.Response) -> dict[str, str]:
    allowed = {"retry-after", "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
               "x-ratelimit-reset-requests"}
    return {k: v for k, v in response.headers.items() if k in allowed}


def check_upstream(response: httpx.Response, provider: str) -> None:
    if 200 <= response.status_code < 300:
        return
    status = response.status_code
    headers = upstream_headers(response)
    if status == 429:
        raise APIError(429, f"{provider} rate/quota limit reached. No retry or paid fallback was attempted.",
                       "upstream_rate_limit", headers=headers)
    if status in {401, 403}:
        raise APIError(502, f"{provider} rejected the server credentials/permissions. "
                       "Check provider credentials, project, enabled API and plan.", "upstream_auth_error")
    if status in {400, 404, 413, 415, 422}:
        raise APIError(400, f"{provider} rejected the request. Check input format, model and language. "
                       f"Upstream status: {status}.", "upstream_invalid_request")
    raise APIError(502, f"{provider} request failed (HTTP {status}).", "upstream_error")


class Providers:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, tokens: TokenSource | None = None):
        self.settings, self.client = settings, client
        self.tokens = tokens or GoogleADC(settings.google_cloud_project)

    async def transcribe(self, *, audio: bytes, filename: str, fields: dict[str, Any]) -> httpx.Response:
        response = await self.client.post(
            GROQ_URL, headers={"Authorization": f"Bearer {self.settings.groq_api_key}"},
            data=fields, files={"file": (filename, audio, "application/octet-stream")},
        )
        check_upstream(response, "Groq")
        return response

    async def synthesize(self, text: str, voice: str, speed: float,
                         headers: dict[str, str]) -> bytes:
        response = await self.client.post(GOOGLE_URL, headers=headers, json={
            "input": {"text": text},
            "voice": {"languageCode": "ko-KR", "name": voice},
            "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000,
                            "speakingRate": speed},
        })
        check_upstream(response, "Google Cloud TTS")
        try:
            encoded = response.json()["audioContent"]
            if not isinstance(encoded, str) or not encoded:
                raise ValueError("Missing audio")
            return base64.b64decode(encoded, validate=True)
        except (KeyError, ValueError, TypeError, binascii.Error) as exc:
            raise APIError(502, "Google returned invalid audioContent.", "invalid_upstream_response") from exc
