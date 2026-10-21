"""原 Web E2E 的真实 stdio MCP 外部服务；目录文件只控制外部依赖。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import anyio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from pydantic import BaseModel

from tests.support.e2e_control.harness import control_request


class Catalog(BaseModel):
    revision: int
    tools: list[types.Tool]
    error: str | None = None


async def run(catalog_path: Path, *, notifications: bool) -> None:
    server = Server("Section 8 Dynamic MCP")
    session: ServerSession | None = None
    address = json.loads(os.environ["BOXTEAM_E2E_TEST_CONTROL"])

    def catalog() -> Catalog:
        return Catalog.model_validate_json(catalog_path.read_bytes())

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        nonlocal session
        session = server.request_context.session
        value = catalog()
        await asyncio.to_thread(control_request, address, {
            "action": "phase", "operation_id": f"mcp-list:{catalog_path}",
            "phase": "mcp.list.after_capture",
            "details": {"catalog_revision": value.revision, "server_id": catalog_path.stem},
        })
        if value.error is not None:
            raise RuntimeError(value.error)
        return value.tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, object]) -> list[types.TextContent]:
        value = catalog()
        if not any(tool.name == name for tool in value.tools):
            raise ValueError(f"MCP target 已不存在: {name}")
        text = arguments["text"]
        if not isinstance(text, str):
            raise TypeError("MCP echo 的 text 必须为字符串")
        await asyncio.to_thread(control_request, address, {
            "action": "phase", "operation_id": "mcp:" + text, "phase": "mcp.call.before_result",
            "details": {"remote_name": name, "catalog_revision": value.revision},
        })
        # 调用已经进入该真实 server；目录的后续变化不能改写本次结果事实。
        return [types.TextContent(type="text", text=f"mini-mcp:{name}:{text}:revision={value.revision}")]

    async def notify_changes() -> None:
        revision = catalog().revision
        while True:
            await asyncio.sleep(0.05)
            current = catalog().revision
            if current != revision:
                revision = current
                if session is not None:
                    await session.send_tool_list_changed()

    async with stdio_server() as (reader, writer), anyio.create_task_group() as tasks:
        if notifications:
            tasks.start_soon(notify_changes)
        await server.run(reader, writer, server.create_initialization_options(
            notification_options=NotificationOptions(tools_changed=notifications),
        ))
        tasks.cancel_scope.cancel()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--notifications", action="store_true")
    options = parser.parse_args()
    asyncio.run(run(options.catalog, notifications=options.notifications))


if __name__ == "__main__":
    main()
