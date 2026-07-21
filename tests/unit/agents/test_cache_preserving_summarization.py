from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse, SummarizationMiddleware
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.agents.cache_preserving_summarization import (
    CACHE_PRESERVING_STRATEGY,
    CACHE_REPLACEMENT_STRATEGY,
    CachePreservingPartition,
    CachePreservingSummarizationMiddleware,
    CachePreservingSummarizationToolMiddleware,
    apply_summarization_event,
    build_cache_preserving_event,
    build_cache_preserving_partition,
    replacement_effective_cutoff_to_state_cutoff,
    strip_media_from_summary_messages,
    validate_summary_text,
)
from app.agents.context_compaction_adapter import AgentSummarizationCompactor


class _SummarizationBoundaryStub:
    """纯分区测试不需要构建真实模型。"""


def _conversation(pair_count: int) -> list[HumanMessage | AIMessage]:
    messages: list[HumanMessage | AIMessage] = []
    for index in range(pair_count):
        messages.extend(
            [
                HumanMessage(content=f"问题-{index}"),
                AIMessage(content=f"回答-{index}"),
            ]
        )
    return messages


def test_first_compaction_preserves_complete_prefix_and_recent_tail() -> None:
    messages = _conversation(7)

    partition = build_cache_preserving_partition(
        _SummarizationBoundaryStub(),  # type: ignore[arg-type]
        messages,
        None,
        summarize_end=10,
    )

    assert partition is not None
    assert partition.prefix_messages == messages[:4]
    assert partition.messages_to_summarize == messages[4:10]
    assert partition.preserved_messages == messages[10:]
    assert partition.state_cutoff == 10


def test_cache_preserving_event_keeps_prefix_byte_equivalent() -> None:
    messages = _conversation(7)
    partition = build_cache_preserving_partition(
        _SummarizationBoundaryStub(),  # type: ignore[arg-type]
        messages,
        None,
        summarize_end=10,
    )
    assert partition is not None
    summary = HumanMessage(content="中段摘要")
    event = build_cache_preserving_event(
        partition,
        summary_message=summary,
        file_path="/history.md",
    )

    effective = apply_summarization_event(messages, event)

    assert event["strategy"] == CACHE_PRESERVING_STRATEGY
    assert [message.model_dump(mode="json") for message in effective[:4]] == [
        message.model_dump(mode="json") for message in messages[:4]
    ]
    assert effective[4] is summary
    assert effective[5:] == messages[10:]


def test_repeated_compaction_rolls_old_summary_without_moving_prefix() -> None:
    raw_messages = _conversation(9)
    first_partition = build_cache_preserving_partition(
        _SummarizationBoundaryStub(),  # type: ignore[arg-type]
        raw_messages,
        None,
        summarize_end=10,
    )
    assert first_partition is not None
    first_event = build_cache_preserving_event(
        first_partition,
        summary_message=HumanMessage(content="第一次摘要"),
        file_path="/history.md",
    )
    first_effective = apply_summarization_event(raw_messages, first_event)

    second_partition = build_cache_preserving_partition(
        _SummarizationBoundaryStub(),  # type: ignore[arg-type]
        first_effective,
        first_event,
        summarize_end=len(first_effective) - 2,
    )

    assert second_partition is not None
    assert second_partition.prefix_messages == raw_messages[:4]
    assert second_partition.messages_to_summarize[0].content == "第一次摘要"
    assert second_partition.state_cutoff == len(raw_messages) - 2

    second_event = build_cache_preserving_event(
        second_partition,
        summary_message=HumanMessage(content="第二次摘要"),
        file_path="/history.md",
    )
    second_effective = apply_summarization_event(raw_messages, second_event)
    assert second_effective[:4] == raw_messages[:4]
    assert second_effective[-2:] == raw_messages[-2:]


def test_media_stripping_replaces_nested_blocks_without_mutating_checkpoint() -> None:
    image_block = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,original"},
    }
    nested_image = {
        "type": "tool_result",
        "tool_use_id": "call-1",
        "content": [
            {"type": "text", "text": "工具文字"},
            {"type": "input_image", "image_url": "data:image/png;base64,nested"},
        ],
    }
    original = HumanMessage(content=[image_block, nested_image])

    stripped, changed = strip_media_from_summary_messages([original])

    assert changed is True
    assert stripped[0] is not original
    assert stripped[0].content == [
        {"type": "text", "text": "[image]"},
        {
            "type": "tool_result",
            "tool_use_id": "call-1",
            "content": [
                {"type": "text", "text": "工具文字"},
                {"type": "text", "text": "[image]"},
            ],
        },
    ]
    assert original.content == [image_block, nested_image]


