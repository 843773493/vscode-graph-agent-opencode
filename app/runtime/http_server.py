"""受管 HTTP 进程唯一入口；支持 listener-ready 启动回调与原应用关闭前钩子。"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import threading
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, cast

import uvicorn


class ManagedHTTPServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, *, shutdown_on_stdin_eof: bool = False) -> None:
        super().__init__(config)
        self.shutdown_on_stdin_eof = shutdown_on_stdin_eof
        self.http_startup_error: BaseException | None = None
        self.http_startup_task: asyncio.Task[None] | None = None

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        # 原 ASGI lifespan state 会由 Uvicorn 交给各请求，不另建服务注册或进程 owner。
        self.lifespan.state["request_process_shutdown"] = self.request_process_shutdown
        await super().startup(sockets=sockets)
        after_http_startup = cast(
            Callable[[], Coroutine[Any, Any, None]] | None,
            self.lifespan.state.get("after_http_startup"),
        )
        if after_http_startup is not None:
            self.http_startup_task = asyncio.create_task(after_http_startup())
            self.http_startup_task.add_done_callback(self._record_http_startup_result)

    def _record_http_startup_result(self, task: asyncio.Task[object]) -> None:
        try:
            task.result()
        except BaseException as error:  # noqa: BLE001 - 交由同一 Uvicorn shutdown 路径关闭 listener/lifespan。
            self.http_startup_error = error
            self.should_exit = True

    def _raise_http_startup_error(self) -> None:
        if self.http_startup_error is not None:
            self.started = False
            raise RuntimeError("HTTP listener 启动后初始化失败") from self.http_startup_error

    async def request_process_shutdown(self) -> None:
        self.should_exit = True

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        if not self.shutdown_on_stdin_eof:
            await super().serve(sockets=sockets)
            self._raise_http_startup_error()
            return
        loop = asyncio.get_running_loop()
        completed = threading.Event()
        dispatch = threading.Lock()
        failures: list[OSError] = []
        descriptor = os.dup(sys.stdin.fileno())

        def request_shutdown(error: OSError | None) -> None:
            if completed.is_set():
                return
            if error is not None:
                failures.append(error)
            self.should_exit = True

        def watch_launcher() -> None:
            failure = None
            try:
                if sys.platform == "win32":
                    from app.runtime.windows_ipc import wait_for_pipe_eof
                    wait_for_pipe_eof(descriptor, completed)
                else:
                    while os.read(descriptor, 4096):
                        pass
            except OSError as error:
                failure = error
            finally:
                os.close(descriptor)
            with dispatch:
                if not completed.is_set():
                    loop.call_soon_threadsafe(request_shutdown, failure)

        # 普通 stdin 不接入 Proactor；daemon 避免信号退出时等待阻塞的 executor。
        threading.Thread(target=watch_launcher, name="managed-http-stdin", daemon=True).start()
        try:
            await super().serve(sockets=sockets)
        finally:
            with dispatch:
                completed.set()
        if failures:
            raise failures[0]
        self._raise_http_startup_error()

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        if self.started:
            before_shutdown = cast(
                Callable[[], Awaitable[None]] | None,
                self.lifespan.state.get("before_http_shutdown"),
            )
            if before_shutdown is not None:
                await before_shutdown()
        if self.http_startup_task is not None and not self.http_startup_task.done():
            await asyncio.shield(asyncio.gather(self.http_startup_task, return_exceptions=True))
        await super().shutdown(sockets=sockets)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        default="info",
    )
    parser.add_argument("--timeout-graceful-shutdown", type=int)
    parser.add_argument("--shutdown-on-stdin-eof", action="store_true")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    config = uvicorn.Config(
        args.app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        timeout_graceful_shutdown=args.timeout_graceful_shutdown,
        reload=args.reload,
        lifespan="on",
    )
    server = ManagedHTTPServer(config, shutdown_on_stdin_eof=args.shutdown_on_stdin_eof)
    if args.reload:
        from uvicorn.supervisors import ChangeReload

        sockets = [config.bind_socket()]
        ChangeReload(config, target=server.run, sockets=sockets).run()
        return
    server.run()
    if not server.started or server.lifespan.shutdown_failed:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
