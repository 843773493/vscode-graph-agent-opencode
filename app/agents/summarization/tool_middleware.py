"""compact_conversation 工具的缓存优先压缩中间件。"""

from __future__ import annotations

from deepagents.middleware.summarization import SummarizationToolMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import (
    AnyMessage,
    ToolMessage,
)
from langgraph.types import Command

from app.agents.summarization.planning import build_safe_compaction_partition
from app.agents.summarization.state import CachePreservingPartition


class CachePreservingSummarizationToolMiddleware(SummarizationToolMiddleware):
    """让 compact_conversation 工具使用与自动压缩相同的缓存优先策略。"""

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
            summarization,
            effective,
            event,
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
