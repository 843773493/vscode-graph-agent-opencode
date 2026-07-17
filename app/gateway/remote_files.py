from __future__ import annotations

import asyncio
import json
import shlex
import subprocess

from app.gateway.registry import SshWorkspaceConnection
from app.gateway.schemas import GatewayDirectoryListDTO
from app.gateway.ssh_command import build_ssh_command


_REMOTE_DIRECTORY_SCRIPT = """
import json
import os
import sys
from pathlib import Path

requested_path = sys.argv[1]
limit = int(sys.argv[2])
directory = Path(os.path.expanduser(requested_path)).resolve()
if not directory.is_dir():
    raise NotADirectoryError(f"远程路径不是目录: {directory}")
with os.scandir(directory) as iterator:
    children = [entry for entry in iterator if entry.is_dir(follow_symlinks=False)]
children.sort(key=lambda entry: (entry.name.lower(), entry.name))
parent = directory.parent if directory.parent != directory else None
print(json.dumps({
    "path": str(directory),
    "parent_path": str(parent) if parent is not None else None,
    "home_path": str(Path.home().resolve()),
    "entries": [
        {"name": entry.name, "path": str(Path(entry.path).resolve())}
        for entry in children[:limit]
    ],
    "truncated": len(children) > limit,
    "limit": limit,
}))
""".strip()


def _run_remote_directory_query(
    connection: SshWorkspaceConnection,
    path: str | None,
    limit: int,
) -> GatewayDirectoryListDTO:
    requested_path = path.strip() if path and path.strip() else "~"
    remote_command = " ".join(
        [
            "python3",
            "-c",
            shlex.quote(_REMOTE_DIRECTORY_SCRIPT),
            shlex.quote(requested_path),
            str(limit),
        ]
    )
    try:
        result = subprocess.run(
            build_ssh_command(
                host=connection.host,
                port=connection.port,
                username=connection.username,
                private_key_path=connection.private_key_path,
                ssh_config_host=connection.ssh_config_host,
                extra_arguments=["-o", "ConnectTimeout=10"],
                remote_command=remote_command,
            ),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"执行远程目录 SSH 命令失败: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "SSH 命令无输出"
        raise RuntimeError(
            f"读取远程目录失败: {connection.username}@{connection.host}:{connection.port}: {detail}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"远程目录响应不是有效 JSON: {result.stdout[:500]}"
        ) from error
    return GatewayDirectoryListDTO.model_validate(payload)


async def list_ssh_directories(
    connection: SshWorkspaceConnection,
    path: str | None,
    limit: int,
) -> GatewayDirectoryListDTO:
    return await asyncio.to_thread(
        _run_remote_directory_query,
        connection,
        path,
        limit,
    )
