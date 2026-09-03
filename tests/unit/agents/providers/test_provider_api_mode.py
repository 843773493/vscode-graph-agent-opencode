from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    message_chunk_to_message,
)

from app.agents.agent_factory import build_model_from_provider
from app.agents.providers.litellm_chat import (
    BoxteamLiteLLMChatModel,
    _StreamPartState,
)
from app.agents.providers.openai_responses import (
    BoxteamOpenAIResponsesModel,
)
from app.agents.upstream_request_trace import (
    UpstreamRequestTraceCallback,
    begin_upstream_capture,
    end_upstream_capture,
    record_upstream_response,
)
from app.services.orchestration.agent_stream_helpers import (
    is_tracked_chat_model_event,
)


def _provider(api_mode: str) -> dict[str, object]:
    if api_mode == "responses":
        structured_api_mode: dict[str, object] = {
            "protocol": "responses",
            "model_info": {
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_vision": True,
            },
            "supports_reasoning": {
                "reasoning_items": {"summary": True, "encrypted_content": True}
            },
            "request_features": {"prompt_cache_key": True},
            "replay_policy": {"encrypted_content": "same_source"},
        }
    else:
        structured_api_mode = {
            "protocol": "chat_completions",
            "model_info": {
                "supports_function_calling": True,
                "supports_reasoning": True,
            },
            "supports_reasoning": {"reasoning_content": True},
        }
    return {
        "id": "provider-test",
        "endpoint": "https://example.com/v1",
        "model": "test-model",
        "api_key": "test-key",
        "custom_llm_provider": "openai",
        "api_mode": structured_api_mode,
    }


def test_chat_completions_mode_uses_litellm_chat_model():
    model = build_model_from_provider(_provider("chat_completions"), {})
    assert isinstance(model, BoxteamLiteLLMChatModel)


def test_litellm_output_projection_preserves_reasoning_and_provider_summary():
    normalized = BoxteamLiteLLMChatModel.normalize_output_content(
        [
            {"type": "reasoning", "reasoning": "模型推理"},
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "安全摘要"}],
            },
        ]
    )

    assert normalized == [
        {"type": "reasoning", "reasoning": "模型推理"},
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "安全摘要"}],
        },
    ]


def test_litellm_output_projection_preserves_redacted_reasoning_payload_marker():
    normalized = BoxteamLiteLLMChatModel.normalize_output_content(
        [
            {
                "type": "redacted_thinking",
                "encrypted_content": "provider-secret",
            }
        ]
    )

    assert normalized == [
        {
            "type": "redacted_thinking",
            "encrypted_content": "provider-secret",
        }
    ]


def test_litellm_unified_reasoning_fields_use_ordered_carrier_blocks():
    model = BoxteamLiteLLMChatModel(
        model="openai/test-model",
        api_key="test-key",
        api_base="https://example.com/v1",
    )
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "最终回答",
                    "reasoning_content": "兼容推理",
                    "thinking_blocks": [
                        {
                            "type": "thinking",
                            "thinking": "结构化推理",
                            "signature": "sig-1",
                        },
                        {"type": "redacted_thinking", "data": "sealed-1"},
                    ],
                    "reasoning_items": [
                        {
                            "type": "reasoning",
                            "id": "rs-1",
                            "encrypted_content": "encrypted-1",
                            "summary": [
                                {"type": "summary_text", "text": "安全摘要"}
                            ],
                        }
                    ],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }

    message = model._create_chat_result(response).generations[0].message

    assert isinstance(message.content, list)
    assert message.content == [
        {"type": "reasoning_content", "reasoning_content": "兼容推理"},
        {
            "type": "thinking",
            "thinking": "结构化推理",
            "signature": "sig-1",
        },
        {"type": "redacted_thinking", "data": "sealed-1"},
        {
            "type": "reasoning_items",
            "reasoning_items": [
                {
                    "type": "reasoning",
                    "id": "rs-1",
                    "encrypted_content": "encrypted-1",
                    "summary": [{"type": "summary_text", "text": "安全摘要"}],
                }
            ],
        },
        {"type": "text", "text": "最终回答"},
    ]
    assert all(
        block.get("type") != "litellm_payload"
        for block in message.content
        if isinstance(block, dict)
    )
    assert "reasoning_content" not in message.additional_kwargs
    assert "thinking_blocks" not in message.additional_kwargs
    assert "reasoning_items" not in message.additional_kwargs


