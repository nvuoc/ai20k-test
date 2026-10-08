"""Exercise deployment startup with a fake Docker CLI and private dummy config."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def bash_path(path: Path) -> str:
    value = path.as_posix()
    if os.name == "nt":
        return f"/{value[0].lower()}{value[2:]}"
    return value


@pytest.fixture
def setup_runner(tmp_path):
    bash = (
        Path("C:/Program Files/Git/bin/bash.exe")
        if os.name == "nt"
        else Path(shutil.which("bash") or "/missing/bash")
    )
    if not bash.is_file():
        pytest.skip("Bash is required for the deployment CLI tests")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    shutil.copyfile(ROOT / "deploy/setup.sh", deploy / "setup.sh")
    shutil.copyfile(ROOT / "deploy/compose.yaml", deploy / "compose.yaml")
    dummy_env = (
        "DOMAIN=beta.test\nACME_EMAIL=admin@beta.test\nBETA_USER=tester\n"
        f"APP_SECRET={'a' * 64}\nBETA_PASSWORD_HASH='$2b$12${'A' * 53}'\n"
        "APP_PROFILE=fixture_demo\n"
    )
    (deploy / ".env").write_text(dummy_env, encoding="utf-8")
    (deploy / ".env.example").write_text(dummy_env, encoding="utf-8")
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    docker = binary_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$MOCK_CALLS"
case "$1 ${2:-}" in
  'image inspect')
    [[ "${MOCK_MISSING_IMAGE:-false}" != true ]] || exit 1
    printf '%s\\n' "${MOCK_IMAGE_PLATFORM:-linux/amd64}"
    ;;
  'version --format')
    printf '%s\\n' "${MOCK_DAEMON_PLATFORM:-linux/amd64}"
    ;;
esac
""",
        encoding="utf-8",
        newline="\n",
    )
    docker.chmod(0o755)
    calls = tmp_path / "docker-calls.txt"

    def run(*arguments, env_extra="", **mock_env):
        if env_extra:
            (deploy / ".env").write_text(dummy_env + env_extra, encoding="utf-8")
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {"SYSTEMROOT", "WINDIR", "USERPROFILE", "HOME", "TEMP", "TMP", "PATH"}
        }
        env.update(MOCK_CALLS=bash_path(calls), **mock_env)
        command = (
            f"export PATH={shlex.quote(bash_path(binary_dir))}:\"$PATH\"; "
            f"exec bash {shlex.quote(bash_path(deploy / 'setup.sh'))} "
            + " ".join(shlex.quote(argument) for argument in arguments)
        )
        result = subprocess.run(
            [str(bash), "-lc", command],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if not env_extra:
            assert (deploy / ".env").read_text(encoding="utf-8") == dummy_env
        commands = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
        return result, commands

    run.env_file = deploy / ".env"
    return run


def test_source_start_builds_without_image_inspection(setup_runner):
    result, calls = setup_runner("--start")
    assert result.returncode == 0, result.stderr
    startup = next(command for command in calls if " up " in command)
    assert "--build" in startup
    assert "--no-build" not in startup
    assert not any(command.startswith("image inspect") for command in calls)


@pytest.mark.parametrize("arguments", [("--start", "--image"), ("--image", "--start")])
def test_loaded_image_start_skips_build_without_source_dockerfile(setup_runner, arguments):
    result, calls = setup_runner(*arguments)
    assert result.returncode == 0, result.stderr
    startup = next(command for command in calls if " up " in command)
    assert "--no-build" in startup
    assert "--build" not in startup
    assert any(command.startswith("image inspect") for command in calls)
    assert not any(command.startswith("pull ") for command in calls)


def test_missing_image_fails_before_compose_start(setup_runner):
    result, calls = setup_runner("--start", "--image", MOCK_MISSING_IMAGE="true")
    assert result.returncode == 1
    assert "docker load" in result.stderr
    assert not any(" up " in command or " config " in command for command in calls)


@pytest.mark.parametrize("platform", ["linux/arm64", "windows/amd64", "<no value>"])
def test_wrong_image_platform_fails_before_start(setup_runner, platform):
    result, calls = setup_runner("--start", "--image", MOCK_IMAGE_PLATFORM=platform)
    assert result.returncode == 1
    assert "platform" in result.stderr
    assert not any(" up " in command for command in calls)


def test_image_flag_requires_start_without_docker_calls(setup_runner):
    result, calls = setup_runner("--image")
    assert result.returncode == 2
    assert "requires --start" in result.stderr
    assert calls == []


def test_registry_image_uses_configured_tag(setup_runner):
    result, calls = setup_runner(
        "--start", "--image", env_extra="PARROTGO_IMAGE=ghcr.io/example/booking:commit-amd64\n"
    )
    assert result.returncode == 0, result.stderr
    assert any("image inspect" in call and "ghcr.io/example/booking:commit-amd64" in call
               for call in calls)


def test_voice_requires_credentials_before_start(setup_runner):
    result, calls = setup_runner("--start", env_extra="VOICE_ENABLED=true\n")
    assert result.returncode == 1
    assert "LIVEKIT_API_KEY" in result.stderr and "AZURE_SPEECH_KEY" in result.stderr
    assert not any(" up " in call for call in calls)


def test_voice_profile_generates_private_bridge_secret(setup_runner):
    extra = (
        "VOICE_ENABLED=true\nLIVEKIT_URL=wss://test.livekit.cloud\n"
        "LIVEKIT_API_KEY=dummy-key\nLIVEKIT_API_SECRET=dummy-secret\n"
        "AZURE_SPEECH_KEY=dummy-speech-key\nAZURE_SPEECH_REGION=southeastasia\n"
        "VOICE_AGENT_SECRET=\n"
    )
    result, calls = setup_runner("--start", "--image", env_extra=extra)
    assert result.returncode == 0, result.stderr
    startup = next(call for call in calls if " up " in call)
    assert "--profile voice" in startup and "--no-build" in startup
    content = setup_runner.env_file.read_text(encoding="utf-8")
    secret = next(line.split("=", 1)[1].strip("'") for line in content.splitlines()
                  if line.startswith("VOICE_AGENT_SECRET="))
    assert len(secret) == 96
    assert secret not in result.stdout + result.stderr + "\n".join(calls)


def test_text_mode_stops_previously_enabled_voice_worker(setup_runner):
    result, calls = setup_runner("--start", env_extra="VOICE_ENABLED=false\n")
    assert result.returncode == 0, result.stderr
    assert any("stop voice-agent" in call for call in calls)
    assert "--profile voice" not in next(call for call in calls if " up " in call)
