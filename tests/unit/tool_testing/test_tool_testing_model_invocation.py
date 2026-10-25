from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.tools.apply_patch import create_apply_patch_tool
from app.runtime.provider_errors import (
    is_transient_provider_error,
    mark_provider_failure,
)
from app.tool_testing.model_invocation import (
    MAX_MODEL_CALLS_PER_ATTEMPT,
    MAX_TRANSIENT_RETRIES,
    ToolCallProbeInvocationError,
    probe_tool_call,
    probe_tool_call_with_transient_retries,
)


def test_transient_provider_error_classification_uses_http_status() -> None:
    class _ProviderHttpError(RuntimeError):
        def __init__(self, status_code: int) -> None:
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code

    assert (
        is_transient_provider_error(mark_provider_failure(_ProviderHttpError(503)))
        is True
    )
    assert (
        is_transient_provider_error(mark_provider_failure(_ProviderHttpError(429)))
        is True
    )
    assert (
        is_transient_provider_error(mark_provider_failure(_ProviderHttpError(400)))
        is False
    )
    assert is_transient_provider_error(TimeoutError("本地 payload 超时")) is False
    assert is_transient_provider_error(ConnectionError("来源未标记")) is False


def test_transient_provider_error_ignores_unmarked_outer_timeout() -> None:
    class _ProviderHttpError(RuntimeError):
        def __init__(self, status_code: int) -> None:
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code

    provider_error = mark_provider_failure(_ProviderHttpError(400))
    local_timeout = TimeoutError("本地 on_llm_error 回调超时")
    local_timeout.__cause__ = provider_error

    assert is_transient_provider_error(local_timeout) is False

    class _LocalStatusError(RuntimeError):
        status_code = 503

    local_status_error = _LocalStatusError("本地回调失败")
    local_status_error.__cause__ = provider_error
    assert is_transient_provider_error(local_status_error) is False


def test_transient_provider_error_follows_explicit_provider_cause() -> None:
    class _ProviderHttpError(RuntimeError):
        def __init__(self, status_code: int) -> None:
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code

    provider_error = mark_provider_failure(_ProviderHttpError(503))
    try:
        raise provider_error
    except _ProviderHttpError as caught:
        try:
            raise RuntimeError("显式包装的 Provider 错误") from caught
        except RuntimeError as wrapped_error:
            assert wrapped_error.__cause__ is provider_error
            assert is_transient_provider_error(wrapped_error) is True


class _BoundScriptedToolCallModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = responses
        self.calls = 0
        self.received_messages: list[list[object]] = []

    async def ainvoke(self, messages: list[object]) -> AIMessage:
        self.received_messages.append(list(messages))
        response = self._responses[self.calls]
        self.calls += 1
        return response


class _ScriptedToolCallModel:
    def __init__(self, bound_model: _BoundScriptedToolCallModel) -> None:
        self._bound_model = bound_model

    def bind_tools(self, tools: list[object]) -> _BoundScriptedToolCallModel:
        assert len(tools) == 1
        return self._bound_model


def _apply_patch_tool_call_message() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "apply_patch",
                "args": {"input": "*** Begin Patch\n*** End Patch"},
                "id": "call-scripted",
                "type": "tool_call",
            }
        ],
    )


@pytest.mark.asyncio
async def test_probe_tool_call_continues_after_reasoning_only_response(
    text_mutations_factory,
    tmp_path: Path,
) -> None:
    bound_model = _BoundScriptedToolCallModel(
        [
            AIMessage(
                content=[
                    {
                        "type": "reasoning",
                        "reasoning": "先确认目标文件和补丁上下文。",
                    }
                ]
            ),
            _apply_patch_tool_call_message(),
        ]
    )
    result = await probe_tool_call(
        model=_ScriptedToolCallModel(bound_model),
        tool=create_apply_patch_tool(text_mutations=text_mutations_factory(tmp_path)),
        messages=[SystemMessage("测试"), HumanMessage("请修改文件")],
    )

    assert result.tool_call is not None
    assert result.model_calls == 2
    assert result.reasoning_only_calls == 1
    assert bound_model.calls == 2
    assert len(bound_model.received_messages[1]) == 4