def test_chat_completions_mode_forwards_stable_prompt_cache_key():
    provider = _provider("chat_completions")
    api_mode = provider["api_mode"]
    assert isinstance(api_mode, dict)
    api_mode["request_features"] = {"prompt_cache_key": True}
    model = build_model_from_provider(
        provider,
        {},
        prompt_cache_key="session-chat-cache",
    )

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model._client_params["extra_body"] == {
        "prompt_cache_key": "session-chat-cache"
    }


def test_chat_completions_mode_omits_cache_key_without_capability():
    model = build_model_from_provider(
        _provider("chat_completions"),
        {},
        prompt_cache_key="session-chat-cache",
    )

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert "extra_body" not in model._client_params


def test_chat_completions_reasoning_replay_capability_configures_model():
    provider = _provider("chat_completions")

    model = build_model_from_provider(provider, {})

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model.reasoning_content_replay is True


def test_chat_completions_thinking_blocks_configures_model_projection():
    provider = _provider("chat_completions")
    provider["api_mode"] = {
        "protocol": "chat_completions",
        "model_info": {"supports_function_calling": True, "supports_reasoning": True},
        "supports_reasoning": {"thinking_blocks": True},
    }

    model = build_model_from_provider(provider, {})

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model.reasoning_content_replay is False
    assert model.thinking_blocks_replay is True


def test_anthropic_messages_mode_uses_litellm_chat_model():
    provider = {
        "id": "anthropic-test",
        "endpoint": "https://example.com/anthropic",
        "model": "claude-test",
        "api_key": "test-key",
        "custom_llm_provider": "anthropic",
        "api_mode": {
            "protocol": "anthropic_messages",
            "model_info": {"supports_reasoning": True},
            "supports_reasoning": {
                "thinking_blocks": {
                    "thinking": True,
                    "redacted_thinking": True,
                }
            },
        },
    }

    model = build_model_from_provider(provider, {})

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model.custom_llm_provider == "anthropic"
    assert model.thinking_blocks_replay is True


def test_anthropic_history_projects_direct_content_to_thinking_blocks():
    from langchain_core.messages import AIMessage

    from app.agents.providers.litellm_content import build_ai_message_content

    model = BoxteamLiteLLMChatModel(
        model="claude-test",
        api_key="test-key",
        api_base="https://example.com/anthropic",
        custom_llm_provider="anthropic",
        provider_id="anthropic-test",
        thinking_blocks_replay=True,
    )
    message = AIMessage(
        content=build_ai_message_content(
            "最终回答",
            source_provider="anthropic-test",
            thinking_blocks=[
                {"type": "thinking", "thinking": "分析"},
                {"type": "redacted_thinking", "data": "sealed"},
            ],
        ),
        response_metadata={"provider_id": "anthropic-test"},
    )

    projected = model._convert_messages_to_dicts([message])[0]

    assert projected["content"] == [{"type": "text", "text": "最终回答"}]
    assert projected["thinking_blocks"] == [
        {"type": "thinking", "thinking": "分析"},
    ]


