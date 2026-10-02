import asyncio
import base64
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from speech_proxy.audio import encode_audio
from speech_proxy.config import Settings
from speech_proxy.errors import APIError
from speech_proxy.providers import ADCAuth, GoogleProvider, GroqProvider


def settings(**kwargs):
    return Settings(
        _env_file=None, proxy_api_key="a" * 40, groq_api_key="upstream-secret", **kwargs
    )


async def token():
    return {"Authorization": "Bearer google-secret", "x-goog-user-project": "test-project"}


async def test_groq_multipart_forwards_fields_and_overrides_model():
    def handler(request):
        assert str(request.url) == "https://api.groq.com/openai/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer upstream-secret"
        body = request.content.decode()
        assert body.count('name="timestamp_granularities[]"') == 2
        assert "whisper-large-v3-turbo" in body
        assert "whisper-1" not in body
        assert "sample.wav" in body
        assert "ko" in body
        return httpx.Response(200, json={"text": "안녕하세요", "segments": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GroqProvider(settings(), client)
        response = await provider.transcribe(
            b"audio",
            "sample.wav",
            "audio/wav",
            {
                "model": "whisper-1",
                "language": "ko",
                "response_format": "verbose_json",
                "timestamp_granularities[]": ["word", "segment"],
            },
        )
        assert response["text"] == "안녕하세요"


async def test_groq_plain_text():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="한국어"))
    ) as client:
        assert (
            await GroqProvider(settings(), client).transcribe(
                b"audio",
                "audio.wav",
                "audio/wav",
                {"response_format": "text"},
            )
            == "한국어"
        )


