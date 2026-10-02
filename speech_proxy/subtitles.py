import math
from typing import Any

from .errors import APIError


def _timestamp(seconds: float, separator: str) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_int, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds_int:02}{separator}{milliseconds:03}"


def render_subtitles(transcription: dict[str, Any], format_name: str) -> str:
    """Render Groq's verbose segments; Groq itself does not accept srt or vtt."""
    segments = transcription.get("segments")
    if not isinstance(segments, list):
        raise APIError(502, "Groq returned no subtitle segments", "invalid_upstream_response")
    if not segments and transcription.get("text", "").strip():
        raise APIError(502, "Groq returned no subtitle segments", "invalid_upstream_response")
    blocks = ["WEBVTT\n"] if format_name == "vtt" else []
    for i, segment in enumerate(segments, 1):
        try:
            start = float(segment["start"])
            end = float(segment["end"])
            text = segment["text"]
            if not isinstance(text, str):
                raise ValueError
            if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start <= end:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise APIError(
                502, "Groq returned invalid subtitle segments", "invalid_upstream_response"
            ) from exc
        separator = "," if format_name == "srt" else "."
        timing = f"{_timestamp(start, separator)} --> {_timestamp(end, separator)}"
        # Embedded blank lines must not terminate a cue early.
        cue = " ".join(text.split())
        if format_name == "vtt":
            cue = cue.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        blocks.append(f"{i}\n{timing}\n{cue}\n")
    return "\n".join(blocks)