def test_anthropic_history_drops_reasoning_for_unsupported_target():
    from langchain_core.messages import AIMessage

    model = BoxteamLiteLLMChatModel(
        model="claude-test",
        api_key="test-key",
        api_base="https://example.com/anthropic",
        custom_llm_provider="anthropic",
        provider_id="target-provider",
        thinking_blocks_replay=False,
    )
    message = AIMessage(
        content=[
            {"type": "reasoning_content", "reasoning_content": "私有推理"},
            {
                "type": "reasoning_items",
                "reasoning_items": [
                    {"type": "reasoning", "content": [{"type": "text", "text": "内部项"}]},
                ],
            },
            {"type": "thinking", "thinking": "思考"},
            {"type": "redacted_thinking", "data": "sealed"},
            {"type": "text", "text": "可见回答"},
        ],
        tool_calls=[
            {"id": "call-1", "name": "read_file", "args": {"path": "a.txt"}},
        ],
        response_metadata={"provider_id": "source-provider"},
    )

    projected = model._convert_messages_to_dicts([message])[0]

    assert projected["content"] == [{"type": "text", "text": "可见回答"}]
    assert projected["tool_calls"] == [
        {
            "type": "function",
            "id": "call-1",
            "function": {
                "name": "read_file",
                "arguments": '{"path": "a.txt"}',
            },
        }
    ]


def test_responses_mode_uses_encrypted_reasoning_and_stable_cache_key():
    model = build_model_from_provider(
        _provider("responses"),
        {},
        prompt_cache_key="session-123",
    )
    assert isinstance(model, BoxteamOpenAIResponsesModel)
    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert is_tracked_chat_model_event(type(model).__name__)
    assert model.responses_store is False
    assert model.responses_include == ["reasoning.encrypted_content"]
    assert model._client_params["prompt_cache_key"] == "session-123"


def test_chatgpt_oauth_responses_uses_stable_litellm_session_id(monkeypatch):
    monkeypatch.setattr(
        "app.runtime.chatgpt_auth.configure_litellm_chatgpt_auth_directory",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.runtime.chatgpt_auth.ensure_chatgpt_oauth_ready",
        lambda _token_dir: None,
    )
    monkeypatch.setattr(
        "app.runtime.chatgpt_auth.ensure_litellm_chatgpt_model_capabilities",
        lambda _: False,
    )
    provider = {
        "id": "backup_4",
        "endpoint": "https://chatgpt.com/backend-api/codex",
        "model": "gpt-5.6-luna",
        "custom_llm_provider": "chatgpt",
        "api_mode": _provider("responses")["api_mode"],
        "auth": {"type": "oauth", "method": "chatgpt"},
    }

    model = build_model_from_provider(
        provider,
        {},
        prompt_cache_key="ses_chatgpt_cache_affinity",
    )

    assert isinstance(model, BoxteamOpenAIResponsesModel)
    assert model._client_params["custom_llm_provider"] == "chatgpt"
    assert model._client_params["litellm_session_id"] == "ses_chatgpt_cache_affinity"
    assert "prompt_cache_key" not in model._client_params

    payload = model._responses_payload(
        [SystemMessage(content="项目系统指令"), HumanMessage(content="用户问题")],
        None,
        {},
    )
    assert payload["instructions"] == "项目系统指令"
    assert [item.get("role") for item in payload["input"]] == ["user"]
    assert payload["litellm_session_id"] == "ses_chatgpt_cache_affinity"


def test_chatgpt_provider_rejects_missing_oauth_config():
    provider = _provider("responses")
    provider["custom_llm_provider"] = "chatgpt"

    with pytest.raises(ValueError, match="auth.type='oauth'"):
        build_model_from_provider(provider, {})


def test_responses_history_replays_encrypted_reasoning_without_server_id():
    from langchain_core.messages import AIMessage

    model = build_model_from_provider(_provider("responses"), {})
    message = AIMessage(
        content=[
            {
                "type": "reasoning",
                "id": "rs_server",
                "status": "completed",
                "content": [{"type": "reasoning_text", "text": "内部推理"}],
                "encrypted_content": "encrypted-reasoning",
                "summary": [],
            }
        ],
        response_metadata={"provider_id": "provider-test"},
    )
    payload = model._responses_payload([message], None, {})
    assert payload["input"] == [
        {
            "type": "reasoning",
            "encrypted_content": "encrypted-reasoning",
            "summary": [],
        }
    ]


