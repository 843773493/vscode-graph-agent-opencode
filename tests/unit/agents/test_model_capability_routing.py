from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.model_capability_routing import (
    CapabilityRoutingMiddleware,
    ProviderModelCandidate,
)
from app.agents.provider_capabilities import (
    detect_required_capabilities_from_messages,
    parse_provider_capabilities,
)
from app.core.failures import is_provider_failure, mark_provider_failure
from app.core.model_request_context import (
    SummaryCandidateScope,
    summary_candidate_scope,
)
from app.core.turn_execution_scope import ScopeCancelledError


def _model() -> BaseChatModel:
    return MagicMock(spec=BaseChatModel)


def _candidate(
    provider_id: str,
    model: BaseChatModel,
    *capabilities: str,
) -> ProviderModelCandidate:
    return ProviderModelCandidate(
        provider_id=provider_id,
        model_id=provider_id,
        model=model,
        capabilities=frozenset({"text_input", *capabilities}),
    )


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_provider_failures_publish_facts_and_preserve_original_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    mode: str, exhausted: bool,
) -> None:
    primary, backup = _model(), _model()
    middleware = CapabilityRoutingMiddleware([_candidate("primary", primary), _candidate("backup", backup)])
    locations = ("/private/provider.sqlite", r"C:\private\provider.sqlite", r"\\target\private\provider.sqlite")
    original = "原始 Provider 故障: " + " | ".join(locations)
    first = mark_provider_failure(OSError(13, original, locations[0]))
    final = mark_provider_failure(RuntimeError(original))
    final.__cause__ = first
    events: list[tuple[str, dict[str, str]]] = []
    requested: list[BaseChatModel] = []

    def record(name: str, data: dict[str, str]) -> None:
        events.append((name, data))

    async def arecord(name: str, data: dict[str, str]) -> None:
        record(name, data)

    monkeypatch.setattr("app.agents.model_capability_routing.dispatch_custom_event", record)
    monkeypatch.setattr("app.agents.model_capability_routing.adispatch_custom_event", arecord)

    def handle(request: ModelRequest) -> ModelResponse:
        requested.append(request.model)
        if request.model is primary:
            raise first
        if exhausted:
            raise final
        return ModelResponse(result=[AIMessage(content="fallback 正常完成")])

    async def ahandle(request: ModelRequest) -> ModelResponse:
        return handle(request)

    request = ModelRequest(model=primary, messages=[HumanMessage(content="保留原始错误归属")])
    with caplog.at_level(logging.ERROR):
        try:
            result = (middleware.wrap_model_call(request, handle) if mode == "sync" else
                      await middleware.awrap_model_call(request, ahandle))
        except RuntimeError as error:
            assert exhausted and error is final and error.__cause__ is first
        else:
            assert not exhausted and isinstance(result, ModelResponse)
            assert result.result[0].content == "fallback 正常完成"
    assert requested == [primary, backup]
    assert [name for name, _ in events] == ["boxteam_model_failed"] * (2 if exhausted else 1)
    for (_, payload), provider, error in zip(events, ("primary", "backup"), (first, final), strict=False):
        assert payload["provider_id"] == provider and payload["model"] == provider
        assert payload["error_type"] == type(error).__name__
        assert "stage=model-provider" in payload["error"] and "provider_id=" + provider in payload["error"]
        assert "provider.sqlite" not in json.dumps(payload)
        assert any(record.name == "app.core.failures" and record.exc_info is not None
                   and record.exc_info[1] is error for record in caplog.records)
    assert "errno=13" in events[0][1]["error"] and str(first) in caplog.text


def test_provider_capabilities_accept_only_canonical_names() -> None:
    capabilities = parse_provider_capabilities(
        {
            "id": "canonical-provider",
            "api_mode": {
                "protocol": "chat_completions",
                "model_info": {
                    "supports_vision": True,
                    "supports_video_input": True,
                    "supports_audio_input": True,
                    "supports_reasoning": True,
                },
                "supports_reasoning": {"reasoning_content": True},
                "request_features": {"prompt_cache_key": True},
            },
        }
    )

    assert capabilities == {
        "text_input",
        "image_input",
        "video_input",
        "audio_input",
        "prompt_cache_key",
    }


def test_provider_capabilities_reject_unknown_model_info_field() -> None:
    with pytest.raises(ValueError, match="model_info.*unknown"):
        parse_provider_capabilities(
            {
                "id": "invalid-provider",
                "api_mode": {
                    "protocol": "chat_completions",
                    "model_info": {"unknown": True},
                    "supports_reasoning": {},
                },
            }
        )


def test_provider_capabilities_reject_old_flat_capabilities_field() -> None:
    with pytest.raises(TypeError, match="provider.api_mode 必须是对象"):
        parse_provider_capabilities(
            {
                "id": "invalid-provider",
                "capabilities": ["image_input"],
            }
        )


