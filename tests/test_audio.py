import asyncio
import io
import json
import math
import struct
import sys
import wave

import pytest

from speech_proxy.audio import _run, audio_duration, encode_audio, split_text, wav_to_pcm
from speech_proxy.errors import APIError


def sample_pcm(seconds=0.2):
    return b"".join(
        struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * sample / 24000)))
        for sample in range(round(seconds * 24000))
    )


@pytest.mark.parametrize(
    "text", ["안녕하세요. " * 2000, "한" * 10001, "😀" * 6000, " a\n\t" * 5000, ""]
)
def test_text_splitting_preserves_every_character_and_byte_bound(text):
    chunks = split_text(text)
    assert "".join(chunks) == text
    assert all(0 < len(chunk.encode("utf-8")) <= 4500 for chunk in chunks)


def test_text_splitting_prefers_sentence_boundaries():
    assert split_text("First sentence. Second sentence. Third sentence.", 35) == [
        "First sentence. Second sentence. ",
        "Third sentence.",
    ]


@pytest.mark.parametrize("kwargs", [{"framerate": 16000}, {"nchannels": 2}, {"sampwidth": 1}])
def test_wrong_wav_contract_rejected(kwargs):
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(kwargs.get("nchannels", 1))
        target.setsampwidth(kwargs.get("sampwidth", 2))
        target.setframerate(kwargs.get("framerate", 24000))
        target.writeframes(b"\0" * 1024)
    with pytest.raises(APIError) as exc:
        wav_to_pcm(output.getvalue())
    assert exc.value.status == 502


async def test_pcm_and_wav_have_exact_sample_contract():
    pcm = sample_pcm()
    assert await encode_audio(pcm, "pcm", 10) == (pcm, "application/octet-stream")
    wav, mime = await encode_audio(pcm, "wav", 10)
    assert mime == "audio/wav"
    assert wav_to_pcm(wav) == pcm
    assert await audio_duration(wav, "ignored.wav", 1, 10) == pytest.approx(0.2)
    with pytest.raises(APIError):
        wav_to_pcm(wav[:-2])


@pytest.mark.parametrize(
    "fmt,codec,mime",
    [
        ("mp3", "mp3", "audio/mpeg"),
        ("opus", "opus", "audio/ogg"),
        ("aac", "aac", "audio/aac"),
        ("flac", "flac", "audio/flac"),
    ],
)
async def test_real_encodings(fmt, codec, mime):
    encoded, content_type = await encode_audio(sample_pcm(), fmt, 10)
    assert content_type == mime
    info = json.loads(
        await _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                "pipe:0",
            ],
            encoded,
            10,
            10000,
        )
    )
    stream = info["streams"][0]
    assert stream["codec_name"] == codec
    assert stream["channels"] == 1
    # Opus decoders expose their native 48 kHz even with a 24 kHz source.
    assert int(stream["sample_rate"]) == (48000 if fmt == "opus" else 24000)
    assert 0 < await audio_duration(encoded, f"input.{fmt}", 1, 10) <= 1


async def test_media_recorder_webm_without_duration_is_decoded_and_capped():
    webm = await _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "s16le",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-c:a",
            "libopus",
            "-f",
            "webm",
            "pipe:1",
        ],
        sample_pcm(1),
        10,
        100000,
    )
    assert await audio_duration(webm, "recording.webm", 2, 10) == pytest.approx(1, abs=0.02)
    with pytest.raises(APIError) as exc:
        await audio_duration(webm, "recording.webm", 0.5, 10)
    assert exc.value.code == "audio_too_long"


@pytest.mark.parametrize(
    "payload",
    [
        b"garbage",
        b"#EXTM3U\nhttp://127.0.0.1/private\n",
        b"ffconcat version 1.0\nfile '/etc/passwd'\n",
    ],
)
async def test_invalid_files_and_external_playlists_are_rejected(payload):
    with pytest.raises(APIError) as exc:
        await audio_duration(payload, "audio.wav", 1, 10)
    assert exc.value.status == 400


async def test_long_audio_rejected():
    wav, _ = await encode_audio(sample_pcm(0.5), "wav", 10)
    with pytest.raises(APIError) as exc:
        await audio_duration(wav, "audio.wav", 0.25, 10)
    assert exc.value.code == "audio_too_long"


async def test_subprocess_timeout_and_cancellation_reap_child(monkeypatch):
    original = asyncio.create_subprocess_exec
    processes = []
    second_started = asyncio.Event()

    async def capture(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        if len(processes) == 2:
            second_started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    command = [sys.executable, "-c", "import time; time.sleep(10)"]
    with pytest.raises(TimeoutError):
        await _run(command, b"", 0.05, 1024)
    assert processes[-1].returncode is not None
    task = asyncio.create_task(_run(command, b"", 10, 1024))
    await second_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[-1].returncode is not None


async def test_m4a_trailing_metadata_is_seekable(tmp_path):
    # Larger than FFmpeg's probe buffer: stdin-only decoding silently misses it.
    target = tmp_path / "recording.m4a"
    await _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=8",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(target),
        ],
        b"",
        10,
        1024,
    )
    assert await audio_duration(target.read_bytes(), "recording.m4a", 10, 10) == pytest.approx(
        8, abs=0.1
    )


@pytest.mark.parametrize("opened_before_cancel", [False, True])
async def test_cancelled_temp_write_unlinks_immediately(
    tmp_path, monkeypatch, opened_before_cancel
):
    import threading

    import speech_proxy.audio as audio_module

    original_temporary_file = audio_module.tempfile.NamedTemporaryFile
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()

    def create_file(**kwargs):
        return original_temporary_file(**kwargs, dir=tmp_path)

    def delayed_write(path, data):
        target = open(path, "r+b") if opened_before_cancel else None
        loop.call_soon_threadsafe(started.set)
        try:
            release.wait(timeout=5)
            if target is None:
                try:
                    audio_module._original_write_audio_file(path, data)
                except FileNotFoundError:
                    pass
            else:
                target.write(data)
        finally:
            if target:
                target.close()
            loop.call_soon_threadsafe(finished.set)

    async def probe(*_args):
        return b'{"streams":[{}],"format":{"format_name":"wav","duration":"0.1"}}'

    monkeypatch.setattr(audio_module.tempfile, "NamedTemporaryFile", create_file)
    monkeypatch.setattr(
        audio_module, "_original_write_audio_file", audio_module._write_audio_file, raising=False
    )
    monkeypatch.setattr(audio_module, "_write_audio_file", delayed_write)
    monkeypatch.setattr(audio_module, "_run", probe)
    task = asyncio.create_task(audio_module.audio_duration(b"audio", "sample.wav", 1, 10))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert list(tmp_path.iterdir())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(tmp_path.iterdir()) == []
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2)
    assert list(tmp_path.iterdir()) == []
