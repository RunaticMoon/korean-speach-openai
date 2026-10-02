"""Installed-tool entry points; configuration and state live outside the package."""

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from . import __version__
from .app import create_app
from .config import Settings

SERVICE_NAME = "korean-speech-openai.service"


def _xdg_path(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    if value:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError(f"{variable} must be an absolute path")
        return path
    return fallback


def default_config_path() -> Path:
    return _xdg_path("XDG_CONFIG_HOME", Path.home() / ".config") / "korean-speech-openai/server.env"


def default_state_dir() -> Path:
    return _xdg_path("XDG_STATE_HOME", Path.home() / ".local/state") / "korean-speech-openai"


def _absolute_path(value: str | Path) -> Path:
    text = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("Paths cannot contain control characters")
    # Do not resolve symlinks: resolving a virtualenv's Python bypasses the environment.
    return Path(text).expanduser().absolute()


def _private_write(path: Path, text: str, *, replace: bool = False) -> bool:
    """Publish a complete 0600 file atomically, preserving existing configs."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".speech-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(text)
            target.flush()
            os.fsync(target.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                return False
        return True
    finally:
        Path(temporary).unlink(missing_ok=True)


def _dotenv_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def initialize(config: Path) -> None:
    if config.exists() or config.is_symlink():
        print(f"Existing configuration preserved: {config}")
        return
    state = default_state_dir()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    text = (
        "# Keep this file private. Credentials are never printed by the CLI.\n"
        f"PROXY_API_KEY={secrets.token_urlsafe(32)}\n"
        "GROQ_API_KEY=\n"
        "# Confirm the key belongs to a Free-plan organization before enabling STT.\n"
        "GROQ_FREE_TIER_CONFIRMED=false\n"
        "GROQ_MODEL=whisper-large-v3-turbo\n"
        "ASR_DEFAULT_LANGUAGE=ko\n"
        "GOOGLE_CLOUD_PROJECT=\n"
        f"GOOGLE_APPLICATION_CREDENTIALS={_dotenv_quote(str(config.parent / 'google-adc.json'))}\n"
        "GOOGLE_TTS_VOICE=ko-KR-Wavenet-A\n"
        "BIND_IP=127.0.0.1\n"
        "PORT=8787\n"
        f"USAGE_DB_PATH={_dotenv_quote(str(state / 'usage.sqlite3'))}\n"
        "TTS_32DAY_CHAR_LIMIT=3500000\n"
        "TTS_DAILY_CHAR_LIMIT=150000\n"
        "# Modest defaults for a single-user server.\n"
        "MAX_CONCURRENT_REQUESTS=2\n"
        "MAX_OUTPUT_AUDIO_BYTES=33554432\n"
        "CACHE_MAX_BYTES=16777216\n"
    )
    created = _private_write(config, text)
    print(f"{'Created' if created else 'Preserved'} configuration: {config}")
    print("Add your Groq key and Google Cloud ADC before using speech APIs.")


def _load_settings(config: Path, host: str | None, port: int | None) -> Settings:
    if not config.is_file():
        raise ValueError(f"Configuration not found: {config}; run 'korean-speech-openai init'")
    overrides = {}
    if host is not None:
        overrides["bind_ip"] = host
    if port is not None:
        overrides["port"] = port
    settings = Settings(_env_file=config, **overrides)
    if not settings.bind_ip or any(char.isspace() for char in settings.bind_ip):
        raise ValueError("BIND_IP must be a valid bind address without whitespace")
    # Relative credentials/state paths are relative to the config, not the caller's cwd.
    for field in ("google_application_credentials", "usage_db_path"):
        value = getattr(settings, field)
        if value and value != ":memory:" and not Path(value).expanduser().is_absolute():
            setattr(settings, field, str(config.parent / value))
        elif value and value != ":memory:":
            setattr(settings, field, str(Path(value).expanduser()))
    return settings


def _check_audio_tools() -> None:
    for name in ("ffmpeg", "ffprobe"):
        if shutil.which(name) is None:
            raise ValueError(f"{name} is required; install FFmpeg with your OS package manager")


def _unit_quote(value: str, *, command: bool = False) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Service arguments cannot contain control characters")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return f'"{value}"'


def service_unit(
    python: Path, config: Path, host: str | None = None, port: int | None = None
) -> str:
    args = [str(python), "-m", "speech_proxy", "serve", "--config", str(config)]
    if host is not None:
        args.extend(["--host", host])
    if port is not None:
        args.extend(["--port", str(port)])
    command = " ".join(_unit_quote(value, command=True) for value in args)
    binary_dirs = [str(python.parent)]
    for tool in ("ffmpeg", "ffprobe"):
        location = shutil.which(tool)
        if location:
            binary_dirs.append(str(Path(location).parent))
    binary_dirs.extend(["/usr/local/bin", "/usr/bin", "/bin"])
    search_path = ":".join(dict.fromkeys(binary_dirs))
    return (
        "# Generated by korean-speech-openai install-service.\n"
        "[Unit]\n"
        "Description=Korean speech bridge for Paseo (Groq and Google TTS)\n"
        "StartLimitIntervalSec=60\n"
        "StartLimitBurst=5\n\n"
        "[Service]\n"
        "Type=exec\n"
        # WorkingDirectory is a path directive, not a shell-style argument list.
        # A fixed directory also keeps local source trees off the import path.
        "WorkingDirectory=/\n"
        f"ExecStart={command}\n"
        f"Environment={_unit_quote('PATH=' + search_path)}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "Environment=PYTHONDONTWRITEBYTECODE=1\n"
        "UMask=0077\n"
        "NoNewPrivileges=yes\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "TimeoutStopSec=20\n"
        "KillMode=control-group\n"
        "TasksMax=64\n"
        "MemoryHigh=256M\n"
        "MemoryMax=512M\n"
        "MemorySwapMax=0\n"
        "CPUQuota=100%\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _require_systemd() -> None:
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        raise ValueError("Service management needs Linux with systemd; use 'serve' on this system")


def install_service(config: Path, host: str | None, port: int | None, no_start: bool) -> None:
    _require_systemd()
    _load_settings(config, host, port)
    _check_audio_tools()
    # Confirm a user manager is reachable before changing its unit file.
    subprocess.run(
        ["systemctl", "--user", "show-environment"], check=True, capture_output=True, timeout=15
    )
    directory = _xdg_path("XDG_CONFIG_HOME", Path.home() / ".config") / "systemd/user"
    unit_path = directory / SERVICE_NAME
    unit = service_unit(_absolute_path(sys.executable), config, host, port)
    _private_write(unit_path, unit, replace=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=30)
    subprocess.run(["systemctl", "--user", "enable", SERVICE_NAME], check=True, timeout=30)
    if not no_start:
        subprocess.run(["systemctl", "--user", "restart", SERVICE_NAME], check=True, timeout=30)
    state = "Enabled" if no_start else "Enabled; startup requested for"
    print(f"{state} service: {SERVICE_NAME}")
    print(f"Configuration: {config}")
    if not no_start:
        print("Verify startup with 'korean-speech-openai status' and GET /health.")
    print('For startup at boot without login, check: loginctl show-user "$USER" -p Linger')


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="OpenAI-compatible Groq/Google speech server")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("init", "Create a private configuration with a random proxy key"),
        ("serve", "Run the speech API in the foreground"),
        ("install-service", "Enable the installed package as a systemd user service"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", type=Path, help="Configuration .env file")
        if name != "init":
            command.add_argument("--host", help="Override BIND_IP (default 127.0.0.1)")
            command.add_argument("--port", type=int, help="Override PORT (default 8787)")
        if name == "install-service":
            command.add_argument("--no-start", action="store_true", help="Enable without starting")
    commands.add_parser("status", help="Show the systemd user service status")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            _require_systemd()
            result = subprocess.run(
                ["systemctl", "--user", "status", SERVICE_NAME, "--no-pager"], timeout=30
            )
            return result.returncode
        config = _absolute_path(args.config or default_config_path())
        if args.command == "init":
            initialize(config)
        elif args.command == "install-service":
            install_service(config, args.host, args.port, args.no_start)
        else:
            settings = _load_settings(config, args.host, args.port)
            _check_audio_tools()
            uvicorn.run(
                create_app(settings),
                host=settings.bind_ip,
                port=settings.port,
                workers=1,
                access_log=False,
            )
        return 0
    except ValidationError:
        print(
            "Invalid configuration; check the API key, model, voice, port and limits.",
            file=sys.stderr,
        )
    except subprocess.CalledProcessError:
        print("systemd command failed; check the user manager and service logs.", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("systemd command timed out.", file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
    return 1
