import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from speech_proxy import cli
from speech_proxy.config import Settings

KEY = "test-private-key-for-local-gateway-123456789"


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    return home


def test_init_creates_private_config_with_safe_provider_defaults(isolated_home, capsys):
    assert cli.main(["init"]) == 0

    config = isolated_home / "config/korean-speech-openai/server.env"
    settings = Settings(_env_file=config)
    key = settings.proxy_api_key.get_secret_value()
    assert len(key) >= 32
    assert settings.groq_api_key is None
    assert settings.google_cloud_project is None
    assert Path(settings.google_application_credentials) == config.parent / "google-adc.json"
    assert not Path(settings.google_application_credentials).exists()
    assert settings.groq_free_tier_confirmed is False
    assert Path(settings.usage_db_path) == (
        isolated_home / "state/korean-speech-openai/usage.sqlite3"
    )
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert key not in capsys.readouterr().out


def test_init_preserves_existing_credentials_and_operator_edits(isolated_home):
    config = isolated_home / "custom configuration.env"
    assert cli.main(["init", "--config", str(config)]) == 0
    original = config.read_bytes() + b"\n# operator notes\nTTS_DAILY_CHAR_LIMIT=42\n"
    config.write_bytes(original)
    original_inode = config.stat().st_ino

    assert cli.main(["init", "--config", str(config)]) == 0

    assert config.read_bytes() == original
    assert config.stat().st_ino == original_inode


def test_init_uses_home_when_xdg_variables_are_unset(isolated_home, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.delenv("XDG_STATE_HOME")

    assert cli.main(["init"]) == 0

    config = isolated_home / ".config/korean-speech-openai/server.env"
    settings = Settings(_env_file=config)
    assert Path(settings.usage_db_path) == (
        isolated_home / ".local/state/korean-speech-openai/usage.sqlite3"
    )


def test_serve_reads_explicit_config_without_executing_shell(isolated_home, tmp_path, monkeypatch):
    marker = tmp_path / "shell-was-executed"
    config = isolated_home / "server.env"
    config.write_text(
        f"PROXY_API_KEY={KEY}\n"
        f"GOOGLE_CLOUD_PROJECT='$(touch {marker})'\n"
        f"USAGE_DB_PATH='{tmp_path / 'usage.sqlite3'}'\n"
    )
    calls = []
    monkeypatch.setattr(cli, "create_app", lambda settings: settings)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))

    assert cli.main(["serve", "--config", str(config), "--port", "9876"]) == 0

    assert len(calls) == 1
    settings, options = calls[0]
    assert settings.proxy_api_key.get_secret_value() == KEY
    assert settings.google_cloud_project == f"$(touch {marker})"
    assert options["host"] == "127.0.0.1"
    assert options["port"] == 9876
    assert options.get("workers", 1) == 1
    assert options["access_log"] is False
    assert not marker.exists()


def test_serve_default_config_does_not_depend_on_working_directory(
    isolated_home, tmp_path, monkeypatch
):
    assert cli.main(["init"]) == 0
    another_directory = tmp_path / "unrelated directory"
    another_directory.mkdir()
    (another_directory / ".env").write_text("PROXY_API_KEY=invalid\n")
    monkeypatch.chdir(another_directory)
    calls = []
    monkeypatch.setattr(cli, "create_app", lambda settings: settings)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: calls.append(app))

    assert cli.main(["serve"]) == 0

    assert len(calls) == 1
    assert Path(calls[0].usage_db_path).is_absolute()


def test_serve_resolves_relative_state_and_adc_against_config_directory(
    isolated_home, tmp_path, monkeypatch
):
    config = isolated_home / "server.env"
    config.write_text(
        f"PROXY_API_KEY={KEY}\n"
        "USAGE_DB_PATH=state/usage.sqlite3\n"
        "GOOGLE_APPLICATION_CREDENTIALS=credentials/google.json\n"
        "BIND_IP=::1\nPORT=9898\n"
    )
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(cli, "create_app", lambda settings: settings)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))

    assert cli.main(["serve", "--config", str(config)]) == 0

    settings, options = calls[0]
    assert settings.usage_db_path == str(config.parent / "state/usage.sqlite3")
    assert settings.google_application_credentials == str(config.parent / "credentials/google.json")
    assert options["host"] == "::1"
    assert options["port"] == 9898


def test_explicit_bind_options_override_file_configuration(isolated_home, monkeypatch):
    config = isolated_home / "server.env"
    config.write_text(f"PROXY_API_KEY={KEY}\nBIND_IP=0.0.0.0\nPORT=9898\n")
    calls = []
    monkeypatch.setattr(cli, "create_app", lambda settings: settings)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))

    assert (
        cli.main(["serve", "--config", str(config), "--host", "127.0.0.1", "--port", "9876"]) == 0
    )

    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 9876


def test_invalid_config_is_reported_without_secrets_or_server_start(
    isolated_home, monkeypatch, capsys
):
    config = isolated_home / "invalid.env"
    leaked_value = "never-print-this-credential"
    config.write_text(f"PROXY_API_KEY={leaked_value}\n")
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append(args))

    assert cli.main(["serve", "--config", str(config)]) != 0

    assert not calls
    output = capsys.readouterr()
    assert leaked_value not in output.out + output.err
    assert "configuration" in (output.out + output.err).lower()


def test_missing_config_fails_even_if_environment_contains_api_key(isolated_home, monkeypatch):
    monkeypatch.setenv("PROXY_API_KEY", KEY)
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append(args))

    assert cli.main(["serve", "--config", str(isolated_home / "missing.env")]) != 0

    assert not calls


