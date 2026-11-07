"""Gateway 唯一 HTTP 进程入口；监听关闭前结清原扩展管理队列。"""

from __future__ import annotations

import argparse
import socket
from typing import TYPE_CHECKING, cast

import uvicorn

if TYPE_CHECKING:
    from app.gateway.runtime.extension_management import GatewayExtensionManagement


class GatewayServer(uvicorn.Server):
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
    args = parser.parse_args()
    server = GatewayServer(uvicorn.Config(
        args.app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        timeout_graceful_shutdown=args.timeout_graceful_shutdown,
        lifespan="on",
    ))
    server.run()
    if not server.started or server.lifespan.shutdown_failed:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