@pytest.mark.asyncio
async def test_probe_tool_call_does_not_retry_plain_text_response(
    text_mutations_factory, tmp_path: Path
) -> None:
    bound_model = _BoundScriptedToolCallModel(
        [AIMessage(content="我将使用工具修改文件。")]
    )
    result = await probe_tool_call(
        model=_ScriptedToolCallModel(bound_model),
        tool=create_apply_patch_tool(text_mutations=text_mutations_factory(tmp_path)),
        messages=[SystemMessage("测试"), HumanMessage("请修改文件")],
    )

    assert result.tool_call is None
    assert result.model_calls == 1
    assert result.reasoning_only_calls == 0
    assert result.failure is not None and "不是纯 reasoning" in result.failure
    assert bound_model.calls == 1


@pytest.mark.asyncio
async def test_probe_tool_call_limits_consecutive_reasoning_responses(
    text_mutations_factory,
    tmp_path: Path,
) -> None:
    reasoning_response = AIMessage(
        content=[{"type": "reasoning", "reasoning": "继续分析补丁格式。"}]
    )
    bound_model = _BoundScriptedToolCallModel(
        [reasoning_response] * MAX_MODEL_CALLS_PER_ATTEMPT
    )
    result = await probe_tool_call(
        model=_ScriptedToolCallModel(bound_model),
        tool=create_apply_patch_tool(text_mutations=text_mutations_factory(tmp_path)),
        messages=[SystemMessage("测试"), HumanMessage("请修改文件")],
    )

    assert result.tool_call is None
    assert result.model_calls == MAX_MODEL_CALLS_PER_ATTEMPT
    assert result.reasoning_only_calls == MAX_MODEL_CALLS_PER_ATTEMPT
    assert result.failure is not None and "连续 3 次" in result.failure
    assert bound_model.calls == MAX_MODEL_CALLS_PER_ATTEMPT


@pytest.mark.asyncio
async def test_probe_tool_call_reports_invocation_count_on_provider_error(
    text_mutations_factory,
    tmp_path: Path,
) -> None:
    class _FailingBoundModel(_BoundScriptedToolCallModel):
        def __init__(self) -> None:
            super().__init__([])

        async def ainvoke(self, messages: list[object]) -> AIMessage:
            raise ConnectionError("上游连接中断")

    failing_model = _ScriptedToolCallModel(_FailingBoundModel())
    with pytest.raises(ToolCallProbeInvocationError) as captured:
        await probe_tool_call(
            model=failing_model,
            tool=create_apply_patch_tool(
                text_mutations=text_mutations_factory(tmp_path)
            ),
            messages=[SystemMessage("测试"), HumanMessage("请修改文件")],
        )

    assert captured.value.model_calls == 1
    assert captured.value.reasoning_only_calls == 0
    assert "ConnectionError: 上游连接中断" in str(captured.value)


@pytest.mark.asyncio
async def test_probe_tool_call_retries_transient_provider_error_three_times(
    text_mutations_factory,
    tmp_path: Path,
) -> None:
    class _FlakyBoundModel(_BoundScriptedToolCallModel):
        def __init__(self) -> None:
            super().__init__([])
            self.calls = 0

        async def ainvoke(self, messages: list[object]) -> AIMessage:
            self.calls += 1
            if self.calls <= MAX_TRANSIENT_RETRIES:
                raise mark_provider_failure(ConnectionError("上游连接中断"))
            return _apply_patch_tool_call_message()

    bound_model = _FlakyBoundModel()
    result = await probe_tool_call_with_transient_retries(
        model=_ScriptedToolCallModel(bound_model),
        tool=create_apply_patch_tool(text_mutations=text_mutations_factory(tmp_path)),
        messages=[SystemMessage("测试"), HumanMessage("请修改文件")],
        retry_delays=(0, 0, 0),
    )

    assert result.tool_call is not None
    assert result.model_calls == 4
    assert result.transient_retries == 3
    assert bound_model.calls == 4


