from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_litellm import ChatLiteLLM

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
from app.runtime.provider_errors import is_provider_failure


class _ChatCompletionChunkStream:
    def __init__(
        self,
        chunks: list[dict[str, Any]],
        *,
        received_finish_reason: str | None,
    ) -> None:
        self._chunks = iter(chunks)
        self.received_finish_reason = received_finish_reason
        self.intermittent_finish_reason = None

    def __aiter__(self) -> _ChatCompletionChunkStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._chunks)
        except StopIteration as error:
            raise StopAsyncIteration from error


def _chat_text_chunk(text: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "delta": {"content": text},
                "finish_reason": None,
            }
        ]
    }


def _chat_reasoning_chunk(text: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "delta": {"reasoning_content": text},
                "finish_reason": None,
            }
        ]
    }


def _chat_synthetic_finish_chunk() -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ]
    }


def _chat_tool_call_chunk(arguments: str) -> dict[str, Any]:
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
        invocation_purpose="tool_test",
    )
    observed: list[str] = []

    class RecordingSink:
        async def accept_message_chunk(self, chunk: AIMessageChunk) -> None:
            observed.append(f"hook:{chunk.content[0]['type']}")

    async def fake_acompletion_with_retry(self, **kwargs):
        del self, kwargs
        return _ChatCompletionChunkStream(
            [
                _chat_reasoning_chunk("先分析"),
                _chat_text_chunk("完成"),
                _chat_synthetic_finish_chunk(),
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
        invocation_purpose="tool_test",
    )
    stream = _ChatCompletionChunkStream(
        [_chat_text_chunk("不会继续"), _chat_synthetic_finish_chunk()],
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
        invocation_purpose="tool_test",
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


@pytest.mark.asyncio
async def test_incomplete_stream_is_not_retried_after_semantic_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """语义 delta 后提前 EOF 不得静默重试，已收到内容随失败事实保留。"""
    streams = [
        _ChatCompletionChunkStream(
            [_chat_text_chunk("半截文本"), _chat_synthetic_finish_chunk()],
            received_finish_reason=None,
        ),
        _ChatCompletionChunkStream(
            [_chat_text_chunk("完整文本"), _chat_synthetic_finish_chunk()],
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
        invocation_purpose="tool_test",
    )

    published_chunks = []
    with pytest.raises(RuntimeError, match="不会静默重试") as raised:
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
    assert is_provider_failure(raised.value)


@pytest.mark.asyncio
async def test_chat_provider_call_marks_and_preserves_original_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UpstreamError(RuntimeError):
        status_code = 503

    cause = ValueError("transport detail")
    error = UpstreamError("upstream unavailable")
    error.__cause__ = cause

    async def fail_upstream(self, **_kwargs):
        del self
        raise error

    monkeypatch.setattr(ChatLiteLLM, "acompletion_with_retry", fail_upstream)
    model = BoxteamLiteLLMChatModel(
        model="test-model",
        api_key="test-key",
        custom_llm_provider="openai",
        invocation_purpose="tool_test",
    )

    with pytest.raises(UpstreamError) as raised:
        await model.acompletion_with_retry(messages=[])

    assert raised.value is error
    assert raised.value.status_code == 503
    assert raised.value.__cause__ is cause
    assert is_provider_failure(error)


@pytest.mark.asyncio
async def test_chat_stream_iteration_marks_original_upstream_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UpstreamError(RuntimeError):
        status_code = 429

    error = UpstreamError("stream disconnected")

    class FailingStream:
        received_finish_reason = None
        intermittent_finish_reason = None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise error

    async def fake_acompletion_with_retry(self, **_kwargs):
        return FailingStream()

    monkeypatch.setattr(
        BoxteamLiteLLMChatModel,
        "acompletion_with_retry",
        fake_acompletion_with_retry,
    )
    model = BoxteamLiteLLMChatModel(
        model="test-model",
        api_key="test-key",
        custom_llm_provider="openai",
        invocation_purpose="tool_test",
    )

    with pytest.raises(UpstreamError) as raised:
        async for _chunk in model._astream([HumanMessage(content="hi")]):
            pass

    assert raised.value is error
    assert is_provider_failure(error)


@pytest.mark.asyncio
async def test_chat_stream_iterator_acquisition_marks_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("upstream stream is not iterable")

    class InvalidStream:
        received_finish_reason = None
        intermittent_finish_reason = None

        def __aiter__(self):
            raise error

    async def fake_acompletion_with_retry(self, **_kwargs):
        return InvalidStream()

    monkeypatch.setattr(
        BoxteamLiteLLMChatModel,
        "acompletion_with_retry",
        fake_acompletion_with_retry,
    )
    model = BoxteamLiteLLMChatModel(
        model="test-model",
        api_key="test-key",
        custom_llm_provider="openai",
        invocation_purpose="tool_test",
    )

    with pytest.raises(RuntimeError) as raised:
        async for _chunk in model._astream([HumanMessage(content="hi")]):
            pass

    assert raised.value is error
    assert is_provider_failure(error)


def test_sync_chat_stream_iterator_acquisition_marks_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("upstream stream is not iterable")

    class InvalidStream:
        received_finish_reason = None
        intermittent_finish_reason = None

        def __iter__(self):
            raise error

        def close(self) -> None:
            return None

    def fake_completion_with_retry(self, **_kwargs):
        return InvalidStream()

    monkeypatch.setattr(
        BoxteamLiteLLMChatModel,
        "completion_with_retry",
        fake_completion_with_retry,
    )
    model = BoxteamLiteLLMChatModel(
        model="test-model",
        api_key="test-key",
        custom_llm_provider="openai",
        invocation_purpose="tool_test",
    )

    with pytest.raises(RuntimeError) as raised:
        next(model._stream([HumanMessage(content="hi")]))

    assert raised.value is error
    assert is_provider_failure(error)


@pytest.mark.asyncio
async def test_synthetic_finish_reason_exposes_partial_tool_call_then_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LiteLLM 合成的 stop 不得静默重试已经暴露的半截工具调用。"""
    request_count = 0

    async def fake_acompletion_with_retry(self, **kwargs):
        nonlocal request_count
        request_count += 1
        return _ChatCompletionChunkStream(
            [
                _chat_tool_call_chunk('{"team_id":"team_partial'),
                _chat_synthetic_finish_chunk(),
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
        invocation_purpose="tool_test",
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