def test_responses_attachment_history_drops_reasoning_output_content():
    from langchain_core.messages import AIMessage, HumanMessage

    model = build_model_from_provider(_provider("responses"), {})
    payload = model._responses_payload(
        [
            HumanMessage(content="先处理上一条请求"),
            AIMessage(
                content=[
                    {
                        "type": "reasoning",
                        "content": [
                            {"type": "reasoning_text", "text": "内部推理"}
                        ],
                        "encrypted_content": "encrypted-reasoning",
                        "summary": [
                            {"type": "summary_text", "text": "已完成上一条请求"}
                        ],
                    },
                    {"type": "text", "text": "上一条请求已完成"},
                ],
                response_metadata={"provider_id": "provider-test"},
            ),
            HumanMessage(
                content=[
                    {"type": "text", "text": "请继续并查看附件"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,eA=="},
                    },
                ]
            ),
        ],
        None,
        {},
    )

    reasoning = next(item for item in payload["input"] if item.get("type") == "reasoning")
    assert "content" not in reasoning
    assert reasoning["encrypted_content"] == "encrypted-reasoning"
    assert any(
        item.get("type") == "message"
        and any(
            block.get("type") == "input_image"
            for block in item.get("content", [])
            if isinstance(block, dict)
        )
        for item in payload["input"]
        if isinstance(item, dict)
    )


def test_responses_payload_final_boundary_drops_reintroduced_reasoning_content(
    monkeypatch,
):
    from langchain_core.messages import AIMessage, HumanMessage

    model = build_model_from_provider(_provider("responses"), {})
    legacy_history = [
        AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "content": [
                        {"type": "reasoning_text", "text": "旧推理"}
                    ],
                    "summary": [],
                }
            ]
        ),
        HumanMessage(content="继续执行只读检查"),
    ]
    monkeypatch.setattr(
        model,
        "_history_messages",
        lambda _messages, *, deadline=None: legacy_history,
    )

    payload = model._responses_payload([HumanMessage(content="当前请求")], None, {})

    assert [item.get("type") for item in payload["input"]] == [
        "reasoning",
        "message",
    ]
    assert "content" not in payload["input"][0]


def test_responses_history_drops_unportable_provider_reasoning_id():
    from langchain_core.messages import AIMessage, HumanMessage

    model = build_model_from_provider(_provider("responses"), {})
    history = [
        HumanMessage(content="执行任务"),
        AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "id": "part-local",
                    "status": "completed",
                    "summary": [],
                }
            ],
            response_metadata={"provider_id": "provider-test"},
        ),
        HumanMessage(content="空响应恢复，请继续处理"),
    ]

    payload = model._responses_payload(history, None, {})

    assert payload["input"] == [
        {"type": "message", "role": "user", "content": "执行任务"},
        {
            "type": "message",
            "role": "user",
            "content": "空响应恢复，请继续处理",
        },
    ]
    assert "provider_part_id" not in str(payload)


def test_responses_history_drops_encrypted_reasoning_from_another_provider():
    from langchain_core.messages import AIMessage, HumanMessage

    model = build_model_from_provider(_provider("responses"), {})
    history = [
        HumanMessage(content="跨模型继续"),
        AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "encrypted_content": "foreign-provider-payload",
                    "summary": [],
                },
                {"type": "text", "text": "保留可见正文"},
            ],
            response_metadata={"provider_id": "primary"},
        ),
    ]

    payload = model._responses_payload(history, None, {})

    assert "foreign-provider-payload" not in str(payload)
    assert any(item.get("type") == "message" for item in payload["input"])


def test_responses_history_keeps_tool_call_when_dropping_unportable_reasoning():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    model = build_model_from_provider(_provider("responses"), {})
    history = [
        HumanMessage(content="读取 README"),
        AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "id": "rs_server",
                    "status": "completed",
                    "summary": [],
                }
            ],
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"path": "README.md"},
                    "id": "call_readme",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="README 内容", tool_call_id="call_readme"),
    ]

    payload = model._responses_payload(history, None, {})

    assert "provider_part_id" not in str(payload)
    assert any(
        item.get("type") == "function_call" and item.get("call_id") == "call_readme"
        for item in payload["input"]
    )
    assert any(
        item.get("type") == "function_call_output"
        and item.get("call_id") == "call_readme"
        for item in payload["input"]
    )


