"""受控 stdio MCP：发布完整契约并记录真正到达 handler 的参数。"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import wave
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

RESULT_IMAGE_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMQCegBAAFsAPH6cDjBAAAAAElFTkSuQmCC"


def audio_asset() -> str:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 80)
    return base64.b64encode(stream.getvalue()).decode("ascii")


RESULT_AUDIO_BASE64 = audio_asset()


def build_server(definitions_path: Path, receipt_path: Path) -> Server:
    server = Server("full-schema-fixture")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        definitions = json.loads(definitions_path.read_text(encoding="utf-8"))
        return [types.Tool(name=name, description="完整输入契约", inputSchema=schema)
                for name, schema in definitions.items()]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, object]) -> list[types.TextContent] | types.CallToolResult:
        receipt = {"name": name, "arguments": arguments}
        with receipt_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False) + "\n")
        call_mode = arguments.get("call_mode")
        if call_mode == "disconnect":
            os._exit(0)
        if call_mode in {"progress", "wait"}:
            context = server.request_context
            token = context.meta.progressToken if context.meta is not None else None
            if token is None:
                raise ValueError("调用必须提供 SDK progress token")
            await context.session.send_progress_notification(
                token, progress=1, total=2, message="私有路径 /private/server/token，不得进入 Activity",
            )
            if call_mode == "wait":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    with receipt_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({**receipt, "cancelled": True}, ensure_ascii=False) + "\n")
                    raise
            await context.session.send_progress_notification(token, progress=2, total=2)
        if arguments.get("result_mode") in {"success", "error"}:
            return types.CallToolResult(
                isError=arguments["result_mode"] == "error", structuredContent={"receipt": receipt},
                content=[
                    types.TextContent(type="text", text="完整协议回执"),
                    types.ImageContent(type="image", data=RESULT_IMAGE_BASE64, mimeType="image/png"),
                    types.AudioContent(type="audio", data=RESULT_AUDIO_BASE64, mimeType="audio/wav"),
                    types.ResourceLink(type="resource_link", uri="https://example.invalid/report", name="report", mimeType="text/plain"),
                    types.EmbeddedResource(type="resource", resource=types.TextResourceContents(uri="https://example.invalid/note", text="嵌入正文")),
                ],
            )
        return [types.TextContent(type="text", text=json.dumps(receipt, ensure_ascii=False))]

    return server


async def serve(definitions_path: Path, receipt_path: Path) -> None:
    server = build_server(definitions_path, receipt_path)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(serve(Path(sys.argv[1]), Path(sys.argv[2])))
