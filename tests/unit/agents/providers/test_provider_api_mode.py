from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    message_chunk_to_message,
)

from app.agents.providers.litellm_chat import (
    BoxteamLiteLLMChatModel,
    _StreamPartState,
)
from app.agents.providers.openai_responses import (
    BoxteamOpenAIResponsesModel,
)
from app.agents.providers.wire_finalize import (
    chat_send_params,
    chat_wire_params,
    responses_wire_params,
    wire_request_params,
)
from app.agents.upstream_request_trace import (
    apply_upstream_call_details,
    begin_upstream_capture,
    end_upstream_capture,
    record_upstream_request,
    record_upstream_response,
)
from app.core.failures import is_provider_failure, public_error
from app.core.path_utils import workspace_context
from app.core.workspace_identity import load_or_create_workspace_id
from app.runtime.agent_runtime import prepare_agent_model
from app.runtime.chatgpt_auth import ChatGPTCredential
from app.runtime.network_proxy import ProxyRoutes
from app.runtime.provider_model import (
    build_model_from_provider,
    provider_model_capacity,
)
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.orchestration.event_stream.model_events import (
    is_chat_model_run_name,
)
from app.services.orchestration.message_stream.runtime import MessageStreamRuntime


@pytest.fixture
def provider_workspace_scope(tmp_path):
    """来源回放只需明确工作区身份，不创建会话或加载项目模板。"""
    with workspace_context(load_or_create_workspace_id(tmp_path), tmp_path):
        yield


def _provider(api_mode: str, direct_proxy_routes: ProxyRoutes) -> dict[str, object]:
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
        "proxy_routes": direct_proxy_routes,
        "provider_id": "provider-test",
        "model_id": "general",
        "endpoint": "https://example.com/v1",
        "model": "test-model",
        "api_key": "test-key",
        "credential_source": "env:PROVIDER_TEST_KEY",
        "custom_llm_provider": "openai",
        "api_mode": structured_api_mode,
    }


def test_chat_completions_mode_uses_litellm_chat_model(direct_proxy_routes):
    model = build_model_from_provider(_provider("chat_completions", direct_proxy_routes), {})
    assert isinstance(model, BoxteamLiteLLMChatModel)


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic_messages"])
def test_input_context_capacity_sets_profile_without_changing_wire(direct_proxy_routes, protocol):
    provider = _provider(protocol, direct_proxy_routes)
    if protocol == "anthropic_messages":
        provider["custom_llm_provider"] = "anthropic"
        provider["api_mode"] = {
            "protocol": protocol,
            "model_info": {"supports_reasoning": True},
            "supports_reasoning": {
                "thinking_blocks": {"thinking": True, "redacted_thinking": False}
            },
        }
    baseline = build_model_from_provider(provider, {}, invocation_purpose="tool_test")
    provider["api_mode"]["model_info"].update(max_input_tokens=400000, context_window=128000)
    model = build_model_from_provider(provider, {}, invocation_purpose="tool_test")

    assert model.profile["max_input_tokens"] == 400000
    assert model.profile["context_window"] == 128000
    assert model.profile["image_inputs"] is provider["api_mode"]["model_info"].get("supports_vision", False)
    assert model.profile["audio_inputs"] is False
    assert model.profile["video_inputs"] is False
    if protocol == "responses":
        baseline_body = baseline._responses_payload([HumanMessage("hello")], None, {})
        body = model._responses_payload([HumanMessage("hello")], None, {})
    else:
        baseline_body = chat_send_params(baseline, stream=True)
        body = chat_send_params(model, stream=True)
    body = wire_request_params(model, body)
    assert body == wire_request_params(baseline, baseline_body)
    assert {"max_input_tokens", "context_window", "profile", "max_tokens", "max_output_tokens"}.isdisjoint(body)


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("call_parameters,expected_output", [
    ({}, 200), ({"max_tokens": None}, None), ({"max_output_tokens": 300}, 300),
])
async def test_capacity_uses_final_wire_output_after_existing_overrides(
    direct_proxy_routes, protocol, call_parameters, expected_output,
):
    provider = _provider(protocol, direct_proxy_routes)
    provider["api_mode"]["model_info"].update(max_input_tokens=900, context_window=1000)
    provider["request_options"] = {"parameters": {"max_tokens": 100}}
    model = build_model_from_provider(provider, {"max_output_tokens": 200}, invocation_purpose="tool_test")
    try:
        parameters = (
            responses_wire_params(model, kwargs=call_parameters) if protocol == "responses"
            else chat_wire_params(model, kwargs=call_parameters)
        )
        capacity = provider_model_capacity(provider, request_parameters=parameters)
        output_field = "max_output_tokens" if protocol == "responses" else "max_tokens"
        assert parameters.get(output_field) == expected_output
        assert (output_field in parameters) is (expected_output is not None)
        assert capacity.output_reservation_tokens == expected_output
        assert capacity.input_budget_tokens == (900 if expected_output is None else 1000 - expected_output)
        assert provider_model_capacity(provider).output_reservation_tokens == 100
        assert {"max_input_tokens", "context_window", "profile"}.isdisjoint(parameters)
    finally:
        await model.aclose()


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