def test_responses_history_reconstructs_content_function_call_before_result():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    model = build_model_from_provider(_provider("responses"), {})
    payload = model._responses_payload(
        [
            HumanMessage(content="读取 README"),
            AIMessage(
                content=[
                    {
                        "type": "function_call",
                        "call_id": "call_content",
                        "name": "read_file",
                        "arguments": '{"path":"README.md"}',
                    }
                ]
            ),
            ToolMessage(
                content="README 内容",
                tool_call_id="call_content",
                name="read_file",
            ),
        ],
        None,
        {},
    )

    assert any(
        item.get("type") == "function_call"
        and item.get("call_id") == "call_content"
        for item in payload["input"]
    )
    assert any(
        item.get("type") == "function_call_output"
        and item.get("call_id") == "call_content"
        for item in payload["input"]
    )


def test_responses_history_drops_unpaired_tool_result_and_allows_continuation():
    from langchain_core.messages import HumanMessage, ToolMessage

    model = build_model_from_provider(_provider("responses"), {})

    payload = model._responses_payload(
        [
            HumanMessage(content="继续执行"),
            ToolMessage(content="旧工具结果", tool_call_id="call_orphan"),
            HumanMessage(content="后续消息应继续发送"),
        ],
        None,
        {},
    )

    assert all(
        item.get("type") != "function_call_output" for item in payload["input"]
    )
    assert any(
        item.get("content") == "后续消息应继续发送" for item in payload["input"]
    )


def test_responses_history_drops_stale_unfinished_tool_segment_at_new_job_boundary():
    from langchain_core.messages import AIMessage, HumanMessage

    stale_call_id = "call_mnM0tiEs03n5IxxaST3"
    model = build_model_from_provider(_provider("responses"), {})
    payload = model._responses_payload(
        [
            HumanMessage(content="旧 job 的任务"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"path": "parry_arena/main.gd"},
                        "id": stale_call_id,
                        "type": "tool_call",
                    }
                ],
            ),
            HumanMessage(content="新 job 继续执行，只读取当前工作区"),
        ],
        None,
        {},
    )

    serialized = repr(payload["input"])
    assert stale_call_id not in serialized
    assert any(
        item.get("content") == "新 job 继续执行，只读取当前工作区"
        for item in payload["input"]
    )


def test_responses_history_drops_late_tool_result_after_job_boundary():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    stale_call_id = "call_mnM0tiEs03n5IxxaST3"
    model = build_model_from_provider(_provider("responses"), {})

    payload = model._responses_payload(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"path": "README.md"},
                        "id": stale_call_id,
                        "type": "tool_call",
                    }
                ],
            ),
            HumanMessage(content="新 job 的用户消息"),
            ToolMessage(content="旧 job 延迟到达的结果", tool_call_id=stale_call_id),
        ],
        None,
        {},
    )

    serialized = repr(payload["input"])
    assert stale_call_id not in serialized
    assert "新 job 的用户消息" in serialized


def test_responses_history_isolates_previous_tool_transactions_and_internal_reminder():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    stale_call_id = "call_QWLcaGAtSpzygm4gaup0JyJS"
    model = build_model_from_provider(_provider("responses"), {})
    payload = model._responses_payload(
        [
            HumanMessage(content="旧任务"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ls",
                        "args": {"path": "parry_arena"},
                        "id": stale_call_id,
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content="旧工具结果", tool_call_id=stale_call_id),
            HumanMessage(
                content=(
                    "<system_reminder>\n"
                    "用户主动取消旧任务。请停止当前输出。\n"
                    "</system_reminder>"
                ),
                response_metadata={"internal": True},
            ),
            HumanMessage(content="当前任务只读取 project.godot"),
        ],
        None,
        {},
    )

    serialized = repr(payload["input"])
    assert stale_call_id not in serialized
    assert "用户主动取消旧任务" not in serialized
    assert any(
        item.get("content") == "当前任务只读取 project.godot"
        for item in payload["input"]
    )


