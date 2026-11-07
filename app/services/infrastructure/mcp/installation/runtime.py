"""生态安装共用的制品摘要、进程容器及有界日志；不持有安装事实。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from app.core.child_process import OwnedChildProcess
from app.core.identifier import create_uuid_hex
from app.core.shielded_cleanup import run_cleanup_shielded
from app.domain.itemized.hashing import sha256_jcs

StageCallback = Callable[[str, str], Awaitable[None]]
_MAX_LOG = 65536


class InstallationUnavailable(RuntimeError):
    """当前目标不能兑现所选生态的制品合同。"""


def artifact_digest(root: Path) -> str:
    """校验固定路径中的已发布文件；日志和安装账本不在此目录。"""
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entry: dict[str, object] = {"path": relative, "symlink": os.readlink(path)}
            if path.is_file():
                with path.open("rb") as stream:
                    entry["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            entries.append(entry)
        elif path.is_file():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            entries.append({"path": relative, "sha256": digest, "mode": path.stat().st_mode & 0o777})
    return sha256_jcs(entries)


async def run_tool(command: list[str], *, directory: Path, progress: StageCallback, stage: str,
                   environment: Mapping[str, str], tool: str) -> str:
    await progress(stage, "")
    owned = OwnedChildProcess(create_uuid_hex(namespace="mcp-installation-process"))
    chunks: list[bytes] = []
    size = 0
    output = ""

    async def read() -> None:
        nonlocal size
        process = owned.process
        assert process is not None and process.stdout is not None
        async for chunk in process.stdout:
            # 排空工具输出但只保留固定上限；不把请求输入传给命令或日志。
            if size < _MAX_LOG:
                chunks.append(chunk[:_MAX_LOG - size])
                size += len(chunks[-1])
        await process.wait()

    async def finish() -> None:
        nonlocal output
        await owned.close()
        output = b"".join(chunks).decode("utf-8", errors="replace")
        await progress(stage, output)

    try:
        async with asyncio.timeout(180):
            await owned.start(command, cwd=directory, env=environment, stderr=-2)
            await read()
    finally:
        # 取消和阶段超时都须核实原进程容器为空；清理失败不能声称已取消。
        await run_cleanup_shielded(finish)
    process = owned.process
    assert process is not None
    if process.returncode != 0:
        raise InstallationUnavailable(f"目标 {tool} 阶段 {stage} 失败，exit_code={process.returncode}；查看原操作日志")
    return output
