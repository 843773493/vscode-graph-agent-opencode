from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from app.agents.agent_factory import _build_model_from_provider
from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel


def test_build_model_passes_provider_request_options():
    """provider.request_options 应透传到模型实例，而不是写死到某个模型分支。"""
    model = _build_model_from_provider(
        {
            "id": "backup_3",
            "interface": "opencode_zen",
            "model": "gpt-5.4-mini",
            "api_key": "test-key",
            "endpoint": "https://example.com/v1",
            "request_options": {
                "extra_body": {
                    "reasoning_effort": "low",
                }
            },
        },
        {
            "temperature": 0,
            "top_p": 1,
            "max_output_tokens": 256,
        },
    )

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model.model == "openai/gpt-5.4-mini"
    assert model.api_base == "https://example.com/v1"
    assert model.streaming is True
    assert model.model_kwargs == {"extra_body": {"reasoning_effort": "low"}}


def test_build_model_rejects_unknown_provider_request_options():
    """provider.request_options 拼错字段时应立即暴露。"""
    with pytest.raises(ValueError, match="unknown_option"):
        _build_model_from_provider(
            {
                "id": "backup_3",
                "interface": "opencode_zen",
                "model": "gpt-5.4-mini",
                "api_key": "test-key",
                "endpoint": "https://example.com/v1",
                "request_options": {
                    "unknown_option": True,
                },
            },
            {
                "temperature": 0,
                "top_p": 1,
                "max_output_tokens": 256,
            },
        )


@pytest.mark.asyncio
async def test_litellm_stream_sends_extra_body(monkeypatch):
    """LiteLLM 流式调用应携带 provider.request_options.extra_body。"""
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
        model_kwargs={"extra_body": {"reasoning_effort": "low"}},
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
    assert captured["extra_body"] == {"reasoning_effort": "low"}
