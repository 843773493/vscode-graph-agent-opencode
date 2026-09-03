from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from app.agents.agent_factory import build_model_from_provider
from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel


def _provider() -> dict[str, Any]:
    return {
        "id": "anthropic-test",
        "endpoint": "https://example.com/anthropic",
        "model": "claude-test",
        "api_key": "test-key",
        "custom_llm_provider": "anthropic",
        "api_mode": {
            "protocol": "anthropic_messages",
            "model_info": {
                "supports_reasoning": True,
                "supports_vision": True,
            },
            "supports_reasoning": {
                "thinking_blocks": {
                    "thinking": True,
                    "redacted_thinking": True,
                }
            },
        },
    }


def test_anthropic_messages_mode_uses_litellm_chat_model() -> None:
    model = build_model_from_provider(_provider(), {})

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model.custom_llm_provider == "anthropic"
    assert model.api_base == "https://example.com/anthropic"
    assert model.thinking_blocks_replay is True
    assert model.image_input_replay is True


@pytest.mark.asyncio
async def test_anthropic_messages_stream_uses_litellm_acompletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class RawStream:
        received_finish_reason = "stop"
        intermittent_finish_reason = None

        def __init__(self) -> None:
            self._chunks = iter(
                [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "完成"},
                                "finish_reason": None,
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                            }
                        ]
                    },
                ]
            )

        def __aiter__(self) -> RawStream:
            return self

        async def __anext__(self) -> dict[str, Any]:
            try:
                return next(self._chunks)
            except StopIteration as error:
                raise StopAsyncIteration from error

    async def fake_acompletion_with_retry(
        _self: BoxteamLiteLLMChatModel,
        **kwargs: Any,
    ) -> RawStream:
        captured.update(kwargs)
        return RawStream()

    monkeypatch.setattr(
        BoxteamLiteLLMChatModel,
        "acompletion_with_retry",
        fake_acompletion_with_retry,
    )
    model = build_model_from_provider(_provider(), {})

    chunks = [
        chunk
        async for chunk in model._astream(
            [
                HumanMessage(
                    content=[
                        {"type": "text", "text": "请看图"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/webp;base64,preview"
                            },
                        },
                    ]
                )
            ]
        )
    ]

    assert chunks
    assert captured["custom_llm_provider"] == "anthropic"
    assert captured["messages"][0]["content"] == [
        {"type": "text", "text": "请看图"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/webp;base64,preview"},
        },
    ]
