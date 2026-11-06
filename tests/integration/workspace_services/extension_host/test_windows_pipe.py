"""Windows 实际私有句柄、字节流和取消；不依赖 Node 或网络服务。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator

import pytest

from app.runtime.windows_ipc import WindowsPipeServer

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows 原生管道验证")


@pytest.fixture
async def pipe_server() -> AsyncIterator[WindowsPipeServer]:
    server = WindowsPipeServer()
    try:
        yield server
    finally:
        server.close()
        await server.wait_closed()
        if not server.connection.cancelled():
            _reader, writer = server.connection.result()
            writer.close()
            await writer.wait_closed()


async def test_private_pipe_is_a_duplex_byte_stream_with_current_user_acl(pipe_server: WindowsPipeServer) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows 原生管道验证")
    from asyncio.windows_events import ProactorEventLoop

    import ntsecuritycon
    import pywintypes
    import win32api
    import win32con
    import win32file
    import win32security

    loop = asyncio.get_running_loop()
    assert isinstance(loop, ProactorEventLoop)
    client_reader = asyncio.StreamReader()
    client_protocol = asyncio.StreamReaderProtocol(client_reader)
    transport, _protocol = await loop.create_pipe_connection(lambda: client_protocol, pipe_server.address)
    client_writer = asyncio.StreamWriter(transport, client_protocol, client_reader, loop)
    try:
        reader, writer = await pipe_server.connection
        descriptor = win32security.GetSecurityInfo(
            transport.get_extra_info("pipe").fileno(), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        acl = descriptor.GetSecurityDescriptorDacl()
        assert acl is not None and acl.GetAceCount() == 1
        control, _revision = descriptor.GetSecurityDescriptorControl()
        assert control & win32security.SE_DACL_PROTECTED
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            user, _attributes = win32security.GetTokenInformation(token, win32security.TokenUser)
        finally:
            win32api.CloseHandle(token)
        assert acl.GetAce(0) == ((win32security.ACCESS_ALLOWED_ACE_TYPE, 0), ntsecuritycon.FILE_ALL_ACCESS, user)
        with pytest.raises(pywintypes.error) as occupied:
            win32file.CreateFile(pipe_server.address, win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                                 0, None, win32con.OPEN_EXISTING, win32con.FILE_FLAG_OVERLAPPED, None)
        assert occupied.value.winerror == 231

        async def send(output: asyncio.StreamWriter, value: bytes) -> None:
            output.write(value)
            await output.drain()

        # 大于单次管道缓冲，含NUL与全部字节；防止message-mode或第二个读者破坏RPC正文。
        outgoing = bytes(range(256)) * 8192
        incoming = outgoing[::-1] + b"opposite-direction"
        results = await asyncio.gather(
            send(client_writer, outgoing), reader.readexactly(len(outgoing)),
            send(writer, incoming), client_reader.readexactly(len(incoming)),
        )
        assert results[1] == outgoing and results[3] == incoming
        client_writer.close()
        await client_writer.wait_closed()
        assert await reader.read() == b""
    finally:
        client_writer.close()
        await client_writer.wait_closed()


@pytest.mark.parametrize("started", [False, True])
async def test_cancelled_accept_releases_pipe_before_or_after_first_schedule(started: bool) -> None:
    import pywintypes
    import win32con
    import win32file

    server = WindowsPipeServer()
    if started:
        await asyncio.sleep(0)
    server.close()
    await server.wait_closed()
    assert server.connection.cancelled()
    with pytest.raises(pywintypes.error) as failure:
        win32file.CreateFile(server.address, win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                             0, None, win32con.OPEN_EXISTING, win32con.FILE_FLAG_OVERLAPPED, None)
    assert failure.value.winerror == 2
    server.close()
    await server.wait_closed()


async def test_cancelling_a_waiter_preserves_the_accept_until_explicit_close(pipe_server: WindowsPipeServer) -> None:
    waiter = asyncio.create_task(pipe_server.wait_closed())
    await asyncio.sleep(0)
    waiter.cancel("取消等待，不取消listener")
    with pytest.raises(asyncio.CancelledError, match="取消等待"):
        await waiter
    assert not pipe_server.connection.done()
    pipe_server.close()
    await pipe_server.wait_closed()
    assert pipe_server.connection.cancelled()
