from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import struct
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import APIError, AudioCache, Settings, UsageLedger, WINDOW_SECONDS, count_units
from app.main import create_app
from app.media import encode_audio, merge_wavs, read_pcm, split_text, write_wav

KEY = "sk-local-unit-test-key-" + "x" * 40
AUTH = {"Authorization": f"Bearer {KEY}"}


def pcm_fixture() -> bytes:
    return b"".join(struct.pack("<h", int(2000 * math.sin(2 * math.pi * 440 * i / 24000)))
                    for i in range(4800))


class FakeTokens:
    async def headers(self):
        return {"Authorization": "Bearer fake-google", "x-goog-user-project": "test-project"}


class Upstream:
    def __init__(self):
        self.google_calls = []
        self.groq_calls = []
        self.failure = None

    def handle(self, request: httpx.Request):
        if request.url.host == "api.groq.com":
            assert request.headers["authorization"] == "Bearer fake-groq"
            self.groq_calls.append(request)
            if self.failure:
                return self.failure(request)
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
            fields = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name != "file":
                    fields.setdefault(name, []).append(part.get_payload(decode=True).decode())
            assert fields["model"] == ["whisper-large-v3-turbo"]
            result = {"text": "안녕하세요.", "language": "korean", "duration": 1.2,
                      "segments": [{"id": 0, "start": 0.0, "end": 1.2, "text": "안녕하세요."}],
                      "x_groq": {"id": "test"}}
            if fields.get("response_format") == ["text"]:
                return httpx.Response(200, text=result["text"])
            return httpx.Response(200, json=result, headers={"x-ratelimit-remaining-requests": "1999"})
        assert request.url.host == "texttospeech.googleapis.com"
        assert request.headers["authorization"] == "Bearer fake-google"
        payload = json.loads(request.content)
        self.google_calls.append(payload)
        if self.failure:
            return self.failure(request)
        assert len(payload["input"]["text"].encode()) <= 4500
        assert payload["audioConfig"]["audioEncoding"] == "LINEAR16"
        assert 0.25 <= payload["audioConfig"]["speakingRate"] <= 2
        return httpx.Response(200, json={"audioContent": base64.b64encode(write_wav(pcm_fixture())).decode()})


@pytest.fixture
def gateway(tmp_path):
    settings = Settings(KEY, "fake-groq", "test-project", data_dir=tmp_path)
    upstream = Upstream()
    app = create_app(settings, transport=httpx.MockTransport(upstream.handle), tokens=FakeTokens())
    with TestClient(app) as client:
        yield client, upstream, settings


def speak(client, **kwargs):
    return client.post("/v1/audio/speech", headers=AUTH,
                       json={"model": "tts-1", "input": "안녕하세요.", "voice": "alloy", "response_format": "wav", **kwargs})


def transcribe(client, **kwargs):
    return client.post("/v1/audio/transcriptions", headers=AUTH,
                       data={"model": "whisper-1", **kwargs}, files={"file": ("recording.wav", b"testaudio", "audio/wav")})


def test_health_is_not_upstream_probe(gateway):
    client, upstream, _ = gateway
    assert client.get("/health").json() == {"status": "ok", "upstreams_verified": False}
    assert not upstream.google_calls and not upstream.groq_calls