def test_provider_capabilities_reject_unknown_request_feature() -> None:
    with pytest.raises(ValueError, match="request_features.*unknown"):
        parse_provider_capabilities(
            {
                "id": "invalid-provider",
                "api_mode": {
                    "protocol": "chat_completions",
                    "model_info": {},
                    "supports_reasoning": {},
                    "request_features": {"unknown": True},
                },
            }
        )


def test_provider_capabilities_reject_legacy_reasoning_name_in_api_mode() -> None:
    with pytest.raises(ValueError, match="api_mode.*reasoning_content_replay"):
        parse_provider_capabilities(
            {
                "id": "invalid-provider",
                "api_mode": {
                    "protocol": "chat_completions",
                    "model_info": {},
                    "supports_reasoning": {
                        "reasoning_content_replay": True,
                    },
                },
            }
        )


@pytest.mark.parametrize(
    ("block_type", "expected"),
    [
        ("text", {"text_input"}),
        ("future_custom_block", {"text_input"}),
        ("image", {"text_input", "image_input"}),
        ("video_url", {"text_input", "video_input"}),
        ("input_audio", {"text_input", "audio_input"}),
    ],
)
def test_message_capability_scan_ignores_unknown_blocks_and_keeps_media_requirements(
    block_type: str,
    expected: set[str],
) -> None:
    capabilities = detect_required_capabilities_from_messages(
        [{"content": [{"type": block_type}]}]
    )

    assert capabilities == expected


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
async def test_text_request_falls_back_in_configured_order(monkeypatch: pytest.MonkeyPatch) -> None:
    primary_model = _model()
    fallback_model = _model()
    middleware = CapabilityRoutingMiddleware(
        [
            _candidate("primary", primary_model),
            _candidate("fallback", fallback_model, "image_input"),
        ]
    )
    requested_models: list[BaseChatModel] = []
    failed_events: list[tuple[str, dict[str, str]]] = []

    async def record_failed_event(name: str, data: dict[str, str]) -> None:
        failed_events.append((name, data))

    monkeypatch.setattr(
        "app.agents.model_capability_routing.adispatch_custom_event",
        record_failed_event,
    )

    async def handler(request: ModelRequest) -> AIMessage:
        requested_models.append(request.model)
        if request.model is primary_model:
            raise mark_provider_failure(RuntimeError("primary failed"))
        return AIMessage(content="ok")

    request = ModelRequest(
        model=primary_model,
        messages=[HumanMessage(content="只处理文本")],
    )

    await middleware.awrap_model_call(request, handler)

    assert requested_models == [primary_model, fallback_model]
    assert failed_events == [
        (
            "boxteam_model_failed",
            {
                "provider_id": "primary",
                "model": "primary",
                "error_type": "RuntimeError",
                "error": "模型候选调用失败: provider_id=primary, model=primary: stage=model-provider, error=RuntimeError",
            },
        )
    ]