def test_litellm_unified_reasoning_fields_use_ordered_carrier_blocks(direct_proxy_routes):
    model = BoxteamLiteLLMChatModel(
        proxy_routes=direct_proxy_routes,
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


def test_chat_completions_mode_forwards_stable_prompt_cache_key(direct_proxy_routes):
    provider = _provider("chat_completions", direct_proxy_routes)
    api_mode = provider["api_mode"]
    assert isinstance(api_mode, dict)
    api_mode["request_features"] = {"prompt_cache_key": True}
    model = build_model_from_provider(
        provider,
        {},
        prompt_cache_key="session-chat-cache",
    )

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model._client_params["prompt_cache_key"] == "session-chat-cache"


def test_chat_completions_mode_omits_cache_key_without_capability(direct_proxy_routes):
    model = build_model_from_provider(
        _provider("chat_completions", direct_proxy_routes),
        {},
        prompt_cache_key="session-chat-cache",
    )

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert "extra_body" not in model._client_params


def test_chat_completions_reasoning_replay_capability_configures_model(direct_proxy_routes):
    provider = _provider("chat_completions", direct_proxy_routes)

    model = build_model_from_provider(provider, {})

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model.reasoning_content_replay is True


def test_chat_completions_thinking_blocks_configures_model_projection(direct_proxy_routes):
    provider = _provider("chat_completions", direct_proxy_routes)
    provider["api_mode"] = {
        "protocol": "chat_completions",
        "model_info": {"supports_function_calling": True, "supports_reasoning": True},
        "supports_reasoning": {"thinking_blocks": True},
    }

    model = build_model_from_provider(provider, {})

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model.reasoning_content_replay is False
    assert model.thinking_blocks_replay is True


def test_anthropic_messages_mode_uses_litellm_chat_model(direct_proxy_routes):
    provider = {
        "proxy_routes": direct_proxy_routes,
        "provider_id": "anthropic-test",
        "model_id": "general",
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


def test_anthropic_history_projects_direct_content_to_thinking_blocks(direct_proxy_routes, provider_workspace_scope):
    from langchain_core.messages import AIMessage

    from app.agents.providers.output_normalization import build_ai_message_content

    provider = _provider("chat_completions", direct_proxy_routes)
    provider.update(model="claude-test", endpoint="https://example.com/anthropic",
                    custom_llm_provider="anthropic", provider_id="anthropic-test")
    provider["api_mode"] = {"protocol": "anthropic_messages", "model_info": {"supports_reasoning": True},
                            "supports_reasoning": {"thinking_blocks": {"thinking": True, "redacted_thinking": False}}}
    model = build_model_from_provider(provider, {}, gateway_id="gateway-test", invocation_purpose="tool_test")
    assert model.model_source is not None
    message = AIMessage(
        content=build_ai_message_content(
            "最终回答",
            source_provider="anthropic-test",
            thinking_blocks=[
                {"type": "thinking", "thinking": "分析"},
                {"type": "redacted_thinking", "data": "sealed"},
            ],
        ),
        response_metadata={"provider_id": "anthropic-test", "model_source": model.model_source.to_dict()},
    )

    projected = model._convert_messages_to_dicts([message])[0]

    assert projected["content"] == [{"type": "text", "text": "最终回答"}]
    assert projected["thinking_blocks"] == [
        {"type": "thinking", "thinking": "分析"},
    ]


def test_anthropic_history_keeps_public_thinking_text_without_replaying_private_blocks(direct_proxy_routes):
    from langchain_core.messages import AIMessage

    model = BoxteamLiteLLMChatModel(
        proxy_routes=direct_proxy_routes,
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

    assert projected["content"] == [{"type": "text", "text": "思考"}, {"type": "text", "text": "可见回答"}]
    assert "thinking_blocks" not in projected and "sealed" not in str(projected)
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


def test_responses_mode_uses_encrypted_reasoning_and_stable_cache_key(direct_proxy_routes):
    model = build_model_from_provider(
        _provider("responses", direct_proxy_routes),
        {},
        prompt_cache_key="session-123",
    )
    assert isinstance(model, BoxteamOpenAIResponsesModel)
    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert is_chat_model_run_name(type(model).__name__)
    assert model.responses_store is False
    assert model.responses_include == ["reasoning.encrypted_content"]
    assert model._client_params["prompt_cache_key"] == "session-123"


def test_chatgpt_oauth_responses_uses_stable_litellm_session_id(direct_proxy_routes, monkeypatch):
    monkeypatch.setattr(
        "app.runtime.chatgpt_auth.configure_litellm_chatgpt_auth_directory",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.runtime.chatgpt_auth.prepare_chatgpt_oauth",
        lambda _token_dir, **_kwargs: ChatGPTCredential("test-oauth-token", None),
    )
    monkeypatch.setattr(
        "app.runtime.chatgpt_auth.ensure_litellm_chatgpt_model_capabilities",
        lambda _: False,
    )
    provider = {
        "proxy_routes": direct_proxy_routes,
        "provider_id": "backup_4",
        "auth_proxy_routes": ProxyRoutes(()),
        "model_id": "general",
        "endpoint": "https://chatgpt.com/backend-api/codex",
        "model": "gpt-5.6-luna",
        "custom_llm_provider": "chatgpt",
        "api_mode": _provider("responses", direct_proxy_routes)["api_mode"],
        "auth": {"type": "oauth", "method": "chatgpt"},
    }

    model = build_model_from_provider(
        provider,
        {},
        prompt_cache_key="ses_chatgpt_cache_affinity",
        invocation_purpose="tool_test",
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


def test_runtime_skips_invalid_fallback_without_blocking_selected_model(
    direct_proxy_routes, monkeypatch,
):
    primary_model = MagicMock(spec=BaseChatModel)
    providers = [_provider("responses", direct_proxy_routes), _provider("responses", direct_proxy_routes)]
    providers[0]["model_id"] = "selected"
    providers[1]["model_id"] = "broken-fallback"
    selected = ModelReferenceDTO(provider_id="provider-test", model_id="selected")

    class ConfigServiceStub:
        def get_revision(self):
            return "config:runtime-fallback"

        def get_agent_runtime_config(self, _agent_id, *, preferred_model=None):
            assert preferred_model == selected
            return {"models": providers, "system_prompt": "test", "runtime_identity_description": ""}

    def build(provider, _runtime, *, prompt_cache_key=None, gateway_id=None):
        assert prompt_cache_key is None
        assert gateway_id is None
        if provider["model_id"] == "broken-fallback":
            raise ValueError("fallback 配置损坏")
        return primary_model

    monkeypatch.setattr("app.runtime.provider_model.build_model_from_provider", build)

    model_setup = prepare_agent_model(
        "default",
        config_service=ConfigServiceStub(),
        preferred_model=selected,
    )

    assert model_setup.model is primary_model
    candidate = model_setup.routing_middleware._candidates[0]
    assert (candidate.provider_id, candidate.model_id) == (selected.provider_id, selected.model_id)
    assert len(model_setup.routing_middleware._candidates) == 1


def test_runtime_reports_invalid_selected_model(direct_proxy_routes, monkeypatch):
    providers = [_provider("responses", direct_proxy_routes)]
    providers[0]["model_id"] = "broken-selected"
    selected = ModelReferenceDTO(provider_id="provider-test", model_id="broken-selected")

    class ConfigServiceStub:
        def get_revision(self):
            return "config:runtime-fallback"

        def get_agent_runtime_config(self, _agent_id, *, preferred_model=None):
            assert preferred_model == selected
            return {"models": providers, "system_prompt": "test", "runtime_identity_description": ""}

    monkeypatch.setattr(
        "app.runtime.provider_model.build_model_from_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("配置损坏")),
    )

    with pytest.raises(
        RuntimeError,
        match="当前选择的模型配置不可用.*provider_id=provider-test, model_id=broken-selected.*配置损坏",
    ):
        prepare_agent_model(
            "default",
            config_service=ConfigServiceStub(),
            preferred_model=selected,
        )


def test_chatgpt_provider_rejects_missing_oauth_config(direct_proxy_routes):
    provider = _provider("responses", direct_proxy_routes)
    provider["custom_llm_provider"] = "chatgpt"

    with pytest.raises(ValueError, match="auth.type='oauth'"):
        build_model_from_provider(provider, {})


def test_chatgpt_nonstream_is_rejected_before_loading_oauth_credentials(direct_proxy_routes, monkeypatch):
    provider = _provider("responses", direct_proxy_routes)
    provider.update(custom_llm_provider="chatgpt", auth={"type": "oauth", "method": "chatgpt"})
    provider["api_mode"]["streaming"] = False
    monkeypatch.setattr("app.runtime.chatgpt_auth.ensure_chatgpt_oauth_ready",
                        lambda *_args: pytest.fail("非流式拒绝不应加载 OAuth 凭据"))
    with pytest.raises(ValueError, match="不能以聚合流代替非流式"):
        build_model_from_provider(provider, {})


def test_responses_history_replays_encrypted_reasoning_without_server_id(direct_proxy_routes, provider_workspace_scope):
    from langchain_core.messages import AIMessage

    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, gateway_id="gateway-test", invocation_purpose="tool_test")
    assert model.model_source is not None
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
        response_metadata={"provider_id": "provider-test", "model_source": model.model_source.to_dict()},
    )
    payload = model._responses_payload([message], None, {})
    assert payload["input"] == [
        {
            "type": "reasoning",
            "encrypted_content": "encrypted-reasoning",
            "summary": [],
        }
    ]


def test_responses_attachment_history_drops_reasoning_output_content(direct_proxy_routes, provider_workspace_scope):
    from langchain_core.messages import AIMessage, HumanMessage

    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, gateway_id="gateway-test", invocation_purpose="tool_test")
    assert model.model_source is not None
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
                response_metadata={"provider_id": "provider-test", "model_source": model.model_source.to_dict()},
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
    direct_proxy_routes, monkeypatch,
):
    from langchain_core.messages import AIMessage, HumanMessage

    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, invocation_purpose="tool_test")
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