def test_summary_overflow_retries_with_media_removed_after_exact_prefix() -> None:
    prefix = _conversation(2)
    media_message = HumanMessage(
        content=[
            {"type": "text", "text": "分析这张图"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,large"},
            },
        ]
    )
    partition = build_cache_preserving_partition(
        _SummarizationBoundaryStub(),  # type: ignore[arg-type]
        [*prefix, media_message, AIMessage(content="图像分析")],
        None,
        summarize_end=5,
    )
    assert partition is not None
    middleware = object.__new__(CachePreservingSummarizationMiddleware)
    request = ModelRequest(
        model=None,
        messages=[],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    )
    observed: list[ModelRequest] = []

    def handler(next_request: ModelRequest) -> ModelResponse:
        observed.append(next_request)
        if len(observed) == 1:
            raise ContextOverflowError("摘要上下文超限")
        return ModelResponse(result=[AIMessage(content="有效摘要")])

    summary = middleware._create_cache_preserving_summary(
        request,
        handler,
        partition,
    )

    assert summary == "有效摘要"
    assert len(observed) == 2
    assert observed[0].messages[: len(partition.prefix_messages)] == (
        partition.prefix_messages
    )
    assert observed[1].messages[: len(partition.prefix_messages)] == (
        partition.prefix_messages
    )
    retry_content = observed[1].messages[len(partition.prefix_messages)].content
    assert retry_content == [
        {"type": "text", "text": "分析这张图"},
        {"type": "text", "text": "[image]"},
    ]
    assert observed[0].tools == request.tools
    assert observed[1].tools == request.tools
    assert observed[0].tool_choice is None
    assert observed[1].tool_choice is None


def test_summary_tool_call_is_rejected_and_retried_without_tools() -> None:
    messages = _conversation(7)
    partition = build_cache_preserving_partition(
        _SummarizationBoundaryStub(),  # type: ignore[arg-type]
        messages,
        None,
        summarize_end=10,
    )
    assert partition is not None
    middleware = object.__new__(CachePreservingSummarizationMiddleware)
    request = ModelRequest(
        model=None,
        messages=[],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    )
    observed: list[ModelRequest] = []

    def handler(next_request: ModelRequest) -> ModelResponse:
        observed.append(next_request)
        if len(observed) == 1:
            return ModelResponse(
                result=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_file",
                                "args": {"path": "README.md"},
                                "id": "call-1",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            )
        return ModelResponse(result=[AIMessage(content="纯文本摘要")])

    summary = middleware._create_cache_preserving_summary(
        request,
        handler,
        partition,
    )

    assert summary == "纯文本摘要"
    assert len(observed) == 2
    assert observed[0].tools == request.tools
    assert observed[0].tool_choice is None
    assert observed[1].tools == []
    assert observed[1].tool_choice is None


def test_tool_call_fallback_overflow_continues_with_media_retry() -> None:
    prefix = _conversation(2)
    media_message = HumanMessage(
        content=[
            {"type": "text", "text": "查看图片"},
            {"type": "image", "base64": "large"},
        ]
    )
    partition = build_cache_preserving_partition(
        _SummarizationBoundaryStub(),  # type: ignore[arg-type]
        [*prefix, media_message, AIMessage(content="图片回复")],
        None,
        summarize_end=5,
    )
    assert partition is not None
    middleware = object.__new__(CachePreservingSummarizationMiddleware)
    request = ModelRequest(
        model=None,
        messages=[],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    )
    observed: list[ModelRequest] = []

    def handler(next_request: ModelRequest) -> ModelResponse:
        observed.append(next_request)
        if len(observed) == 1:
            return ModelResponse(
                result=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_file",
                                "args": {},
                                "id": "call-1",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            )
        if len(observed) == 2:
            raise ContextOverflowError("无工具重试仍超限")
        return ModelResponse(result=[AIMessage(content="媒体降级摘要")])

    summary = middleware._create_cache_preserving_summary(
        request,
        handler,
        partition,
    )

    assert summary == "媒体降级摘要"
    assert len(observed) == 3
    assert observed[1].tools == []
    assert observed[2].tools == request.tools
    assert observed[2].messages[len(partition.prefix_messages)].content == [
        {"type": "text", "text": "查看图片"},
        {"type": "text", "text": "[image]"},
    ]