def test_responses_history_recovers_from_persisted_orphan_tool_result():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    orphan_call_id = "call_EgMF1XbUORJwSX5TTOXTBmBg"
    model = build_model_from_provider(_provider("responses"), {})

    payload = model._responses_payload(
        [
            HumanMessage(content="历史任务"),
            AIMessage(
                content=["...[BoxTeam list items 已截断]"],
                tool_calls=[],
            ),
            ToolMessage(
                content="旧 job 延迟写入的 read_file 结果",
                tool_call_id=orphan_call_id,
                name="read_file",
            ),
            HumanMessage(content="新 job 继续执行"),
        ],
        None,
        {},
    )

    serialized = repr(payload["input"])
    assert orphan_call_id not in serialized
    assert "新 job 继续执行" in serialized


def test_responses_history_replays_image_tool_result_as_portable_text_pair():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    call_id = "call_UDf0yQNxKcvfzQMU265h0Egf"
    model = build_model_from_provider(_provider("responses"), {})
    payload = model._responses_payload(
        [
            HumanMessage(content="继续处理附件任务"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"path": "verification.png"},
                        "id": call_id,
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content=[
                    {
                        "type": "image",
                        "base64": "eA==",
                        "mime_type": "image/png",
                    }
                ],
                tool_call_id=call_id,
                additional_kwargs={"read_file_path": "verification.png"},
            ),
            HumanMessage(content="继续验证，不要丢失工具配对"),
        ],
        None,
        {},
    )

    call_indexes = [
        index
        for index, item in enumerate(payload["input"])
        if item.get("call_id") == call_id
    ]
    assert len(call_indexes) == 2
    assert payload["input"][call_indexes[0]]["type"] == "function_call"
    output = payload["input"][call_indexes[1]]
    assert output["type"] == "function_call_output"
    assert output["output"] == (
        "[工具结果包含 1 个图片媒体，路径：verification.png。"
        "图片仍保留在会话记录中；本次 Responses 请求将其按文本占位符回放，"
        "以兼容仅接受字符串 function_call_output 的 provider。]"
    )


def test_responses_payload_converts_image_and_replays_encrypted_reasoning():
    from langchain_core.messages import AIMessage, HumanMessage

    model = build_model_from_provider(_provider("responses"), {})
    history = [
        HumanMessage(
            content=[
                {"type": "text", "text": "描述图片"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,eA=="},
                },
            ]
        ),
        AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "encrypted_content": "encrypted-image-reasoning",
                    "summary": [],
                },
                {"type": "text", "text": "图片中有测试图案"},
            ],
            response_metadata={"provider_id": "provider-test"},
        ),
    ]

    payload = model._responses_payload(history, None, {})

    assert payload["input"][0] == {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "描述图片"},
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,eA==",
            },
        ],
    }
    assert payload["input"][1] == {
        "type": "reasoning",
        "encrypted_content": "encrypted-image-reasoning",
        "summary": [],
    }
    assert payload["input"][2] == {
        "type": "message",
        "role": "assistant",
        "id": None,
        "content": [
            {
                "type": "output_text",
                "text": "图片中有测试图案",
                "annotations": [],
            }
        ],
    }


def test_responses_output_keeps_direct_reasoning_item():
    block = BoxteamOpenAIResponsesModel._normalize_response_block(
        {
            "type": "reasoning",
            "id": "rs_server",
            "status": "completed",
            "content": [
                {"type": "reasoning_text", "text": "先确认范围"},
            ],
            "encrypted_content": "encrypted-reasoning",
            "summary": [{"type": "summary_text", "text": "摘要"}],
            "provider_extension": {"trace_group": "reasoning-1"},
        }
    )
    assert block == {
        "type": "reasoning",
        "id": "rs_server",
        "status": "completed",
        "content": [
            {"type": "reasoning_text", "text": "先确认范围"},
        ],
        "encrypted_content": "encrypted-reasoning",
        "summary": [{"type": "summary_text", "text": "摘要"}],
        "provider_extension": {"trace_group": "reasoning-1"},
    }


