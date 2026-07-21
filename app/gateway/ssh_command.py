from __future__ import annotations

import os
from pathlib import Path


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
                "-i",
                str(Path(private_key_path).expanduser().resolve()),
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
                (
                    "UserKnownHostsFile="
                    f"{Path(known_hosts_file).expanduser().resolve()}"
                ),
                "-o",
                "StrictHostKeyChecking=accept-new",
            ]
        )
    command.append(destination)
    if remote_command is not None:
        command.append(remote_command)
    return command