def test_authentication(gateway):
    client, upstream, _ = gateway
    response = client.post("/v1/audio/speech", json={"input": "secret"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert "secret" not in response.text
    assert not upstream.google_calls


def test_model_and_voice_discovery(gateway):
    client, _, _ = gateway
    assert {x["id"] for x in client.get("/v1/models", headers=AUTH).json()["data"]} == {
        "tts-1", "google-wavenet", "whisper-1", "whisper-large-v3-turbo"}
    assert client.get("/v1/voices", headers=AUTH).status_code == 200
    assert client.get("/usage").status_code == 401


def test_asr_alias_and_metadata(gateway):
    client, upstream, _ = gateway
    response = transcribe(client, language="ko")
    assert response.status_code == 200
    assert response.json()["text"] == "안녕하세요."
    assert "x_groq" not in response.json()
    assert response.headers["x-speech-model"] == "whisper-large-v3-turbo"
    assert response.headers["x-ratelimit-remaining-requests"] == "1999"
    assert len(upstream.groq_calls) == 1


@pytest.mark.parametrize("fmt", ["text", "json", "verbose_json", "srt", "vtt"])
def test_asr_formats(gateway, fmt):
    client, _, _ = gateway
    response = transcribe(client, response_format=fmt)
    assert response.status_code == 200
    if fmt == "srt":
        assert "00:00:00,000 --> 00:00:01,200" in response.text
    if fmt == "vtt":
        assert response.text.startswith("WEBVTT")
    if fmt == "text":
        assert response.text == "안녕하세요."


def test_asr_timestamp_list(gateway):
    client, upstream, _ = gateway
    response = transcribe(client, response_format="verbose_json", **{"timestamp_granularities[]": ["word", "segment"]})
    assert response.status_code == 200
    assert b'word' in upstream.groq_calls[0].content
    assert upstream.groq_calls[0].content.count(b'name="timestamp_granularities[]"') == 2


@pytest.mark.parametrize("extra", [
    {"model": "gpt-4o-transcribe"}, {"stream": "true"}, {"language": "ko-KR"},
    {"temperature": "nan"}, {"temperature": "3"}, {"response_format": "bad"},
    {"timestamp_granularities[]": "word"}, {"include[]": "logprobs"},
])
def test_asr_rejects_unsupported(gateway, extra):
    client, upstream, _ = gateway
    assert transcribe(client, **extra).status_code == 400
    assert not upstream.groq_calls


def test_asr_empty_file(gateway):
    client, upstream, _ = gateway
    response = client.post("/v1/audio/transcriptions", headers=AUTH, data={"model": "whisper-1"},
                           files={"file": ("empty.wav", b"")})
    assert response.status_code == 400
    assert not upstream.groq_calls


def test_tts_alias_cache_and_quota(gateway):
    client, upstream, _ = gateway
    response = speak(client)
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["x-tts-cache"] == "MISS"
    assert read_pcm(response.content) == pcm_fixture()
    assert upstream.google_calls[0]["voice"]["name"] == "ko-KR-Wavenet-A"
    again = speak(client)
    assert again.headers["x-tts-cache"] == "HIT"
    assert len(upstream.google_calls) == 1
    assert client.get("/usage", headers=AUTH).json()["rolling_32_days"]["reserved"] == count_units("안녕하세요.")


@pytest.mark.parametrize("fmt,magic", [("mp3", b"ID3"), ("opus", b"OggS"), ("aac", b"\xff"),
                                       ("flac", b"fLaC"), ("wav", b"RIFF"), ("pcm", None)])
def test_tts_all_six_formats(gateway, fmt, magic):
    client, _, _ = gateway
    response = speak(client, response_format=fmt)
    assert response.status_code == 200
    if magic:
        assert response.content.startswith(magic)
    else:
        assert response.content == pcm_fixture()


def test_long_korean_is_split_without_losing_characters(gateway):
    client, upstream, _ = gateway
    text = "가나다라마바사 아자차카타파하. " * 150
    response = speak(client, input=text)
    assert response.status_code == 200
    assert len(upstream.google_calls) >= 2
    assert "".join(c["input"]["text"] for c in upstream.google_calls) == text
    assert len(read_pcm(response.content)) == len(pcm_fixture()) * len(upstream.google_calls)


@pytest.mark.parametrize("extra", [
    {"voice": "ko-KR-Chirp3-HD-Kore"}, {"model": "tts-1-hd"},
    {"instructions": "whisper softly"}, {"stream_format": "sse"},
    {"input": "   "}, {"input": "가" * 4097}, {"speed": 4.1}, {"speed": 0.1},
    {"response_format": "bad"}, {"surprise": "not supported"},
])
def test_tts_rejects_unsupported_before_billing(gateway, extra):
    client, upstream, _ = gateway
    response = speak(client, **extra)
    assert response.status_code == 400
    assert not upstream.google_calls
    assert client.get("/usage", headers=AUTH).json()["rolling_32_days"]["reserved"] == 0


def test_speed_four_uses_ffmpeg_after_google(gateway):
    client, upstream, _ = gateway
    response = speak(client, speed=4.0)
    assert response.status_code == 200
    assert upstream.google_calls[0]["audioConfig"]["speakingRate"] == 2.0
    assert len(read_pcm(response.content)) < len(pcm_fixture())


def test_tts_quota_blocks_upstream(tmp_path):
    settings = Settings(KEY, "fake-groq", "test-project", data_dir=tmp_path, tts_limit=5, tts_daily_limit=5)
    upstream = Upstream()
    with TestClient(create_app(settings, transport=httpx.MockTransport(upstream.handle), tokens=FakeTokens())) as client:
        response = speak(client, input="가" * 6)
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "local_tts_limit"
        assert not upstream.google_calls


def test_groq_429_passthrough_no_retry(gateway):
    client, upstream, _ = gateway
    upstream.failure = lambda request: httpx.Response(429, json={"error": "limit"}, headers={"retry-after": "7"})
    response = transcribe(client)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert len(upstream.groq_calls) == 1


def test_tts_timeout_reservation_retained(gateway):
    client, upstream, _ = gateway
    def timeout(request):
        raise httpx.ReadTimeout("timeout", request=request)
    upstream.failure = timeout
    assert speak(client).status_code == 504
    assert len(upstream.google_calls) == 1
    assert client.get("/usage", headers=AUTH).json()["rolling_32_days"]["reserved"] == count_units("안녕하세요.")


def test_google_auth_failure_does_not_spend_quota(gateway):
    client, _, _ = gateway
    class BadTokens:
        async def headers(self):
            raise APIError(503, "Missing ADC", "google_auth_failed")
    client.app.state.providers.tokens = BadTokens()
    assert speak(client).status_code == 503
    assert client.get("/usage", headers=AUTH).json()["rolling_32_days"]["reserved"] == 0


def test_malformed_upstream_audio_is_safe(gateway):
    client, upstream, _ = gateway
    upstream.failure = lambda request: httpx.Response(200, json={"audioContent": "not-valid-base64@@"})
    response = speak(client)
    assert response.status_code == 502
    assert "안녕하세요" not in response.text


def test_json_error_does_not_echo_secret(gateway):
    client, _, _ = gateway
    response = client.post("/v1/audio/speech", headers=AUTH, content='{"private_secret": ',
                           extensions={},)
    assert response.status_code == 400
    assert "private_secret" not in response.text


def test_body_limit(gateway):
    client, upstream, _ = gateway
    response = client.post("/v1/audio/speech", headers=AUTH, content=b"x" * 128001)
    assert response.status_code == 413
    assert not upstream.google_calls


def test_file_limit(tmp_path):
    settings = Settings(KEY, "fake-groq", "test-project", data_dir=tmp_path, max_file_bytes=8)
    upstream = Upstream()
    with TestClient(create_app(settings, transport=httpx.MockTransport(upstream.handle), tokens=FakeTokens())) as client:
        assert transcribe(client).status_code == 413
        assert not upstream.groq_calls


def test_ledger_persists_and_expires(tmp_path):
    settings = Settings(KEY, "fake-groq", "test-project", data_dir=tmp_path)
    ledger = UsageLedger(settings)
    ledger.reserve(100, now=10000000)
    fresh = UsageLedger(settings)
    assert fresh.snapshot(now=10000001)["rolling_32_days"]["reserved"] == 100
    assert fresh.snapshot(now=10000000 + WINDOW_SECONDS + 1)["rolling_32_days"]["reserved"] == 0


def test_ledger_atomic_concurrent_reservations(tmp_path):
    settings = Settings(KEY, "fake-groq", "test-project", data_dir=tmp_path, tts_limit=10, tts_daily_limit=10)
    ledger = UsageLedger(settings)
    barrier = threading.Barrier(10)
    def reserve(_):
        barrier.wait()
        try:
            ledger.reserve(2)
            return True
        except APIError:
            return False
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(reserve, range(10)))
    assert sum(results) == 5
    assert ledger.snapshot()["rolling_32_days"]["reserved"] == 10


def test_utf8_split_and_utf16_accounting():
    text = "한국어 👨‍👩‍👦 punctuation.\n" * 500
    chunks = split_text(text)
    assert "".join(chunks) == text
    assert all(len(c.encode()) <= 4500 for c in chunks)
    assert count_units("가나") == 2
    assert count_units("😀") == 2


def test_cache_byte_bound():
    cache = AudioCache(10, 60)
    cache.put("a", b"123456")
    cache.put("b", b"123456")
    assert cache.get("a") is None
    assert cache.get("b") == b"123456"
    cache.put("big", b"x" * 11)
    assert cache.get("big") is None
    assert cache.size == 6


def test_wave_merge_has_valid_header():
    wav = write_wav(pcm_fixture())
    merged = merge_wavs([wav, wav])
    with wave.open(io.BytesIO(merged), "rb") as audio:
        assert audio.getnframes() == 9600
    assert read_pcm(merged) == pcm_fixture() * 2
