from __future__ import annotations

import json

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import ValidationError

from app.agents.structured_tool_call_middleware import (
    MAX_INVALID_TOOL_CALL_RETRIES,
    StructuredToolCallMiddleware,
)


class _ToolBindingFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


@pytest.fixture
def middleware() -> StructuredToolCallMiddleware:
    return StructuredToolCallMiddleware()


@pytest.fixture
def executions() -> list[str]:
    return []


def _raw_tool_call(
    arguments: object,
    *,
    call_id: str | None = "call-invalid",
    name: str | None = "read_file",
) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }


def _malformed_tool_message(
    *,
    include_parsed_call: bool = False,
    call_id: str = "call-invalid",
) -> AIMessage:
    tool_calls = []
    if include_parsed_call:
        tool_calls.append(
            {
                "name": "read_file",
                "args": {},
                "id": call_id,
                "type": "tool_call",
            }
        )
    return AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                _raw_tool_call(
                    '{"file_path": invalid}',
                    call_id=call_id,
                )
            ]
        },
        tool_calls=tool_calls,
    )


@pytest.mark.parametrize(
    "content",
    [
        '<TOOLCALL>[{"name":"persistent_terminal","arguments":{}}]',
        '```json\n{"name":"read_file","arguments":{"file_path":"a"}}\n```',
        '<tool_call>{"name":"read_file","arguments":{}}</tool_call>',
        '{"name":"read_file","arguments":"{bad}"}',
        '请调用 read_file({"file_path":"a"})，但这只是解释文字。',
    ],
    ids=["legacy-marker", "json-code-block", "xml", "bare-json", "natural-text"],
)
def test_plain_text_that_looks_like_tool_json_is_not_executed_or_rewritten(
    middleware: StructuredToolCallMiddleware,
    content: str,
):
    message = AIMessage(content=content)

    update = middleware.after_model({"messages": [message]}, None)

    assert update is None


def test_malformed_structured_arguments_return_error_and_retry_model(
    middleware: StructuredToolCallMiddleware,
):
    update = middleware.after_model(
        {"messages": [_malformed_tool_message(include_parsed_call=True)]},
        None,
    )

    assert update is not None
    assert update["jump_to"] == "model"
    assert update["_invalid_tool_call_retry_count"] == 1
    tool_message = update["messages"][0]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.status == "error"
    assert tool_message.tool_call_id == "call-invalid"
    assert "Please check your input and try again" in tool_message.content


@pytest.mark.parametrize(
    "arguments",
    [
        '{"path":"a",}',
        '{"path": unquoted}',
        '{"path":"unterminated}',
        '{"path":"bad\\q"}',
        '{"value":NaN}',
        '{"value":Infinity}',
    ],
    ids=[
        "trailing-comma",
        "unquoted-value",
        "unterminated-string",
        "invalid-escape",
        "nan",
        "infinity",
    ],
)
def test_unusual_invalid_json_is_returned_to_model_for_correction(
    middleware: StructuredToolCallMiddleware,
    arguments: str,
):
    message = AIMessage(
        content="",
        additional_kwargs={"tool_calls": [_raw_tool_call(arguments)]},
    )

    update = middleware.after_model({"messages": [message]}, None)

    assert update is not None
    assert update["jump_to"] == "model"
    assert update["messages"][0].status == "error"


@pytest.mark.parametrize(
    ("arguments", "expected_type"),
    [
        (None, "NoneType"),
        ({"file_path": "a"}, "dict"),
    ],
    ids=["missing-arguments", "provider-returned-object"],
)
def test_non_string_raw_arguments_fail_as_provider_protocol_errors(
    middleware: StructuredToolCallMiddleware,
    arguments: object,
    expected_type: str,
):
    message = AIMessage(
        content="",
        additional_kwargs={"tool_calls": [_raw_tool_call(arguments)]},
    )

    with pytest.raises(RuntimeError, match=expected_type):
        middleware.after_model({"messages": [message]}, None)


@pytest.mark.parametrize(
    "arguments",
    ["[1]", '"text"', "123", "true"],
    ids=["non-empty-array", "string", "number", "true"],
)
def test_langchain_rejects_non_object_json_before_middleware(arguments: str):
    with pytest.raises(ValidationError, match="tool_calls.0.args"):
        AIMessage(
            content="",
            additional_kwargs={"tool_calls": [_raw_tool_call(arguments)]},
        )


@pytest.mark.parametrize(
    ("arguments", "expected_type"),
    [("null", "NoneType"), ("[]", "list"), ("false", "bool")],
    ids=["null", "empty-array", "false"],
)
def test_falsy_non_objects_are_rejected_after_langchain_normalizes_them(
    middleware: StructuredToolCallMiddleware,
    arguments: str,
    expected_type: str,
):
    message = AIMessage(
        content="",
        additional_kwargs={"tool_calls": [_raw_tool_call(arguments)]},
    )
    assert message.tool_calls[0]["args"] == {}

    update = middleware.after_model({"messages": [message]}, None)

    assert update is not None
    assert expected_type in update["messages"][0].content


