import asyncio
import base64
import io
import json
import math
import struct
import wave
from contextlib import asynccontextmanager
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI

from speech_proxy.app import create_app
from speech_proxy.config import Settings
from speech_proxy.errors import APIError

KEY = "test-private-key-for-local-gateway-123456789"


def wav_audio(seconds=0.2):
    pcm = b"".join(
        struct.pack("<h", int(5000 * math.sin(2 * math.pi * 440 * i / 24000)))
        for i in range(int(seconds * 24000))
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(pcm)
    return buffer.getvalue(), pcm


def multipart_fields(request):
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
    )
    result = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        result.setdefault(name, []).append(part.get_payload(decode=True))
    return result


@pytest.fixture
def gateway(tmp_path):
    @asynccontextmanager
    async def start(*, use_default_google_auth=False, **overrides):
        state = SimpleNamespace(
            google_calls=[],
            google_requests=[],
            groq_calls=[],
            auth_calls=0,
            google_status=200,
            groq_status=200,
            auth_failure=False,
            google_failure=None,
            groq_failure=None,
            google_delay=0,
            transcript={
                "text": "안녕하세요",
                "segments": [{"id": 0, "start": 0.05, "end": 1.25, "text": "안녕하세요"}],
                "x_groq": {"id": "not-openai-metadata"},
            },
            audio=wav_audio()[0],
            pcm=wav_audio()[1],
            google_started=asyncio.Event(),
        )

        async def tokens():
            state.auth_calls += 1
            if state.auth_failure:
                raise APIError(503, "ADC unavailable", "provider_not_configured")
            return {"Authorization": "Bearer fake-google-token"}

        async def handler(request):
            if request.url.host == "texttospeech.googleapis.com":
                state.google_calls.append(json.loads(request.content))
                state.google_requests.append(request)
                state.google_started.set()
                if state.google_delay:
                    await asyncio.sleep(state.google_delay)
                if state.google_failure:
                    raise state.google_failure
                return httpx.Response(
                    state.google_status,
                    json={"audioContent": base64.b64encode(state.audio).decode()},
                    headers={"Retry-After": "7"},
                )
            if request.url.host == "api.groq.com":
                fields = multipart_fields(request)
                state.groq_calls.append(fields)
                if state.groq_failure:
                    raise state.groq_failure
                if fields["response_format"] == [b"text"]:
                    return httpx.Response(state.groq_status, text="안녕하세요")
                return httpx.Response(
                    state.groq_status, json=state.transcript, headers={"Retry-After": "7"}
                )
            raise AssertionError(f"Unexpected host: {request.url.host}")

        settings = Settings(
            _env_file=None,
            **{
                "proxy_api_key": KEY,
                "groq_api_key": "fake-groq-key",
                "groq_free_tier_confirmed": True,
                "usage_db_path": str(tmp_path / "usage.sqlite3"),
                **overrides,
            },
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            app = create_app(
                settings,
                http_client=upstream,
                google_token_provider=None if use_default_google_auth else tokens,
            )
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://gateway",
                    headers={"Authorization": f"Bearer {KEY}"},
                ) as client:
                    yield client, state, app

    return start


def speech_body(**overrides):
    return {"model": "tts-1", "voice": "alloy", "input": "안녕하세요", **overrides}


async def transcribe(client, **fields):
    return await client.post(
        "/v1/audio/transcriptions",
        data={"model": "whisper-1", **fields},
        files={"file": ("audio.wav", wav_audio()[0], "audio/wav")},
    )


async def counters(client):
    return (await client.get("/usage")).json()["usage"]


async def test_health_auth_discovery(gateway):
    async with gateway() as (client, state, _):
        client.headers.pop("authorization")
        health = await client.get("/health")
        assert health.status_code == 200
        for path in ("/usage", "/ready", "/v1/models", "/v1/voices"):
            response = await client.get(path)
            assert response.status_code == 401
            assert response.json()["error"]["type"] == "authentication_error"
            assert response.headers["www-authenticate"] == "Bearer"
            assert response.headers["x-request-id"]
        client.headers["authorization"] = f"bearer {KEY}"
        assert (await client.get("/ready")).json()["upstream_verified"] is False
        assert "tts-1-hd" in {
            item["id"] for item in (await client.get("/v1/models")).json()["data"]
        }
        voices = (await client.get("/v1/voices")).json()["data"]
        assert next(v for v in voices if v["id"] == "cedar")["provider_voice"].endswith("D")
        assert not state.google_calls and not state.groq_calls and not state.auth_calls


