from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.runtime.provider_model import build_model_from_provider


def _chat_api_mode() -> dict[str, object]:
    return {
        "protocol": "chat_completions",
        "model_info": {"supports_function_calling": True, "supports_reasoning": True},
        "supports_reasoning": {"reasoning_content": True},
    }


class _EmptyChatCompletionStream:
    received_finish_reason = "stop"
    intermittent_finish_reason = None

    def __aiter__(self) -> _EmptyChatCompletionStream:
        return self

    async def __anext__(self) -> dict[str, object]:
        raise StopAsyncIteration


@pytest.fixture
def empty_chat_completion_stream() -> _EmptyChatCompletionStream:
    return _EmptyChatCompletionStream()


def test_build_model_omits_unspecified_generation_parameters(direct_proxy_routes):
    """最小 provider 配置不得人为限制采样策略或输出长度。"""
    model = build_model_from_provider(
        provider={
            "id": "primary",
            "custom_llm_provider": "openai",
            "model": "work-model",
            "api_key": "test-key",
            "endpoint": "https://example.com/v1",
            "api_mode": _chat_api_mode(),
            "proxy_routes": direct_proxy_routes,
        },
        runtime_config={},
    )

    assert model.temperature is None
    assert model.top_p is None
    assert model.max_tokens is None
    assert model.model_kwargs == {}
    assert model._default_params["temperature"] is None
    assert "top_p" not in model._default_params
    assert model._default_params["max_tokens"] is None


@pytest.mark.asyncio
async def test_minimal_model_does_not_send_unspecified_generation_parameters(
    monkeypatch: pytest.MonkeyPatch,
    empty_chat_completion_stream: _EmptyChatCompletionStream,
    direct_proxy_routes,
) -> None:
    captured: dict[str, object] = {}

    async def fake_acompletion_with_retry(self, **kwargs):
        captured.update(kwargs)
        return empty_chat_completion_stream

    monkeypatch.setattr(
        BoxteamLiteLLMChatModel,
        "acompletion_with_retry",
        fake_acompletion_with_retry,
    )
    model = build_model_from_provider(
        provider={
            "id": "primary",
            "custom_llm_provider": "openai",
            "model": "work-model",
            "api_key": "test-key",
            "endpoint": "https://example.com/v1",
            "api_mode": _chat_api_mode(),
            "proxy_routes": direct_proxy_routes,
        },
        runtime_config={},
        invocation_purpose="tool_test",
    )

    chunks = [chunk async for chunk in model._astream([HumanMessage(content="hi")])]

    assert chunks == []
    assert "temperature" not in captured
    assert "top_p" not in captured
    assert "max_tokens" not in captured


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_nonstream_call_keeps_nonstream_request_parameters(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    direct_proxy_routes,
) -> None:
    """共享参数转换不得把同步或异步的非流式调用改成流式请求。"""
    captured: dict[str, object] = {}
    response = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "完成"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def fake_completion_with_retry(
        self: BoxteamLiteLLMChatModel,
        **kwargs: object,
    ) -> dict[str, object]:
        del self
        captured.update(kwargs)
        return response

    async def fake_acompletion_with_retry(
        self: BoxteamLiteLLMChatModel,
        **kwargs: object,
    ) -> dict[str, object]:
        return fake_completion_with_retry(self, **kwargs)

    monkeypatch.setattr(
        BoxteamLiteLLMChatModel,
        "completion_with_retry",
        fake_completion_with_retry,
    )
    monkeypatch.setattr(
        BoxteamLiteLLMChatModel,
        "acompletion_with_retry",
        fake_acompletion_with_retry,
    )
    model = BoxteamLiteLLMChatModel(
        model="test-model",
        api_key="test-key",
        custom_llm_provider="openai",
        streaming=False,
        max_retries=0,
        invocation_purpose="tool_test",
        proxy_routes=direct_proxy_routes,
    )
    messages = [HumanMessage(content="hi")]
    if mode == "sync":
        model._generate(messages, stream=False)
    else:
        await model._agenerate(messages, stream=False)

    assert captured.get("stream", False) is False
    assert "stream_options" not in captured
    assert captured["messages"] == [{"role": "user", "content": "hi"}]


def test_build_model_passes_provider_request_parameters(direct_proxy_routes):
    """未声明 Agent 参数时透传模型参数。"""
    model = build_model_from_provider(
        provider={
            "id": "backup_3",
            "custom_llm_provider": "openai",
            "model": "gpt-5.4-mini",
            "api_key": "test-key",
            "endpoint": "https://example.com/v1",
            "api_mode": _chat_api_mode(),
            "request_options": {
                "parameters": {
                    "temperature": 1,
                    "extra_body": {
                        "reasoning": True,
                    },
                }
            },
            "proxy_routes": direct_proxy_routes,
        },
        runtime_config={},
    )

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model.model == "gpt-5.4-mini"
    assert model.custom_llm_provider == "openai"
    assert model.api_base == "https://example.com/v1"
    assert model.streaming is True
    assert model.temperature is None
    assert model.max_tokens is None
    assert model.model_kwargs == {
        "temperature": 1,
        "extra_body": {"reasoning": True},
    }
    assert model._default_params["temperature"] == 1


