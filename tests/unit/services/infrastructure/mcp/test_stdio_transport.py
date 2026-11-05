"""原进程必须先结束，才能排空仍阻塞的 stdio 读取。"""

from __future__ import annotations

import asyncio
import shutil
from types import SimpleNamespace

import anyio
import pytest
from anyio.abc import ByteReceiveStream
from mcp import StdioServerParameters

from app.services.infrastructure.mcp.transport import stdio
from app.services.infrastructure.mcp.transport.stdio import mcp_stdio_transport


@pytest.fixture
def blocked_stdio_process():
    reading, closed = asyncio.Event(), asyncio.Event()
    closes, starts = [], []

    class Output(ByteReceiveStream):
        async def receive(self, max_bytes: int = 65536) -> bytes:
            reading.set()
            try:
                await asyncio.Event().wait()
            finally:
                # 模拟无法仅凭任务取消结束的原生管道读取。
                with anyio.CancelScope(shield=True):
                    await asyncio.wait_for(closed.wait(), 1)
            raise anyio.EndOfStream

        async def aclose(self) -> None:
            return None

    async def start(command, **kwargs):
        starts.append(command)

    async def close():
        closes.append(True)
        closed.set()

    async def on_started():
        return None

    owner = SimpleNamespace(start=start, close=close, process=SimpleNamespace(stdout=Output(), stdin=object()))
    return SimpleNamespace(owner=owner, reading=reading, closed=closed, closes=closes, starts=starts, on_started=on_started)


async def test_process_close_unblocks_reader_before_transport_joins_io_tasks(blocked_stdio_process):
    case = blocked_stdio_process
    async with mcp_stdio_transport(StdioServerParameters(command="fixture"), case.owner, case.on_started):
        await asyncio.wait_for(case.reading.wait(), 1)
        assert not case.closed.is_set()
    assert case.closed.is_set() and case.closes == [True]


@pytest.mark.parametrize(("command", "lookup", "resolved"), [
    ("npx", {"npx": r"C:\Tools\npx.cmd"}, r"C:\Tools\npx.cmd"),
    ("npm", {"npm.cmd": r"C:\Tools\npm.cmd"}, r"C:\Tools\npm.cmd"),
    (r"C:\Tools with spaces\server.cmd", {r"C:\Tools with spaces\server.cmd": r"C:\Tools with spaces\server.cmd"}, r"C:\Tools with spaces\server.cmd"),
    ("server.exe", {"server.exe": r"C:\Tools\server.exe"}, r"C:\Tools\server.exe"),
    ("unresolved", {}, "unresolved"),
])
async def test_windows_stdio_keeps_official_command_resolution_and_argument_array(
    blocked_stdio_process, monkeypatch, command, lookup, resolved,
):
    case = blocked_stdio_process
    queries = []

    def which(value):
        queries.append(value)
        return lookup.get(value)

    monkeypatch.setattr(shutil, "which", which)
    with monkeypatch.context() as platform_patch:
        platform_patch.setattr(stdio.sys, "platform", "win32")
        async with mcp_stdio_transport(StdioServerParameters(command=command, args=["argument with space", "&literal"]),
                                        case.owner, case.on_started):
            assert case.starts == [[resolved, "argument with space", "&literal"]]
            assert queries[0] == command


async def test_posix_stdio_preserves_command_without_windows_resolution(blocked_stdio_process, monkeypatch):
    case = blocked_stdio_process

    def unexpected_resolution(value):
        pytest.fail("POSIX 启动不能进入 Windows 命令解析")

    monkeypatch.setattr(stdio, "get_windows_executable_command", unexpected_resolution)
    with monkeypatch.context() as platform_patch:
        platform_patch.setattr(stdio.sys, "platform", "linux")
        async with mcp_stdio_transport(StdioServerParameters(command="server", args=["argument with space"]),
                                        case.owner, case.on_started):
            assert case.starts == [["server", "argument with space"]]
