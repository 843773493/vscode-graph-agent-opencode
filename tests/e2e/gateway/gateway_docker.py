from __future__ import annotations

import fcntl
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from tests.e2e.gateway.gateway_target import GatewaySshTarget, wait_for_ssh_ready


TARGET_ID = "docker-debian"
TARGET_REPOSITORY = "/opt/boxteam-dev/repository"
TARGET_CONFIG = "tools/cross-platform-development-targets/targets.example.jsonc"


def docker_daemon_error() -> str | None:
    result = subprocess.run(
        [*docker_command_prefix(), "version", "--format", "{{.Server.Version}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return None
    return result.stderr.strip()


def ensure_gateway_ssh_container(*, known_hosts_path: Path) -> GatewaySshTarget:
    project_root = Path.cwd().resolve()
    lock_path = (
        project_root
        / "out"
        / "cross-platform-dev-targets"
        / TARGET_ID
        / ".e2e-provision.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        _run_target_command(project_root, "provision")
        _run_target_command(project_root, "sync", "--activate")
        _run_target_command(project_root, "bootstrap")

    configured_known_hosts = (
        project_root
        / "out"
        / "cross-platform-dev-targets"
        / TARGET_ID
        / "ssh"
        / "known_hosts"
    )
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(configured_known_hosts, known_hosts_path)
    private_key = (
        project_root / "asset" / "gateway_ssh" / "boxteam_gateway_e2e_ed25519"
    )
    private_key.chmod(0o600)
    target = GatewaySshTarget(
        target_id=TARGET_ID,
        platform="linux",
        host="127.0.0.1",
        ssh_port=22222,
        username="boxteam",
        private_key=private_key,
        known_hosts_path=known_hosts_path.resolve(),
        repository_path=TARGET_REPOSITORY,
        python_environment=f"{TARGET_REPOSITORY}/.venv",
    )
    wait_for_ssh_ready(target)
    return target


def docker_command_prefix() -> list[str]:
    raw_command = os.environ.get("BOXTEAM_DOCKER_COMMAND", "docker").strip()
    if not raw_command:
        raise ValueError("BOXTEAM_DOCKER_COMMAND 不能为空")
    return shlex.split(raw_command)


def _run_target_command(project_root: Path, command: str, *arguments: str) -> None:
    result = subprocess.run(
        [
            "bun",
            "run",
            "scripts/cross-platform-development-target.mjs",
            command,
            TARGET_ID,
            "--config",
            TARGET_CONFIG,
            *arguments,
        ],
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=900,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"准备跨端开发目标失败: command={command}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
