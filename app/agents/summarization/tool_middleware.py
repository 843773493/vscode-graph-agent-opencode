"""compact_conversation 工具的缓存优先压缩中间件。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from deepagents.backends.protocol import BackendProtocol, ReadResult
from deepagents.backends.utils import format_content_with_line_numbers
from deepagents.middleware.summarization import SummarizationToolMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import (
    AnyMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from app.agents.summarization.middleware import CachePreservingSummarizationMiddleware
from app.agents.summarization.planning import build_safe_compaction_partition
from app.agents.summarization.state import CachePreservingPartition
from app.core.failures import failure_detail


class CompactConversationInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    runtime: ToolRuntime


class ReadCompactionHistoryInput(CompactConversationInput):
    line_offset: Annotated[int, Field(ge=1)] = 1
    limit: Annotated[int, Field(ge=1, le=2000)] = 200


# 声明本身没有可调用实现；目录和执行共用同一参数模型与描述。
COMPACT_CONVERSATION_TOOL = StructuredTool(
    name="compact_conversation",
    description=(
        "Compact the conversation by summarizing older messages "
        "into a concise summary. Use this proactively when the "
        "conversation is getting long to free up context window "
        "space. This tool takes no arguments."
    ),
    args_schema=CompactConversationInput,
)
READ_COMPACTION_HISTORY_TOOL = StructuredTool(
    name="read_compaction_history",
    description=(
        "读取当前会话精确 thread 的压缩归档原文，包含已从模型上下文移除的消息。"
        "line_offset 从1开始，limit 是行数；返回带行号的原文，可按下一行继续读取。"
        "不接受路径、Session ID 或 Thread ID。"
    ),
    args_schema=ReadCompactionHistoryInput,
    handle_tool_error=True,
)


def create_compact_conversation_tool(
    run: Callable[[ToolRuntime], Command],
    arun: Callable[[ToolRuntime], Awaitable[Command]],
) -> BaseTool:
    def sync_compact(runtime: ToolRuntime) -> Command:
        return run(runtime)

    async def async_compact(runtime: ToolRuntime) -> Command:
        return await arun(runtime)

    return COMPACT_CONVERSATION_TOOL.model_copy(update={
        "func": sync_compact, "coroutine": async_compact,
    })


def create_read_compaction_history_tool(
    history_path: Callable[[], str], resolve_backend: Callable[[ToolRuntime], BackendProtocol],
) -> BaseTool:
    def content(result: ReadResult, line_offset: int) -> str:
        if result.error:
            cause = RuntimeError(result.error)
            failure_detail(cause, stage="compaction-history-read", label="压缩历史 backend 读取失败")
            raise ToolException(
                "detail-unavailable: 当前 thread 的压缩归档不可读取；"
                f"line_offset={line_offset}。归档缺失或读取失败时不会改读其它 thread。"
            ) from cause
        if result.file_data is None or result.file_data["encoding"] != "utf-8":
            raise RuntimeError("压缩历史必须返回 UTF-8 正文")
        return format_content_with_line_numbers(
            result.file_data["content"], start_line=line_offset,
        )

    def read_history(
        runtime: ToolRuntime,
        line_offset: Annotated[int, Field(ge=1)] = 1,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
    ) -> str:
        # 路径只在本次调用栈推导；模型不能选择其它 SessionThread。
        path = history_path()
        result = resolve_backend(runtime).read(path, offset=line_offset - 1, limit=limit)
        return content(result, line_offset)

    async def aread_history(
        runtime: ToolRuntime,
        line_offset: Annotated[int, Field(ge=1)] = 1,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
    ) -> str:
        path = history_path()
        result = await resolve_backend(runtime).aread(path, offset=line_offset - 1, limit=limit)
        return content(result, line_offset)

    return READ_COMPACTION_HISTORY_TOOL.model_copy(update={
        "func": read_history, "coroutine": aread_history,
    })


class CachePreservingSummarizationToolMiddleware(SummarizationToolMiddleware):
    """让 compact_conversation 工具使用与自动压缩相同的缓存优先策略。"""

    def __init__(
        self,
        summarization: CachePreservingSummarizationMiddleware,
        *,
        system_prompt: str | None = None,
    ) -> None:
        super().__init__(summarization, system_prompt=system_prompt)
        self.tools.append(create_read_compaction_history_tool(
            summarization._get_history_path, self._resolve_backend,
        ))

    def _create_compact_tool(self) -> BaseTool:
        return create_compact_conversation_tool(self._run_compact, self._arun_compact)

    def _partition_for_tool(
        self,
        runtime: ToolRuntime,
    ) -> tuple[CachePreservingPartition, object, list[AnyMessage]] | None:
        summarization = self._summarization
        messages = runtime.state.get("messages", [])
        event = runtime.state.get("_summarization_event")
        effective = summarization._apply_event_to_messages(messages, event)
        if not self._is_eligible_for_compaction(effective):
            return None
        partition = build_safe_compaction_partition(
            effective,
            event,
            summarize_end=summarization._determine_cutoff_index(effective),
        )
        if partition is None:
            return None
        return partition, event, effective

    def _run_compact(self, runtime: ToolRuntime) -> Command:
        prepared = self._partition_for_tool(runtime)
        if prepared is None:
            return self._schedule_result(runtime, summarized_count=0)
        partition, _, _ = prepared
        return self._schedule_result(
            runtime,
            summarized_count=len(partition.messages_to_summarize),
        )

    async def _arun_compact(self, runtime: ToolRuntime) -> Command:
        prepared = self._partition_for_tool(runtime)
        if prepared is None:
            return self._schedule_result(runtime, summarized_count=0)
        partition, _, _ = prepared
        return self._schedule_result(
            runtime,
            summarized_count=len(partition.messages_to_summarize),
        )

    @staticmethod
    def _schedule_result(
        runtime: ToolRuntime,
        *,
        summarized_count: int,
    ) -> Command:
        if summarized_count > 0:
            content = (
                "Conversation compaction scheduled. The next model call will create "
                f"a summary of {summarized_count} messages, preserving the prompt "
                "cache when a safe stable prefix exists."
            )
        else:
            content = "Conversation does not contain enough history to compact."
        return Command(
            update={
                "_force_cache_compaction": summarized_count > 0,
                "messages": [
                    ToolMessage(
                        content=content,
                        tool_call_id=runtime.tool_call_id or "",
                    )
                ],
            }
        )