@pytest.mark.parametrize("google_key", [None, "", "test-google-cloud-api-key-1234567890"])
async def test_readiness_reports_google_auth_mode_without_secrets_or_provider_calls(
    gateway, google_key
):
    async with gateway(google_api_key=google_key) as (client, state, _):
        response = await client.get("/ready")

        assert response.status_code == 200
        assert response.json()["google_api_key_configured"] is bool(google_key)
        assert response.json()["google_auth"] == (
            "API key" if google_key else "ADC resolved on first synthesis"
        )
        assert response.json()["upstream_verified"] is False
        assert not state.google_calls and not state.auth_calls
        if google_key:
            assert google_key not in response.text
        assert KEY not in response.text


async def test_speech_with_google_api_key_preserves_auth_quota_and_cache(gateway, monkeypatch):
    google_key = "test-google-cloud-api-key-1234567890"

    async def unexpected_adc(self):
        raise AssertionError("API key synthesis must not access ADC")

    monkeypatch.setattr("speech_proxy.providers.ADCAuth.headers", unexpected_adc)
    async with gateway(
        use_default_google_auth=True,
        google_api_key=google_key,
        google_application_credentials="/nonexistent/google-adc.json",
    ) as (client, state, _):
        responses = [
            await client.post("/v1/audio/speech", json=speech_body(response_format="pcm"))
            for _ in range(2)
        ]

        assert all(response.status_code == 200 for response in responses)
        assert all(response.content == state.pcm for response in responses)
        assert [response.headers["x-tts-cache"] for response in responses] == ["MISS", "HIT"]
        assert len(state.google_requests) == 1
        request = state.google_requests[0]
        assert request.headers["x-goog-api-key"] == google_key
        assert "authorization" not in request.headers
        assert "x-goog-user-project" not in request.headers
        assert not request.url.query
        assert google_key.encode() not in request.content
        assert not state.auth_calls
        assert (await counters(client))["google_tts"]["32_days"]["amount"] == len("안녕하세요")


@pytest.mark.parametrize("fmt", ["json", "text", "verbose_json", "srt", "vtt"])
async def test_transcription_formats_and_korean_default(gateway, fmt):
    async with gateway() as (client, state, _):
        result = await transcribe(client, response_format=fmt)
        assert result.status_code == 200, result.text
        fields = state.groq_calls[0]
        assert fields["model"] == [b"whisper-large-v3-turbo"]
        assert fields["language"] == [b"ko"]
        assert result.headers["x-speech-provider"] == "groq"
        assert "안녕하세요" in result.text
        if fmt in {"srt", "vtt"}:
            assert fields["response_format"] == [b"verbose_json"]
            assert fields["timestamp_granularities[]"] == [b"segment"]
            assert ("00:00:00,050" if fmt == "srt" else "00:00:00.050") in result.text
        elif fmt != "text":
            assert "x_groq" not in result.json()
        assert (await counters(client))["groq_stt"]["audio_day"]["amount"] == 10


async def test_explicit_groq_model_and_auto_language(gateway):
    async with gateway(asr_default_language="auto") as (client, state, _):
        result = await transcribe(client, model="whisper-large-v3")
        assert result.status_code == 200
        assert state.groq_calls[0]["model"] == [b"whisper-large-v3"]
        assert "language" not in state.groq_calls[0]
        assert result.headers["x-speech-model"] == "whisper-large-v3"


async def test_repeated_timestamp_granularities(gateway):
    async with gateway() as (client, state, _):
        result = await transcribe(
            client,
            response_format="verbose_json",
            **{"timestamp_granularities[]": ["word", "segment"]},
        )
        assert result.status_code == 200
        assert state.groq_calls[0]["timestamp_granularities[]"] == [b"word", b"segment"]


@pytest.mark.parametrize(
    "fields",
    [
        {"model": "unknown"},
        {"response_format": "diarized_json"},
        {"temperature": "NaN"},
        {"temperature": "1.1"},
        {"language": "Korean"},
        {"stream": "true"},
        {"timestamp_granularities[]": ["word"]},
        {"extra": "unsupported"},
    ],
)
async def test_invalid_transcription_never_calls_provider(gateway, fields):
    async with gateway() as (client, state, _):
        result = await transcribe(client, **fields)
        assert result.status_code == 400
        assert not state.groq_calls
        assert (await counters(client))["groq_stt"]["day"]["requests"] == 0


