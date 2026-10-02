import pytest
from pydantic import ValidationError

from speech_proxy.config import Settings

KEY = "test-private-key-for-local-gateway-123456789"


def test_dotenv_adc_and_legacy_settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        f"PROXY_API_KEY={KEY}\nGOOGLE_APPLICATION_CREDENTIALS=/private/adc.json\n"
        "TTS_ROLLING_32D_CHAR_LIMIT=300\nTTS_ROLLING_24H_CHAR_LIMIT=100\n"
        "TTS_CACHE_MAX_BYTES=1234\nTTS_CACHE_TTL_SECONDS=55\nDATA_DIR=/private/data\n"
        "ASR_DEFAULT_LANGUAGE=auto\n"
    )
    settings = Settings(_env_file=env)
    assert settings.google_application_credentials == "/private/adc.json"
    assert settings.tts_32day_char_limit == 300
    assert settings.tts_daily_char_limit == 100
    assert settings.cache_max_bytes == 1234
    assert settings.cache_ttl_seconds == 55
    assert settings.usage_db_path == "/private/data/usage.sqlite3"
    assert settings.asr_default_language == "auto"


def test_canonical_env_takes_precedence_and_zero_disables(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        f"PROXY_API_KEY={KEY}\nTTS_ROLLING_32D_CHAR_LIMIT=300\nTTS_32DAY_CHAR_LIMIT=0\n"
        "DATA_DIR=/old\nUSAGE_DB_PATH=/new/usage.sqlite3\n"
    )
    settings = Settings(_env_file=env)
    assert settings.tts_32day_char_limit == 0
    assert settings.usage_db_path == "/new/usage.sqlite3"


@pytest.mark.parametrize(
    "fields",
    [
        {"proxy_api_key": "short"},
        {"proxy_api_key": "replace-me-" * 5},
        {"proxy_api_key": "CHANGE_ME_TO_A_RANDOM_SECRET_AT_LEAST_32_CHARACTERS"},
        {"google_tts_voice": "ko-KR-Neural2-A"},
        {"tts_32day_char_limit": 3500001},
        {"cache_max_bytes": -1},
        {"request_timeout_seconds": float("inf")},
    ],
)
def test_bad_configuration_fails_closed(fields):
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **{"proxy_api_key": KEY, **fields})
    assert KEY not in str(error.value)
