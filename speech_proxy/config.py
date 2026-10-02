import re
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

VOICE_PATTERN = re.compile(r"ko-KR-(?:Wavenet|Standard)-[A-D]\Z")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    proxy_api_key: SecretStr
    groq_api_key: SecretStr | None = None
    groq_free_tier_confirmed: bool = False
    groq_model: Literal["whisper-large-v3-turbo", "whisper-large-v3"] = "whisper-large-v3-turbo"
    google_cloud_project: str | None = None
    google_application_credentials: str | None = None
    google_tts_voice: str = "ko-KR-Wavenet-A"
    usage_db_path: str = "data/usage.sqlite3"
    data_dir: str | None = None
    asr_default_language: str = "ko"
    tts_32day_char_limit: int = Field(
        default=3_500_000,
        ge=0,
        le=3_500_000,
        validation_alias=AliasChoices("TTS_32DAY_CHAR_LIMIT", "TTS_ROLLING_32D_CHAR_LIMIT"),
    )
    tts_daily_char_limit: int = Field(
        default=150_000,
        ge=0,
        le=3_500_000,
        validation_alias=AliasChoices("TTS_DAILY_CHAR_LIMIT", "TTS_ROLLING_24H_CHAR_LIMIT"),
    )
    groq_minute_request_limit: int = Field(default=20, ge=0)
    groq_daily_request_limit: int = Field(default=2_000, ge=0)
    groq_hourly_audio_seconds_limit: int = Field(default=7_200, ge=0)
    groq_daily_audio_seconds_limit: int = Field(default=28_800, ge=0)
    max_audio_seconds: float = Field(default=600, gt=0, le=3600, allow_inf_nan=False)
    max_upload_bytes: int = Field(default=25_000_000, gt=0, le=25_000_000)
    max_tts_input_chars: int = Field(default=20_000, gt=0, le=100_000)
    max_output_audio_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    upstream_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    request_timeout_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)
    ffmpeg_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    max_concurrent_requests: int = Field(default=4, ge=1, le=64)
    cache_max_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=0,
        validation_alias=AliasChoices("CACHE_MAX_BYTES", "TTS_CACHE_MAX_BYTES"),
    )
    cache_ttl_seconds: float = Field(
        default=3600,
        ge=0,
        allow_inf_nan=False,
        validation_alias=AliasChoices("CACHE_TTL_SECONDS", "TTS_CACHE_TTL_SECONDS"),
    )

    @model_validator(mode="after")
    def legacy_data_directory(self) -> "Settings":
        if self.data_dir and "usage_db_path" not in self.model_fields_set:
            self.usage_db_path = str(Path(self.data_dir) / "usage.sqlite3")
        return self

    @field_validator("asr_default_language")
    @classmethod
    def valid_default_language(cls, value: str) -> str:
        if value != "auto" and not re.fullmatch("[a-z]{2}", value):
            raise ValueError("Use a two-letter language code or auto")
        return value

    @field_validator("proxy_api_key")
    @classmethod
    def strong_proxy_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value()
        if len(key) < 32 or key != key.strip() or not key.isascii():
            raise ValueError(
                "PROXY_API_KEY must contain at least 32 ASCII characters, no edge spaces"
            )
        if (
            key.lower()
            .replace("_", "-")
            .startswith(("change-me", "replace-me", "your-", "example-"))
        ):
            raise ValueError("Generate a random PROXY_API_KEY before starting the server")
        return value

    @field_validator("groq_api_key", mode="before")
    @classmethod
    def empty_key_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("google_cloud_project", "google_application_credentials", mode="before")
    @classmethod
    def empty_project_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("google_tts_voice")
    @classmethod
    def allowed_voice(cls, value: str) -> str:
        if not VOICE_PATTERN.fullmatch(value):
            raise ValueError("Use a Korean Standard or Wavenet voice A, B, C or D")
        return value
