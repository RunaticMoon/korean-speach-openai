from __future__ import annotations

import asyncio
import io
import math
import wave

from .core import APIError

MIME = {"mp3": "audio/mpeg", "wav": "audio/wav", "pcm": "application/octet-stream",
        "opus": "audio/ogg", "aac": "audio/aac", "flac": "audio/flac"}


def split_text(text: str, max_bytes: int = 4500) -> list[str]:
    """Preserve all characters; prefer sentence/whitespace boundaries within UTF-8 cap."""
    if max_bytes < 4:
        raise ValueError("max_bytes must be at least 4")
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining.encode("utf-8")) <= max_bytes:
            chunks.append(remaining)
            break
        length, end, preferred = 0, 0, 0
        for index, char in enumerate(remaining):
            size = len(char.encode("utf-8"))
            if length + size > max_bytes:
                break
            length += size
            end = index + 1
            if char.isspace() or char in ".!?。！？":
                preferred = end
        cut = preferred if preferred >= end // 2 else end
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    return chunks


def read_pcm(wav: bytes) -> bytes:
    try:
        with wave.open(io.BytesIO(wav), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 24000, "NONE"):
                raise ValueError("Unexpected WAV format")
            data = audio.readframes(audio.getnframes())
            if not data or len(data) % 2:
                raise ValueError("Missing or invalid PCM frames")
            return data
    except (wave.Error, EOFError, ValueError) as exc:
        raise APIError(502, "Google returned unexpected audio; expected 24kHz mono 16-bit WAV.",
                       "invalid_upstream_audio") from exc


def write_wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(pcm)
    return out.getvalue()


def merge_wavs(chunks: list[bytes]) -> bytes:
    if not chunks:
        raise APIError(502, "No audio returned.", "invalid_upstream_audio")
    return write_wav(b"".join(read_pcm(chunk) for chunk in chunks))


async def encode_audio(wav: bytes, fmt: str, extra_speed: float = 1.0) -> bytes:
    if fmt not in MIME:
        raise APIError(400, "Unsupported audio format.", param="response_format")
    if extra_speed == 1.0:
        if fmt == "wav":
            return wav
        if fmt == "pcm":
            return read_pcm(wav)
    outputs = {
        "mp3": ["-c:a", "libmp3lame", "-b:a", "96k", "-f", "mp3"],
        "opus": ["-c:a", "libopus", "-b:a", "48k", "-f", "ogg"],
        "aac": ["-c:a", "aac", "-b:a", "96k", "-f", "adts"],
        "flac": ["-c:a", "flac", "-f", "flac"],
        # Raw output allows writing a correct finite WAV header after ffmpeg exits.
        "wav": ["-c:a", "pcm_s16le", "-f", "s16le"],
        "pcm": ["-c:a", "pcm_s16le", "-f", "s16le"],
    }
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
           "-map", "0:a:0", "-vn", "-ar", "24000", "-ac", "1", "-map_metadata", "-1"]
    if extra_speed != 1.0:
        cmd += ["-af", f"atempo={extra_speed:.8f}"]
    cmd += outputs[fmt] + ["pipe:1"]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE,
                                                  stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError as exc:
        raise APIError(503, "ffmpeg is required; use the provided Docker image.", "missing_ffmpeg") from exc
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(wav), timeout=90)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    if proc.returncode or not stdout:
        raise APIError(502, "Audio encoding failed.", "audio_encode_failed")
    return write_wav(stdout) if fmt == "wav" else stdout


def _stamp(seconds: float, separator: str) -> str:
    if not math.isfinite(seconds):
        raise ValueError("Non-finite timestamp")
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{ms:03}"


def subtitle_text(data: dict, fmt: str) -> str:
    separator = "," if fmt == "srt" else "."
    output = [] if fmt == "srt" else ["WEBVTT", ""]
    try:
        segments = data.get("segments", [])
        if not isinstance(segments, list):
            raise ValueError("Invalid segments")
        if not segments and str(data.get("text", "")).strip():
            raise ValueError("Upstream omitted segment timestamps")
        for index, segment in enumerate(segments, 1):
            output += [str(index),
                       f"{_stamp(float(segment['start']), separator)} --> {_stamp(float(segment['end']), separator)}",
                       str(segment["text"]).strip(), ""]
    except (ValueError, KeyError, TypeError) as exc:
        raise APIError(502, "Groq did not return valid segment timestamps.", "invalid_upstream_response") from exc
    return "\n".join(output) + "\n"
