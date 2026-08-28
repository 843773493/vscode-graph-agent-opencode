from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage

from app.agents.agent_factory import build_model_from_provider
from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.core.model_delta_context import (
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)
from app.core.turn_execution_scope import (
    ScopeCancelledError,
    TurnExecutionScope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)


def _chat_api_mode() -> dict[str, object]:
    return {
        "protocol": "chat_completions",
        "model_info": {"supports_function_calling": True, "supports_reasoning": True},
        "supports_reasoning": {"reasoning_content": True},
    }


class AsyncChunkStream:
    def __init__(
        self,
        chunks: list[dict[str, Any]],
        *,
        received_finish_reason: str | None,
    ) -> None:
        self._chunks = iter(chunks)
        self.received_finish_reason = received_finish_reason
        self.intermittent_finish_reason = None

    def __aiter__(self) -> AsyncChunkStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._chunks)
        except StopIteration as error:
            raise StopAsyncIteration from error


def _text_chunk(text: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "delta": {"content": text},
                "finish_reason": None,
            }
        ]
    }


def _reasoning_chunk(text: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "delta": {"reasoning_content": text},
                "finish_reason": None,
            }
        ]
    }


def _synthetic_finish_chunk() -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ]
    }


def _tool_chunk(arguments: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_partial",
                            "type": "function",
                            "function": {
                                "name": "get_team_board",
                                "arguments": arguments,
                            },
                        }
                    ]
                },
                "finish_reason": None,
            }
        ]
    }


def test_build_model_omits_unspecified_generation_parameters():
    """最小 provider 配置不得人为限制采样策略或输出长度。"""
    model = build_model_from_provider(
        provider={
            "id": "primary",
            "custom_llm_provider": "openai",
            "model": "work-model",
            "api_key": "test-key",
            "endpoint": "https://example.com/v1",
            "api_mode": _chat_api_mode(),
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
        received_finish_reason = "stop"
        intermittent_finish_reason = None

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
            "api_mode": _chat_api_mode(),
        },
        runtime_config={},
    )

    chunks = [chunk async for chunk in model._astream([HumanMessage(content="hi")])]

    assert chunks == []
    assert "temperature" not in captured
    assert "top_p" not in captured
    assert "max_tokens" not in captured


@pytest.mark.asyncio
async def test_raw_semantic_delta_reaches_hook_before_langchain_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reasoning/text 在 LangChain 聚合前按原始语义顺序进入消息流 hook。"""
    model = BoxteamLiteLLMChatModel(
        model="test-model",
        api_key="test-key",
        custom_llm_provider="openai",
        streaming=True,
    )
    observed: list[str] = []

    class RecordingSink:
        async def accept_message_chunk(self, chunk: AIMessageChunk) -> None:
            observed.append(f"hook:{chunk.content[0]['type']}")

    async def fake_acompletion_with_retry(self, **kwargs):
        del self, kwargs
        return AsyncChunkStream(
            [
                _reasoning_chunk("先分析"),
                _text_chunk("完成"),
                _synthetic_finish_chunk(),
            ],
            received_finish_reason="stop",
        )

    monkeypatch.setattr(
        BoxteamLiteLLMChatModel,
        "acompletion_with_retry",
        fake_acompletion_with_retry,
    )
    token = set_current_model_delta_sink(RecordingSink())
    try:
        chunks = []
        async for chunk in model._astream([HumanMessage(content="hi")]):
            observed.append(f"yield:{chunk.message.content[0]['type']}")
            chunks.append(chunk)
    finally:
        reset_current_model_delta_sink(token)

    assert observed == [
        "hook:reasoning_content",
        "yield:reasoning_content",
        "hook:text",
        "yield:text",
    ]
    assert len(chunks) == 2


@pytest.mark.asyncio
async def test_async_delta_hook_cancellation_stops_upstream_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用户取消消息流时，hook 的 CancelledError 不被 provider 吞掉。"""
    model = BoxteamLiteLLMChatModel(
        model="test-model",
        api_key="test-key",
        custom_llm_provider="openai",
        streaming=True,
    )
    stream = AsyncChunkStream(
        [_text_chunk("不会继续"), _synthetic_finish_chunk()],
        received_finish_reason="stop",
    )

    async def fake_acompletion_with_retry(self, **kwargs):
        del self, kwargs
        return stream

    class CancellingSink:
        async def accept_message_chunk(self, chunk: AIMessageChunk) -> None:
            del chunk
            raise asyncio.CancelledError

    monkeypatch.setattr(
        BoxteamLiteLLMChatModel,
        "acompletion_with_retry",
        fake_acompletion_with_retry,
    )
    token = set_current_model_delta_sink(CancellingSink())
    try:
        with pytest.raises(asyncio.CancelledError):
            async for _ in model._astream([HumanMessage(content="hi")]):
                pass
    finally:
        reset_current_model_delta_sink(token)


