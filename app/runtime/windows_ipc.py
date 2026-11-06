"""私有 Windows 字节管道；将同一个原生句柄交给 asyncio IOCP。"""

from __future__ import annotations

import asyncio
import sys
from typing import cast
from uuid import uuid4

# Windows SDK 的具名管道标志；pywin32 未统一导出这两个常量。
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008


class WindowsPipeServer:
    """只接受一次连接，不增加线程、TCP 监听或协议封装。"""

    address: str
    connection: asyncio.Task[tuple[asyncio.StreamReader, asyncio.StreamWriter]]

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows 私有管道入口只能在 Windows 调用")
        from asyncio.windows_events import ProactorEventLoop
        from asyncio.windows_utils import PipeHandle

        import ntsecuritycon
        import pywintypes
        import win32api
        import win32con
        import win32pipe
        import win32security

        loop = asyncio.get_running_loop()
        if not isinstance(loop, ProactorEventLoop):
            raise TypeError("Windows 私有管道需要 asyncio ProactorEventLoop")
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            user, _attributes = win32security.GetTokenInformation(token, win32security.TokenUser)
        finally:
            win32api.CloseHandle(token)
        acl = win32security.ACL()
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, user)
        descriptor = win32security.SECURITY_DESCRIPTOR()
        descriptor.SetSecurityDescriptorDacl(True, acl, False)
        descriptor.SetSecurityDescriptorControl(win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED)
        security = pywintypes.SECURITY_ATTRIBUTES()
        security.SECURITY_DESCRIPTOR = descriptor
        security.bInheritHandle = False
        self.address = rf"\\.\pipe\boxteam-extension-{uuid4()}"
        native = win32pipe.CreateNamedPipe(
            self.address,
            win32pipe.PIPE_ACCESS_DUPLEX | win32con.FILE_FLAG_OVERLAPPED | _FILE_FLAG_FIRST_PIPE_INSTANCE,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
            1, 65536, 65536, 0, security,
        )
        # pywin32 的stub把实际持有Detach的PyHANDLE标成int。
        self._pipe = PipeHandle(native.Detach())  # pyright: ignore[reportAttributeAccessIssue]
        pipe = self._pipe

        async def accept() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            transport: asyncio.Transport | None = None
            protocol: asyncio.StreamReaderProtocol | None = None
            try:
                # TODO: CPython 尚无公开的已连接双向管道 transport 入口；升级 Python
                # 时须复验这两个 IOCP 接点。沿标准 PipeServer 的句柄和取消语义接管，
                # 不复制 overlapped 读写、缓冲和流控实现。
                await loop._proactor.accept_pipe(pipe)  # pyright: ignore[reportAttributeAccessIssue]
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)
                ready: asyncio.Future[None] = loop.create_future()
                transport = cast(asyncio.Transport, loop._make_duplex_pipe_transport(pipe, protocol, waiter=ready))  # pyright: ignore[reportAttributeAccessIssue]
                self._pipe = None
                await ready
                return reader, asyncio.StreamWriter(transport, protocol, reader, loop)
            except BaseException as error:
                if transport is not None and protocol is not None:
                    writer = asyncio.StreamWriter(transport, protocol, None, loop)
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except BaseException as close_error:  # noqa: BLE001 - 同时保留原失败、取消与关闭失败
                        raise BaseExceptionGroup("Windows 管道接管及关闭均失败", [error, close_error]) from None
                raise
            finally:
                if transport is None:
                    pipe.close()
                    self._pipe = None

        self.connection = loop.create_task(accept(), name="windows-private-pipe-accept")

    def close(self) -> None:
        if not self.connection.done():
            self.connection.cancel()

    async def wait_closed(self) -> None:
        # 取消会沿原 overlapped future 撤销 accept；等待任务后才能结束 listener 生命周期。
        try:
            await asyncio.shield(self.connection)
        except asyncio.CancelledError:
            waiter = asyncio.current_task()
            if not self.connection.cancelled() or waiter is not None and waiter.cancelling():
                raise
        finally:
            # close可能发生在accept协程首次调度前，此时协程finally尚未获得执行机会。
            if self.connection.done() and self._pipe is not None:
                self._pipe.close()
                self._pipe = None