def test_responses_output_text_only_normalizes_type_without_dropping_fields():
    block = BoxteamOpenAIResponsesModel._normalize_response_block(
        {
            "type": "output_text",
            "text": "完成",
            "annotations": [{"type": "url_citation", "url": "https://example.com"}],
            "phase": "final_answer",
            "provider_extension": {"trace_group": "text-1"},
        }
    )

    assert block == {
        "type": "text",
        "text": "完成",
        "annotations": [{"type": "url_citation", "url": "https://example.com"}],
        "phase": "final_answer",
        "provider_extension": {"trace_group": "text-1"},
    }


def test_responses_stream_keeps_one_portable_reasoning_item() -> None:
    model = BoxteamOpenAIResponsesModel(
        model="gpt-test",
        api_key="test-key",
        custom_llm_provider="openai",
    )
    part_state = _StreamPartState()
    indexes = (-1, -1, -1)
    chunks = []
    for event_type, encrypted_content in (
        ("response.output_item.added", None),
        ("response.output_item.done", "encrypted-reasoning"),
    ):
        item = {
            "type": "reasoning",
            "id": "rs_test",
            "summary": [],
        }
        if encrypted_content is not None:
            item["encrypted_content"] = encrypted_content
        event = SimpleNamespace(type=event_type, output_index=0, item=item)
        index, output_index, sub_index, chunk = model._convert_response_event(
            event,
            current_index=indexes[0],
            current_output_index=indexes[1],
            current_sub_index=indexes[2],
            part_state=part_state,
            original_schema=None,
        )
        indexes = (index, output_index, sub_index)
        if event_type == "response.output_item.added":
            assert chunk is None
        else:
            assert chunk is not None
            chunks.append(chunk.message)

    message = message_chunk_to_message(chunks[0])
    from app.agents.providers.litellm_content import canonicalize_ai_message

    message = canonicalize_ai_message(message, source_provider="openai")
    assert isinstance(message.content, list)
    assert len(message.content) == 1
    assert message.content[0] == {
        "type": "reasoning_items",
        "reasoning_items": [
            {
                "type": "reasoning",
                "id": "rs_test",
                "summary": [],
                "encrypted_content": "encrypted-reasoning",
            }
        ],
    }


def test_responses_summary_delta_emits_reasoning_text_block() -> None:
    model = BoxteamOpenAIResponsesModel(
        model="gpt-test",
        api_key="test-key",
        custom_llm_provider="openai",
    )
    part_state = _StreamPartState()
    event = SimpleNamespace(
        type="response.reasoning_summary_text.delta",
        item_id="rs_summary",
        delta="先读取 README",
    )

    _index, _output_index, _sub_index, chunk = model._convert_response_event(
        event,
        current_index=-1,
        current_output_index=-1,
        current_sub_index=-1,
        part_state=part_state,
        original_schema=None,
    )

    assert chunk is not None
    block = chunk.message.content[0]
    assert block["type"] == "reasoning"
    assert block["content"] == [
        {
            "type": "reasoning_text",
            "text": "先读取 README",
        }
    ]
    assert isinstance(block["id"], str)
    assert block["index"] == 0
    assert block["extras"] == {"provider_part_id": "rs_summary"}


def test_responses_empty_added_summary_does_not_emit_empty_reasoning_block() -> None:
    model = BoxteamOpenAIResponsesModel(
        model="gpt-test",
        api_key="test-key",
        custom_llm_provider="openai",
    )
    event = SimpleNamespace(
        type="response.output_item.added",
        output_index=0,
        item={
            "type": "reasoning",
            "id": "rs_empty",
            "summary": [{"type": "summary_text", "text": ""}],
        },
    )

    _index, _output_index, _sub_index, chunk = model._convert_response_event(
        event,
        current_index=-1,
        current_output_index=-1,
        current_sub_index=-1,
        part_state=_StreamPartState(),
        original_schema=None,
    )

    assert chunk is None


