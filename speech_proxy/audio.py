"""Bounded audio conversion and lossless UTF-8 text chunking."""

from __future__ import annotations

import asyncio
import io
import json
import math
import re
import tempfile
import wave

from .errors import APIError

SAMPLE_RATE = 24_000
PCM_BYTES_PER_SECOND = SAMPLE_RATE * 2
_DEMUXERS = "wav,mp3,flac,ogg,matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,mpeg,aac"


def split_text(text: str, max_bytes: int = 4500) -> list[str]:
    """Split at natural boundaries when possible without changing any character."""
    if max_bytes < 4:
        raise ValueError("max_bytes must accommodate a UTF-8 code point (at least 4)")
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start
        used = 0
        while end < len(text):
            try:
                size = len(text[end].encode("utf-8"))
            except UnicodeEncodeError:
                raise APIError(
                    400, "Input must contain valid Unicode text.", param="input"
                ) from None
            if used + size > max_bytes:
                break
            used += size
            end += 1
        if end < len(text):
            # Prefer a sentence/line boundary in the latter half of the chunk,
            # otherwise a word boundary, then a code-point boundary.
            window = text[start:end]
            boundary = list(re.finditer(r"[.!?。！？][\"'’”)]*\s+|\n+", window))
            if not boundary:
                boundary = list(re.finditer(r"\s+", window))
            if boundary and boundary[-1].end() >= len(window) // 2:
                end = start + boundary[-1].end()
        chunks.append(text[start:end])
        start = end
    return chunks


def wav_to_pcm(audio: bytes) -> bytes:
    """Read Google's LINEAR16 container, checking the promised PCM contract."""
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() != SAMPLE_RATE
                or source.getcomptype() != "NONE"
            ):
                raise ValueError("unexpected PCM format")
            frames = source.getnframes()
            pcm = source.readframes(frames)
            if not pcm or len(pcm) != frames * 2:
                raise ValueError("empty or truncated WAV")
            return pcm
    except (wave.Error, EOFError, ValueError):
        raise APIError(
            502,
            "Speech provider returned invalid audio.",
            "invalid_upstream_response",
            error_type="server_error",
        ) from None


class _ProcessOutputLimit(Exception):
    pass


def _write_audio_file(path: str, audio: bytes) -> None:
    # Open an existing file only. If cancellation has already unlinked it,
    # this worker must never recreate a file that no request owns anymore.
    with open(path, "r+b") as target:
        target.write(audio)


