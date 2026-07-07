from __future__ import annotations

from app.agents.tool_call_recovery import (
    extract_pseudo_tool_call,
    format_recovered_tool_result,
    safe_final_text,
)
from app.services.orchestration.agent_stream_helpers import unwrap_json_string_tool_result


def test_safe_final_text_keeps_normal_reply():
    text = "后台终端可用，可以从资源视图打开。"

    assert safe_final_text(text) == text


def test_safe_final_text_keeps_normal_tool_explanation():
    text = "persistent_terminal 工具的 arguments 参数会由模型调用协议传入。"

    assert safe_final_text(text) == text


def test_safe_final_text_blocks_toolcall_marker():
    text = '<TOOLCALL>[{"name": "persistent_terminal", "arguments": {"action": "run_command"}}]'

    sanitized = safe_final_text(text)

    assert "TOOLCALL" not in sanitized
    assert "persistent_terminal" not in sanitized
    assert "系统已拦截" in sanitized


def test_safe_final_text_blocks_truncated_toolcall_tail():
    text = 'name": "persistent_terminal", "arguments": {"action": "run_command", "command": "echo test"}}]'

    sanitized = safe_final_text(text)

    assert "arguments" not in sanitized
    assert "系统已拦截" in sanitized


def test_unwrap_json_string_tool_result_keeps_exact_tool_text():
    assert unwrap_json_string_tool_result('"4568"', "4568") == "4568"


def test_unwrap_escaped_json_string_tool_result_keeps_exact_tool_text():
    assert unwrap_json_string_tool_result('\\"4568\\"', "4568") == "4568"


def test_unwrap_nested_json_string_tool_result_keeps_exact_tool_text():
    assert unwrap_json_string_tool_result('"\\"4568\\""', "4568") == "4568"


def test_unwrap_json_string_tool_result_keeps_user_quotes_when_not_tool_result():
    assert unwrap_json_string_tool_result('"4568"', "other") == '"4568"'


def test_extract_pseudo_tool_call_from_toolcall_marker():
    text = (
        '<TOOLCALL>[{"name": "persistent_terminal", '
        '"arguments": {"action": "run_command", "command": "echo test"}}]'
    )

    assert extract_pseudo_tool_call(text) == (
        "persistent_terminal",
        {"action": "run_command", "command": "echo test"},
    )


def test_extract_pseudo_tool_call_from_truncated_tail():
    text = (
        'name": "persistent_terminal", '
        '"arguments": {"action": "run_command", "command": "echo test"}}]'
    )

    assert extract_pseudo_tool_call(text) == (
        "persistent_terminal",
        {"action": "run_command", "command": "echo test"},
    )


def test_format_recovered_persistent_terminal_result_hides_raw_json():
    text = format_recovered_tool_result(
        "persistent_terminal",
        {
            "status": "completed",
            "terminal_id": "term_123",
            "command": "echo test",
            "exit_code": 0,
            "output": "test",
            "attach_url": "http://127.0.0.1:8013/?terminalId=term_123",
        },
    )

    assert "命令已在持久终端中执行完成" in text
    assert "term_123" in text
    assert "```text\ntest\n```" in text
    assert '"arguments"' not in text
