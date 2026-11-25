"""压缩工具只登记原后台任务；原文读取只进入同 Thread 的 canonical owner。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from app.core.shielded_cleanup import run_sync_shielded
from app.domain.itemized.mutation_intents import MutationIntentOwner

if TYPE_CHECKING:
    from app.runtime.context_compaction import AgentSummarizationCompactor
    from app.schemas.internal_v2.model_reference import ModelReferenceDTO


class CompactConversationInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    runtime: ToolRuntime


class ReadCompactionHistoryInput(CompactConversationInput):
    line_offset: Annotated[int, Field(ge=1)] = 1
    limit: Annotated[int, Field(ge=1, le=2000)] = 200


COMPACT_CONVERSATION_TOOL = StructuredTool(
    name="compact_conversation",
    description="安排当前 Thread 的历史摘要；后台完整生成后，在下一次模型请求通过预算核验时采用。无需参数。",
    args_schema=CompactConversationInput,
)
READ_COMPACTION_HISTORY_TOOL = StructuredTool(
    name="read_compaction_history",
    description=("读取当前精确 Thread 已实际采用摘要对应的历史原文。line_offset从1开始，limit是行数，"
                 "默认200；返回带行号的有界原文，不接受路径、Session或Thread参数。"),
    args_schema=ReadCompactionHistoryInput,
    handle_tool_error=True,
)


class CompactionToolsMiddleware(AgentMiddleware):
    """两个工具共用一个无本地业务状态的运行端口。"""

    def __init__(self, *, compactor: AgentSummarizationCompactor | None,
                 owner: MutationIntentOwner, agent_id: str, preferred_model: ModelReferenceDTO | None = None) -> None:
        self._compactor, self._owner, self._agent_id = compactor, owner, agent_id
        self._preferred_model = preferred_model
        self.tools: list[BaseTool] = [
            COMPACT_CONVERSATION_TOOL.model_copy(update={"func": self._run_compact, "coroutine": self._arun_compact}),
            READ_COMPACTION_HISTORY_TOOL.model_copy(update={"func": self._read_history, "coroutine": self._aread_history}),
        ]

    def _require_compactor(self) -> AgentSummarizationCompactor:
        if self._compactor is None:
            raise RuntimeError("compaction-owner-unavailable: 压缩工具未绑定 durable owner")
        return self._compactor

    def _run_compact(self, runtime: ToolRuntime) -> Command:
        return asyncio.run(self._arun_compact(runtime))

    async def _arun_compact(self, runtime: ToolRuntime) -> Command:
        handle = await self._require_compactor().request(
            self._owner, agent_id=self._agent_id, preferred_model=self._preferred_model, manual=True,
        )
        content = ("当前没有可安全压缩的历史，未创建摘要任务。" if handle is None else
                   f"历史摘要已安排，task_id={handle.task_id}。这只表示任务已受理，尚未切换当前上下文。")
        return Command(update={"messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id or "")]})

    def _read_history(self, runtime: ToolRuntime, line_offset: int = 1, limit: int = 200) -> str:
        try:
            return self._require_compactor().read_history(self._owner, line_offset=line_offset, limit=limit)
        except ValueError as error:
            raise ToolException(str(error)) from error

    async def _aread_history(self, runtime: ToolRuntime, line_offset: int = 1, limit: int = 200) -> str:
        return await run_sync_shielded(self._read_history, runtime, line_offset, limit)