@pytest.mark.parametrize("audio", [b"", b"not audio"])
async def test_bad_audio_rejected_before_reservation(gateway, audio):
    async with gateway() as (client, state, _):
        response = await client.post(
            "/v1/audio/transcriptions",
            data={"model": "whisper-1"},
            files={"file": ("audio.wav", audio)},
        )
        assert response.status_code == 400
        assert not state.groq_calls
        assert (await counters(client))["groq_stt"]["day"]["requests"] == 0


async def test_audio_size_and_duration_limits(gateway):
    async with gateway(max_upload_bytes=100) as (client, state, _):
        assert (await transcribe(client)).status_code == 413
        assert not state.groq_calls
    async with gateway(max_audio_seconds=0.05) as (client, state, _):
        assert (await transcribe(client)).status_code == 400
        assert not state.groq_calls


async def test_free_plan_confirmation(gateway):
    async with gateway(groq_free_tier_confirmed=False) as (client, state, _):
        response = await transcribe(client)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "free_tier_not_confirmed"
        assert not state.groq_calls


async def test_groq_local_rate_limit_blocks_upstream(gateway):
    async with gateway(groq_minute_request_limit=1) as (client, state, _):
        assert (await transcribe(client)).status_code == 200
        blocked = await transcribe(client)
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) > 0
        assert len(state.groq_calls) == 1


@pytest.mark.parametrize(
    "fmt,magic",
    [
        ("mp3", b"ID3"),
        ("wav", b"RIFF"),
        ("pcm", None),
        ("opus", b"OggS"),
        ("aac", b"\xff"),
        ("flac", b"fLaC"),
    ],
)
async def test_tts_formats_and_paseo_pcm(gateway, fmt, magic):
    async with gateway() as (client, state, _):
        response = await client.post("/v1/audio/speech", json=speech_body(response_format=fmt))
        assert response.status_code == 200, response.text
        assert response.headers["x-speech-voice"] == "ko-KR-Wavenet-A"
        if magic:
            assert response.content.startswith(magic)
        else:
            assert response.content == state.pcm
        sent = state.google_calls[0]
        assert sent["audioConfig"] == {
            "audioEncoding": "LINEAR16",
            "sampleRateHertz": 24000,
            "speakingRate": 1.0,
        }


async def test_original_models_voices_speed_and_default_voice(gateway):
    async with gateway() as (client, state, _):
        response = await client.post(
            "/v1/audio/speech", json=speech_body(model="google-wavenet", voice="cedar", speed=4)
        )
        assert response.status_code == 200
        assert state.google_calls[0]["audioConfig"]["speakingRate"] == 2
        assert state.google_calls[0]["voice"]["name"] == "ko-KR-Wavenet-D"
        response = await client.post(
            "/v1/audio/speech", json={"model": "tts-1-hd", "input": "기본"}
        )
        assert response.status_code == 200


async def test_long_korean_unicode_text_split_and_accounting(gateway):
    text = "안녕하세요! 🙂\n" * 500
    async with gateway() as (client, state, _):
        response = await client.post(
            "/v1/audio/speech", json=speech_body(input=text, response_format="wav")
        )
        assert response.status_code == 200
        chunks = [call["input"]["text"] for call in state.google_calls]
        assert len(chunks) > 1 and "".join(chunks) == text
        assert all(len(chunk.encode()) <= 4500 for chunk in chunks)
        with wave.open(io.BytesIO(response.content)) as audio:
            assert audio.getnframes() * 2 == len(state.pcm) * len(chunks)
        assert (await counters(client))["google_tts"]["32_days"]["amount"] == len(
            text.encode("utf-16-le")
        ) // 2


async def test_cache_cross_format_and_single_concurrent_synthesis(gateway):
    async with gateway() as (client, state, app):
        state.google_delay = 0.05
        responses = await asyncio.gather(
            *(
                client.post("/v1/audio/speech", json=speech_body(response_format=fmt))
                for fmt in ("pcm", "wav", "mp3", "opus")
            )
        )
        assert all(response.status_code == 200 for response in responses)
        assert len(state.google_calls) == 1
        assert [r.headers["x-tts-cache"] for r in responses].count("MISS") == 1
        assert (await counters(client))["google_tts"]["32_days"]["amount"] == len("안녕하세요")
        assert not app.state.synthesis_locks


