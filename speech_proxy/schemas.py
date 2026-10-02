from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    model: Literal["tts-1", "tts-1-hd", "google-wavenet"]
    input: str = Field(min_length=1)
    voice: str = Field(default="alloy", min_length=1, max_length=100)
    response_format: Literal["mp3", "wav", "pcm", "opus", "aac", "flac"] = "mp3"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    instructions: str | None = None
    stream_format: Literal["audio"] = "audio"

    @field_validator("input")
    @classmethod
    def text_must_be_encodable(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input must contain text")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("input must be valid Unicode") from exc
        return value


class TranscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    model: Literal["whisper-1", "whisper-large-v3-turbo", "whisper-large-v3"]
    language: str = Field(default="ko", pattern=r"^(?:[a-z]{2}|auto)$")
    prompt: str | None = Field(default=None, max_length=4000)
    response_format: Literal["json", "text", "verbose_json", "srt", "vtt"] = "json"
    temperature: float = Field(default=0, ge=0, le=1)
    timestamp_granularities: list[Literal["word", "segment"]] | None = None
    stream: Literal[False] = False
