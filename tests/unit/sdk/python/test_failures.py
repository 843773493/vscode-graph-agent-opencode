"""公开错误与诊断预算在提取阶段生效。"""

import json

import pytest
from boxteam_plugin_sdk.failures import (
    DIAGNOSTIC_BYTES,
    MAX_DESCENDANTS,
    MESSAGE_BYTES,
    PUBLIC_ERROR_BYTES,
    failure_message,
    format_failure_diagnostic,
    public_error,
)
from pydantic import TypeAdapter, ValidationError


@pytest.fixture
def validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(dict[str, int]).validate_python({"field.with.dot": "private-input"})
    return caught.value


def test_validation_keeps_path_code_and_reason_without_input(validation_error: ValidationError) -> None:
    result = public_error(validation_error)
    issue, = result["details"]["issues"]
    assert issue["path"] == ["field.with.dot"]
    assert issue["code"] == "int_parsing"
    assert "valid integer" in issue["message"]
    assert "private-input" not in json.dumps(result)
    assert "private-input" not in format_failure_diagnostic(validation_error)


def test_unknown_empty_and_credentials_are_safe_before_clipping() -> None:
    secret = "operation-private-value"
    error = RuntimeError("真实原因 " + "x" * 8100 + secret + " https://user:password@host/?token=secret")
    error.__cause__ = ValueError("Authorization: Bearer " + secret)
    result = public_error(error, code="original-control-code", secrets=(secret,))
    diagnostic = format_failure_diagnostic(error, secrets=(secret,))
    serialized = json.dumps(result, ensure_ascii=False).encode()
    assert result["code"] == "original-control-code"
    assert result["message"].startswith("真实原因")
    assert len(serialized) <= PUBLIC_ERROR_BYTES
    assert len(result["message"].encode()) <= MESSAGE_BYTES
    assert len(diagnostic.encode()) <= DIAGNOSTIC_BYTES
    assert secret not in diagnostic and secret not in serialized.decode()
    assert "未提供错误文本" == failure_message(RuntimeError())
    assert failure_message(RuntimeError("正文"), max_bytes=-1) == ""


def test_group_stops_formatting_unselected_children() -> None:
    visited: list[int] = []

    class ObservedError(Exception):
        def __str__(self) -> str:
            visited.append(1)
            return "次生原因"

    error = ExceptionGroup("主失败", [ObservedError() for _ in range(1000)])
    result = public_error(error)
    assert len(visited) <= MAX_DESCENDANTS
    assert result["details"]["truncated"] is True
    visited.clear()
    diagnostic = format_failure_diagnostic(error)
    assert len(visited) <= MAX_DESCENDANTS
    assert "主失败" in diagnostic and "已截断" in diagnostic


def test_diagnostic_stops_at_frame_and_byte_limits() -> None:
    def throw_at_leaf() -> None:
        raise RuntimeError("真实原因")

    def fail(depth: int) -> None:
        if depth:
            fail(depth - 1)
        throw_at_leaf()

    with pytest.raises(RuntimeError) as caught:
        fail(100)
    diagnostic = format_failure_diagnostic(caught.value, max_frames=3, max_bytes=1024)
    assert diagnostic.count('  File "') == 3
    assert "in throw_at_leaf" in diagnostic
    assert "已截断" in diagnostic and len(diagnostic.encode()) <= 1024


def test_deep_cyclic_causes_are_bounded() -> None:
    error = RuntimeError("主失败")
    current = error
    for _ in range(100):
        current.__cause__ = RuntimeError("原因")
        current = current.__cause__
    current.__cause__ = error
    result = public_error(error)
    depth = 0
    while "cause" in result["details"]:
        depth += 1
        result = result["details"]["cause"]
    assert depth <= 4 and result["details"]["truncated"] is True


def test_diagnostic_notes_preserve_request_and_cleanup_with_shared_budget() -> None:
    error = RuntimeError("主失败")
    error.add_note("request_id=original-request path=/api/v1/workspace response_started=True")
    error.add_note("清理失败 password=private-value")
    error.add_note("x" * 100000)
    diagnostic = format_failure_diagnostic(error, max_bytes=1024)
    assert "request_id=original-request" in diagnostic and "response_started=True" in diagnostic
    assert "清理失败" in diagnostic and "private-value" not in diagnostic
    assert len(diagnostic.encode()) <= 1024 and "已截断" in diagnostic


def test_extra_fields_are_structural_coordinates_not_arbitrary_input_keys() -> None:
    from pydantic import BaseModel, ConfigDict

    class DeclaredModel(BaseModel):
        model_config = ConfigDict(extra="forbid")
        count: int

    with pytest.raises(ValidationError) as caught:
        DeclaredModel.model_validate({"count": "bad-input", "private-field-name": "private-input"})
    result = public_error(caught.value)
    assert [issue["path"] for issue in result["details"]["issues"]] == [["count"], ["<extra>"]]
    assert "private-field-name" not in json.dumps(result)
    assert "private-input" not in format_failure_diagnostic(caught.value)


def test_json_schema_enum_never_formats_input_or_full_schema() -> None:
    from jsonschema import Draft202012Validator

    schema = {"type": "object", "properties": {"api_key": {"enum": ["private-schema-value"]}}}
    error = next(Draft202012Validator(schema).iter_errors({"api_key": "private-input"}))
    assert "private-input" in error.message
    result = public_error(error)
    issue, = result["details"]["issues"]
    assert issue["path"] == ["api_key"] and issue["code"] == "enum"
    assert "enum" in issue["message"]
    assert "private-input" not in json.dumps(result) and "private-schema-value" not in json.dumps(result)
    assert "private-input" not in format_failure_diagnostic(error)


def test_json_escaping_of_code_and_stage_cannot_exhaust_message_reduction() -> None:
    result = public_error(RuntimeError("主失败"), code="\x00" * 5000, stage="\x01" * 5000, max_bytes=1024)
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 1024
    assert result["message"] == "主失败" and result["details"]["truncated"] is True