@pytest.mark.parametrize(
    ("raw_tool_calls", "error"),
    [
        ({"id": "call-invalid"}, "tool_calls 必须为数组"),
        (["broken"], r"tool_calls\[0\] 必须为 object"),
        ([{"id": "call-invalid", "type": "function"}], "缺少 function object"),
        (
            [{"type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
            "缺少 tool_call_id",
        ),
        (
            [{"id": "call-invalid", "type": "function", "function": {"arguments": "{}"}}],
            "缺少工具名称",
        ),
    ],
    ids=["mapping-list", "non-object-call", "no-function", "no-id", "no-name"],
)
def test_broken_structured_envelope_fails_loudly(
    middleware: StructuredToolCallMiddleware,
    raw_tool_calls: object,
    error: str,
):
    message = AIMessage(
        content="",
        additional_kwargs={"tool_calls": raw_tool_calls},
    )

    with pytest.raises(RuntimeError, match=error):
        middleware.after_model({"messages": [message]}, None)


def test_tuple_tool_call_collection_is_accepted_when_call_is_valid(
    middleware: StructuredToolCallMiddleware,
):
    message = AIMessage(
        content="",
        additional_kwargs={"tool_calls": (_raw_tool_call('{"file_path":"a"}'),)},
    )

    assert middleware.after_model({"messages": [message]}, None) is None


def test_unicode_quotes_newlines_and_backslashes_remain_exact(
    middleware: StructuredToolCallMiddleware,
):
    arguments = {
        "file_path": '目录/C:\\tmp\\雪人".txt',
        "content": "第一行\n第二行\t末尾\\",
    }
    message = AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                _raw_tool_call(json.dumps(arguments, ensure_ascii=False))
            ]
        },
    )

    assert middleware.after_model({"messages": [message]}, None) is None
    assert message.tool_calls[0]["args"] == arguments


def test_multiple_invalid_calls_each_get_one_error_message(
    middleware: StructuredToolCallMiddleware,
):
    message = AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                _raw_tool_call("{bad}", call_id="bad-1"),
                _raw_tool_call("", call_id="bad-2"),
            ]
        },
    )

    update = middleware.after_model({"messages": [message]}, None)

    assert update is not None
    assert [item.tool_call_id for item in update["messages"]] == ["bad-1", "bad-2"]


def test_duplicate_tool_call_id_fails_loudly(
    middleware: StructuredToolCallMiddleware,
):
    message = AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                _raw_tool_call("{bad}", call_id="duplicate"),
                _raw_tool_call("{still_bad}", call_id="duplicate"),
            ]
        },
    )

    with pytest.raises(RuntimeError, match="重复 tool_call_id"):
        middleware.after_model({"messages": [message]}, None)


def test_valid_parallel_call_remains_pending_while_invalid_call_gets_error(
    middleware: StructuredToolCallMiddleware,
):
    message = _malformed_tool_message()
    message.tool_calls.append(
        {
            "name": "grep",
            "args": {"pattern": "needle"},
            "id": "call-valid",
            "type": "tool_call",
        }
    )

    update = middleware.after_model({"messages": [message]}, None)

    assert update is not None
    assert "jump_to" not in update
    assert update["messages"][0].tool_call_id == "call-invalid"


def test_malformed_structured_arguments_fail_after_retry_limit(
    middleware: StructuredToolCallMiddleware,
):
    with pytest.raises(RuntimeError, match="已达到重试上限"):
        middleware.after_model(
            {
                "messages": [_malformed_tool_message()],
                "_invalid_tool_call_retry_count": MAX_INVALID_TOOL_CALL_RETRIES,
            },
            None,
        )


def test_valid_response_resets_previous_invalid_retry_count(
    middleware: StructuredToolCallMiddleware,
):
    update = middleware.after_model(
        {
            "messages": [AIMessage(content="最终回答")],
            "_invalid_tool_call_retry_count": 3,
        },
        None,
    )

    assert update == {"_invalid_tool_call_retry_count": 0}


def test_agent_returns_malformed_json_error_to_model_without_executing_tool(
    executions: list[str],
):

    @tool
    def read_file(file_path: str) -> str:
        """读取指定文件。"""
        executions.append(file_path)
        return file_path

    model = _ToolBindingFakeModel(
        responses=[
            _malformed_tool_message(),
            AIMessage(content="已收到参数错误并停止错误调用。"),
        ]
    )
    agent = create_agent(
        model,
        tools=[read_file],
        middleware=[StructuredToolCallMiddleware()],
    )

    result = agent.invoke({"messages": [HumanMessage(content="读取文件")]})

    error_message = next(
        message for message in result["messages"] if isinstance(message, ToolMessage)
    )
    assert error_message.status == "error"
    assert error_message.tool_call_id == "call-invalid"
    assert executions == []
    assert result["messages"][-1].content == "已收到参数错误并停止错误调用。"


