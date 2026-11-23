"""SDK 分页与参数／结果薄适配合同。"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import ToolMessage
from mcp import types
from mcp.client.session import ClientSession
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_connected_server_and_client_session

from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp.catalog_owner import McpCatalogOwner
from app.services.infrastructure.mcp.session_runtime import _McpServerSession
from app.services.infrastructure.mcp.tool_adapter import list_mcp_tools
from app.services.infrastructure.mcp.tool_definitions import PluginToolSource


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


@pytest.mark.parametrize("plugin_transport", [False, True])
async def test_sdk_adapter_uses_only_local_plugin_identity(sdk_session: MagicMock, plugin_transport: bool) -> None:
    source = PluginToolSource(gateway_id="native-gateway", package_id="proof.tools", plugin_id="tools",
        package_version="1.0.0", package_digest="a" * 64, service_id="mcp", workspace_id="workspace",
        contract_version=1, instance_id="actual-instance")
    sdk_session.list_tools.return_value = types.ListToolsResult(tools=[types.Tool(name="probe",
        inputSchema={"type": "object"}, annotations=types.ToolAnnotations.model_validate({
            "readOnlyHint": True, "plugin_tool_source": {**source.to_dict(), "instance_id": "forged-instance"},
            "product_tool_contract": ("probe", 1)}), _meta={"server-detail": "retained"})])
    target, = await list_mcp_tools(sdk_session, source=source if plugin_transport else None)
    assert target.metadata is not None
    assert target.metadata["readOnlyHint"] is True
    assert target.metadata["_meta"] == {"server-detail": "retained"}
    assert "product_tool_contract" not in target.metadata
    if plugin_transport:
        assert target.metadata["plugin_tool_source"] == source.to_dict()
    else:
        assert "plugin_tool_source" not in target.metadata


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
    assert sdk_session.call_tool.await_count == 1
    assert sdk_session.call_tool.await_args.args == ("probe", arguments)
    assert callable(sdk_session.call_tool.await_args.kwargs["progress_callback"])
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


async def test_sdk_transport_failure_is_explicit_unknown(sdk_session: MagicMock) -> None:
    sdk_session.call_tool.side_effect = ConnectionError("closed")
    target, = await list_mcp_tools(sdk_session)
    result = await target.ainvoke({"type": "tool_call", "id": "lost", "name": target.name, "args": {}})
    assert result.status == "error" and result.tool_call_id == "lost"
    assert result.artifact["mcp_outcome"]["retryable"] is False
    assert result.artifact["mcp_outcome"]["error_type"] == "ConnectionError"


@pytest.fixture
async def memory_owner(tmp_path, event_channel_service):
    """真实 SDK 双向内存协议接入原 Owner，不启动网络或替换会话方法。"""
    server = Server("business-error-contract")
    calls: list[dict[str, object]] = []
    sessions: list[ClientSession] = []
    lists: list[str] = []

    @server.list_tools()
    async def tools():
        lists.append("tools/list")
        return [types.Tool(name="probe", inputSchema={
            "type": "object", "properties": {"fail": {"type": "boolean"}},
            "required": ["fail"], "additionalProperties": False,
        })]

    @server.call_tool()
    async def call(name: str, arguments: dict[str, object]):
        assert name == "probe"
        calls.append(arguments)
        failed = arguments["fail"] is True
        return types.CallToolResult(isError=failed, structuredContent={"accepted": not failed}, content=[
            types.TextContent(type="text", text="业务拒绝" if failed else "随后成功"),
            types.ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png"),
        ])

    @asynccontextmanager
    async def connect(_config, _notify):
        async with create_connected_server_and_client_session(server) as session:
            sessions.append(session)
            yield _McpServerSession(session)

    owner = McpCatalogOwner(leases=ExternalResourceLeaseLedger(), workspace_root=tmp_path,
        event_service=event_channel_service, session_factory=connect, raw_config={"servers": {"memory": {
            "enabled": True, "transport": "streamable_http", "url": "https://memory.invalid/mcp",
        }}})
    await owner.start()
    try:
        yield owner, calls, sessions, lists
    finally:
        await owner.shutdown()


async def test_real_sdk_business_error_keeps_owner_connection_ready_and_next_call_succeeds(memory_owner) -> None:
    owner, calls, sessions, lists = memory_owner
    tool, = owner.get_tools()
    before, = owner.list_servers()
    revision = owner.catalog_revision()
    assert before.status == "ready" and before.connection_ref is not None
    first = await tool.ainvoke({"type": "tool_call", "id": "business-failed", "name": tool.name, "args": {"fail": True}})
    assert isinstance(first, ToolMessage) and first.status == "error"
    assert first.tool_call_id == "business-failed"
    assert first.content[0]["text"] == "业务拒绝"
    assert first.content[1]["type"] == "image" and first.content[1]["base64"] == "aW1hZ2U="
    assert first.artifact == {"structured_content": {"accepted": False}}
    after_error, = owner.list_servers()
    assert after_error.status == "ready" and after_error.connection_ref == before.connection_ref
    second = await tool.ainvoke({"type": "tool_call", "id": "business-success", "name": tool.name, "args": {"fail": False}})
    assert isinstance(second, ToolMessage) and second.status == "success"
    assert second.tool_call_id == "business-success" and second.content[0]["text"] == "随后成功"
    assert second.artifact == {"structured_content": {"accepted": True}}
    after_success, = owner.list_servers()
    assert after_success.status == "ready" and after_success.connection_ref == before.connection_ref
    assert owner.catalog_revision() == revision
    assert calls == [{"fail": True}, {"fail": False}]
    assert len(sessions) == 1 and lists == ["tools/list"]