def test_overflow_final_fallback_drops_prefix_and_uses_minimal_request() -> None:
    prefix = [
        HumanMessage(
            content=[
                {"type": "text", "text": "稳定前缀图片"},
                {"type": "image_url", "image_url": {"url": "data:large"}},
            ]
        ),
        AIMessage(content="稳定前缀回复"),
        HumanMessage(content="稳定前缀第二轮"),
        AIMessage(content="稳定前缀第二轮回复"),
    ]
    middle = [
        HumanMessage(content="中段一"),
        AIMessage(content="中段回复一"),
        HumanMessage(content="中段二"),
        AIMessage(content="中段回复二"),
    ]
    cache_partition = build_cache_preserving_partition(
        _SummarizationBoundaryStub(),  # type: ignore[arg-type]
        [*prefix, *middle, HumanMessage(content="最近消息")],
        None,
        summarize_end=8,
    )
    assert cache_partition is not None
    assert cache_partition.prefix_messages == prefix
    middleware = object.__new__(CachePreservingSummarizationMiddleware)
    request = ModelRequest(
        model=None,
        messages=[],
        system_message=SystemMessage(content="巨大主系统提示"),
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    )
    observed: list[ModelRequest] = []

    def handler(next_request: ModelRequest) -> ModelResponse:
        observed.append(next_request)
        if next_request.system_message == request.system_message:
            raise ContextOverflowError("保持缓存的候选都超限")
        return ModelResponse(result=[AIMessage(content="最终降级摘要")])

    summary = middleware._create_cache_preserving_summary(
        request,
        handler,
        cache_partition,
    )

    assert summary == "最终降级摘要"
    final_request = observed[-1]
    assert final_request.system_message == SystemMessage(
        content="Summarize the supplied conversation."
    )
    assert final_request.tools == []
    assert final_request.messages[: len(prefix)] != prefix
    assert all(message not in final_request.messages for message in prefix)


def _single_human_tool_rounds(
    *,
    with_media: bool = False,
) -> list[HumanMessage | AIMessage | ToolMessage]:
    messages: list[HumanMessage | AIMessage | ToolMessage] = [
        HumanMessage(
            content=(
                [
                    {"type": "text", "text": "执行连续工具任务"},
                    {"type": "image_url", "image_url": {"url": "data:large"}},
                ]
                if with_media
                else "执行连续工具任务"
            )
        )
    ]
    for index in range(3):
        call_id = f"call-{index}"
        messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"path": f"large-{index}.txt"},
                            "id": call_id,
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(
                    content=f"工具结果-{index}-" + "大段文本" * 1000,
                    tool_call_id=call_id,
                ),
            ]
        )
    return messages


def _assert_no_orphan_tool_messages(messages: list[AnyMessage]) -> None:
    known_call_ids: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            known_call_ids.update(call["id"] for call in message.tool_calls)
        if isinstance(message, ToolMessage):
            assert message.tool_call_id in known_call_ids


def test_tool_round_overflow_retries_drop_complete_rounds_without_mutation() -> None:
    middle = _single_human_tool_rounds()
    original = [message.model_dump(mode="json") for message in middle]
    partition = CachePreservingPartition(
        prefix_messages=[],
        messages_to_summarize=middle,
        preserved_messages=[HumanMessage(content="当前用户消息")],
        state_cutoff=len(middle),
    )
    middleware = object.__new__(CachePreservingSummarizationMiddleware)
    request = ModelRequest(model=None, messages=[])
    observed: list[ModelRequest] = []

    def handler(next_request: ModelRequest) -> ModelResponse:
        observed.append(next_request)
        if len(observed) <= 2:
            raise ContextOverflowError("工具历史仍超限")
        return ModelResponse(result=[AIMessage(content="工具轮摘要")])

    summary = middleware._create_cache_preserving_summary(
        request,
        handler,
        partition,
    )

    assert summary == "工具轮摘要"
    bodies = [candidate.messages[:-1] for candidate in observed]
    assert [len(body) for body in bodies] == [7, 7, 5]
    assert bodies[1][2].content == "[large tool result omitted for compaction retry]"
    for body in bodies:
        _assert_no_orphan_tool_messages(body)
    assert [message.model_dump(mode="json") for message in middle] == original


def _single_oversized_tool_round() -> list[HumanMessage | AIMessage | ToolMessage]:
    call_id = "oversized-call"
    return [
        HumanMessage(content="处理单个大型工具结果"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"path": "large.txt", "content": "参数" * 5000},
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="结果" * 5000, tool_call_id=call_id),
    ]


def test_single_oversized_tool_round_is_microcompacted_for_retry() -> None:
    middle = _single_oversized_tool_round()
    original = [message.model_dump(mode="json") for message in middle]
    partition = CachePreservingPartition(
        prefix_messages=[],
        messages_to_summarize=middle,
        preserved_messages=[HumanMessage(content="当前用户消息")],
        state_cutoff=3,
    )
    middleware = object.__new__(CachePreservingSummarizationMiddleware)
    observed: list[ModelRequest] = []

    def handler(next_request: ModelRequest) -> ModelResponse:
        observed.append(next_request)
        if len(observed) == 1:
            raise ContextOverflowError("单工具轮超限")
        return ModelResponse(result=[AIMessage(content="单工具轮摘要")])

    summary = middleware._create_cache_preserving_summary(
        ModelRequest(model=None, messages=[]),
        handler,
        partition,
    )

    assert summary == "单工具轮摘要"
    retry_body = observed[1].messages[:-1]
    assert len(retry_body) == 3
    _assert_no_orphan_tool_messages(retry_body)
    assert retry_body[1].tool_calls[0]["args"] == {
        "_omitted": "large tool arguments"
    }
    assert retry_body[2].content == "[large tool result omitted for compaction retry]"
    assert [message.model_dump(mode="json") for message in middle] == original