def test_responses_history_drops_unportable_provider_reasoning_id(direct_proxy_routes):
    from langchain_core.messages import AIMessage, HumanMessage

    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, invocation_purpose="tool_test")
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


def test_responses_history_drops_encrypted_reasoning_from_another_provider(direct_proxy_routes):
    from langchain_core.messages import AIMessage, HumanMessage

    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, invocation_purpose="tool_test")
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


@pytest.mark.parametrize("text", [None, "上一轮工具调用已完成"])
def test_responses_native_summary_projects_reasoning_before_encoding_message_text(text):
    from app.services.infrastructure.rollout_context.provider.native_request import (
        _summary_message_inputs,
    )

    content = [{"type": "reasoning_items", "reasoning_items": [{"type": "reasoning", "summary": []}]}]
    if text is not None:
        content.append({"type": "text", "text": text})
    # 私有块先经过共享投影，纯文本编码器只接收该投影的结果。
    inputs, losses = _summary_message_inputs({
        "message": {"type": "ai", "data": {"content": content}},
        "item_ids": [], "retry_source_item_ids": [],
    }, target_source=None, target_capabilities=frozenset({"reasoning_items"}))
    assert inputs == ([] if text is None else [{"role": "assistant", "content": [{"type": "output_text", "text": text}]}])
    assert losses == []


