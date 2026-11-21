from __future__ import annotations

from app.services.orchestration.execution_step.retry import (
    _unwrap_matching_tool_result,
)


def test_unwrap_json_string_tool_result_keeps_exact_tool_text():
    assert _unwrap_matching_tool_result('"4568"', "4568") == "4568"


def test_unwrap_escaped_json_string_tool_result_keeps_exact_tool_text():
    assert _unwrap_matching_tool_result('\\"4568\\"', "4568") == "4568"


def test_unwrap_nested_json_string_tool_result_keeps_exact_tool_text():
    assert _unwrap_matching_tool_result('"\\"4568\\""', "4568") == "4568"


def test_unwrap_json_string_tool_result_keeps_user_quotes_when_not_tool_result():
    assert _unwrap_matching_tool_result('"4568"', "other") == '"4568"'