@pytest.mark.asyncio
async def test_async_single_oversized_tool_round_reaches_minimal_retry() -> None:
    middle = _single_oversized_tool_round()
    original = [message.model_dump(mode="json") for message in middle]
    partition = CachePreservingPartition(
        prefix_messages=[],
        messages_to_summarize=middle,
        preserved_messages=[HumanMessage(content="当前用户消息")],
        state_cutoff=3,
    )
    middleware = object.__new__(CachePreservingSummarizationMiddleware)
    observed: list[ModelRequest] = []

    async def handler(next_request: ModelRequest) -> ModelResponse:
        observed.append(next_request)
        if len(observed) <= 2:
            raise ContextOverflowError("异步单工具轮仍超限")
        return ModelResponse(result=[AIMessage(content="异步单工具轮摘要")])

    summary = await middleware._acreate_cache_preserving_summary(
        ModelRequest(
            model=None,
            messages=[],
            tools=[{"type": "function", "function": {"name": "write_file"}}],
        ),
        handler,
        partition,
    )

    assert summary == "异步单工具轮摘要"
    assert len(observed) == 3
    assert observed[-1].tools == []
    retry_body = observed[-1].messages[:-1]
    _assert_no_orphan_tool_messages(retry_body)
    assert retry_body[-1].content == "[large tool result omitted for compaction retry]"
    assert [message.model_dump(mode="json") for message in middle] == original


def _parallel_aggregate_tool_round() -> list[HumanMessage | AIMessage | ToolMessage]:
    tool_calls = [
        {
            "name": "read_file",
            "args": {"path": f"part-{index}.txt"},
            "id": f"parallel-{index}",
            "type": "tool_call",
        }
        for index in range(6)
    ]
    return [
        HumanMessage(content="读取多个文件"),
        AIMessage(content="", tool_calls=tool_calls),
        *[
            ToolMessage(
                content=f"并行结果-{index}-" + "x" * 3000,
                tool_call_id=f"parallel-{index}",
            )
            for index in range(6)
        ],
    ]


def test_parallel_tool_payloads_are_compacted_by_aggregate_budget() -> None:
    middle = _parallel_aggregate_tool_round()
    original = [message.model_dump(mode="json") for message in middle]
    partition = CachePreservingPartition(
        prefix_messages=[],
        messages_to_summarize=middle,
        preserved_messages=[HumanMessage(content="当前用户消息")],
        state_cutoff=len(middle),
    )
    middleware = object.__new__(CachePreservingSummarizationMiddleware)
    observed: list[ModelRequest] = []

    def handler(next_request: ModelRequest) -> ModelResponse:
        observed.append(next_request)
        if len(observed) == 1:
            raise ContextOverflowError("并行工具聚合结果超限")
        return ModelResponse(result=[AIMessage(content="并行工具摘要")])

    summary = middleware._create_cache_preserving_summary(
        ModelRequest(model=None, messages=[]),
        handler,
        partition,
    )

    assert summary == "并行工具摘要"
    retry_body = observed[1].messages[:-1]
    _assert_no_orphan_tool_messages(retry_body)
    compacted_results = [
        message.content
        for message in retry_body
        if isinstance(message, ToolMessage)
        and message.content == "[large tool result omitted for compaction retry]"
    ]
    assert len(compacted_results) >= 4
    assert isinstance(retry_body[-1], ToolMessage)
    assert retry_body[-1].content.startswith("并行结果-5-")
    assert [message.model_dump(mode="json") for message in middle] == original


@pytest.mark.asyncio
async def test_async_parallel_tool_payloads_reach_minimal_retry() -> None:
    middle = _parallel_aggregate_tool_round()
    original = [message.model_dump(mode="json") for message in middle]
    partition = CachePreservingPartition(
        prefix_messages=[],
        messages_to_summarize=middle,
        preserved_messages=[HumanMessage(content="当前用户消息")],
        state_cutoff=len(middle),
    )
    middleware = object.__new__(CachePreservingSummarizationMiddleware)
    observed: list[ModelRequest] = []

    async def handler(next_request: ModelRequest) -> ModelResponse:
        observed.append(next_request)
        if len(observed) <= 2:
            raise ContextOverflowError("异步并行工具仍超限")
        return ModelResponse(result=[AIMessage(content="异步并行工具摘要")])

    summary = await middleware._acreate_cache_preserving_summary(
        ModelRequest(
            model=None,
            messages=[],
            tools=[{"type": "function", "function": {"name": "read_file"}}],
        ),
        handler,
        partition,
    )

    assert summary == "异步并行工具摘要"
    assert observed[-1].tools == []
    retry_body = observed[-1].messages[:-1]
    _assert_no_orphan_tool_messages(retry_body)
    assert [message.model_dump(mode="json") for message in middle] == original