def test_responses_native_projection_rejects_unknown_system_block():
    from app.services.infrastructure.rollout_context.provider.native_request import (
        _text_blocks,
    )

    with pytest.raises(ValueError, match="source-mismatch"):
        _text_blocks([{"type": "provider_private_extension"}], role="system")


def test_responses_history_keeps_tool_call_when_dropping_unportable_reasoning(direct_proxy_routes):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, invocation_purpose="tool_test")
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


def test_responses_history_reconstructs_content_function_call_before_result(direct_proxy_routes):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, invocation_purpose="tool_test")
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


def test_responses_history_drops_unpaired_tool_result_and_allows_continuation(direct_proxy_routes):
    from langchain_core.messages import HumanMessage, ToolMessage

    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, invocation_purpose="tool_test")

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


def test_responses_history_drops_stale_unfinished_tool_segment_at_new_job_boundary(direct_proxy_routes):
    from langchain_core.messages import AIMessage, HumanMessage

    stale_call_id = "call_mnM0tiEs03n5IxxaST3"
    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, invocation_purpose="tool_test")
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


def test_responses_history_drops_late_tool_result_after_job_boundary(direct_proxy_routes):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    stale_call_id = "call_mnM0tiEs03n5IxxaST3"
    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, invocation_purpose="tool_test")

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


