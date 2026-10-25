from __future__ import annotations

import pytest
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic.errors import PydanticInvalidForJsonSchema

from app.tool_testing.definitions import get_model_tool_parameters


def test_model_tool_parameters_reports_missing_public_schema() -> None:
    class _ToolWithoutPublicSchema:
        name = "missing_schema"
        tool_call_schema = None

    with pytest.raises(
        TypeError,
        match=("tool_name=missing_schema tool_type=_ToolWithoutPublicSchema"),
    ):
        get_model_tool_parameters(_ToolWithoutPublicSchema())  # type: ignore[arg-type]


def test_model_tool_parameters_excludes_tool_runtime() -> None:
    @tool
    def runtime_tool(value: str, runtime: ToolRuntime) -> str:
        """返回公开值，运行时对象只由 LangGraph 注入。"""
        del runtime
        return value

    with pytest.raises(PydanticInvalidForJsonSchema):
        runtime_tool.args_schema.model_json_schema()

    parameters = get_model_tool_parameters(runtime_tool)

    assert parameters["properties"] == {
        "value": {"title": "Value", "type": "string"},
    }
    assert parameters["required"] == ["value"]
