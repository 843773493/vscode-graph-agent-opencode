from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.model_capability_routing import (
    CapabilityRoutingMiddleware,
    ProviderModelCandidate,
)
from app.agents.provider_capabilities import parse_provider_capabilities


def _model() -> BaseChatModel:
    return MagicMock(spec=BaseChatModel)


def _candidate(
    provider_id: str,
    model: BaseChatModel,
    *capabilities: str,
) -> ProviderModelCandidate:
    return ProviderModelCandidate(
        provider_id=provider_id,
        model=model,
        capabilities=frozenset({"text_input", *capabilities}),
    )


def test_provider_capabilities_accept_only_canonical_names() -> None:
    capabilities = parse_provider_capabilities(
        {
            "id": "canonical-provider",
            "capabilities": [
                "image_input",
                "video_input",
                "audio_input",
                "reasoning_content_replay",
                "prompt_cache_key",
            ],
        }
    )

    assert capabilities == {
        "text_input",
        "image_input",
        "video_input",
        "audio_input",
        "reasoning_content_replay",
        "prompt_cache_key",
    }


@pytest.mark.parametrize("capability", ["vision", "image", "Image_input", "unknown"])
def test_provider_capabilities_reject_legacy_and_unknown_names(
    capability: str,
) -> None:
    with pytest.raises(
        ValueError,
        match=rf"不支持的 capability: '{capability}'",
    ):
        parse_provider_capabilities(
            {
                "id": "invalid-provider",
                "capabilities": [capability],
            }
        )


@pytest.mark.asyncio
async def test_direct_image_attachment_skips_text_only_model() -> None:
    text_model = _model()
    image_model = _model()
    middleware = CapabilityRoutingMiddleware(
        [
            _candidate("text", text_model),
            _candidate("image", image_model, "image_input"),
        ]
    )
    requested_models: list[BaseChatModel] = []

    async def handler(request: ModelRequest) -> AIMessage:
        requested_models.append(request.model)
        return AIMessage(content="ok")

    request = ModelRequest(
        model=text_model,
        messages=[
            HumanMessage(
                content=[
                    {"type": "text", "text": "描述图片"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,eA=="},
                    },
                ]
            )
        ],
    )

    await middleware.awrap_model_call(request, handler)

    assert requested_models == [image_model]


@pytest.mark.asyncio
async def test_tool_returned_image_uses_same_context_capability_routing() -> None:
    text_model = _model()
    image_model = _model()
    middleware = CapabilityRoutingMiddleware(
        [
            _candidate("text", text_model),
            _candidate("image", image_model, "image_input"),
        ]
    )
    requested_models: list[BaseChatModel] = []

    async def handler(request: ModelRequest) -> AIMessage:
        requested_models.append(request.model)
        return AIMessage(content="ok")

    request = ModelRequest(
        model=text_model,
        messages=[
            HumanMessage(content="读取并描述图片"),
            ToolMessage(
                content_blocks=[
                    {
                        "type": "image",
                        "base64": "eA==",
                        "mime_type": "image/png",
                    }
                ],
                tool_call_id="read-file-call",
            ),
        ],
    )

    await middleware.awrap_model_call(request, handler)

    assert requested_models == [image_model]


@pytest.mark.asyncio
async def test_text_request_falls_back_in_configured_order() -> None:
    primary_model = _model()
    fallback_model = _model()
    middleware = CapabilityRoutingMiddleware(
        [
            _candidate("primary", primary_model),
            _candidate("fallback", fallback_model, "image_input"),
        ]
    )
    requested_models: list[BaseChatModel] = []

    async def handler(request: ModelRequest) -> AIMessage:
        requested_models.append(request.model)
        if request.model is primary_model:
            raise RuntimeError("primary failed")
        return AIMessage(content="ok")

    request = ModelRequest(
        model=primary_model,
        messages=[HumanMessage(content="只处理文本")],
    )

    await middleware.awrap_model_call(request, handler)

    assert requested_models == [primary_model, fallback_model]


@pytest.mark.asyncio
async def test_missing_context_capability_fails_before_model_request() -> None:
    text_model = _model()
    middleware = CapabilityRoutingMiddleware([_candidate("text", text_model)])
    handler_called = False

    async def handler(request: ModelRequest) -> AIMessage:
        nonlocal handler_called
        handler_called = True
        return AIMessage(content="unexpected")

    request = ModelRequest(
        model=text_model,
        messages=[
            ToolMessage(
                content_blocks=[
                    {
                        "type": "image",
                        "base64": "eA==",
                        "mime_type": "image/png",
                    }
                ],
                tool_call_id="read-file-call",
            )
        ],
    )

    with pytest.raises(RuntimeError, match="image_input"):
        await middleware.awrap_model_call(request, handler)

    assert handler_called is False