@pytest.mark.asyncio
async def test_async_tool_round_overflow_retries_drop_complete_rounds() -> None:
    middle = _single_human_tool_rounds(with_media=True)
    original = [message.model_dump(mode="json") for message in middle]
    partition = CachePreservingPartition(
        prefix_messages=[],
        messages_to_summarize=middle,
        preserved_messages=[HumanMessage(content="当前用户消息")],
        state_cutoff=len(middle),
    )
    middleware = object.__new__(CachePreservingSummarizationMiddleware)
    observed: list[ModelRequest] = []

    async def handler(next_request: ModelRequest) -> ModelResponse:
        observed.append(next_request)
        if len(observed) <= 3:
            raise ContextOverflowError("异步工具历史仍超限")
        return ModelResponse(result=[AIMessage(content="异步工具轮摘要")])

    summary = await middleware._acreate_cache_preserving_summary(
        ModelRequest(
            model=None,
            messages=[],
            tools=[{"type": "function", "function": {"name": "read_file"}}],
        ),
        handler,
        partition,
    )

    assert summary == "异步工具轮摘要"
    bodies = [candidate.messages[:-1] for candidate in observed]
    assert [len(body) for body in bodies] == [7, 7, 7, 3]
    assert bodies[1][0].content[-1] == {"type": "text", "text": "[image]"}
    assert observed[-1].tools == []
    for body in bodies:
        _assert_no_orphan_tool_messages(body)
    assert [message.model_dump(mode="json") for message in middle] == original


@pytest.mark.asyncio
async def test_async_summary_path_matches_media_overflow_fallback() -> None:
    prefix = _conversation(2)
    media_message = HumanMessage(
        content=[
            {"type": "text", "text": "异步图片"},
            {"type": "input_image", "image_url": "data:large"},
        ]
    )
    cache_partition = build_cache_preserving_partition(
        _SummarizationBoundaryStub(),  # type: ignore[arg-type]
        [*prefix, media_message, AIMessage(content="异步回复")],
        None,
        summarize_end=5,
    )
    assert cache_partition is not None
    middleware = object.__new__(CachePreservingSummarizationMiddleware)
    request = ModelRequest(model=None, messages=[])
    observed: list[ModelRequest] = []

    async def handler(next_request: ModelRequest) -> ModelResponse:
        observed.append(next_request)
        if len(observed) == 1:
            raise ContextOverflowError("异步摘要超限")
        return ModelResponse(result=[AIMessage(content="异步摘要")])

    summary = await middleware._acreate_cache_preserving_summary(
        request,
        handler,
        cache_partition,
    )

    assert summary == "异步摘要"
    assert len(observed) == 2
    assert observed[1].messages[len(cache_partition.prefix_messages)].content == [
        {"type": "text", "text": "异步图片"},
        {"type": "text", "text": "[image]"},
    ]


def test_summary_failure_strings_are_never_accepted() -> None:
    with pytest.raises(RuntimeError, match="压缩摘要生成失败"):
        validate_summary_text("Error generating summary: provider failed")
    with pytest.raises(RuntimeError, match="预处理后为空"):
        validate_summary_text("Previous conversation was too long to summarize.")
    with pytest.raises(ValueError, match="空文本"):
        validate_summary_text("   ")


def test_safe_cutoff_keeps_tool_call_and_result_together() -> None:
    messages = [
        HumanMessage(content="运行工具"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"path": "README.md"},
                    "id": "call-1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="结果", tool_call_id="call-1"),
        HumanMessage(content="继续"),
    ]

    cutoff = SummarizationMiddleware._find_safe_cutoff_point(
        messages,
        2,
    )

    assert cutoff == 1


