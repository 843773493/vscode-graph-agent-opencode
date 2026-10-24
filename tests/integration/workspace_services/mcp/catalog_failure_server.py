"""本地 stdio 协议替身：按显式标记返回 tools/list 错误，不调用模型。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError


async def serve(failure_path: Path, receipt_path: Path) -> None:
    server = Server("catalog-failure-fixture")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        failure = json.loads(failure_path.read_text(encoding="utf-8")) if failure_path.exists() else None
        with receipt_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"method": "tools/list", "failure": failure}) + "\n")
        if failure is not None:
            raise McpError(types.ErrorData(
                code=-32603, message=failure["message"], data={"path": failure["path"]},
            ))
        return [types.Tool(
            name="echo", description="确定性目录成员",
            inputSchema={"type": "object", "properties": {}},
        )]

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(serve(Path(sys.argv[1]), Path(sys.argv[2])))