@pytest.mark.parametrize("port", ["0", "-1", "65536"])
def test_invalid_port_is_rejected_before_server_start(isolated_home, monkeypatch, port):
    assert cli.main(["init"]) == 0
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append(args))
    try:
        result = cli.main(["serve", "--port", port])
    except SystemExit as exc:
        result = exc.code
    assert result != 0
    assert not calls


def test_generated_credentials_are_distinct(isolated_home):
    configs = [isolated_home / "first.env", isolated_home / "second.env"]
    for config in configs:
        assert cli.main(["init", "--config", str(config)]) == 0
    keys = {Settings(_env_file=config).proxy_api_key.get_secret_value() for config in configs}
    assert len(keys) == 2


@pytest.fixture
def systemctl_calls(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        assert isinstance(args, list)
        assert not kwargs.get("shell", False)
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    return calls


def test_install_service_uses_package_python_and_config_without_secrets(
    isolated_home, systemctl_calls
):
    config = isolated_home / "configuration with spaces.env"
    assert cli.main(["init", "--config", str(config)]) == 0

    assert cli.main(["install-service", "--config", str(config)]) == 0

    unit = isolated_home / "config/systemd/user/korean-speech-openai.service"
    content = unit.read_text()
    assert "[Service]" in content
    assert "ExecStart=" in content
    assert str(Path(cli.sys.executable)) in content
    assert "-m" in content and "speech_proxy" in content and "serve" in content
    assert str(config) in content
    assert Settings(_env_file=config).proxy_api_key.get_secret_value() not in content
    assert "PROXY_API_KEY" not in content
    assert "EnvironmentFile=" not in content
    assert "source " not in content
    assert ["systemctl", "--user", "daemon-reload"] in systemctl_calls
    assert ["systemctl", "--user", "enable", "korean-speech-openai.service"] in systemctl_calls
    assert ["systemctl", "--user", "restart", "korean-speech-openai.service"] in systemctl_calls


def test_install_service_no_start_enables_without_launching(isolated_home, systemctl_calls):
    assert cli.main(["init"]) == 0

    assert cli.main(["install-service", "--no-start"]) == 0

    assert ["systemctl", "--user", "daemon-reload"] in systemctl_calls
    assert ["systemctl", "--user", "enable", "korean-speech-openai.service"] in systemctl_calls
    assert not any("restart" in call or "start" in call for call in systemctl_calls)


def test_install_service_validates_config_before_registering(isolated_home, systemctl_calls):
    config = isolated_home / "invalid.env"
    config.write_text("PROXY_API_KEY=too-short\n")

    assert cli.main(["install-service", "--config", str(config)]) != 0

    assert not systemctl_calls
    assert not (isolated_home / "config/systemd/user/korean-speech-openai.service").exists()


def test_install_service_missing_ffmpeg_does_not_register(
    isolated_home, systemctl_calls, monkeypatch
):
    assert cli.main(["init"]) == 0
    real_which = cli.shutil.which
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: None if name == "ffmpeg" else real_which(name)
    )

    assert cli.main(["install-service"]) != 0

    assert not systemctl_calls
    assert not (isolated_home / "config/systemd/user/korean-speech-openai.service").exists()


def test_install_service_unsupported_platform_has_friendly_error(
    isolated_home, monkeypatch, capsys, systemctl_calls
):
    assert cli.main(["init"]) == 0
    monkeypatch.setattr(cli.sys, "platform", "darwin")

    assert cli.main(["install-service"]) != 0

    assert not systemctl_calls
    output = capsys.readouterr()
    assert "linux" in (output.out + output.err).lower()
    assert "Traceback" not in output.out + output.err


def test_install_service_systemctl_failure_is_sanitized(isolated_home, monkeypatch, capsys):
    assert cli.main(["init"]) == 0
    leaked_value = "an-unexpected-upstream-secret-in-subprocess-error"

    def fail_run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr=leaked_value)

    monkeypatch.setattr(cli.subprocess, "run", fail_run)

    assert cli.main(["install-service"]) != 0

    output = capsys.readouterr()
    assert leaked_value not in output.out + output.err
    assert "Traceback" not in output.out + output.err


def test_service_unit_escapes_systemd_expansions_and_argument_boundaries():
    executable = Path('/home/user/python binaries/100%/$bin/python"quoted')
    config = Path('/home/user/config 100%/$HOME/server "quoted".env')

    unit = cli.service_unit(executable, config, "127.0.0.1", 9876)

    line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert "100%%" in line
    assert "$$HOME" in line
    assert "$$bin" in line
    assert '\\"quoted' in line
    assert '"/home/user/python binaries/' in line
    assert '"/home/user/config ' in line
    assert "127.0.0.1" in line
    assert "9876" in line


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze unavailable")
def test_generated_service_is_accepted_by_real_systemd_parser(tmp_path):
    config = tmp_path / 'config with spaces 100% $HOME "quoted"' / "server.env"
    config.parent.mkdir()
    config.write_text(f"PROXY_API_KEY={KEY}\n")
    unit = tmp_path / "korean-speech-openai.service"
    unit.write_text(cli.service_unit(Path(cli.sys.executable), config, "127.0.0.1", 9876))

    # Offline validation checks every directive without contacting a user manager
    # or registering/starting the service. In particular, path directives do not
    # share ExecStart's quoting grammar.
    result = subprocess.run(
        ["systemd-analyze", "verify", str(unit)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("special", ["\n", "\r", "\x00"])
def test_service_unit_rejects_control_character_injection(special):
    config = Path(f"/home/user/config{special}ExecStart=/bin/false")

    with pytest.raises(ValueError):
        cli.service_unit(Path("/usr/bin/python"), config, None, None)
