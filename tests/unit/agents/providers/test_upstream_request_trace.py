"""upstream attempt 记录不得随进程内调用次数累积。

历史缺陷：trace 回调挂在 LiteLLM 全局回调表上，而该表只增不减，导致第 N 次
模型调用的日志里出现 N 条 attempt（见 ses_019c546afc9f7b4b8a2048025ee8bebc）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_litellm import ChatLiteLLM
from litellm.types.llms.openai import ResponsesAPIResponse
from openai.types.responses.response import IncompleteDetails

from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.providers.openai_responses import (
    BoxteamOpenAIResponsesModel,
    ResponsesStreamOpenTimeoutError,
)
from app.agents.upstream_request_trace import (
    begin_upstream_capture,
    end_upstream_capture,
    record_upstream_error,
)
from app.runtime.model_stream import ModelStreamTransportController
from app.runtime.provider_errors import is_provider_failure
from app.testing.model_stream import (
    load_model_stream_config,
)

CHAT_CONFIG_PATH = (
    Path.cwd() / "configs" / "tests" / "model_stream" / "model_stream_chat_basic.jsonc"
)
RESPONSES_CONFIG_PATH = (
    Path.cwd() / "configs" / "tests" / "model_stream" / "model_stream_responses.jsonc"
)


async def _capture(model, messages) -> list[dict]:
    token = begin_upstream_capture()
    async for _chunk in model.astream(messages):
        pass
    return end_upstream_capture(token)


@pytest.mark.asyncio
async def test_chat_attempts_do_not_accumulate_across_calls() -> None:
    config = load_model_stream_config(CHAT_CONFIG_PATH)
    controller = ModelStreamTransportController.configure(config)
    if controller is None:
        raise RuntimeError("model stream replay controller 未安装")
    try:
        model = BoxteamLiteLLMChatModel(
            model="openai/big-pickle",
            api_key="test-key",
            api_base="https://opencode.ai/zen/v1",
            custom_llm_provider="openai",
            provider_id="primary",
            streaming=True,
            invocation_purpose="tool_test",
        )
        for round_index in range(4):
            attempts = await _capture(
                model,
                [SystemMessage(content="s"), HumanMessage(content=f"第 {round_index} 轮")],
            )
            assert len(attempts) == 1, f"第 {round_index} 轮 attempts={len(attempts)}"
            assert attempts[0]["call_type"] == "acompletion"
            assert attempts[0]["api_base"] == "https://opencode.ai/zen/v1"
            assert attempts[0]["response"] is not None
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_responses_attempts_do_not_accumulate_across_calls() -> None:
    config = load_model_stream_config(RESPONSES_CONFIG_PATH)
    controller = ModelStreamTransportController.configure(config)
    if controller is None:
        raise RuntimeError("model stream replay controller 未安装")
    try:
        model = BoxteamOpenAIResponsesModel(
            model="gpt-5.6-luna",
            api_key="test-key",
            api_base="https://www.cctq.ai/v1",
            custom_llm_provider="openai",
            provider_id="backup_3",
            responses_store=False,
            streaming=True,
            invocation_purpose="tool_test",
        )
        for round_index in range(4):
            attempts = await _capture(
                model,
                [SystemMessage(content="s"), HumanMessage(content="请读取 README.md")],
            )
            assert len(attempts) == 1, f"第 {round_index} 轮 attempts={len(attempts)}"
            assert attempts[0]["call_type"] == "aresponses"
            assert attempts[0]["api_base"] == "https://www.cctq.ai/v1/responses"
            assert attempts[0]["response"] is not None
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_model_calls_leave_litellm_callback_tables_untouched() -> None:
    config = load_model_stream_config(CHAT_CONFIG_PATH)
    controller = ModelStreamTransportController.configure(config)
    if controller is None:
        raise RuntimeError("model stream replay controller 未安装")
    try:
        model = BoxteamLiteLLMChatModel(
            model="openai/big-pickle",
            api_key="test-key",
            api_base="https://opencode.ai/zen/v1",
            custom_llm_provider="openai",
            streaming=True,
            invocation_purpose="tool_test",
        )
        for round_index in range(3):
            await _capture(
                model,
                [SystemMessage(content="s"), HumanMessage(content=f"第 {round_index} 轮")],
            )
        assert litellm.input_callback == []
        assert litellm.success_callback == []
        assert litellm.failure_callback == []
    finally:
        await controller.aclose()


@pytest.fixture
def failed_provider(
    monkeypatch: pytest.MonkeyPatch, protocol: str, failure_stage: str,
) -> tuple[BoxteamLiteLLMChatModel | BoxteamOpenAIResponsesModel, RuntimeError]:
    error = RuntimeError(f"original {protocol} {failure_stage} overloaded")

    class FailedStream:
        logging_obj = SimpleNamespace(model_call_details={"additional_args": {}})

        def __iter__(self):
            if failure_stage == "iterator":
                raise error
            return self

        def __next__(self):
            if failure_stage in {"response.failed", "error"}:
                return SimpleNamespace(type=failure_stage)
            raise error

        def __aiter__(self):
            if failure_stage == "iterator":
                raise error
            return self

        async def __anext__(self):
            return next(self)

        def close(self) -> None:
            pass

    def open_stream(**_kwargs: object) -> FailedStream:
        if failure_stage == "open":
            raise error
        return FailedStream()

    async def open_async_stream(**kwargs: object) -> FailedStream:
        return open_stream(**kwargs)

    model: BoxteamLiteLLMChatModel | BoxteamOpenAIResponsesModel
    if protocol == "responses":
        monkeypatch.setattr("app.agents.providers.openai_responses.litellm.responses", open_stream)
        monkeypatch.setattr("app.agents.providers.openai_responses.litellm.aresponses", open_async_stream)
        model = BoxteamOpenAIResponsesModel(
            model="test-model", api_key="test-key", api_base="https://example.com/v1",
            custom_llm_provider="openai", invocation_purpose="tool_test",
        )
    else:
        def chat_open(_self: ChatLiteLLM, **kwargs: object) -> FailedStream:
            return open_stream(**kwargs)

        async def chat_async_open(_self: ChatLiteLLM, **kwargs: object) -> FailedStream:
            return open_stream(**kwargs)

        monkeypatch.setattr(ChatLiteLLM, "completion_with_retry", chat_open)
        monkeypatch.setattr(ChatLiteLLM, "acompletion_with_retry", chat_async_open)
        model = BoxteamLiteLLMChatModel(
            model="test-model", api_key="test-key", api_base="https://example.com/v1",
            custom_llm_provider="openai", invocation_purpose="tool_test", max_retries=0,
        )
    return model, error


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("failure_stage", ["open", "iterator", "next"])
@pytest.mark.parametrize("is_async", [False, True])
async def test_upstream_attempt_keeps_original_error_after_outer_failure(
    failed_provider: tuple[BoxteamLiteLLMChatModel | BoxteamOpenAIResponsesModel, RuntimeError],
    protocol: str, failure_stage: str, is_async: bool,
) -> None:
    model, error = failed_provider
    token = begin_upstream_capture()
    try:
        with pytest.raises(RuntimeError) as raised:
            if is_async:
                async for _chunk in model._astream([HumanMessage(content="hello")]):
                    pass
            else:
                list(model._stream([HumanMessage(content="hello")]))
        record_upstream_error(RuntimeError("outer provider-profile-change-requires-rebuild"))
    finally:
        attempts = end_upstream_capture(token)

    assert raised.value is error
    assert is_provider_failure(error)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["error"] == f"original {protocol} {failure_stage} overloaded"
    assert attempt["response"] is None
    assert attempt["model"] == "test-model"
    assert attempt["sealed_dispatch"] is None
    assert attempt["request"]["model"] == "test-model"
    assert attempt["call_type"] == (
        ("aresponses" if is_async else "responses") if protocol == "responses"
        else ("acompletion" if is_async else "completion")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["responses"])
@pytest.mark.parametrize("failure_stage", ["response.failed", "error"])
@pytest.mark.parametrize("is_async", [False, True])
async def test_responses_failure_event_settles_attempt_before_outer_failure(
    failed_provider: tuple[BoxteamLiteLLMChatModel | BoxteamOpenAIResponsesModel, RuntimeError],
    is_async: bool,
) -> None:
    model, _error = failed_provider
    token = begin_upstream_capture()
    try:
        with pytest.raises(RuntimeError, match="LiteLLM Responses 请求失败") as raised:
            if is_async:
                async for _chunk in model._astream([HumanMessage(content="hello")]):
                    pass
            else:
                list(model._stream([HumanMessage(content="hello")]))
        record_upstream_error(RuntimeError("outer routing failed"))
    finally:
        attempts = end_upstream_capture(token)
    assert is_provider_failure(raised.value)
    assert len(attempts) == 1
    assert attempts[0]["error"] == str(raised.value)
    assert attempts[0]["response"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True])
async def test_nonstream_chat_generation_keeps_existing_capture_scope(
    monkeypatch: pytest.MonkeyPatch, is_async: bool,
) -> None:
    requests: list[dict[str, object]] = []

    def complete(_self: ChatLiteLLM, **kwargs: object) -> dict:
        requests.append(kwargs)
        return {"choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]}

    async def acomplete(_self: ChatLiteLLM, **kwargs: object) -> dict:
        return complete(_self, **kwargs)

    monkeypatch.setattr(ChatLiteLLM, "completion_with_retry", complete)
    monkeypatch.setattr(ChatLiteLLM, "acompletion_with_retry", acomplete)
    model = BoxteamLiteLLMChatModel(
        model="test-model", api_key="test-key", custom_llm_provider="openai",
        invocation_purpose="tool_test", streaming=False,
    )
    token = begin_upstream_capture()
    try:
        result = await model._agenerate([HumanMessage(content="hello")], stream=False) if is_async else model._generate([HumanMessage(content="hello")], stream=False)
    finally:
        attempts = end_upstream_capture(token)
    assert len(requests) == 1 and requests[0].get("stream") is not True
    assert result.generations[0].message.text == "done"
    assert attempts == []


@pytest.mark.asyncio
async def test_responses_attempt_keeps_detailed_open_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never_open(**_kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr("app.agents.providers.openai_responses.litellm.aresponses", never_open)
    monkeypatch.setattr("app.agents.providers.openai_responses.DEFAULT_RESPONSES_STREAM_OPEN_TIMEOUT_SECONDS", 0.01)
    model = BoxteamOpenAIResponsesModel(
        model="test-model", api_key="test-key", custom_llm_provider="openai",
        invocation_purpose="tool_test",
    )
    token = begin_upstream_capture()
    try:
        with pytest.raises(ResponsesStreamOpenTimeoutError) as raised:
            async for _chunk in model._astream([HumanMessage(content="hello")]):
                pass
        record_upstream_error(RuntimeError("outer routing failed"))
    finally:
        attempts = end_upstream_capture(token)
    assert attempts[0]["error"] == str(raised.value)
    assert "provider=openai model=test-model timeout_seconds=0.01" in attempts[0]["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True])
async def test_empty_chat_retry_settles_each_attempt_before_next_request(
    monkeypatch: pytest.MonkeyPatch, is_async: bool,
) -> None:
    class EmptyStream:
        logging_obj = SimpleNamespace(model_call_details={"additional_args": {}})
        received_finish_reason = None
        intermittent_finish_reason = None

        def __iter__(self):
            return self

        def __next__(self):
            raise StopIteration

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        def close(self) -> None:
            pass

    def open_stream(_self: ChatLiteLLM, **_kwargs: object) -> EmptyStream:
        return EmptyStream()

    async def open_async_stream(_self: ChatLiteLLM, **_kwargs: object) -> EmptyStream:
        return EmptyStream()

    monkeypatch.setattr(ChatLiteLLM, "completion_with_retry", open_stream)
    monkeypatch.setattr(ChatLiteLLM, "acompletion_with_retry", open_async_stream)
    model = BoxteamLiteLLMChatModel(
        model="test-model", api_key="test-key", custom_llm_provider="openai",
        invocation_purpose="tool_test", max_retries=1,
    )
    token = begin_upstream_capture()
    try:
        with pytest.raises(RuntimeError, match="真实 finish_reason"):
            if is_async:
                async for _chunk in model._astream([HumanMessage(content="hello")]):
                    pass
            else:
                list(model._stream([HumanMessage(content="hello")]))
        record_upstream_error(RuntimeError("outer routing failed"))
    finally:
        attempts = end_upstream_capture(token)
    assert len(attempts) == 2
    for ordinal, attempt in enumerate(attempts, 1):
        assert f"已尝试 {ordinal} 次" in attempt["error"]
        assert attempt["response"] is None


@pytest.fixture
def event_provider(monkeypatch: pytest.MonkeyPatch, protocol: str):
    """只替换 SDK 开流，真实 Provider 继续转换事件并结算 attempt。"""
    class EventStream:
        logging_obj = SimpleNamespace(model_call_details={"additional_args": {}})
        received_finish_reason = None
        intermittent_finish_reason = None

        def __init__(self, events: list[object]) -> None:
            self.events = iter(events)
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.events)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self)
            except StopIteration as error:
                raise StopAsyncIteration from error

        def close(self) -> None:
            self.closed = True

    def create(events: list[object]):
        opened: list[EventStream] = []

        def open_stream(**_kwargs: object) -> EventStream:
            stream = EventStream(events)
            opened.append(stream)
            return stream

        async def open_async_stream(**kwargs: object) -> EventStream:
            return open_stream(**kwargs)

        model_class = BoxteamOpenAIResponsesModel if protocol == "responses" else BoxteamLiteLLMChatModel
        if protocol == "responses":
            monkeypatch.setattr("app.agents.providers.openai_responses.litellm.responses", open_stream)
            monkeypatch.setattr("app.agents.providers.openai_responses.litellm.aresponses", open_async_stream)
        else:
            monkeypatch.setattr(ChatLiteLLM, "completion_with_retry", lambda _self, **kwargs: open_stream(**kwargs))
            monkeypatch.setattr(ChatLiteLLM, "acompletion_with_retry", lambda _self, **kwargs: open_async_stream(**kwargs))
        model = model_class(
            model="test-model", api_key="test-key", api_base="https://example.com/v1",
            custom_llm_provider="openai", invocation_purpose="tool_test", max_retries=1,
        )
        return model, opened

    return create


@pytest.fixture
def response_events(scenario: str) -> list[object]:
    if scenario == "empty":
        return []
    if scenario == "created":
        return [SimpleNamespace(type="response.created", response=ResponsesAPIResponse(
            id="response-1", created_at=1, model="test-model", output=[], status="in_progress",
        ))]
    if scenario == "text":
        return [SimpleNamespace(type="response.output_text.delta", output_index=0, content_index=0, delta="partial")]
    if scenario == "tool":
        return [SimpleNamespace(type="response.output_item.added", output_index=0, item={
            "type": "function_call", "id": "item-call", "call_id": "call-1", "name": "read_file", "arguments": "",
        })]
    if scenario == "reasoning":
        return [SimpleNamespace(type="response.output_item.added", output_index=0, item={
            "type": "reasoning", "id": "reasoning-1", "summary": [{"type": "summary_text", "text": "partial"}],
        })]
    raise ValueError(f"未知 Responses 场景: {scenario}")


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["responses"])
@pytest.mark.parametrize("scenario", ["empty", "created", "text", "tool", "reasoning"])
@pytest.mark.parametrize("is_async", [False, True])
async def test_responses_clean_eof_without_terminal_settles_provider_error(
    event_provider, response_events: list[object], scenario: str, is_async: bool,
) -> None:
    model, opened = event_provider(response_events)
    chunks = []
    token = begin_upstream_capture()
    try:
        with pytest.raises(RuntimeError, match="EOF 前未收到.*terminal event") as raised:
            if is_async:
                async for chunk in model._astream([HumanMessage(content="hello")]):
                    chunks.append(chunk)
            else:
                chunks.extend(model._stream([HumanMessage(content="hello")]))
        record_upstream_error(RuntimeError("outer routing failed"))
    finally:
        attempts = end_upstream_capture(token)
    assert is_provider_failure(raised.value)
    assert len(opened) == len(attempts) == 1
    assert attempts[0]["error"] == str(raised.value)
    assert attempts[0]["response"] is None
    assert any(chunk.message.content or chunk.message.tool_call_chunks for chunk in chunks) == (scenario in {"text", "tool", "reasoning"})
    assert all(chunk.message.chunk_position != "last" for chunk in chunks)
    if is_async:
        assert opened[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["responses"])
@pytest.mark.parametrize("status", ["completed", "incomplete"])
@pytest.mark.parametrize("is_async", [False, True])
async def test_responses_terminal_response_keeps_existing_semantics(
    event_provider, status: str, is_async: bool,
) -> None:
    response = ResponsesAPIResponse(
        id="response-1", created_at=1, model="test-model", output=[], status=status,
        incomplete_details=IncompleteDetails(reason="max_output_tokens") if status == "incomplete" else None,
    )
    model, opened = event_provider([SimpleNamespace(type=f"response.{status}", response=response)])
    token = begin_upstream_capture()
    try:
        chunks = [chunk async for chunk in model._astream([HumanMessage(content="hello")])] if is_async else list(model._stream([HumanMessage(content="hello")]))
        record_upstream_error(RuntimeError("outer routing failed"))
    finally:
        attempts = end_upstream_capture(token)
    assert len(opened) == len(attempts) == len(chunks) == 1
    assert chunks[0].message.chunk_position == "last"
    assert attempts[0]["response"] == response.model_dump(exclude_none=True, mode="json")
    assert attempts[0]["error"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["responses"])
@pytest.mark.parametrize("event_type,with_response", [
    ("response.completed", False), ("response.completed", True),
    ("response.incomplete", False), ("response.incomplete", True),
    ("response.output_item.added", False),
])
@pytest.mark.parametrize("is_async", [False, True])
async def test_malformed_responses_event_is_archived_as_provider_error(
    event_provider, event_type: str, with_response: bool, is_async: bool,
) -> None:
    # terminal 缺 response，或 function_call 缺 item ID，均属于实际收到的协议错误。
    event = SimpleNamespace(type=event_type, output_index=0, item={"type": "function_call"})
    if with_response:
        event.response = None
    model, opened = event_provider([event])
    token = begin_upstream_capture()
    try:
        with pytest.raises(RuntimeError, match="缺少") as raised:
            if is_async:
                async for _chunk in model._astream([HumanMessage(content="hello")]):
                    pass
            else:
                list(model._stream([HumanMessage(content="hello")]))
        record_upstream_error(RuntimeError("outer routing failed"))
    finally:
        attempts = end_upstream_capture(token)
    assert is_provider_failure(raised.value)
    assert len(opened) == len(attempts) == 1
    assert attempts[0]["error"] == str(raised.value)
    assert attempts[0]["response"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol,preflight", [
    ("chat_completions", "dispatch"), ("chat_completions", "install"),
    ("chat_completions", "client"), ("responses", "dispatch"), ("responses", "install"),
])
@pytest.mark.parametrize("is_async", [False, True])
async def test_local_preflight_rejection_does_not_create_provider_attempt(
    monkeypatch: pytest.MonkeyPatch, event_provider, protocol: str, preflight: str, is_async: bool,
) -> None:
    model, opened = event_provider([])
    error = ValueError(f"source-mismatch: local {preflight} rejected")

    def reject(*_args: object, **_kwargs: object) -> None:
        raise error

    module = "app.agents.providers.openai_responses" if protocol == "responses" else "app.agents.providers.litellm_chat"
    if preflight == "dispatch" and protocol == "chat_completions":
        monkeypatch.setattr(BoxteamLiteLLMChatModel, "_validate_completion_dispatch", reject)
    elif preflight == "dispatch":
        monkeypatch.setattr(f"{module}.validate_sealed_assembly_dispatch", reject)
    elif preflight == "client":
        monkeypatch.setattr(BoxteamLiteLLMChatModel, "_provider_http_client", reject)
    else:
        monkeypatch.setattr(f"{module}.ensure_model_stream_installed", reject)
    token = begin_upstream_capture()
    try:
        with pytest.raises(ValueError) as raised:
            if is_async:
                async for _chunk in model._astream([HumanMessage(content="hello")]):
                    pass
            else:
                list(model._stream([HumanMessage(content="hello")]))
        record_upstream_error(RuntimeError("outer routing failed"))
    finally:
        attempts = end_upstream_capture(token)
    assert raised.value is error
    assert not is_provider_failure(error)
    assert opened == attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions"])
@pytest.mark.parametrize("is_async", [False, True])
async def test_chat_partial_output_without_real_finish_does_not_retry(
    event_provider, is_async: bool,
) -> None:
    model, opened = event_provider([
        {"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])
    chunks = []
    token = begin_upstream_capture()
    try:
        with pytest.raises(RuntimeError, match="真实 finish_reason") as raised:
            if is_async:
                async for chunk in model._astream([HumanMessage(content="hello")]):
                    chunks.append(chunk)
            else:
                chunks.extend(model._stream([HumanMessage(content="hello")]))
        record_upstream_error(RuntimeError("outer routing failed"))
    finally:
        attempts = end_upstream_capture(token)
    assert any(chunk.message.text == "partial" for chunk in chunks)
    assert len(opened) == len(attempts) == 1
    assert attempts[0]["error"] == str(raised.value)
    assert attempts[0]["response"] is None