def test_prepare_compaction_never_rewrites_stable_prefix_tool_arguments() -> None:
    tool_ai = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "large.txt", "content": "原始内容" * 500},
                "id": "call-write",
                "type": "tool_call",
            }
        ],
    )
    messages = [
        HumanMessage(content="写文件"),
        tool_ai,
        ToolMessage(content="完成", tool_call_id="call-write"),
        HumanMessage(content="第二轮"),
        AIMessage(content="第二轮回复"),
        HumanMessage(content="第三轮"),
        AIMessage(content="第三轮回复"),
        HumanMessage(content="最近一轮"),
        AIMessage(content="最近回复"),
    ]

    class _PrepareStub:
        @staticmethod
        def _get_effective_messages(_: ModelRequest) -> list:
            return messages

        @staticmethod
        def _count_request_tokens(_: ModelRequest, __: list) -> int:
            return 99999

        @staticmethod
        def _should_summarize(_: list, __: int) -> bool:
            return True

        @staticmethod
        def _determine_cutoff_index(_: list) -> int:
            return 7

    request = ModelRequest(model=None, messages=messages)
    prepared = CachePreservingSummarizationMiddleware._prepare_cache_compaction(
        _PrepareStub(),  # type: ignore[arg-type]
        request,
    )

    assert prepared is not None
    effective, partition = prepared
    assert effective[1] is tool_ai
    assert partition.prefix_messages[1] is tool_ai
    assert partition.prefix_messages[1].tool_calls[0]["args"]["content"] == (
        "原始内容" * 500
    )


def test_token_non_reduction_fails_before_event_is_committed() -> None:
    class _TokenCounterStub:
        @staticmethod
        def _count_request_tokens(_: ModelRequest, messages: list) -> int:
            return 100 if messages[0].content == "before" else 100

    with pytest.raises(RuntimeError, match="没有缩短模型上下文"):
        CachePreservingSummarizationMiddleware._ensure_compaction_reduces_tokens(
            _TokenCounterStub(),  # type: ignore[arg-type]
            ModelRequest(model=None, messages=[]),
            [HumanMessage(content="before")],
            [HumanMessage(content="after")],
        )


def test_compact_tool_schedules_next_full_model_request_instead_of_summarizing() -> None:
    runtime = SimpleNamespace(tool_call_id="compact-call")

    command = CachePreservingSummarizationToolMiddleware._schedule_result(
        runtime,  # type: ignore[arg-type]
        summarized_count=6,
    )

    assert command.update["_force_cache_compaction"] is True
    tool_message = command.update["messages"][0]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "compact-call"
    assert "next model call" in tool_message.text


def test_compact_tool_schedules_emergency_replacement_when_regular_cutoff_is_zero() -> None:
    messages = [*_conversation(2), HumanMessage(content="当前用户消息")]

    class _SummarizationStub:
        @staticmethod
        def _apply_event_to_messages(raw_messages: list, _: object) -> list:
            return raw_messages

        @staticmethod
        def _determine_cutoff_index(_: list) -> int:
            return 0

        @staticmethod
        def _find_safe_cutoff_point(_: list, target: int) -> int:
            return target

    middleware = object.__new__(CachePreservingSummarizationToolMiddleware)
    middleware._summarization = _SummarizationStub()  # type: ignore[attr-defined]
    middleware._is_eligible_for_compaction = lambda _: True  # type: ignore[method-assign]
    runtime = SimpleNamespace(
        tool_call_id="compact-emergency",
        state={"messages": messages},
    )

    command = middleware._run_compact(runtime)  # type: ignore[arg-type]

    assert command.update["_force_cache_compaction"] is True
    assert "summary of 4 messages" in command.update["messages"][0].text


@pytest.mark.asyncio
async def test_http_compaction_check_uses_emergency_replacement_partition() -> None:
    messages = [*_conversation(2), HumanMessage(content="当前用户消息")]

    class _SummarizationStub:
        @staticmethod
        def _determine_cutoff_index(_: list) -> int:
            return 0

        @staticmethod
        def _find_safe_cutoff_point(_: list, target: int) -> int:
            return target

    compactor = object.__new__(AgentSummarizationCompactor)
    compactor._build_summarization = lambda _: _SummarizationStub()  # type: ignore[method-assign]

    check = await compactor.check(
        agent_id="default",
        raw_messages=messages,
        event=None,
    )

    assert check.cutoff == 4
    assert check.effective_messages == messages


def test_untriggered_path_calls_handler_without_parent_argument_truncation() -> None:
    tool_ai = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "keep.txt", "content": "必须原样保留" * 500},
                "id": "call-keep",
                "type": "tool_call",
            }
        ],
    )
    messages = [HumanMessage(content="写入"), tool_ai]
    observed: list[ModelRequest] = []

    class _UntriggeredStub:
        @staticmethod
        def _prepare_cache_compaction(_: ModelRequest) -> None:
            return None

        @staticmethod
        def _get_effective_messages(_: ModelRequest) -> list:
            return messages

    def handler(next_request: ModelRequest) -> ModelResponse:
        observed.append(next_request)
        return ModelResponse(result=[AIMessage(content="完成")])

    response = CachePreservingSummarizationMiddleware.wrap_model_call(
        _UntriggeredStub(),  # type: ignore[arg-type]
        ModelRequest(model=None, messages=messages),
        handler,
    )

    assert isinstance(response, ModelResponse)
    assert observed[0].messages[1] is tool_ai
    assert observed[0].messages[1].tool_calls[0]["args"]["content"] == (
        "必须原样保留" * 500
    )