def test_responses_history_isolates_previous_tool_transactions_and_internal_reminder(direct_proxy_routes):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from app.prompting import internal_message_factory

    reminder = internal_message_factory.build(
        kind="checkpoint_reminder", control="用户主动取消旧任务。请停止当前输出。",
    )

    stale_call_id = "call_QWLcaGAtSpzygm4gaup0JyJS"
    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, invocation_purpose="tool_test")
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
                content=reminder.content,
                response_metadata=reminder.metadata,
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


def test_responses_payload_preserves_user_literal_tag_and_tool_retry(direct_proxy_routes):
    from langchain_core.messages import HumanMessage

    from app.prompting import internal_message_factory

    user = "<system_reminder>请解释这个标签</system_reminder>"
    retry = internal_message_factory.build(kind="tool_test_retry", control="继续调用测试工具。")
    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, invocation_purpose="tool_test")
    payload = model._responses_payload([
        HumanMessage(content=user), HumanMessage(content=retry.content, response_metadata=retry.metadata),
    ], None, {})
    assert [item["content"] for item in payload["input"]] == [user, retry.content]


def test_responses_history_recovers_from_persisted_orphan_tool_result(direct_proxy_routes):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    orphan_call_id = "call_EgMF1XbUORJwSX5TTOXTBmBg"
    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, invocation_purpose="tool_test")

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


def test_responses_history_replays_image_tool_result_as_portable_text_pair(direct_proxy_routes):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    call_id = "call_UDf0yQNxKcvfzQMU265h0Egf"
    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, invocation_purpose="tool_test")
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


def test_responses_payload_converts_image_and_replays_encrypted_reasoning(direct_proxy_routes, provider_workspace_scope):
    from langchain_core.messages import AIMessage, HumanMessage

    model = build_model_from_provider(_provider("responses", direct_proxy_routes), {}, gateway_id="gateway-test", invocation_purpose="tool_test")
    assert model.model_source is not None
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
            response_metadata={"provider_id": "provider-test", "model_source": model.model_source.to_dict()},
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


