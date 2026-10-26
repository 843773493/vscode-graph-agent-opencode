from __future__ import annotations

from typing import Any, ClassVar

import pytest
from jsonschema import ValidationError as JsonSchemaValidationError
from langchain_core.tools import StructuredTool, tool
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.errors import PydanticInvalidForJsonSchema

from app.agents.model_tool_schema import (
    export_model_tool_json_schema,
    filter_tool_event_injected_arguments,
    get_model_tool_schema,
    model_tool_schema_validator,
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
        {"value": "ready", "extra": "forwarded"},
    ) == {"value": "ready", "extra": "forwarded"}

    with pytest.raises(JsonSchemaValidationError):
        validate_model_tool_arguments(  # type: ignore[arg-type]
            _ToolWithDictionarySchema(),
            {"value": 123},
        )


def test_declared_draft_is_used_instead_of_default() -> None:
    validator = model_tool_schema_validator({
        "$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
        "dependencies": {"flag": ["other"]},
    })
    with pytest.raises(JsonSchemaValidationError, match="dependency"):
        validator.validate({"flag": True})
    validator.validate({"flag": True, "other": 1})


@pytest.mark.parametrize("schema, message", [
    ({"$schema": "https://unsupported.invalid/schema"}, "方言不受支持"),
    ({"$ref": "https://offline.invalid/schema"}, "只支持本地引用"),
    ({"$ref": "#/$defs/missing"}, "本地引用无法解析"),
])
def test_unsupported_contract_is_rejected_before_execution(schema, message) -> None:
    with pytest.raises(ValueError, match=message):
        model_tool_schema_validator(schema)


def test_schema_export_isolated_from_dictionary_owner() -> None:
    schema = {"type": "object", "$defs": {"value": {"enum": ["a", "b"]}},
              "properties": {"value": {"$ref": "#/$defs/value"}}}
    target = StructuredTool(name="dictionary", description="字典契约", args_schema=schema,
                            func=lambda **arguments: arguments)
    exported = export_model_tool_json_schema(target)
    exported["$defs"]["value"]["enum"].append("mutated")
    assert schema["$defs"]["value"]["enum"] == ["a", "b"]


@pytest.mark.parametrize("extra", ["forbid", "allow", "ignore"])
def test_local_model_keeps_defaults_conversions_and_extra_policy(extra) -> None:
    class LocalInput(BaseModel):
        model_config = ConfigDict(extra=extra)
        count: int
        label: str = "default"

    target = StructuredTool(name="local", description="本地模型", args_schema=LocalInput,
                            func=lambda **arguments: arguments)
    assert validate_model_tool_arguments(target, {"count": "2"}) == {"count": 2, "label": "default"}
    exported = export_model_tool_json_schema(target)
    if extra == "forbid":
        assert exported["additionalProperties"] is False
        with pytest.raises(ValidationError, match="extra_forbidden"):
            validate_model_tool_arguments(target, {"count": 2, "extra": 3})
    else:
        result = validate_model_tool_arguments(target, {"count": "2", "extra": 3})
        assert result == {"count": 2, "label": "default", **({"extra": 3} if extra == "allow" else {})}


def test_local_allow_does_not_grant_access_to_injected_arguments() -> None:
    class LocalInput(BaseModel):
        model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)
        value: str
        runtime: ToolRuntime

    @tool(args_schema=LocalInput)
    def local(value: str, runtime: ToolRuntime, **extra) -> str:
        """后端运行时不属于公开的可扩展字段。"""
        del runtime, extra
        return value

    assert validate_model_tool_arguments(local, {"value": "ok", "extra": 1}) == {"value": "ok", "extra": 1}
    with pytest.raises(ValueError, match="后端注入参数不得由模型提供"):
        validate_model_tool_arguments(local, {"value": "ok", "runtime": {"privilege": "forged"}})