@pytest.mark.parametrize(
    "upstream,expected",
    [
        (400, 400),
        (401, 502),
        (403, 502),
        (413, 413),
        (422, 400),
        (429, 429),
        (500, 502),
        (504, 504),
        (302, 502),
    ],
)
async def test_upstream_errors_are_sanitized_without_retries(upstream, expected):
    calls = 0

    def handler(_):
        nonlocal calls
        calls += 1
        return httpx.Response(
            upstream,
            text="upstream-secret google-secret private text",
            headers={"Retry-After": "20", "Location": "https://example.com"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(APIError) as exc:
            await GroqProvider(settings(), client).transcribe(b"audio", "a.wav", "audio/wav", {})
    assert calls == 1
    assert exc.value.status == expected
    assert "secret" not in exc.value.message
    if upstream == 429:
        assert exc.value.headers == {"Retry-After": "20"}


@pytest.mark.parametrize("failure,expected", [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)])
async def test_http_transport_errors(failure, expected):
    def handler(_):
        raise failure("https://secret.invalid secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(APIError) as exc:
            await GroqProvider(settings(), client).transcribe(b"audio", "a.wav", "audio/wav", {})
    assert exc.value.status == expected
    assert "secret" not in exc.value.message


async def test_google_splits_utf8_and_joins_pcm_without_wav_headers():
    parts = []
    pcm = b"\0\0\1\0" * 500
    wav, _ = await encode_audio(pcm, "wav", 10)

    def handler(request):
        assert str(request.url) == "https://texttospeech.googleapis.com/v1/text:synthesize"
        assert request.headers["x-goog-user-project"] == "test-project"
        body = json.loads(request.content)
        parts.append(body["input"]["text"])
        assert len(parts[-1].encode()) <= 4500
        assert body["voice"] == {"languageCode": "ko-KR", "name": "ko-KR-Wavenet-A"}
        assert body["audioConfig"] == {
            "audioEncoding": "LINEAR16",
            "sampleRateHertz": 24000,
            "speakingRate": 1.2,
        }
        return httpx.Response(200, json={"audioContent": base64.b64encode(wav).decode()})

    text = "안녕하세요. 한국어를 읽습니다. " * 400
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await GoogleProvider(settings(), client, token).synthesize(
            text, "ko-KR-Wavenet-A", 1.2
        )
    assert "".join(parts) == text
    assert len(parts) > 1
    assert result == pcm * len(parts)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"audioContent": "!!bad!!"},
        {"audioContent": 123},
        {"audioContent": base64.b64encode(b"not wav").decode()},
    ],
)
async def test_google_invalid_audio_rejected(payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(APIError) as exc:
            await GoogleProvider(settings(), client, token).synthesize("test", "ko-KR-Wavenet-A", 1)
    assert exc.value.status == 502


async def test_google_total_pcm_limit():
    wav, _ = await encode_audio(b"\0\0" * 100, "wav", 10)
    response = {"audioContent": base64.b64encode(wav).decode()}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as client:
        with pytest.raises(APIError) as exc:
            await GoogleProvider(settings(max_output_audio_bytes=100), client, token).synthesize(
                "test", "ko-KR-Wavenet-A", 1
            )
    assert exc.value.code == "audio_too_large"


async def test_adc_lazy_cached_refresh_runs_off_event_loop(monkeypatch):
    main_thread = threading.get_ident()
    calls = {"load": 0, "refresh": 0}
    credential = SimpleNamespace(
        valid=False, token="test-token", quota_project_id="credential-quota"
    )

    def refresh(_):
        assert threading.get_ident() != main_thread
        calls["refresh"] += 1
        time.sleep(0.03)
        credential.valid = True

    credential.refresh = refresh

    def default(**kwargs):
        assert threading.get_ident() != main_thread
        calls["load"] += 1
        assert kwargs["scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]
        return credential, "project"

    monkeypatch.setattr("speech_proxy.providers.google.auth.default", default)
    auth = ADCAuth(None, 1)
    assert calls == {"load": 0, "refresh": 0}
    headers = await asyncio.gather(*(auth.headers() for _ in range(5)))
    assert all(header["x-goog-user-project"] == "credential-quota" for header in headers)
    await auth.headers()
    assert calls == {"load": 1, "refresh": 1}
    credential.valid = False
    await auth.headers()
    assert calls == {"load": 1, "refresh": 2}


async def test_adc_timeout_shares_inflight_refresh_and_sanitizes_failures(monkeypatch):
    calls = 0

    def default(**_):
        nonlocal calls
        calls += 1
        time.sleep(0.07)
        raise RuntimeError("private credential path and token")

    monkeypatch.setattr("speech_proxy.providers.google.auth.default", default)
    auth = ADCAuth("explicit-project", 0.01)
    for _ in range(2):
        with pytest.raises(APIError) as exc:
            await auth.headers()
        assert exc.value.status == 504
    assert calls == 1
    await asyncio.sleep(0.08)

    def failed_default(**_):
        raise RuntimeError("private credential path and token")

    monkeypatch.setattr("speech_proxy.providers.google.auth.default", failed_default)
    with pytest.raises(APIError) as exc:
        await auth.headers()
    assert exc.value.status == 503
    assert "private" not in exc.value.message


async def test_groq_preserves_explicit_supported_model():
    def handler(request):
        body = request.content.decode()
        assert "whisper-large-v3\r\n" in body
        assert "whisper-large-v3-turbo" not in body
        return httpx.Response(200, json={"text": "한국어"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await GroqProvider(settings(), client).transcribe(
            b"audio",
            "a.wav",
            "audio/wav",
            {"model": "whisper-large-v3"},
        )


async def test_google_speed_four_reuses_authorization_and_adjusts_pcm():
    import math
    import struct

    pcm = b"".join(
        struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * n / 24000))) for n in range(24000)
    )
    wav, _ = await encode_audio(pcm, "wav", 10)

    async def unexpected_auth():
        raise AssertionError("Must reuse the preauthorized token")

    def handler(request):
        assert request.headers["authorization"] == "Bearer authorized"
        assert json.loads(request.content)["audioConfig"]["speakingRate"] == 2
        return httpx.Response(200, json={"audioContent": base64.b64encode(wav).decode()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await GoogleProvider(settings(), client, unexpected_auth).synthesize(
            "안녕하세요",
            "ko-KR-Wavenet-A",
            4,
            auth_headers={"Authorization": "Bearer authorized"},
        )
    assert len(result) / len(pcm) == pytest.approx(0.5, abs=0.03)
    assert len(result) % 2 == 0


async def test_adc_explicit_credentials_file_and_quota_project(monkeypatch):
    credential = SimpleNamespace(valid=True, token="token", quota_project_id="from-file")
    calls = []

    def load(filename, **kwargs):
        calls.append((filename, kwargs))
        return credential, "source-project"

    monkeypatch.setattr("speech_proxy.providers.google.auth.load_credentials_from_file", load)
    auth = ADCAuth("billing-project", 1, "/safe/credentials.json")
    headers = await auth.headers()
    assert calls[0][0] == "/safe/credentials.json"
    assert calls[0][1]["quota_project_id"] == "billing-project"
    assert headers == {"Authorization": "Bearer token", "x-goog-user-project": "billing-project"}


async def test_google_api_key_is_header_only_and_takes_precedence_over_adc(monkeypatch, caplog):
    google_key = "test-google-cloud-api-key-1234567890"
    adc_calls = []
    requests = []
    pcm = b"\0\0\1\0" * 100
    wav, _ = await encode_audio(pcm, "wav", 10)

    async def unexpected_adc(self):
        adc_calls.append(True)
        raise AssertionError("API key authentication must not load ADC")

    monkeypatch.setattr(ADCAuth, "headers", unexpected_adc)

    def handler(request):
        requests.append(request)
        assert request.headers["x-goog-api-key"] == google_key
        assert "authorization" not in request.headers
        assert "x-goog-user-project" not in request.headers
        assert not request.url.query
        assert google_key not in str(request.url)
        assert google_key.encode() not in request.content
        return httpx.Response(200, json={"audioContent": base64.b64encode(wav).decode()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleProvider(
            settings(
                google_api_key=google_key,
                google_application_credentials="/does-not-exist/google-adc.json",
                google_cloud_project="must-not-be-used-as-quota-project",
            ),
            client,
        )
        result = await provider.synthesize("한국어 API 키 합성", "ko-KR-Wavenet-A", 1)

    assert result == pcm
    assert len(requests) == 1
    assert not adc_calls
    assert google_key not in caplog.text


async def test_google_api_key_forbidden_does_not_retry_or_fall_back_to_adc(monkeypatch, caplog):
    google_key = "test-google-cloud-api-key-1234567890"
    calls = []
    adc_calls = []

    async def unexpected_adc(self):
        adc_calls.append(True)
        raise AssertionError("A rejected API key must not silently switch credentials")

    monkeypatch.setattr(ADCAuth, "headers", unexpected_adc)

    def handler(request):
        calls.append(request)
        assert request.headers["x-goog-api-key"] == google_key
        return httpx.Response(403, text=f"Permission denied for private key {google_key}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleProvider(settings(google_api_key=google_key), client)
        with pytest.raises(APIError) as error:
            await provider.synthesize("안녕하세요", "ko-KR-Wavenet-A", 1)

    assert len(calls) == 1
    assert not adc_calls
    assert error.value.status == 502
    assert error.value.code == "upstream_authentication_error"
    assert google_key not in json.dumps(error.value.payload())
    assert google_key not in caplog.text


async def test_injected_google_token_provider_still_overrides_configured_api_key():
    google_key = "test-google-cloud-api-key-1234567890"
    calls = []
    wav, _ = await encode_audio(b"\0\0" * 100, "wav", 10)

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer google-secret"
        assert request.headers["x-goog-user-project"] == "test-project"
        assert "x-goog-api-key" not in request.headers
        return httpx.Response(200, json={"audioContent": base64.b64encode(wav).decode()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await GoogleProvider(settings(google_api_key=google_key), client, token).synthesize(
            "안녕하세요", "ko-KR-Wavenet-A", 1
        )

    assert len(calls) == 1


@pytest.mark.parametrize("google_key", [None, ""])
async def test_google_missing_or_empty_api_key_uses_default_adc(google_key, monkeypatch):
    calls = []
    credential = SimpleNamespace(valid=True, token="adc-access-token", quota_project_id="adc-quota")

    def default(**kwargs):
        calls.append(kwargs)
        return credential, "adc-project"

    monkeypatch.setattr("speech_proxy.providers.google.auth.default", default)
    async with httpx.AsyncClient() as client:
        provider = GoogleProvider(settings(google_api_key=google_key), client)
        headers = await provider.authorize()

    assert headers == {
        "Authorization": "Bearer adc-access-token",
        "x-goog-user-project": "adc-quota",
    }
    assert len(calls) == 1
