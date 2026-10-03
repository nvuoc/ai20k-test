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

    def run(*arguments, **mock_env):
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
        assert (deploy / ".env").read_text(encoding="utf-8") == dummy_env
        commands = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
        return result, commands

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
