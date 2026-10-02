"""Install a built wheel in a clean venv and exercise its public CLI and HTTP API."""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def run(args, *, cwd, env):
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    ).stdout


def request(base_url, path, key=None):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    request = urllib.request.Request(f"{base_url}{path}", headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=2) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def smoke(wheel):
    with tempfile.TemporaryDirectory(prefix="korean-speech-wheel-") as temporary:
        scratch = Path(temporary)
        home = scratch / "home"
        home.mkdir()
        environment = scratch / "tools"
        binaries = scratch / "bin"
        command = binaries / (
            "korean-speech-openai.exe" if os.name == "nt" else "korean-speech-openai"
        )
        installer_env = os.environ.copy()
        installer_env.pop("PYTHONPATH", None)
        installer_env.pop("PYTHONHOME", None)
        installer_env["HOME"] = str(home)
        installer_env["UV_TOOL_DIR"] = str(environment)
        installer_env["UV_TOOL_BIN_DIR"] = str(binaries)
        print("Installing the wheel with uv tool install in an isolated directory...", flush=True)
        run(
            ["uv", "tool", "install", "--python", sys.executable, wheel],
            cwd=scratch,
            env=installer_env,
        )
        python = (
            environment
            / "korean-speech-openai"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        runtime_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_STATE_HOME": str(home / "state"),
            "LANG": "C.UTF-8",
        }
        for name in ("SYSTEMROOT", "LD_LIBRARY_PATH"):
            if name in os.environ:
                runtime_env[name] = os.environ[name]
        module_path = run(
            [python, "-c", "import speech_proxy; print(speech_proxy.__file__)"],
            cwd=scratch,
            env=runtime_env,
        ).strip()
        assert Path(module_path).is_relative_to(environment), module_path
        run([command, "--help"], cwd=scratch, env=runtime_env)
        run([python, "-m", "speech_proxy", "--help"], cwd=scratch, env=runtime_env)
        run([command, "init"], cwd=scratch, env=runtime_env)
        config = home / "config/korean-speech-openai/server.env"
        original = config.read_bytes()
        run([command, "init"], cwd=scratch, env=runtime_env)
        assert config.read_bytes() == original, "init unexpectedly replaced existing config"
        metadata = json.loads(
            run(
                [
                    python,
                    "-c",
                    "import json, sys; from speech_proxy.config import Settings; "
                    "s=Settings(_env_file=sys.argv[1]); "
                    "print(json.dumps({'key':s.proxy_api_key.get_secret_value(),"
                    "'db':s.usage_db_path}))",
                    config,
                ],
                cwd=scratch,
                env=runtime_env,
            )
        )
        assert len(metadata["key"]) >= 32
        assert Path(metadata["db"]).is_absolute()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        log_path = scratch / "server.log"
        with log_path.open("w+") as log:
            process = subprocess.Popen(
                [str(command), "serve", "--config", str(config), "--port", str(port)],
                cwd=scratch,
                env=runtime_env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                base_url = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 20
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Installed server exited before becoming healthy")
                    try:
                        status, body = request(base_url, "/health")
                    except (OSError, urllib.error.URLError):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Installed server did not become healthy") from None
                        time.sleep(0.1)
                        continue
                    assert status == 200 and body["status"] == "ok"
                    break
                assert request(base_url, "/v1/models")[0] == 401
                assert request(base_url, "/ready", "wrong-key")[0] == 401
                status, models = request(base_url, "/v1/models", metadata["key"])
                assert status == 200
                assert {"whisper-1", "tts-1"} <= {item["id"] for item in models["data"]}
                status, readiness = request(base_url, "/ready", metadata["key"])
                assert status == 200
                assert readiness["groq_key_configured"] is False
                assert readiness["groq_free_tier_confirmed"] is False
                assert readiness["upstream_verified"] is False
                assert Path(metadata["db"]).is_file()
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        print("Wheel smoke passed: isolated import, console/module CLI, init, health and auth.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="Path to the built .whl")
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    if wheel.suffix != ".whl":
        parser.error("Expected a .whl file")
    smoke(wheel)