def test_forced_compaction_without_safe_partition_clears_one_shot_flag() -> None:
    messages = [HumanMessage(content="消息太少")]

    class _UnavailableStub:
        @staticmethod
        def _prepare_cache_compaction(
            _: ModelRequest,
        ) -> tuple[list, None]:
            return messages, None

        @staticmethod
        def _handle_unavailable_forced_compaction(
            request: ModelRequest,
            handler,
            effective: list,
        ) -> ExtendedModelResponse:
            return CachePreservingSummarizationMiddleware._handle_unavailable_forced_compaction(
                _UnavailableStub(),  # type: ignore[arg-type]
                request,
                handler,
                effective,
            )

    response = CachePreservingSummarizationMiddleware.wrap_model_call(
        _UnavailableStub(),  # type: ignore[arg-type]
        ModelRequest(
            model=None,
            messages=messages,
            state={"_force_cache_compaction": True},
        ),
        lambda _: ModelResponse(result=[AIMessage(content="继续")]),
    )

    assert isinstance(response, ExtendedModelResponse)
    assert response.command.update["_force_cache_compaction"] is False


def test_prepare_preserves_complete_api_round_without_new_human_boundary() -> None:
    messages = _single_human_tool_rounds()

    class _PrepareStub:
        @staticmethod
        def _get_effective_messages(_: ModelRequest) -> list:
            return messages

        @staticmethod
        def _count_request_tokens(_: ModelRequest, __: list) -> int:
            return 99999

        @staticmethod
        def _should_summarize(_: list, __: int) -> bool:
            return True

        @staticmethod
        def _determine_cutoff_index(_: list) -> int:
            return 5

    prepared = CachePreservingSummarizationMiddleware._prepare_cache_compaction(
        _PrepareStub(),  # type: ignore[arg-type]
        ModelRequest(model=None, messages=messages),
    )

    assert prepared is not None
    _, partition = prepared
    assert partition is not None
    assert partition.prefix_messages == messages[:3]
    assert partition.messages_to_summarize == messages[3:5]
    event = build_cache_preserving_event(
        partition,
        summary_message=HumanMessage(content="工具轮摘要"),
        file_path="/history.md",
    )
    projected = apply_summarization_event(messages, event)
    assert event["strategy"] == CACHE_PRESERVING_STRATEGY
    assert [message.model_dump(mode="json") for message in projected[:3]] == [
        message.model_dump(mode="json") for message in messages[:3]
    ]
    assert projected[3] is event["summary_message"]
    assert projected[4:] == messages[5:]


def _emergency_replacement_partition() -> tuple[
    list[HumanMessage | AIMessage],
    CachePreservingPartition,
]:
    messages = [*_conversation(2), HumanMessage(content="当前用户消息")]

    class _EmergencyPrepareStub:
        @staticmethod
        def _get_effective_messages(_: ModelRequest) -> list:
            return messages

        @staticmethod
        def _count_request_tokens(_: ModelRequest, __: list) -> int:
            return 200_000

        @staticmethod
        def _should_summarize(_: list, __: int) -> bool:
            return True

        @staticmethod
        def _determine_cutoff_index(_: list) -> int:
            return 0

        @staticmethod
        def _find_safe_cutoff_point(_: list, target: int) -> int:
            return target

    prepared = CachePreservingSummarizationMiddleware._prepare_cache_compaction(
        _EmergencyPrepareStub(),  # type: ignore[arg-type]
        ModelRequest(model=None, messages=messages),
    )
    assert prepared is not None
    _, partition = prepared
    assert partition is not None
    assert partition.prefix_messages == []
    assert partition.messages_to_summarize == messages[:4]
    assert partition.preserved_messages == [messages[-1]]
    return messages, partition


def test_cache_replacement_sync_overflow_reaches_minimal_fallback() -> None:
    messages, partition = _emergency_replacement_partition()
    middleware = object.__new__(CachePreservingSummarizationMiddleware)
    request = ModelRequest(
        model=None,
        messages=[],
        system_message=SystemMessage(content="巨大系统提示"),
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    )
    observed: list[ModelRequest] = []

    def handler(next_request: ModelRequest) -> ModelResponse:
        observed.append(next_request)
        if next_request.system_message == request.system_message:
            raise ContextOverflowError("替换摘要仍超限")
        return ModelResponse(result=[AIMessage(content="替换摘要成功")])

    summary = middleware._create_cache_preserving_summary(
        request,
        handler,
        partition,
    )

    assert summary == "替换摘要成功"
    assert observed[-1].tools == []
    assert observed[-1].system_message != request.system_message
    summary_message = HumanMessage(content=summary)
    event = build_cache_preserving_event(
        partition,
        summary_message=summary_message,
        file_path="/history.md",
        strategy=CACHE_REPLACEMENT_STRATEGY,
    )
    assert apply_summarization_event(messages, event) == [
        summary_message,
        messages[-1],
    ]


