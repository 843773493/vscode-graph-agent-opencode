"""通过唯一平台监督器执行命令，并有界收集标准流。"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path


class CommandOutputLimitError(ValueError):
    pass


@dataclass(frozen=True)
class NativeCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


async def run_native_command(command: list[str], *, workspace: Path, stdin: bytes,
                             timeout_seconds: float, memory_limit_mb: int,
                             output_limit_bytes: int) -> NativeCommandResult:
    guard = files("app.runtime").joinpath("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(guard), "--memory-limit-kb", str(memory_limit_mb * 1024),
        "--timeout-seconds", str(timeout_seconds), "--", *command,
        cwd=workspace, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    total = 0
    exceeded = False

    def terminate() -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    async def collect(stream: asyncio.StreamReader | None) -> bytes:
        nonlocal total, exceeded
        assert stream is not None
        chunks: list[bytes] = []
        while chunk := await stream.read(8192):
            total += len(chunk)
            if total > output_limit_bytes:
                if not exceeded:
                    exceeded = True
                    terminate()
                # 保持排空管道，避免退出回收被StreamReader反压阻塞。
            else:
                chunks.append(chunk)
        return b"".join(chunks)

    async def write_input() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(stdin)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # 命令可以在读完输入前明确退出；实际退出码仍由监督器返回。
            pass
        finally:
            process.stdin.close()

    readers = [asyncio.create_task(collect(process.stdout)), asyncio.create_task(collect(process.stderr))]
    writer = asyncio.create_task(write_input())
    try:
        # OS监督器自身保证期限；此处只约束管道或监督器异常挂起。
        async with asyncio.timeout(timeout_seconds + 15):
            stdout, stderr = await asyncio.gather(*readers)
            await writer
            code = await process.wait()
            if exceeded:
                raise CommandOutputLimitError("脚本标准输出与错误输出总量超过配置上限")
            return NativeCommandResult(code, stdout, stderr)
    finally:
        if process.returncode is None:
            terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=12)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in [*readers, writer]:
            task.cancel()
        await asyncio.gather(asyncio.gather(*readers, return_exceptions=True), writer, return_exceptions=True)