@pytest.mark.asyncio
async def test_async_reader_cancels_pending_litellm_anext_and_closes_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = BoxteamLiteLLMChatModel(
        model="test-model",
        api_key="test-key",
        custom_llm_provider="openai",
        streaming=True,
    )

    class BlockingStream:
        def __init__(self) -> None:
            self.read_started = asyncio.Event()
            self.closed = asyncio.Event()
            self.read_cancelled = False
            self.close_count = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.read_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.read_cancelled = True
                raise
            raise AssertionError("测试流不应自行返回 chunk")

        async def aclose(self) -> None:
            self.close_count += 1
            self.closed.set()

    stream = BlockingStream()

    async def fake_acompletion_with_retry(self, **kwargs):
        del self, kwargs
        return stream

    monkeypatch.setattr(
        BoxteamLiteLLMChatModel,
        "acompletion_with_retry",
        fake_acompletion_with_retry,
    )
    scope = TurnExecutionScope("stream_litellm_reader")
    scope_token = set_current_turn_execution_scope(scope)
    try:
        model_stream = model._astream([HumanMessage(content="hi")])
        read_task = asyncio.create_task(model_stream.__anext__())
        await stream.read_started.wait()
        assert await scope.cancel("user_requested") is True
        with pytest.raises(ScopeCancelledError, match="user_requested"):
            await read_task
        await stream.closed.wait()
    finally:
        reset_current_turn_execution_scope(scope_token)
        await scope.close()

    assert stream.read_cancelled is True
    assert stream.close_count == 1


def test_build_model_passes_provider_request_overrides():
    """provider 请求覆盖应优先于 agent 通用参数并透传给 LiteLLM。"""
    model = build_model_from_provider(
        provider={
            "id": "backup_3",
            "custom_llm_provider": "openai",
            "model": "gpt-5.4-mini",
            "api_key": "test-key",
            "endpoint": "https://example.com/v1",
            "api_mode": _chat_api_mode(),
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
            "api_mode": _chat_api_mode(),
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
async def test_litellm_stream_sends_extra_body(monkeypatch):
    """LiteLLM 流式调用应携带 overrides 中的顶层参数和 extra_body。"""
    captured: dict[str, object] = {}

    class EmptyStream:
        received_finish_reason = "stop"
        intermittent_finish_reason = None

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


@pytest.mark.asyncio
async def test_incomplete_stream_is_not_retried_after_semantic_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """语义 delta 后提前 EOF 不得静默重试，已收到内容随失败事实保留。"""
    streams = [
        AsyncChunkStream(
            [_text_chunk("半截文本"), _synthetic_finish_chunk()],
            received_finish_reason=None,
        ),
        AsyncChunkStream(
            [_text_chunk("完整文本"), _synthetic_finish_chunk()],
            received_finish_reason="stop",
        ),
    ]
    request_count = 0

    async def fake_acompletion_with_retry(self, **kwargs):
        nonlocal request_count
        stream = streams[request_count]
        request_count += 1
        return stream

    monkeypatch.setattr(
        BoxteamLiteLLMChatModel,
        "acompletion_with_retry",
        fake_acompletion_with_retry,
    )
    model = BoxteamLiteLLMChatModel(
        model="test-model",
        api_key="test-key",
        custom_llm_provider="openai",
        max_retries=1,
        streaming=True,
    )

    published_chunks = []
    with pytest.raises(RuntimeError, match="不会静默重试"):
        async for chunk in model._astream([HumanMessage(content="hi")]):
            published_chunks.append(chunk)
    text = "".join(
        block["text"]
        for chunk in published_chunks
        for block in chunk.message.content
        if isinstance(block, dict) and block.get("type") == "text"
    )

    assert request_count == 1
    assert text == "半截文本"


@pytest.mark.asyncio
async def test_synthetic_finish_reason_exposes_partial_tool_call_then_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LiteLLM 合成的 stop 不得静默重试已经暴露的半截工具调用。"""
    request_count = 0

    async def fake_acompletion_with_retry(self, **kwargs):
        nonlocal request_count
        request_count += 1
        return AsyncChunkStream(
            [
                _tool_chunk('{"team_id":"team_partial'),
                _synthetic_finish_chunk(),
            ],
            received_finish_reason=None,
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
        provider_id="test-provider",
        max_retries=2,
        streaming=True,
    )
    published_chunks = []

    with pytest.raises(
        RuntimeError,
        match=r"真实 finish_reason 前提前结束.*不会静默重试",
    ):
        async for chunk in model._astream([HumanMessage(content="hi")]):
            published_chunks.append(chunk)

    assert request_count == 1
    assert len(published_chunks) == 1
