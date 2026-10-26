from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.gateway.runtime.process_lifecycle import ManagedProcess, _spawn_logged_process
from app.gateway.runtime.process_logs import ProcessLogStore
from app.gateway.service_types import LocalForwardSpec
from app.gateway.ssh_command import build_ssh_command

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SshLocalForwardSpec:
    local_port: int
    remote_host: str
    remote_port: int


def start_ssh_tunnel_process(
    *,
    host: str,
    port: int,
    username: str,
    private_key_path: Path | None,
    ssh_config_host: str | None,
    forwards: tuple[LocalForwardSpec, ...],
    log_dir: Path,
) -> ManagedProcess:
    if not forwards:
        raise ValueError("SSH 隧道至少需要一个端口转发")
    forward_arguments: list[str] = ["-N"]
    for forward in forwards:
        forward_arguments.extend(
            [
                "-L",
                (
                    f"127.0.0.1:{forward.local_port}:"
                    f"{forward.remote_host}:{forward.remote_port}"
                ),
            ]
        )
    forward_arguments.extend(["-o", "ExitOnForwardFailure=yes"])
    command = build_ssh_command(
        host=host,
        port=port,
        username=username,
        private_key_path=(
            str(private_key_path) if private_key_path is not None else None
        ),
        ssh_config_host=ssh_config_host,
        extra_arguments=forward_arguments,
    )
    log_store = ProcessLogStore(log_dir)
    log_path = log_store.path_for("ssh-tunnel", forwards[0].local_port)
    log_file = log_store.open("ssh-tunnel", forwards[0].local_port)
    process = _spawn_logged_process(
        command,
        log_file=log_file,
    )
    logger.info(
        "启动 SSH 隧道: pid=%s host=%s port=%s forwards=%s log=%s",
        process.pid,
        host,
        port,
        len(forwards),
        log_path,
    )
    return ManagedProcess(process=process, log_file=log_file)


def _assert_ssh_config_has_no_forwards(
    *,
    host: str,
    port: int,
    username: str,
    private_key_path: Path | None,
    ssh_config_host: str | None,
) -> None:
    command = build_ssh_command(
        host=host,
        port=port,
        username=username,
        private_key_path=(
            str(private_key_path) if private_key_path is not None else None
        ),
        ssh_config_host=ssh_config_host,
        extra_arguments=["-G"],
    )
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "ssh -G 没有输出"
        raise RuntimeError(f"无法检查 SSH 有效配置: {detail}")
    forwarding_keys = {"localforward", "remoteforward", "dynamicforward"}
    configured = sorted(
        {
            line.split(maxsplit=1)[0].lower()
            for line in result.stdout.splitlines()
            if line.strip() and line.split(maxsplit=1)[0].lower() in forwarding_keys
        }
    )
    if configured:
        destination = ssh_config_host or f"{username}@{host}:{port}"
        raise ValueError(
            "SSH 有效配置包含转发指令，无法为每条工作区转发启动独立进程: "
            f"destination={destination}, options={','.join(configured)}。"
            "请创建一个不含 LocalForward/RemoteForward/DynamicForward 的专用 Host "
            "别名；ProxyJump、IdentityFile 等连接配置可以保留。"
        )


def start_workspace_ssh_port_forward_process(
    *,
    host: str,
    port: int,
    username: str,
    private_key_path: Path | None,
    ssh_config_host: str | None,
    forward: SshLocalForwardSpec,
    log_dir: Path,
    forward_id: str,
) -> tuple[ManagedProcess, Path]:
    _assert_ssh_config_has_no_forwards(
        host=host,
        port=port,
        username=username,
        private_key_path=private_key_path,
        ssh_config_host=ssh_config_host,
    )
    forward_arguments = [
        "-N",
        "-T",
        "-L",
        (f"127.0.0.1:{forward.local_port}:{forward.remote_host}:{forward.remote_port}"),
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
    ]
    command = build_ssh_command(
        host=host,
        port=port,
        username=username,
        private_key_path=(
            str(private_key_path) if private_key_path is not None else None
        ),
        ssh_config_host=ssh_config_host,
        extra_arguments=forward_arguments,
    )
    log_store = ProcessLogStore(log_dir)
    log_path = log_store.path_for("ssh-port-forward", forward_id)
    log_file = log_store.open("ssh-port-forward", forward_id)
    process = _spawn_logged_process(command, log_file=log_file)
    logger.info(
        "启动工作区 SSH 端口转发: pid=%s destination=%s local_port=%s "
        "remote_port=%s log=%s",
        process.pid,
        ssh_config_host or host,
        forward.local_port,
        forward.remote_port,
        log_path,
    )
    return ManagedProcess(process=process, log_file=log_file), log_path