def test_responses_interleaved_function_call_deltas_keep_call_identity() -> None:
    model = BoxteamOpenAIResponsesModel(
        model="gpt-test",
        api_key="test-key",
        custom_llm_provider="openai",
    )
    part_state = _StreamPartState()
    indexes = (-1, -1, -1)
    events = [
        SimpleNamespace(
            type="response.output_item.added",
            output_index=1,
            item={
                "type": "function_call",
                "id": "fc_one",
                "call_id": "call_one",
                "name": "read_file",
                "arguments": "",
            },
        ),
        SimpleNamespace(
            type="response.output_item.added",
            output_index=2,
            item={
                "type": "function_call",
                "id": "fc_two",
                "call_id": "call_two",
                "name": "read_file",
                "arguments": "",
            },
        ),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            output_index=1,
            item_id="fc_one",
            delta='{"path":"README',
        ),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            output_index=2,
            item_id="fc_two",
            delta='{"path":"pyproject',
        ),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            output_index=1,
            item_id="fc_one",
            delta='.md"}',
        ),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            output_index=2,
            item_id="fc_two",
            delta='.toml"}',
        ),
    ]

    observed: list[tuple[str, str, int]] = []
    observed_names: list[str | None] = []
    arguments_by_call_id: dict[str, list[str]] = {}
    for event in events:
        index, output_index, sub_index, chunk = model._convert_response_event(
            event,
            current_index=indexes[0],
            current_output_index=indexes[1],
            current_sub_index=indexes[2],
            part_state=part_state,
            original_schema=None,
        )
        indexes = (index, output_index, sub_index)
        assert chunk is not None
        tool_chunk = chunk.message.tool_call_chunks[0]
        call_id = tool_chunk.get("id")
        args = tool_chunk.get("args")
        assert isinstance(call_id, str)
        assert isinstance(args, str)
        observed.append((call_id, args, int(tool_chunk["index"])))
        observed_names.append(tool_chunk.get("name"))
        arguments_by_call_id.setdefault(call_id, []).append(args)

    assert observed == [
        ("call_one", "", 1),
        ("call_two", "", 2),
        ("call_one", '{"path":"README', 1),
        ("call_two", '{"path":"pyproject', 2),
        ("call_one", '.md"}', 1),
        ("call_two", '.toml"}', 2),
    ]
    assert {
        call_id: "".join(parts)
        for call_id, parts in arguments_by_call_id.items()
    } == {
        "call_one": '{"path":"README.md"}',
        "call_two": '{"path":"pyproject.toml"}',
    }
    assert observed_names == ["read_file", "read_file", None, None, None, None]


def test_responses_upstream_trace_uses_final_payload_when_litellm_input_is_empty():
    callback = UpstreamRequestTraceCallback(
        fallback_request={
            "model": "gpt-5.6-luna",
            "input": [{"type": "message", "role": "user", "content": "hello"}],
            "include": ["reasoning.encrypted_content"],
            "store": False,
            "api_key": "secret",
        }
    )
    token = begin_upstream_capture()
    callback.log_pre_api_call(
        "gpt-5.6-luna",
        None,
        {
            "litellm_call_id": "responses-1",
            "call_type": "aresponses",
            "custom_llm_provider": "openai",
            "additional_args": {"complete_input_dict": {}},
        },
    )
    attempts = end_upstream_capture(token)

    assert attempts[0]["request"]["input"][0]["role"] == "user"
    assert attempts[0]["request"]["include"] == ["reasoning.encrypted_content"]
    assert attempts[0]["request"]["api_key"] == "[REDACTED]"


def test_responses_stream_terminal_event_records_complete_upstream_response():
    callback = UpstreamRequestTraceCallback()
    token = begin_upstream_capture()
    callback.log_pre_api_call(
        "gpt-5.6-luna",
        None,
        {
            "litellm_call_id": "responses-stream-1",
            "call_type": "aresponses",
            "custom_llm_provider": "openai",
            "additional_args": {
                "complete_input_dict": {
                    "model": "gpt-5.6-luna",
                    "input": [{"type": "message", "role": "user"}],
                }
            },
        },
    )

    record_upstream_response({"id": "resp_stream", "output": []})
    attempts = end_upstream_capture(token)

    assert attempts[0]["response"] == {"id": "resp_stream", "output": []}