@pytest.mark.parametrize(
    "override",
    [
        {"input": " "},
        {"input": "\ud800"},
        {"voice": "ko-KR-Neural2-A"},
        {"model": "unknown"},
        {"response_format": "invalid"},
        {"speed": 4.1},
        {"instructions": "Speak softly"},
        {"stream_format": "sse"},
        {"unsupported": "value"},
    ],
)
async def test_invalid_tts_does_not_spend_quota(gateway, override):
    async with gateway() as (client, state, _):
        response = await client.post(
            "/v1/audio/speech",
            content=json.dumps(speech_body(**override)),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert not state.google_calls and state.auth_calls == 0
        assert (await counters(client))["google_tts"]["32_days"]["amount"] == 0


async def test_adc_failure_does_not_spend_quota(gateway):
    async with gateway() as (client, state, _):
        state.auth_failure = True
        response = await client.post("/v1/audio/speech", json=speech_body())
        assert response.status_code == 503
        assert not state.google_calls
        assert (await counters(client))["google_tts"]["32_days"]["amount"] == 0


async def test_tts_quota_full_text_reservation_before_first_chunk(gateway):
    async with gateway(tts_32day_char_limit=10) as (client, state, _):
        response = await client.post("/v1/audio/speech", json=speech_body(input="가" * 3000))
        assert response.status_code == 429
        assert not state.google_calls


async def test_provider_timeout_keeps_reservation_without_retry(gateway):
    async with gateway() as (client, state, _):
        state.google_failure = httpx.ReadTimeout("must-not-echo-secret")
        response = await client.post("/v1/audio/speech", json=speech_body())
        assert response.status_code == 504
        assert "must-not-echo-secret" not in response.text
        assert len(state.google_calls) == 1
        assert (await counters(client))["google_tts"]["32_days"]["amount"] == len("안녕하세요")


async def test_upstream_429_retry_after_and_no_retry(gateway):
    async with gateway() as (client, state, _):
        state.groq_status = 429
        response = await transcribe(client)
        assert response.status_code == 429
        assert response.headers["retry-after"] == "7"
        assert len(state.groq_calls) == 1


async def test_malformed_upstream_audio_and_subtitle_data(gateway):
    async with gateway() as (client, state, _):
        state.audio = b"invalid wav"
        assert (await client.post("/v1/audio/speech", json=speech_body())).status_code == 502
        state.transcript["segments"] = []
        assert (await transcribe(client, response_format="srt")).status_code == 502


async def test_request_guard_chunked_body_limit_before_parse(gateway):
    async with gateway(max_tts_input_chars=1) as (client, state, _):

        async def chunks():
            yield b"x" * 4096
            yield b"x" * 4096
            yield b"x" * 100

        response = await client.post("/v1/audio/speech", content=chunks())
        assert response.status_code == 413
        assert not state.google_calls


async def test_auth_precedes_body_consumption(gateway):
    async with gateway() as (client, state, _):

        async def chunks():
            raise AssertionError("Unauthenticated body should never be consumed")
            yield b""  # pragma: no cover

        response = await client.post(
            "/v1/audio/speech", content=chunks(), headers={"Authorization": "Bearer invalid"}
        )
        assert response.status_code == 401
        assert not state.google_calls


async def test_busy_requests_are_rejected_and_slots_recover(gateway):
    async with gateway(max_concurrent_requests=1) as (client, state, _):
        state.google_delay = 0.1
        first = asyncio.create_task(client.post("/v1/audio/speech", json=speech_body()))
        await asyncio.wait_for(state.google_started.wait(), timeout=5)
        second = await client.post("/v1/audio/speech", json=speech_body(input="다음"))
        assert second.status_code == 503
        assert (await first).status_code == 200
        assert (await client.post("/v1/audio/speech", json=speech_body())).status_code == 200


async def test_overall_request_timeout_and_lock_cleanup(gateway):
    async with gateway(request_timeout_seconds=0.05) as (client, state, app):
        state.google_delay = 0.2
        response = await client.post("/v1/audio/speech", json=speech_body())
        assert response.status_code == 504
        assert not app.state.synthesis_locks
        assert (await counters(client))["google_tts"]["32_days"]["amount"] == len("안녕하세요")


async def test_openai_sdk_transcription_and_binary_streaming(gateway, tmp_path):
    async with gateway() as (client, state, _):
        sdk = AsyncOpenAI(
            api_key=KEY, base_url="http://gateway/v1", http_client=client, max_retries=0
        )
        result = await sdk.audio.transcriptions.create(
            file=("speech.wav", wav_audio()[0], "audio/wav"), model="whisper-1", language="ko"
        )
        assert result.text == "안녕하세요"
        async with sdk.audio.speech.with_streaming_response.create(
            model="tts-1", voice="alloy", input="Paseo 음성 확인", response_format="pcm"
        ) as response:
            destination = tmp_path / "speech.pcm"
            await response.stream_to_file(destination)
        assert destination.read_bytes() == state.pcm
        assert (await sdk.models.list()).data