def test_agent_parameters_override_model_defaults_and_preserve_extension_null(direct_proxy_routes):
    """Agent 显式参数高于模型默认；扩展正文中的 JSON null 保持。"""
    model = build_model_from_provider(
        provider={
            "provider_id": "service", "model_id": "configured", "model": "work-model",
            "custom_llm_provider": "openai", "api_key": "test-key",
            "api_mode": _chat_api_mode(),
            "proxy_routes": direct_proxy_routes,
            "request_options": {"parameters": {
                "temperature": 1, "top_p": None, "max_tokens": None,
                "extra_body": {"vendor_feature": None},
            }},
        },
        runtime_config={"temperature": 0, "max_output_tokens": 512},
    )
    assert model.model_kwargs == {
        "temperature": 0, "max_tokens": 512, "extra_body": {"vendor_feature": None},
    }
    assert "top_p" not in model.model_kwargs


def test_build_model_resolves_direct_proxy_routes(provider_with_direct_network):
    """Provider 网络声明解析为模型实际使用的直连路由。"""
    model = build_model_from_provider(
        provider={
            "id": "backup_3",
            "custom_llm_provider": "openai",
            "model": "gpt-5.4-mini",
            "api_key": "test-key",
            "endpoint": "https://example.com/v1",
            "api_mode": _chat_api_mode(),
            **provider_with_direct_network,
        },
        runtime_config={},
    )

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model.proxy_routes is provider_with_direct_network["proxy_routes"]


def test_build_model_uses_provider_network_policy(provider_with_direct_network):
    """模型实例接收已解析的 Provider 网络策略。"""
    model = build_model_from_provider(
        provider={
            "id": "backup_3",
            "custom_llm_provider": "openai",
            "model": "DeepSeek-V4.1-Flash",
            "api_key": "test-key",
            "endpoint": "http://100.64.0.58:8079/v1",
            "api_mode": _chat_api_mode(),
            **provider_with_direct_network,
        },
        runtime_config={},
    )

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model.proxy_routes is provider_with_direct_network["proxy_routes"]


def test_direct_route_injects_trust_env_false_client(direct_proxy_routes):
    """显式路由策略创建不读取环境代理的 HTTP 客户端。"""
    model = BoxteamLiteLLMChatModel(
        model="openai/DeepSeek-V4.1-Flash",
        api_key="test-key",
        api_base="http://100.64.0.58:8079/v1",
        proxy_routes=direct_proxy_routes,
    )

    client = model._provider_http_client(is_async=False)
    assert client is not None
    # 缓存复用：同一实例重复获取应返回同一个客户端
    assert model._provider_http_client(is_async=False) is client


def test_build_model_rejects_unknown_provider_request_options():
    """provider.request_options 拼错字段时应立即暴露。"""
    with pytest.raises(ValueError, match="extra_body"):
        build_model_from_provider(
            provider={
                "id": "backup_3",
                "custom_llm_provider": "openai",
                "model": "gpt-5.4-mini",
                "api_key": "test-key",
                "endpoint": "https://example.com/v1",
                "api_mode": _chat_api_mode(),
                "request_options": {
                    "extra_body": {"reasoning": True},
                },
            },
            runtime_config={
                "temperature": 0,
                "top_p": 1,
                "max_output_tokens": 256,
            },
        )


@pytest.mark.asyncio
async def test_litellm_stream_sends_extra_body(
    monkeypatch: pytest.MonkeyPatch,
    empty_chat_completion_stream: _EmptyChatCompletionStream,
    direct_proxy_routes,
) -> None:
    """LiteLLM 流式调用保留正式参数和独立扩展正文。"""
    captured: dict[str, object] = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return empty_chat_completion_stream

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    model = BoxteamLiteLLMChatModel(
        model="openai/gpt-5.4-mini",
        api_key="test-key",
        api_base="https://example.com/v1",
        model_kwargs={
            "temperature": 1,
            "extra_body": {"vendor_feature": "low"},
        },
        invocation_purpose="tool_test",
        proxy_routes=direct_proxy_routes,
    )

    async def fake_acompletion_with_retry(self, **kwargs):
        return await FakeClient().chat.completions.create(**kwargs)

    monkeypatch.setattr(
        BoxteamLiteLLMChatModel,
        "acompletion_with_retry",
        fake_acompletion_with_retry,
    )

    chunks = [chunk async for chunk in model._astream([HumanMessage(content="hi")])]

    assert chunks == []
    assert captured["temperature"] == 1
    assert captured["extra_body"] == {"vendor_feature": "low"}