def test_agent_treats_pseudo_tool_syntax_as_final_text(
    executions: list[str],
):
    @tool
    def read_file(file_path: str) -> str:
        """读取指定文件。"""
        executions.append(file_path)
        return file_path

    pseudo_call = (
        '<TOOLCALL>[{"name":"read_file",'
        '"arguments":{"file_path":"should-not-run"}}]'
    )
    agent = create_agent(
        _ToolBindingFakeModel(responses=[AIMessage(content=pseudo_call)]),
        tools=[read_file],
        middleware=[StructuredToolCallMiddleware()],
    )

    result = agent.invoke({"messages": [HumanMessage(content="解释示例代码")]})

    assert executions == []
    assert result["messages"][-1].content == pseudo_call


def test_agent_executes_corrected_call_after_malformed_first_attempt(
    executions: list[str],
):
    @tool
    def read_file(file_path: str) -> str:
        """读取指定文件。"""
        executions.append(file_path)
        return f"内容:{file_path}"

    model = _ToolBindingFakeModel(
        responses=[
            _malformed_tool_message(),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": "src/main.py"},
                        "id": "call-corrected",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="修正后读取成功。"),
        ]
    )
    agent = create_agent(
        model,
        tools=[read_file],
        middleware=[StructuredToolCallMiddleware()],
    )

    result = agent.invoke({"messages": [HumanMessage(content="读取文件")]})

    tool_messages = [
        message for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    assert [(message.tool_call_id, message.status) for message in tool_messages] == [
        ("call-invalid", "error"),
        ("call-corrected", "success"),
    ]
    assert executions == ["src/main.py"]
    assert result["messages"][-1].content == "修正后读取成功。"


def test_agent_executes_valid_parallel_call_while_rejecting_broken_peer(
    executions: list[str],
):
    @tool
    def grep(pattern: str) -> str:
        """查找文本。"""
        executions.append(pattern)
        return "found"

    mixed_message = AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                _raw_tool_call("{bad}", call_id="call-bad", name="grep"),
                _raw_tool_call(
                    '{"pattern":"needle"}',
                    call_id="call-good",
                    name="grep",
                ),
            ]
        },
    )
    model = _ToolBindingFakeModel(
        responses=[mixed_message, AIMessage(content="并行调用处理完成。")]
    )
    agent = create_agent(
        model,
        tools=[grep],
        middleware=[StructuredToolCallMiddleware()],
    )

    result = agent.invoke({"messages": [HumanMessage(content="查找文本")]})

    tool_messages = [
        message for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    assert {(message.tool_call_id, message.status) for message in tool_messages} == {
        ("call-bad", "error"),
        ("call-good", "success"),
    }
    assert executions == ["needle"]


def test_agent_returns_schema_error_to_model_before_retrying():
    @tool
    def read_required(file_path: str) -> str:
        """读取指定文件。"""
        return file_path

    model = _ToolBindingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_required",
                        "args": {},
                        "id": "call-schema-error",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="已根据工具错误重新生成调用。"),
        ]
    )
    agent = create_agent(model, tools=[read_required])

    result = agent.invoke({"messages": [HumanMessage(content="读取文件")]})

    tool_message = next(
        message for message in result["messages"] if isinstance(message, ToolMessage)
    )
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.status == "error"
    assert tool_message.tool_call_id == "call-schema-error"
    assert "file_path" in tool_message.content
    assert result["messages"][-1].content == "已根据工具错误重新生成调用。"


def test_agent_returns_unknown_tool_error_to_model_before_retrying():
    @tool
    def available_tool() -> str:
        """用于建立工具节点的占位工具。"""
        return "available"

    model = _ToolBindingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "tool_that_does_not_exist",
                        "args": {},
                        "id": "call-unknown",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="已停止调用不存在的工具。"),
        ]
    )
    agent = create_agent(model, tools=[available_tool])

    result = agent.invoke({"messages": [HumanMessage(content="调用工具")]})

    tool_message = next(
        message for message in result["messages"] if isinstance(message, ToolMessage)
    )
    assert tool_message.status == "error"
    assert tool_message.tool_call_id == "call-unknown"
    assert "not a valid tool" in tool_message.content


def test_agent_does_not_hide_runtime_tool_exception():
    @tool
    def explode(value: str) -> str:
        """始终抛出运行时错误。"""
        raise RuntimeError(f"boom:{value}")

    model = _ToolBindingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "explode",
                        "args": {"value": "edge"},
                        "id": "call-explode",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    agent = create_agent(model, tools=[explode])

    with pytest.raises(RuntimeError, match="boom:edge"):
        agent.invoke({"messages": [HumanMessage(content="触发错误")]})