@pytest.mark.asyncio
async def test_reasoning_only_response_falls_back_to_next_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_model = _model()
    fallback_model = _model()
    middleware = CapabilityRoutingMiddleware(
        [
            _candidate("primary", primary_model),
            _candidate("fallback", fallback_model),
        ]
    )
    requested_models: list[BaseChatModel] = []
    failed_events: list[tuple[str, dict[str, str]]] = []

    async def record_failed_event(name: str, data: dict[str, str]) -> None:
        failed_events.append((name, data))

    monkeypatch.setattr(
        "app.agents.model_capability_routing.adispatch_custom_event",
        record_failed_event,
    )

    async def handler(request: ModelRequest) -> AIMessage:
        requested_models.append(request.model)
        if request.model is primary_model:
            return AIMessage(
                content=[{"type": "reasoning", "reasoning": "内部推理"}]
            )
        return AIMessage(content="ok")

    request = ModelRequest(
        model=primary_model,
        messages=[HumanMessage(content="只处理文本")],
    )

    response = await middleware.awrap_model_call(request, handler)

    assert isinstance(response, AIMessage)
    assert response.text == "ok"
    assert requested_models == [primary_model, fallback_model]
    assert failed_events == [
        (
            "boxteam_model_failed",
            {
                "provider_id": "primary",
                "model": "primary",
                "error_type": "RuntimeError",
                "error": "模型只返回内部推理，没有用户可见正文或工具调用。 provider_id=primary model=primary",
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("provider_failure", [False, True])
async def test_summary_reasoning_response_reaches_summary_owner(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool, provider_failure: bool,
) -> None:
    primary, fallback = _model(), _model()
    middleware = CapabilityRoutingMiddleware([
        _candidate("primary", primary), _candidate("fallback", fallback),
    ])
    response = ModelResponse(result=[AIMessage(content=[{"type": "reasoning", "reasoning": "内部推理"}])])
    requested: list[BaseChatModel] = []
    failed_events: list[str] = []

    def record_failure(name: str, _data: dict[str, str]) -> None:
        failed_events.append(name)

    async def arecord_failure(name: str, data: dict[str, str]) -> None:
        record_failure(name, data)

    monkeypatch.setattr("app.agents.model_capability_routing.dispatch_custom_event", record_failure)
    monkeypatch.setattr("app.agents.model_capability_routing.adispatch_custom_event", arecord_failure)

    def handler(request: ModelRequest) -> ModelResponse:
        requested.append(request.model)
        if provider_failure and request.model is primary:
            raise mark_provider_failure(RuntimeError("上游请求失败"))
        return response

    async def ahandler(request: ModelRequest) -> ModelResponse:
        return handler(request)

    request = ModelRequest(model=primary, messages=[HumanMessage(content="生成摘要")])
    with summary_candidate_scope(SummaryCandidateScope(0, "initial", True, False, canonical_messages=tuple(request.messages))):
        actual = await middleware.awrap_model_call(request, ahandler) if asynchronous else middleware.wrap_model_call(request, handler)
    assert actual is response
    assert requested == ([primary, fallback] if provider_failure else [primary])
    assert failed_events == (["boxteam_model_failed"] if provider_failure else [])


@pytest.mark.asyncio
async def test_unmarked_local_error_is_not_retried_or_reported_as_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_model = _model()
    fallback_model = _model()
    middleware = CapabilityRoutingMiddleware(
        [
            _candidate("primary", primary_model),
            _candidate("fallback", fallback_model),
        ]
    )
    local_error = ValueError("本地 payload 校验失败")
    requested_models: list[BaseChatModel] = []
    failed_events: list[tuple[str, dict[str, str]]] = []

    async def record_failed_event(name: str, data: dict[str, str]) -> None:
        failed_events.append((name, data))

    monkeypatch.setattr(
        "app.agents.model_capability_routing.adispatch_custom_event",
        record_failed_event,
    )

    async def handler(request: ModelRequest) -> AIMessage:
        requested_models.append(request.model)
        raise local_error

    request = ModelRequest(
        model=primary_model,
        messages=[HumanMessage(content="只处理文本")],
    )

    with pytest.raises(ValueError) as raised:
        await middleware.awrap_model_call(request, handler)

    assert raised.value is local_error
    assert not is_provider_failure(local_error)
    assert requested_models == [primary_model]
    assert failed_events == []


def test_sync_router_falls_back_for_provider_failure_and_preserves_final_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_model = _model()
    fallback_model = _model()
    middleware = CapabilityRoutingMiddleware(
        [
            _candidate("primary", primary_model),
            _candidate("fallback", fallback_model),
        ]
    )
    first_error = mark_provider_failure(RuntimeError("primary failed"))
    cause = ValueError("upstream detail")
    final_error = RuntimeError("fallback failed")
    final_error.status_code = 503
    final_error.__cause__ = cause
    mark_provider_failure(final_error)
    requested_models: list[BaseChatModel] = []
    failed_events: list[tuple[str, dict[str, str]]] = []

    def record_failed_event(name: str, data: dict[str, str]) -> None:
        failed_events.append((name, data))

    monkeypatch.setattr(
        "app.agents.model_capability_routing.dispatch_custom_event",
        record_failed_event,
    )

    def handler(request: ModelRequest) -> AIMessage:
        requested_models.append(request.model)
        if request.model is primary_model:
            raise first_error
        raise final_error

    request = ModelRequest(
        model=primary_model,
        messages=[HumanMessage(content="只处理文本")],
    )

    with pytest.raises(RuntimeError) as raised:
        middleware.wrap_model_call(request, handler)

    assert raised.value is final_error
    assert raised.value.status_code == 503
    assert raised.value.__cause__ is cause
    assert requested_models == [primary_model, fallback_model]
    assert [event for event, _data in failed_events] == [
        "boxteam_model_failed",
        "boxteam_model_failed",
    ]
    assert failed_events[-1][1] == {
        "provider_id": "fallback",
        "model": "fallback",
        "error_type": "RuntimeError",
        "error": "模型候选调用失败: provider_id=fallback, model=fallback: stage=model-provider, error=RuntimeError",
    }


@pytest.mark.asyncio
async def test_scope_cancellation_is_not_retried_on_fallback_provider() -> None:
    primary_model = _model()
    fallback_model = _model()
    middleware = CapabilityRoutingMiddleware(
        [
            _candidate("backup_3", primary_model),
            _candidate("backup_4", fallback_model),
        ]
    )
    requested_models: list[BaseChatModel] = []

    async def handler(request: ModelRequest) -> AIMessage:
        requested_models.append(request.model)
        raise ScopeCancelledError("scope_deadline_exceeded")

    request = ModelRequest(
        model=primary_model,
        messages=[HumanMessage(content="在预算内完成")],
    )

    with pytest.raises(ScopeCancelledError, match="scope_deadline_exceeded"):
        await middleware.awrap_model_call(request, handler)

    assert requested_models == [primary_model]


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