def test_responses_stream_keeps_one_portable_reasoning_item(direct_proxy_routes) -> None:
    model = BoxteamOpenAIResponsesModel(
        proxy_routes=direct_proxy_routes,
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
    from app.agents.providers.response_normalization import canonicalize_ai_message

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


def test_responses_summary_delta_emits_reasoning_text_block(direct_proxy_routes) -> None:
    model = BoxteamOpenAIResponsesModel(
        proxy_routes=direct_proxy_routes,
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


def test_responses_empty_added_summary_does_not_emit_empty_reasoning_block(direct_proxy_routes) -> None:
    model = BoxteamOpenAIResponsesModel(
        proxy_routes=direct_proxy_routes,
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


def test_responses_interleaved_function_call_deltas_keep_call_identity(direct_proxy_routes) -> None:
    model = BoxteamOpenAIResponsesModel(
        proxy_routes=direct_proxy_routes,
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


@pytest.mark.asyncio
async def test_responses_empty_function_call_arguments_remain_incomplete_after_model_finish(direct_proxy_routes) -> None:
    model = BoxteamOpenAIResponsesModel(
        proxy_routes=direct_proxy_routes,
        model="gpt-test",
        api_key="test-key",
        custom_llm_provider="openai",
    )
    _, _, _, chunk = model._convert_response_event(
        SimpleNamespace(
            type="response.output_item.added",
            output_index=1,
            item={
                "type": "function_call",
                "id": "fc_empty",
                "call_id": "call_empty",
                "name": "read_file",
                "arguments": "",
            },
        ),
        current_index=-1,
        current_output_index=-1,
        current_sub_index=-1,
        part_state=_StreamPartState(),
        original_schema=None,
    )
    assert chunk is not None

    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)
    await runtime.start_model("model_empty_arguments", "gpt-test")
    await runtime.accept_message_chunk(chunk.message)
    await runtime.finish_model()

    call_id = "model_empty_arguments:tool-call:call_empty"
    delta = next(
        call.args[1]
        for call in writer.commit.await_args_list
        if call.args[0] == "tool_call.delta"
    )
    assert delta["arguments"] == {}
    assert delta["arguments_complete"] is False
    assert runtime.pending_tool_calls() == ((call_id, "read_file", False),)


def _fake_llm_stream(model_call_details: dict[str, object]) -> SimpleNamespace:
    """构造只带 LiteLLM 调用细节的最小流对象。"""
    return SimpleNamespace(
        logging_obj=SimpleNamespace(model_call_details=model_call_details)
    )


def test_responses_upstream_trace_uses_final_payload_when_litellm_input_is_empty():
    token = begin_upstream_capture()
    record_upstream_request(
        request={
            "model": "gpt-5.6-luna",
            "input": [{"type": "message", "role": "user", "content": "hello"}],
            "include": ["reasoning.encrypted_content"],
            "store": False,
            "api_key": "secret",
        },
        model="gpt-5.6-luna",
        provider="openai",
        api_base="https://example.com/v1",
        call_type="aresponses",
        dispatch_ref=None,
    )
    apply_upstream_call_details(
        _fake_llm_stream(
            {
                "call_type": "aresponses",
                "custom_llm_provider": "openai",
                "model": "gpt-5.6-luna",
                "additional_args": {"complete_input_dict": {}},
            }
        )
    )
    attempts = end_upstream_capture(token)

    assert attempts[0]["request"]["input"][0]["role"] == "user"
    assert attempts[0]["request"]["include"] == ["reasoning.encrypted_content"]
    assert attempts[0]["request"]["api_key"] == "[REDACTED]"


def test_responses_stream_terminal_event_records_complete_upstream_response():
    token = begin_upstream_capture()
    record_upstream_request(
        request={
            "model": "gpt-5.6-luna",
            "input": [{"type": "message", "role": "user"}],
        },
        model="gpt-5.6-luna",
        provider="openai",
        api_base="https://example.com/v1",
        call_type="aresponses",
        dispatch_ref=None,
    )
    apply_upstream_call_details(
        _fake_llm_stream(
            {
                "call_type": "aresponses",
                "custom_llm_provider": "openai",
                "model": "gpt-5.6-luna",
                "additional_args": {
                    "api_base": "https://example.com/v1/responses",
                    "complete_input_dict": {
                        "model": "gpt-5.6-luna",
                        "input": [{"type": "message", "role": "user"}],
                    },
                },
            }
        )
    )

    record_upstream_response({"id": "resp_stream", "output": []})
    attempts = end_upstream_capture(token)

    assert attempts[0]["api_base"] == "https://example.com/v1/responses"
    assert attempts[0]["response"] == {"id": "resp_stream", "output": []}


@pytest.mark.parametrize("event_type", ["response.failed", "error"])
def test_responses_failure_events_are_marked_as_provider_failures(event_type: str, monkeypatch):
    model = BoxteamOpenAIResponsesModel(
        model="gpt-test",
        api_key="test-key",
        custom_llm_provider="openai",
        invocation_purpose="tool_test",
        proxy_routes=ProxyRoutes(()),
        streaming=True,
    )
    monkeypatch.setattr(
        "app.agents.providers.openai_responses.litellm.responses",
        lambda **_kwargs: iter([SimpleNamespace(type=event_type, message="upstream exposed test-key")]),
    )
    try:
        with pytest.raises(RuntimeError, match="LiteLLM Responses 请求失败") as raised:
            list(model.stream("hi"))
        assert is_provider_failure(raised.value)
        assert "test-key" not in json.dumps(public_error(raised.value))
    finally:
        asyncio.run(model.aclose())
