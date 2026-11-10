"""测试包的多会话 MCP 入口；协议与响应复用官方 SDK 和既有资产。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.datastructures import Headers
from starlette.responses import Response

# 此文件作为测试包入口执行，项目路径由原运行 owner 显式交付。
sys.path.insert(0, os.environ["BOXTEAM_PROJECT_ROOT"])

from app.core.logging_config import configure_application_logging
from app.core.trace_middleware import TraceMiddleware
from app.runtime.plugin_commands import plugin_command_router
from tests.integration.workspace_services.mcp.schema_server import (
    build_server,
)


def main() -> None:
    configure_application_logging(service="shared-mcp-probe")
    parameters = json.loads(os.environ["BOXTEAM_PLUGIN_PARAMETERS"])

    def observe(name: str, arguments: dict[str, object], headers: Headers) -> None:
        with Path(parameters["calls"]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"name": name, "arguments": arguments,
                "workspace_id": headers["X-BoxTeam-Workspace-Id"],
                "session_id": headers["Mcp-Session-Id"], "pid": os.getpid()}) + "\n")

    manager = StreamableHTTPSessionManager(app=build_server(
        Path(parameters["definitions"]), Path(parameters["receipts"]),
        call_observer=observe, page_size=1,
    ))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with manager.run():
            yield

    async def shutdown() -> None:
        server.should_exit = True

    async def mcp(scope, receive, send) -> None:
        if Headers(scope=scope).get("X-Local-Token") != os.environ["BOXTEAM_RUNTIME_CONTROL_TOKEN"]:
            await Response(status_code=403)(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(TraceMiddleware)
    app.include_router(plugin_command_router({}, shutdown=shutdown))
    app.mount("/services/mcp", mcp)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1",
        port=int(os.environ["BOXTEAM_PLUGIN_PORT"]), log_config=None))
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