@pytest.mark.asyncio
async def test_cache_replacement_async_overflow_reaches_minimal_fallback() -> None:
    _, partition = _emergency_replacement_partition()
    middleware = object.__new__(CachePreservingSummarizationMiddleware)
    request = ModelRequest(
        model=None,
        messages=[],
        system_message=SystemMessage(content="巨大系统提示"),
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    )
    observed: list[ModelRequest] = []

    async def handler(next_request: ModelRequest) -> ModelResponse:
        observed.append(next_request)
        if next_request.system_message == request.system_message:
            raise ContextOverflowError("异步替换摘要仍超限")
        return ModelResponse(result=[AIMessage(content="异步替换摘要成功")])

    summary = await middleware._acreate_cache_preserving_summary(
        request,
        handler,
        partition,
    )

    assert summary == "异步替换摘要成功"
    assert observed[-1].tools == []
    assert observed[-1].system_message != request.system_message


def test_replacement_rolls_existing_cache_prefix_and_summary_together() -> None:
    raw_messages = _conversation(5)
    old_summary = HumanMessage(content="旧摘要")
    old_event = {
        "strategy": CACHE_PRESERVING_STRATEGY,
        "cutoff_index": 8,
        "cache_prefix_messages": raw_messages[:4],
        "summary_message": old_summary,
        "file_path": "/history.md",
    }
    effective = apply_summarization_event(raw_messages, old_event)

    for invalid_cutoff in (3, 4):
        with pytest.raises(ValueError, match="必须同时吞并"):
            replacement_effective_cutoff_to_state_cutoff(
                old_event,
                invalid_cutoff,
            )
    assert replacement_effective_cutoff_to_state_cutoff(old_event, 5) == 8
    assert replacement_effective_cutoff_to_state_cutoff(old_event, 6) == 9

    class _PrepareStub:
        @staticmethod
        def _get_effective_messages(_: ModelRequest) -> list:
            return effective

        @staticmethod
        def _count_request_tokens(_: ModelRequest, __: list) -> int:
            return 99999

        @staticmethod
        def _should_summarize(_: list, __: int) -> bool:
            return True

        @staticmethod
        def _determine_cutoff_index(_: list) -> int:
            return 4

    prepared = CachePreservingSummarizationMiddleware._prepare_cache_compaction(
        _PrepareStub(),  # type: ignore[arg-type]
        ModelRequest(
            model=None,
            messages=raw_messages,
            state={"_summarization_event": old_event},
        ),
    )
    assert prepared is not None
    _, replacement = prepared
    assert replacement is not None
    assert replacement.prefix_messages == []
    assert replacement.messages_to_summarize == effective[:5]

    new_summary = HumanMessage(content="新摘要")
    new_event = build_cache_preserving_event(
        replacement,
        summary_message=new_summary,
        file_path="/history.md",
        strategy=CACHE_REPLACEMENT_STRATEGY,
    )
    modified = [new_summary, *replacement.preserved_messages]
    assert apply_summarization_event(raw_messages, new_event) == modified


def test_encrypted_reasoning_block_survives_prefix_and_tail_projection() -> None:
    encrypted_reasoning = {
        "type": "reasoning",
        "reasoning": "内部推理",
        "encrypted_content": "encrypted-payload",
    }
    prefix_ai = AIMessage(
        content=[encrypted_reasoning, {"type": "text", "text": "前缀正文"}]
    )
    tail_ai = AIMessage(
        content=[
            {
                "type": "reasoning",
                "reasoning": "最近推理",
                "encrypted_content": "recent-encrypted-payload",
            },
            {"type": "text", "text": "最近正文"},
        ]
    )
    messages = [
        HumanMessage(content="第一轮"),
        prefix_ai,
        HumanMessage(content="第二轮"),
        AIMessage(content="第二轮回复"),
        HumanMessage(content="需要摘要"),
        AIMessage(content="需要摘要的回复"),
        HumanMessage(content="最近一轮"),
        tail_ai,
    ]
    partition = build_cache_preserving_partition(
        _SummarizationBoundaryStub(),  # type: ignore[arg-type]
        messages,
        None,
        summarize_end=6,
    )
    assert partition is not None
    event = build_cache_preserving_event(
        partition,
        summary_message=HumanMessage(content="摘要"),
        file_path="/history.md",
    )

    projected = apply_summarization_event(messages, event)

    assert projected[1] is prefix_ai
    assert projected[-1] is tail_ai
    assert projected[1].content[0]["encrypted_content"] == "encrypted-payload"
    assert projected[-1].content[0]["encrypted_content"] == (
        "recent-encrypted-payload"
    )
