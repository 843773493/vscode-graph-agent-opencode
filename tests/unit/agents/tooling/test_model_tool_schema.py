from __future__ import annotations

from typing import Any, ClassVar

import pytest
from jsonschema import ValidationError as JsonSchemaValidationError
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import ValidationError
from pydantic.errors import PydanticInvalidForJsonSchema

from app.agents.model_tool_schema import (
    export_model_tool_json_schema,
    filter_tool_event_injected_arguments,
    get_model_tool_schema,
    validate_model_tool_arguments,
)


def test_model_tool_schema_excludes_runtime_from_all_public_operations() -> None:
    @tool
    def runtime_aware(value: str, count: int, runtime: ToolRuntime) -> str:
        """组合公开参数，运行时由后端注入。"""
        del runtime
        return f"{value}:{count}"

    with pytest.raises(PydanticInvalidForJsonSchema):
        runtime_aware.args_schema.model_json_schema()

    schema = get_model_tool_schema(runtime_aware)
    parameters = export_model_tool_json_schema(runtime_aware)
    validated = validate_model_tool_arguments(
        runtime_aware,
        {"value": "ready", "count": 2},
    )

    assert set(schema.model_fields) == {"value", "count"}
    assert set(parameters["properties"]) == {"value", "count"}
    assert parameters["required"] == ["value", "count"]
    assert validated == {"value": "ready", "count": 2}

    event_arguments = filter_tool_event_injected_arguments(
        runtime_aware,
        {
            "value": "ready",
            "count": 2,
            "runtime": object(),
        },
    )

    assert event_arguments == {"value": "ready", "count": 2}


def test_model_tool_argument_validation_uses_public_schema() -> None:
    @tool
    def count_items(count: int) -> int:
        """返回项目数量。"""
        return count

    with pytest.raises(ValidationError):
        validate_model_tool_arguments(count_items, {"count": "invalid"})

    arguments = {"count": "invalid", "foreign_state": {"status": "completed"}}
    assert filter_tool_event_injected_arguments(count_items, arguments) == arguments

    with pytest.raises(TypeError, match="actual_type=list"):
        validate_model_tool_arguments(count_items, [])  # type: ignore[arg-type]


def test_model_tool_schema_reports_tool_identity_when_public_schema_is_missing() -> (
    None
):
    class _ToolWithoutPublicSchema:
        name = "missing_public_schema"
        tool_call_schema = None

    with pytest.raises(
        TypeError,
        match=("tool_name=missing_public_schema tool_type=_ToolWithoutPublicSchema"),
    ):
        get_model_tool_schema(_ToolWithoutPublicSchema())  # type: ignore[arg-type]


def test_model_tool_schema_exports_dictionary_schema_for_protocol_tools() -> None:
    class _ToolWithDictionarySchema:
        name = "dictionary_schema"
        tool_call_schema: ClassVar[dict[str, Any]] = {"type": "object"}

    assert export_model_tool_json_schema(
        _ToolWithDictionarySchema(),  # type: ignore[arg-type]
    ) == {"type": "object"}


def test_model_tool_arguments_validate_dictionary_schema_for_protocol_tools() -> None:
    class _ToolWithDictionarySchema:
        name = "dictionary_schema"
        tool_call_schema: ClassVar[dict[str, Any]] = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    assert validate_model_tool_arguments(
        _ToolWithDictionarySchema(),  # type: ignore[arg-type]
        {"value": "ready", "ignored": "not-forwarded"},
    ) == {"value": "ready"}

    with pytest.raises(JsonSchemaValidationError):
        validate_model_tool_arguments(  # type: ignore[arg-type]
            _ToolWithDictionarySchema(),
            {"value": 123},
        )
