"""受控 stdio MCP：发布完整契约并记录真正到达 handler 的参数。"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import wave
from collections.abc import Callable
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import UrlElicitationRequiredError
from pydantic import AnyUrl
from starlette.datastructures import Headers
from starlette.requests import Request

RESULT_IMAGE_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMQCegBAAFsAPH6cDjBAAAAAElFTkSuQmCC"


def audio_asset() -> str:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 80)
    return base64.b64encode(stream.getvalue()).decode("ascii")


RESULT_AUDIO_BASE64 = audio_asset()


def build_server(
    definitions_path: Path, receipt_path: Path,
    *, call_observer: Callable[[str, dict[str, object], Headers], None] | None = None,
    list_tools_observer: Callable[[], None] | None = None,
    call_result_provider: Callable[[str, dict[str, object]], list[types.TextContent] | types.CallToolResult | None] | None = None,
    page_size: int | None = None,
) -> Server:
    server = Server("full-schema-fixture")

    @server.list_tools()
    async def list_tools(request: types.ListToolsRequest) -> types.ListToolsResult:
        if list_tools_observer is not None:
            list_tools_observer()
        definitions = json.loads(definitions_path.read_text(encoding="utf-8"))
        tools = [types.Tool(name=name, description="完整输入契约", inputSchema=schema)
                 for name, schema in definitions.items()]
        start = int(request.params.cursor) if request is not None and request.params and request.params.cursor else 0
        end = len(tools) if page_size is None else min(start + page_size, len(tools))
        return types.ListToolsResult(tools=tools[start:end], nextCursor=str(end) if end < len(tools) else None)

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, object]) -> list[types.TextContent] | types.CallToolResult:
        if call_observer is not None:
            request = server.request_context.request
            if not isinstance(request, Request):
                raise TypeError("HTTP MCP 观察必须取得原 tools/call Request")
            call_observer(name, arguments, request.headers)
        receipt = {"name": name, "arguments": arguments}
        with receipt_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False) + "\n")
        if call_result_provider is not None:
            result = call_result_provider(name, arguments)
            if result is not None:
                return result
        if name in {"upstream_query", "upstream_manage"}:
            # 真实远端 owner 的应用授权需求；只提供等待事实，不伪造用户同意。
            application = arguments["application"]
            state = {
                "authorization_layer": "upstream_app", "application": application,
                "status": "authorization_required" if name == "upstream_query" else "waiting_user",
            }
            if name == "upstream_query":
                state["management_tool"] = "upstream_manage"
            return types.CallToolResult(
                isError=name == "upstream_query", structuredContent=state,
                content=[types.TextContent(type="text", text=json.dumps(state))],
            )
        call_mode = arguments.get("call_mode")
        if call_mode == "notify_tools_changed":
            await server.request_context.session.send_tool_list_changed()
        if call_mode == "protocol_error":
            raise UrlElicitationRequiredError([
                types.ElicitRequestURLParams(
                    mode="url", message="先完成远端授权", url="https://example.invalid/authorize",
                    elicitationId="protocol-error-authorization",
                ),
            ])
        if call_mode == "disconnect":
            os._exit(0)
        if call_mode in {"progress", "wait"}:
            context = server.request_context
            token = context.meta.progressToken if context.meta is not None else None
            if token is None:
                raise ValueError("调用必须提供 SDK progress token")
            await context.session.send_progress_notification(
                token, progress=1, total=2, message="私有路径 /private/server/token，不得进入 Activity",
                related_request_id=context.request_id,
            )
            if call_mode == "wait":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    with receipt_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({**receipt, "cancelled": True}, ensure_ascii=False) + "\n")
                    raise
            await context.session.send_progress_notification(
                token, progress=2, total=2, related_request_id=context.request_id,
            )
        if arguments.get("result_mode") in {"success", "error"}:
            return types.CallToolResult(
                isError=arguments["result_mode"] == "error", structuredContent={"receipt": receipt},
                content=[
                    types.TextContent(type="text", text="完整协议回执"),
                    types.ImageContent(type="image", data=RESULT_IMAGE_BASE64, mimeType="image/png"),
                    types.AudioContent(type="audio", data=RESULT_AUDIO_BASE64, mimeType="audio/wav"),
                    types.ResourceLink(type="resource_link", uri=AnyUrl("https://example.invalid/report"), name="report", mimeType="text/plain"),
                    types.EmbeddedResource(type="resource", resource=types.TextResourceContents(uri=AnyUrl("https://example.invalid/note"), text="嵌入正文")),
                ],
            )
        return [types.TextContent(type="text", text=json.dumps(receipt, ensure_ascii=False))]

    return server


async def serve(definitions_path: Path, receipt_path: Path, *, page_size: int | None = None,
                notify_changes: bool = False) -> None:
    server = build_server(definitions_path, receipt_path, page_size=page_size)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options(
            notification_options=NotificationOptions(tools_changed=notify_changes),
        ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("definitions", type=Path)
    parser.add_argument("receipts", type=Path)
    parser.add_argument("--page-size", type=int)
    parser.add_argument("--notify-changes", action="store_true")
    options = parser.parse_args()
    asyncio.run(serve(options.definitions, options.receipts, page_size=options.page_size,
                      notify_changes=options.notify_changes))
