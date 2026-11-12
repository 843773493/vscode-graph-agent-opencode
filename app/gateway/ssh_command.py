from __future__ import annotations

import os
from pathlib import Path


def ssh_path_option(name: str, value: str) -> str:
    # OpenSSH 没有字面 ${...} 的转义语法，禁止静默改读环境变量展开后的路径。
    if "${" in value:
        raise ValueError(f"{name} 路径不支持 OpenSSH 环境变量语法: {value}")
    # TODO: 在原生 Windows OpenSSH 上验证路径转义。
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'{name}="{escaped}"'


def build_ssh_command(
    *,
    host: str,
    port: int,
    username: str,
    private_key_path: str | None,
    ssh_config_host: str | None,
    extra_arguments: list[str] | None = None,
    remote_command: str | None = None,
) -> list[str]:
    command = ["ssh"]
    if extra_arguments:
        command.extend(extra_arguments)
    if ssh_config_host:
        destination = ssh_config_host
    else:
        if not private_key_path:
            raise ValueError("显式 SSH 连接缺少 private_key_path")
        command.extend(
            [
                "-o",
                ssh_path_option("IdentityFile", str(Path(private_key_path).expanduser().resolve())),
                "-p",
                str(port),
            ]
        )
        destination = f"{username}@{host}"
    command.extend(["-o", "BatchMode=yes"])
    known_hosts_file = os.environ.get("BOXTEAM_GATEWAY_SSH_KNOWN_HOSTS_FILE")
    if known_hosts_file:
        command.extend(
            [
                "-o",
                ssh_path_option("UserKnownHostsFile", str(Path(known_hosts_file).expanduser().resolve())),
                "-o",
                "StrictHostKeyChecking=accept-new",
            ]
        )
    command.append(destination)
    if remote_command is not None:
        command.append(remote_command)
    return command