async def _run(command: list[str], data: bytes, timeout: float, output_limit: int) -> bytes:  # noqa: ASYNC109
    """Drain both pipes with bounds, and reap the child even on cancellation."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise APIError(
            503,
            "Audio processing is unavailable.",
            "audio_processor_unavailable",
            error_type="server_error",
        ) from None

    async def read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while block := await stream.read(64 * 1024):
            size += len(block)
            if size > limit:
                raise _ProcessOutputLimit
            chunks.append(block)
        return b"".join(chunks)

    async def write_input() -> None:
        assert process.stdin is not None
        try:
            for offset in range(0, len(data), 64 * 1024):
                process.stdin.write(data[offset : offset + 64 * 1024])
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    assert process.stdout is not None and process.stderr is not None
    tasks = [
        asyncio.create_task(write_input()),
        asyncio.create_task(read_bounded(process.stdout, output_limit)),
        asyncio.create_task(read_bounded(process.stderr, 64 * 1024)),
        asyncio.create_task(process.wait()),
    ]
    try:
        async with asyncio.timeout(timeout):
            results = await asyncio.gather(*tasks)
        if process.returncode:
            raise ValueError("audio processor rejected input")
        return results[1]
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await process.wait()


async def audio_duration(audio: bytes, filename: str, max_seconds: float, timeout: float) -> float:  # noqa: ASYNC109
    """Measure bounded decoded audio, including duration-less MediaRecorder WebM.

    Upload filenames never become paths. A private, automatically deleted file
    permits seeking in MP4 with trailing metadata. The demuxer allowlist excludes
    playlists and MOV external data references are explicitly disabled.
    """
    del filename
    common = ["-v", "error", "-protocol_whitelist", "pipe", "-format_whitelist", _DEMUXERS]

    try:
        async with asyncio.timeout(timeout):
            info = json.loads(
                await _run(
                    [
                        "ffprobe",
                        *common,
                        "-select_streams",
                        "a:0",
                        "-show_entries",
                        "stream=codec_type,duration:format=duration,format_name",
                        "-of",
                        "json",
                        "pipe:0",
                    ],
                    audio,
                    timeout,
                    64 * 1024,
                )
            )
            streams = info.get("streams", [])
            if not streams:
                raise ValueError("no audio stream")
            metadata = info.get("format", {})
            durations = []
            for value in [streams[0].get("duration"), metadata.get("duration")]:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and value > 0:
                    durations.append(value)
            declared_duration = max(durations, default=0)
            if declared_duration > max_seconds:
                raise APIError(
                    400,
                    f"Audio exceeds the {max_seconds:g}-second limit.",
                    "audio_too_long",
                    "file",
                )
            # Decode even when metadata exists: forged metadata must not bypass
            # the duration budget. Both -t and the output-byte cap bound work.
            sample_rate = 8_000
            cap_seconds = max_seconds + 0.1
            demuxer = metadata["format_name"].split(",")[0]
            if demuxer not in _DEMUXERS.split(","):
                raise ValueError("unsupported demuxer")
            input_options = (
                ["-enable_drefs", "0", "-use_absolute_path", "0"] if demuxer == "mov" else []
            )
            # Establish ownership before awaiting the write. The writer uses
            # its own handle, so cancellation immediately closes/unlinks this
            # file without waiting for, or depending on GC of, the worker.
            with tempfile.NamedTemporaryFile(prefix="speech-proxy-", suffix=".audio") as source:
                await asyncio.to_thread(_write_audio_file, source.name, audio)
                decoded = await _run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-xerror",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-format_whitelist",
                        _DEMUXERS,
                        "-f",
                        demuxer,
                        *input_options,
                        "-threads",
                        "1",
                        "-i",
                        source.name,
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-sn",
                        "-dn",
                        "-t",
                        str(cap_seconds),
                        "-ac",
                        "1",
                        "-ar",
                        str(sample_rate),
                        "-c:a",
                        "pcm_s16le",
                        "-threads",
                        "1",
                        "-f",
                        "s16le",
                        "pipe:1",
                    ],
                    b"",
                    timeout,
                    math.ceil(cap_seconds * sample_rate * 2) + 4096,
                )
            if not decoded:
                raise ValueError("empty audio")
            duration = max(declared_duration, len(decoded) / (sample_rate * 2))
            if duration > max_seconds:
                raise APIError(
                    400,
                    f"Audio exceeds the {max_seconds:g}-second limit.",
                    "audio_too_long",
                    "file",
                )
            return duration
    except TimeoutError:
        raise APIError(
            408, "Audio validation timed out.", "audio_validation_timeout", "file"
        ) from None
    except (ValueError, TypeError, KeyError, _ProcessOutputLimit):
        raise APIError(
            400, "File must contain valid supported audio.", "invalid_audio", "file"
        ) from None


async def encode_audio(pcm: bytes, response_format: str, timeout: float) -> tuple[bytes, str]:  # noqa: ASYNC109
    if not pcm or len(pcm) % 2:
        raise APIError(
            502,
            "Speech provider returned invalid audio.",
            "invalid_upstream_response",
            error_type="server_error",
        )
    if response_format == "pcm":
        return pcm, "application/octet-stream"
    if response_format == "wav":
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(SAMPLE_RATE)
            target.writeframes(pcm)
        return output.getvalue(), "audio/wav"
    encoders = {
        "mp3": (["-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3"], "audio/mpeg"),
        "opus": (["-c:a", "libopus", "-b:a", "48k", "-f", "ogg"], "audio/ogg"),
        "aac": (["-c:a", "aac", "-b:a", "64k", "-f", "adts"], "audio/aac"),
        "flac": (["-c:a", "flac", "-f", "flac"], "audio/flac"),
    }
    if response_format not in encoders:
        raise APIError(400, "Unsupported response format.", param="response_format")
    options, content_type = encoders[response_format]
    try:
        result = await _run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "pipe",
                "-f",
                "s16le",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-map_metadata",
                "-1",
                "-threads",
                "1",
                *options,
                "pipe:1",
            ],
            pcm,
            timeout,
            len(pcm) * 2 + 64 * 1024,
        )
        if not result:
            raise ValueError("empty output")
        return result, content_type
    except TimeoutError:
        raise APIError(
            504, "Audio encoding timed out.", "audio_encoding_timeout", error_type="server_error"
        ) from None
    except (ValueError, _ProcessOutputLimit):
        raise APIError(
            500, "Audio encoding failed.", "audio_encoding_failed", error_type="server_error"
        ) from None


async def change_speed(pcm: bytes, speed_ratio: float, timeout: float) -> bytes:  # noqa: ASYNC109
    """Apply the remaining 1–2x tempo change above Google's 2x rate limit."""
    if not math.isfinite(speed_ratio) or not 1 <= speed_ratio <= 2:
        raise APIError(400, "Invalid audio speed.", param="speed")
    if speed_ratio == 1:
        return pcm
    try:
        result = await _run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "pipe",
                "-f",
                "s16le",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-af",
                f"atempo={speed_ratio:.8f}",
                "-threads",
                "1",
                "-c:a",
                "pcm_s16le",
                "-f",
                "s16le",
                "pipe:1",
            ],
            pcm,
            timeout,
            len(pcm) + 64 * 1024,
        )
        if not result or len(result) % 2:
            raise ValueError("invalid tempo output")
        return result
    except TimeoutError:
        raise APIError(
            504, "Audio processing timed out.", "audio_encoding_timeout", error_type="server_error"
        ) from None
    except (ValueError, _ProcessOutputLimit):
        raise APIError(
            500,
            "Audio speed adjustment failed.",
            "audio_encoding_failed",
            error_type="server_error",
        ) from None
