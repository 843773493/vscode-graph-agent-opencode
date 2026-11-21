from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.file_permissions import restrict_private_path_access
from app.gateway.runtime.process_lifecycle import ManagedProcess, _spawn_logged_process
from app.gateway.runtime.process_logs import ProcessLogStore
from app.gateway.service_types import LocalForwardSpec
from app.gateway.ssh_command import build_ssh_command

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SshPortForwardSpec:
    direction: str
    listen_address: str
    listen_port: int
    destination_address: str
    destination_port: int

    def argument(self) -> str:
        def address(value: str) -> str:
            return f"[{value}]" if ":" in value else value
        return (
            f"{address(self.listen_address)}:{self.listen_port}:"
            f"{address(self.destination_address)}:{self.destination_port}"
        )


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


def _effective_forward_config(
    *,
    host: str,
    port: int,
    username: str,
    private_key_path: Path | None,
    ssh_config_host: str | None,
) -> str:
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
    # 只剔除已有转发；保留 ssh -G 已解析的 ProxyJump、认证、端口及已知主机规则。
    # 使用独立配置，避免 Host 自带的 LocalForward／RemoteForward 干扰本条规则。
    target = ssh_config_host or host
    lines = [f"Host {target}"]
    single_path_options = {"identityfile", "certificatefile", "identityagent", "controlpath",
                           "revokedhostkeys", "securitykeyprovider", "pkcs11provider", "xauthlocation"}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key.lower() in {"host", "localforward", "remoteforward", "dynamicforward"}:
            continue
        if key.lower() in single_path_options:
            value = '"' + value.replace('"', '\\"') + '"'
        lines.append(f"{key} {value}")
    # ProxyJump 子 SSH 会继承 -F；只对目标应用快照，跳板机仍读取自己的原配置。
    lines.append(f"Host * !{target}")
    system_config = (Path(os.environ["PROGRAMDATA"]) / "ssh/ssh_config"
                     if os.name == "nt" else Path("/etc/ssh/ssh_config"))
    for original in (Path.home() / ".ssh/config", system_config):
        lines.append('Include "' + original.as_posix().replace('"', '\\"') + '"')
    return "\n".join(lines) + "\n"


@dataclass(slots=True)
class _SshPortForwardProcess(ManagedProcess):
    configuration_path: Path | None = None

    def close(self, *, timeout_seconds: float = 8) -> None:
        ManagedProcess.close(self, timeout_seconds=timeout_seconds)
        if self.configuration_path is not None:
            self.configuration_path.unlink(missing_ok=True)



def start_ssh_port_forward_process(
    *,
    host: str,
    port: int,
    username: str,
    private_key_path: Path | None,
    ssh_config_host: str | None,
    forward: SshPortForwardSpec,
    log_dir: Path,
    forward_id: str,
) -> tuple[ManagedProcess, Path, int]:
    configuration = _effective_forward_config(
        host=host,
        port=port,
        username=username,
        private_key_path=private_key_path,
        ssh_config_host=ssh_config_host,
    )
    forward_arguments = [
        "-N",
        "-T",
        "-v",
        f"-{forward.direction}",
        forward.argument(),
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "CanonicalizeHostname=no",
        "-o",
        "ForkAfterAuthentication=no",
    ]
    log_store = ProcessLogStore(log_dir)
    log_path = log_store.path_for("ssh-port-forward", forward_id)
    log_file = log_store.open("ssh-port-forward", forward_id)
    log_start = log_path.stat().st_size
    descriptor, filename = tempfile.mkstemp(prefix=f"{forward_id}-", suffix=".ssh-config", dir=log_dir)
    configuration_path = Path(filename)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        restrict_private_path_access(configuration_path, descriptor=stream.fileno())
        stream.write(configuration)
    command = ["ssh", "-F", str(configuration_path), *forward_arguments, "--", ssh_config_host or f"{username}@{host}"]
    try:
        process = _spawn_logged_process(command, log_file=log_file)
    except BaseException:
        log_file.close()
        configuration_path.unlink()
        raise
    logger.info(
        "启动 SSH 端口转发: pid=%s destination=%s listen_port=%s "
        "destination_port=%s log=%s",
        process.pid,
        ssh_config_host or host,
        forward.listen_port,
        forward.destination_port,
        log_path,
    )
    return _SshPortForwardProcess(process=process, log_file=log_file, configuration_path=configuration_path), log_path, log_start
