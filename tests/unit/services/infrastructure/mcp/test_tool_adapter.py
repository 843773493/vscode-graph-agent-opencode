"""SDK 分页与参数／结果薄适配合同。"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import ToolMessage
from mcp import types
from mcp.client.session import ClientSession

from app.services.infrastructure.mcp.tool_adapter import list_mcp_tools


@pytest.fixture
def sdk_session() -> MagicMock:
    session = MagicMock(spec=ClientSession)
    session.list_tools = AsyncMock(return_value=types.ListToolsResult(tools=[
        types.Tool(name="probe", inputSchema={"type": "object"}),
    ]))
    session.call_tool = AsyncMock(return_value=types.CallToolResult(
        content=[types.TextContent(type="text", text="ready")], structuredContent={"value": 2},
    ))
    return session


async def test_sdk_adapter_reads_every_page(sdk_session: MagicMock) -> None:
    sdk_session.list_tools.side_effect = [
        types.ListToolsResult(tools=[types.Tool(name="one", inputSchema={"type": "object"})], nextCursor="next"),
        types.ListToolsResult(tools=[types.Tool(name="two", inputSchema={"type": "object"})]),
    ]
    tools = await list_mcp_tools(sdk_session)
    assert [tool.name for tool in tools] == ["one", "two"]
    assert [call.kwargs for call in sdk_session.list_tools.await_args_list] == [{"cursor": None}, {"cursor": "next"}]


async def test_sdk_adapter_rejects_repeated_cursor(sdk_session: MagicMock) -> None:
    sdk_session.list_tools.return_value = types.ListToolsResult(tools=[], nextCursor="same")
    with pytest.raises(ValueError, match="重复分页"):
        await list_mcp_tools(sdk_session)
    assert sdk_session.list_tools.await_count == 2


async def test_sdk_adapter_rejects_invalid_second_page(sdk_session: MagicMock) -> None:
    sdk_session.list_tools.side_effect = [
        types.ListToolsResult(tools=[types.Tool(name="one", inputSchema={"type": "object"})], nextCursor="next"),
        types.ListToolsResult(tools=[types.Tool(name="two", inputSchema={"$ref": "https://offline.invalid/schema"})]),
    ]
    with pytest.raises(ValueError, match="只支持本地引用"):
        await list_mcp_tools(sdk_session)


async def test_sdk_adapter_preserves_arguments_and_structured_result(sdk_session: MagicMock) -> None:
    target, = await list_mcp_tools(sdk_session)
    arguments = {
        "runtime": "public", "callbacks": "public", "config": {"public": True},
        "run_manager": "public", "tool_call_id": "public", "tool_name": "public",
        "arguments": {"value": 1},
    }
    result = await target.ainvoke({"type": "tool_call", "id": "probe-id", "name": target.name, "args": arguments})
    sdk_session.call_tool.assert_awaited_once_with("probe", arguments)
    assert isinstance(result, ToolMessage)
    assert result.status == "success" and result.tool_call_id == "probe-id"
    assert result.artifact == {"structured_content": {"value": 2}}
    assert isinstance(result.content, list) and isinstance(result.content[0], dict)
    assert result.content[0]["text"] == "ready"


@pytest.mark.parametrize("is_error", [False, True])
async def test_sdk_adapter_preserves_protocol_error_and_media(sdk_session: MagicMock, is_error: bool) -> None:
    sdk_session.call_tool.return_value = types.CallToolResult(isError=is_error, structuredContent={"failure": "真实结构化正文"}, content=[
        types.TextContent(type="text", text="真实错误"),
        types.ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png"),
        types.AudioContent(type="audio", data="YXVkaW8=", mimeType="audio/wav"),
        types.ResourceLink(type="resource_link", uri="https://example.invalid/report", name="report", mimeType="text/plain"),
    ])
    target, = await list_mcp_tools(sdk_session)
    result = await target.ainvoke({"type": "tool_call", "id": "probe-error", "name": target.name, "args": {}})
    assert isinstance(result, ToolMessage)
    assert result.status == ("error" if is_error else "success") and result.tool_call_id == "probe-error"
    assert result.artifact == {"structured_content": {"failure": "真实结构化正文"}}
    assert isinstance(result.content, list) and all(isinstance(block, dict) for block in result.content)
    assert isinstance(result.content[0], dict) and isinstance(result.content[1], dict)
    assert result.content[0]["text"] == "真实错误"
    assert result.content[1]["type"] == "image"
    assert result.content[1]["base64"] == "aW1hZ2U="
    assert result.content[2]["type"] == "audio" and result.content[2]["base64"] == "YXVkaW8="
    assert result.content[3]["type"] == "file" and result.content[3]["url"] == "https://example.invalid/report"


async def test_sdk_transport_failure_propagates(sdk_session: MagicMock) -> None:
    sdk_session.call_tool.side_effect = ConnectionError("closed")
    target, = await list_mcp_tools(sdk_session)
    with pytest.raises(ConnectionError, match="closed"):
        await target.ainvoke({})
