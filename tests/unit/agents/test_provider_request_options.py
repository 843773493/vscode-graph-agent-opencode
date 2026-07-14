from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from app.agents.agent_factory import build_model_from_provider
from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel


def test_build_model_omits_unspecified_generation_parameters():
    """最小 provider 配置不得人为限制采样策略或输出长度。"""
    model = build_model_from_provider(
        provider={
            "id": "primary",
            "custom_llm_provider": "openai",
            "model": "work-model",
            "api_key": "test-key",
            "endpoint": "https://example.com/v1",
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
) -> None:
    captured: dict[str, object] = {}

    class EmptyStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def fake_acompletion_with_retry(self, **kwargs):
        captured.update(kwargs)
        return EmptyStream()

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
        },
        runtime_config={},
    )

    chunks = [chunk async for chunk in model._astream([HumanMessage(content="hi")])]

    assert chunks == []
    assert "temperature" not in captured
    assert "top_p" not in captured
    assert "max_tokens" not in captured


def test_build_model_passes_provider_request_overrides():
    """provider 请求覆盖应优先于 agent 通用参数并透传给 LiteLLM。"""
    model = build_model_from_provider(
        provider={
            "id": "backup_3",
            "custom_llm_provider": "openai",
            "model": "gpt-5.4-mini",
            "api_key": "test-key",
            "endpoint": "https://example.com/v1",
            "request_options": {
                "overrides": {
                    "temperature": 1,
                    "extra_body": {
                        "reasoning": True,
                    },
                }
            },
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


def test_request_overrides_replace_output_parameter_without_model_branch():
    """特殊参数完全由配置表达，不依赖 provider 或模型名称判断。"""
    model = build_model_from_provider(
        provider={
            "id": "backup_3",
            "custom_llm_provider": "openai",
            "model": "vendor-special-model",
            "api_key": "test-key",
            "endpoint": "https://www.cctq.ai/v1",
            "request_options": {
                "overrides": {
                    "temperature": 1,
                    "max_tokens": None,
                    "extra_body": {
                        "reasoning_effort": "low",
                        "max_output_tokens": 512,
                    },
                }
            },
        },
        runtime_config={
            "temperature": 0,
            "top_p": 1,
            "max_output_tokens": 512,
        },
    )

    assert model.max_tokens is None
    assert model.model_kwargs == {
        "temperature": 1,
        "top_p": 1,
        "max_tokens": None,
        "extra_body": {
            "reasoning_effort": "low",
            "max_output_tokens": 512,
        },
    }
    assert model._default_params["temperature"] == 1
    assert model._default_params["max_tokens"] is None


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
async def test_litellm_stream_sends_extra_body(monkeypatch):
    """LiteLLM 流式调用应携带 overrides 中的顶层参数和 extra_body。"""
    captured: dict[str, object] = {}

    class EmptyStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return EmptyStream()

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
            "extra_body": {"reasoning_effort": "low"},
        },
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
    assert captured["extra_body"] == {"reasoning_effort": "low"}