@pytest.mark.asyncio
async def test_probe_tool_call_does_not_retry_non_transient_provider_error(
    text_mutations_factory,
    tmp_path: Path,
) -> None:
    class _InvalidRequestBoundModel(_BoundScriptedToolCallModel):
        def __init__(self) -> None:
            super().__init__([])
            self.calls = 0

        async def ainvoke(self, messages: list[object]) -> AIMessage:
            self.calls += 1
            raise ValueError("请求参数无效")

    bound_model = _InvalidRequestBoundModel()
    with pytest.raises(ToolCallProbeInvocationError) as captured:
        await probe_tool_call_with_transient_retries(
            model=_ScriptedToolCallModel(bound_model),
            tool=create_apply_patch_tool(
                text_mutations=text_mutations_factory(tmp_path)
            ),
            messages=[SystemMessage("测试"), HumanMessage("请修改文件")],
            retry_delays=(0, 0, 0),
        )

    assert captured.value.model_calls == 1
    assert captured.value.transient_retries == 0
    assert bound_model.calls == 1


@pytest.mark.asyncio
async def test_probe_tool_call_does_not_retry_local_timeout_error(
    text_mutations_factory,
    tmp_path: Path,
) -> None:
    class _LocalTimeoutBoundModel(_BoundScriptedToolCallModel):
        def __init__(self) -> None:
            super().__init__([])
            self.calls = 0

        async def ainvoke(self, messages: list[object]) -> AIMessage:
            self.calls += 1
            raise TimeoutError("Responses payload 在本地构造超时")

    bound_model = _LocalTimeoutBoundModel()
    with pytest.raises(ToolCallProbeInvocationError) as captured:
        await probe_tool_call_with_transient_retries(
            model=_ScriptedToolCallModel(bound_model),
            tool=create_apply_patch_tool(
                text_mutations=text_mutations_factory(tmp_path)
            ),
            messages=[SystemMessage("测试"), HumanMessage("请修改文件")],
            retry_delays=(0, 0, 0),
        )

    assert captured.value.model_calls == 1
    assert captured.value.transient_retries == 0
    assert bound_model.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_status", [400, 503])
async def test_probe_tool_call_does_not_retry_local_error_with_provider_context(
    text_mutations_factory,
    tmp_path: Path,
    provider_status: int,
) -> None:
    class _ProviderHttpError(RuntimeError):
        def __init__(self, status_code: int) -> None:
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code

    provider_error = mark_provider_failure(_ProviderHttpError(provider_status))

    class _LocalCallbackErrorBoundModel(_BoundScriptedToolCallModel):
        def __init__(self) -> None:
            super().__init__([])
            self.local_error: TimeoutError | None = None

        async def ainvoke(self, messages: list[object]) -> AIMessage:
            self.received_messages.append(list(messages))
            self.calls += 1
            try:
                raise provider_error
            except _ProviderHttpError:
                self.local_error = TimeoutError("本地 on_llm_error 回调超时")
                raise self.local_error

    bound_model = _LocalCallbackErrorBoundModel()
    with pytest.raises(ToolCallProbeInvocationError) as captured:
        await probe_tool_call_with_transient_retries(
            model=_ScriptedToolCallModel(bound_model),
            tool=create_apply_patch_tool(
                text_mutations=text_mutations_factory(tmp_path)
            ),
            messages=[SystemMessage("测试"), HumanMessage("请修改文件")],
            retry_delays=(0, 0, 0),
        )

    assert bound_model.calls == 1
    assert captured.value.model_calls == 1
    assert captured.value.transient_retries == 0
    assert captured.value.cause is bound_model.local_error
    assert captured.value.cause.__context__ is provider_error
