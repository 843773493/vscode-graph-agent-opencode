"""受控 stdio MCP：发布完整契约并记录真正到达 handler 的参数。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


async def serve(definitions_path: Path, receipt_path: Path) -> None:
    server = Server("full-schema-fixture")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        definitions = json.loads(definitions_path.read_text(encoding="utf-8"))
        return [types.Tool(name=name, description="完整输入契约", inputSchema=schema)
                for name, schema in definitions.items()]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, object]) -> list[types.TextContent]:
        receipt = {"name": name, "arguments": arguments}
        with receipt_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False) + "\n")
        return [types.TextContent(type="text", text=json.dumps(receipt, ensure_ascii=False))]

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(serve(Path(sys.argv[1]), Path(sys.argv[2])))
