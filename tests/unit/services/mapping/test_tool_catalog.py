from __future__ import annotations

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.agents.policy.tool_capability import metadata_for_tool
from app.services.infrastructure.mcp.tool_definitions import definition_from_tool
from app.services.mapping.tool_catalog import tools_to_catalog_definitions


def test_tool_catalog_uses_model_visible_schema_without_runtime_fields():
    @tool
    def runtime_aware_tool(value: str, runtime: ToolRuntime) -> str:
        """返回输入值。"""
        assert runtime.tool_call_id
        return value

    definitions = tools_to_catalog_definitions([runtime_aware_tool])

    assert len(definitions) == 1
    assert definitions[0]["name"] == "runtime_aware_tool"
    assert definitions[0]["parameters"]["properties"] == {
        "value": {"title": "Value", "type": "string"}
    }
    assert definitions[0]["parameters"]["required"] == ["value"]


def test_mcp_catalog_definition_uses_extension_group_metadata():
    @tool
    def mcp_status(value: str) -> str:
        """查询 MCP 状态。"""
        return value

    mcp_status = mcp_status.model_copy(
        update={"metadata": {"mcp_server_id": "tui-mcp"}}
    )

    @tool
    def read_file(value: str) -> str:
        """读取文件。"""
        return value

    published = definition_from_tool(mcp_status, metadata_for_tool(mcp_status, origin="mcp"))
    definitions = tools_to_catalog_definitions([read_file], extension_definitions=[published])
    mcp_definition = next(
        definition for definition in definitions if definition["name"] == "mcp_status"
    )

    assert mcp_definition["kind"] == "extension"
    assert mcp_definition["origin"] == "mcp"
    assert mcp_definition["parameters"] == published.input_schema
    assert mcp_definition["group_id"] == "mcp:tui-mcp"
    assert mcp_definition["group_name"] == "扩展工具 · MCP · tui-mcp"
