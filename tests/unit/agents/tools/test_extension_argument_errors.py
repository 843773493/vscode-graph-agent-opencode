"""固定信封校验失败保留参数位置与规则，原输入只留诊断。"""

from __future__ import annotations

import json

import pytest
from jsonschema import ValidationError as JsonSchemaValidationError
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, ValidationError

from app.agents.tools.custom_invocation import (
    create_extension_tool_invoker_tool,
    seal_extension_catalog_binding_from_tools,
)
from app.core import failures


class _CountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int


class _ValuesInput(BaseModel):
    values: dict[str, int]


@pytest.fixture(params=["pydantic", "json-schema"])
def argument_tool(request: pytest.FixtureRequest):
    calls: list[int] = []

    def count_values(count: int) -> str:
        calls.append(count)
        return f"count={count}"

    schema = _CountInput if request.param == "pydantic" else {
        "type": "object", "properties": {"count": {"type": "integer"}},
        "required": ["count"], "additionalProperties": False,
    }
    target = StructuredTool.from_function(count_values, name="count_values", description="计数", args_schema=schema)
    envelope = create_extension_tool_invoker_tool(
        [target], catalog_binding_resolver=seal_extension_catalog_binding_from_tools([target]),
    )
    return envelope, calls


@pytest.fixture(params=[
    "/private/extensions/arguments.json", r"C:\private\extensions\arguments.json",
    r"\\host\private\extensions\arguments.json",
])
def native_path(request: pytest.FixtureRequest) -> str:
    return request.param


async def test_argument_errors_keep_pairing_diagnostics_and_allow_corrected_call(
    argument_tool, native_path: str, caplog: pytest.LogCaptureFixture,
) -> None:
    envelope, calls = argument_tool
    arguments = {"count": native_path}
    result = await envelope.ainvoke({
        "type": "tool_call", "id": "invalid-arguments", "name": envelope.name,
        "args": {"tool_name": "count_values", "arguments": arguments},
    })
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert (result.name, result.tool_call_id) == (envelope.name, "invalid-arguments")
    assert isinstance(result.content, str)
    assert "private" not in result.content and "arguments.json" not in result.content
    assert all(value in result.content for value in ("count_values", "参数校验失败", "count"))
    assert calls == [] and arguments == {"count": native_path}
    errors = [record.exc_info[1] for record in caplog.records if record.exc_info]
    assert len(errors) == 1
    error, = errors
    assert isinstance(error, (ValidationError, JsonSchemaValidationError))
    assert "private" in str(error)
    if isinstance(error, ValidationError):
        assert error.errors()[0]["input"] == native_path
        assert "int_parsing" in result.content
    else:
        assert error.instance == native_path and error.validator == "type"
        assert "type" in result.content
    assert "stage=extension-tool-arguments" in result.content

    corrected = await envelope.ainvoke({
        "type": "tool_call", "id": "corrected-arguments", "name": envelope.name,
        "args": {"tool_name": "count_values", "arguments": {"count": 2}},
    })
    assert isinstance(corrected, ToolMessage) and corrected.status == "success"
    assert (corrected.name, corrected.tool_call_id) == (envelope.name, "corrected-arguments")
    assert corrected.content == "count=2" and calls == [2]
    assert json.dumps(arguments) not in result.content


async def test_argument_error_survives_diagnostic_sink_failure(
    argument_tool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope, calls = argument_tool
    errors: list[BaseException] = []

    def fail_log(_message: str, *_args: object, exc_info: BaseException) -> None:
        errors.append(exc_info)
        raise OSError("/private/diagnostics/output.log")

    monkeypatch.setattr(failures.logger, "error", fail_log)
    result = await envelope.ainvoke({
        "type": "tool_call", "id": "sink-failure", "name": envelope.name,
        "args": {"tool_name": "count_values", "arguments": {"count": "/private/input.json"}},
    })
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert (result.name, result.tool_call_id) == (envelope.name, "sink-failure")
    assert isinstance(result.content, str) and "private" not in result.content
    assert "diagnostic_sink_error=OSError" in result.content
    assert calls == [] and len(errors) == 1
    assert "/private/diagnostics/output.log" in "".join(errors[0].__notes__)


@pytest.fixture(params=["pydantic", "json-schema"])
def mapping_argument_tool(request: pytest.FixtureRequest):
    calls: list[dict[str, int]] = []

    def count_values(values: dict[str, int]) -> str:
        calls.append(values)
        return str(sum(values.values()))

    schema = _ValuesInput if request.param == "pydantic" else {
        "type": "object", "properties": {"values": {
            "type": "object", "additionalProperties": {"type": "integer"},
        }}, "required": ["values"],
    }
    target = StructuredTool.from_function(count_values, name="count_values", description="计数", args_schema=schema)
    envelope = create_extension_tool_invoker_tool(
        [target], catalog_binding_resolver=seal_extension_catalog_binding_from_tools([target]),
    )
    return envelope, calls


async def test_argument_error_coordinates_do_not_copy_dynamic_mapping_keys(
    mapping_argument_tool, native_path: str, caplog: pytest.LogCaptureFixture,
) -> None:
    envelope, calls = mapping_argument_tool
    result = await envelope.ainvoke({
        "type": "tool_call", "id": "mapping-key", "name": envelope.name,
        "args": {"tool_name": "count_values", "arguments": {"values": {native_path: "invalid"}}},
    })
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert (result.name, result.tool_call_id) == (envelope.name, "mapping-key")
    assert isinstance(result.content, str) and "private" not in result.content
    issues = json.loads(result.content.split("issues=", 1)[1])
    assert issues in ([{"parameter": "values", "depth": 2, "rule": "int_parsing"}],
                      [{"parameter": "values", "depth": 2, "rule": "type"}])
    assert calls == []
    errors = [record.exc_info[1] for record in caplog.records if record.exc_info]
    assert len(errors) == 1 and "private" in str(errors[0])


async def test_argument_errors_do_not_copy_native_paths_used_as_unknown_top_level_keys(
    argument_tool, native_path: str, caplog: pytest.LogCaptureFixture,
) -> None:
    envelope, calls = argument_tool
    result = await envelope.ainvoke({
        "type": "tool_call", "id": "unknown-key", "name": envelope.name,
        "args": {"tool_name": "count_values", "arguments": {"count": 2, native_path: "invalid"}},
    })
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert (result.name, result.tool_call_id) == (envelope.name, "unknown-key")
    assert isinstance(result.content, str) and "private" not in result.content
    issues = json.loads(result.content.split("issues=", 1)[1])
    assert all(issue["parameter"] is None for issue in issues)
    assert calls == []
    errors = [record.exc_info[1] for record in caplog.records if record.exc_info]
    assert len(errors) == 1 and "private" in str(errors[0])
