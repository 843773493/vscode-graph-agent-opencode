"""Gateway 唯一 HTTP 进程入口；监听关闭前结清原扩展管理队列。"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import threading
from typing import TYPE_CHECKING, cast

import uvicorn

if TYPE_CHECKING:
    from app.gateway.runtime.extension_management import GatewayExtensionManagement


class GatewayServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, *, shutdown_on_stdin_eof: bool = False) -> None:
        super().__init__(config)
        self.shutdown_on_stdin_eof = shutdown_on_stdin_eof

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        if not self.shutdown_on_stdin_eof:
            await super().serve(sockets=sockets)
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
        threading.Thread(target=watch_launcher, name="gateway-launcher-stdin", daemon=True).start()
        try:
            await super().serve(sockets=sockets)
        finally:
            with dispatch:
                completed.set()
        if failures:
            raise failures[0]

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        if self.started:
            # 句柄由原应用 lifespan 交付；此时 HTTP listener 仍接受已受理安装的回调。
            owner = cast(
                "GatewayExtensionManagement | None",
                self.lifespan.state["extension_management"],
            )
            if owner is not None:
                await owner.quiesce()
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
    args = parser.parse_args()
    server = GatewayServer(uvicorn.Config(
        args.app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        timeout_graceful_shutdown=args.timeout_graceful_shutdown,
        lifespan="on",
    ), shutdown_on_stdin_eof=args.shutdown_on_stdin_eof)
    server.run()
    if not server.started or server.lifespan.shutdown_failed:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
