"""每个文件监听服务共用一个进程，隔离 watchfiles 初始化和关闭所持的 GIL。"""

from __future__ import annotations

import asyncio
import multiprocessing
import traceback
from collections.abc import AsyncIterator
from multiprocessing.connection import Connection
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Literal, cast

from boxteam_plugin_sdk.lifecycle import run_cleanup_shielded
from watchfiles import Change, watch
from watchfiles._rust_notify import WatchfilesRustInternalError

from app.core.shielded_cleanup import run_sync_shielded

_Changes = set[tuple[Change, str]]
_Message = tuple[Literal["batch", "error", "stopped"], int, _Changes | BaseException | None]


def _restore_native_error(args: tuple[object, ...], attributes: dict[str, object]) -> WatchfilesRustInternalError:
    error = WatchfilesRustInternalError(*args)
    error.__dict__.update(attributes)
    return error


def _reduce_native_error(error: WatchfilesRustInternalError):
    return _restore_native_error, (error.args, error.__dict__)


# 当前原生异常的 __module__ 是不可导入的 `_rust_notify`；显式登记进程间编码，保留类别和诊断。
ForkingPickler.register(WatchfilesRustInternalError, _reduce_native_error)


def _serve(connection: Connection) -> None:
    send_lock = Lock()
    workers: dict[int, tuple[Thread, Event]] = {}

    def send(message: _Message) -> None:
        with send_lock:
            connection.send(message)

    def observe(token: int, root: Path, options: dict[str, object], stop: Event) -> None:
        try:
            for changes in watch(root, stop_event=stop, **options):
                send(("batch", token, changes))
        except BaseException as error:  # noqa: BLE001 - 把原生错误交回原监听 owner。
            error.add_note(f"原生文件监听：root={root}\n" + traceback.format_exc())
            send(("error", token, error))
        finally:
            send(("stopped", token, None))
            workers.pop(token, None)

    try:
        while True:
            command, token, payload = connection.recv()
            if command == "shutdown":
                break
            if command == "start":
                root, options = payload
                stop = Event()
                thread = Thread(target=observe, args=(token, root, options, stop))
                workers[token] = (thread, stop)
                thread.start()
            elif command == "stop":
                if worker := workers.get(token):
                    worker[1].set()
            else:
                raise ValueError(f"未知文件监听指令: {command}")
    except EOFError:
        pass
    finally:
        pending = tuple(workers.values())
        for _, stop in pending:
            stop.set()
        for thread, _ in pending:
            thread.join()
        connection.close()


class FileWatchRuntime:
    def __init__(self) -> None:
        self._connection: Connection | None = None
        self._process: multiprocessing.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._close_owner: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._queues: dict[int, asyncio.Queue[_Message]] = {}
        self._next_token = 0
        self._failure: RuntimeError | None = None

    async def _start(self) -> None:
        async with self._start_lock:
            if self._close_owner is not None:
                raise RuntimeError("文件监听进程已停止接受订阅")
            if self._failure is not None:
                raise self._failure
            if self._process is not None:
                return
            context = multiprocessing.get_context("spawn")
            connection, child = context.Pipe()
            process = context.Process(target=_serve, args=(child,), name="boxteam-file-watch")
            try:
                await run_sync_shielded(process.start)
            finally:
                child.close()
                if process.pid is not None:
                    self._process, self._connection = process, connection
                    self._reader = asyncio.create_task(self._read(), name="boxteam-file-watch-reader")
                else:
                    connection.close()
                    process.close()

    async def _send(self, command: str, token: int = 0, payload: object = None) -> None:
        connection = self._connection
        if connection is None:
            raise RuntimeError("文件监听进程尚未启动")
        async with self._send_lock:
            await run_sync_shielded(connection.send, (command, token, payload))

    async def _read(self) -> None:
        assert self._connection is not None
        try:
            while True:
                message = cast(_Message, await asyncio.to_thread(self._connection.recv))
                await self._queues[message[1]].put(message)
        except (EOFError, OSError) as error:
            if self._close_owner is None:
                failure = RuntimeError(f"文件监听进程意外断开: {type(error).__name__}: {error}")
                failure.__cause__ = error
                self._failure = failure
                for token, queue in tuple(self._queues.items()):
                    await queue.put(("error", token, failure))
                    await queue.put(("stopped", token, None))

    async def watch(self, root: Path, **options: object) -> AsyncIterator[_Changes]:
        await self._start()
        self._next_token += 1
        token = self._next_token
        queue: asyncio.Queue[_Message] = asyncio.Queue(maxsize=1)
        self._queues[token] = queue
        stopped = False
        try:
            await self._send("start", token, (root, options))
            while True:
                kind, _, payload = await queue.get()
                if kind == "stopped":
                    stopped = True
                    return
                if kind == "error":
                    raise cast(BaseException, payload)
                yield cast(_Changes, payload)
        finally:
            async def release() -> None:
                if not stopped:
                    if self._failure is None:
                        try:
                            await self._send("stop", token)
                        except (EOFError, OSError):
                            # reader 负责交回进程断开的原错和 stopped；仍须排空该回执。
                            pass
                    failure = None
                    while True:
                        kind, _, payload = await queue.get()
                        if kind == "stopped":
                            break
                        if kind == "error":
                            failure = cast(BaseException, payload)
                    if failure is not None:
                        raise failure

            try:
                await run_cleanup_shielded(release)
            finally:
                del self._queues[token]

    async def close(self) -> None:
        if self._close_owner is None:
            self._close_owner = asyncio.create_task(self._close(), name="boxteam-file-watch-close")
        await asyncio.shield(self._close_owner)

    async def _close(self) -> None:
        async with self._start_lock:
            if self._process is None:
                return
            try:
                await self._send("shutdown")
            except (EOFError, OSError):
                # 即使管道已经断开，也必须回收原进程并报告实际退出码。
                pass
            assert self._reader is not None
            await self._reader
            await run_sync_shielded(self._process.join)
            assert self._connection is not None
            self._connection.close()
            exitcode = self._process.exitcode
            self._process.close()
            if exitcode != 0:
                raise RuntimeError(f"文件监听进程退出失败: exitcode={exitcode}")
