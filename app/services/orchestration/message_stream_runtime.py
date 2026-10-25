from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence

from langchain_core.messages import AIMessage

from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.runtime import ModelCallRecord
from app.services.infrastructure.message_stream_store import MessageStreamWriter
from app.services.infrastructure.rollout_context.runtime.stream_accumulator import (
    CanonicalBlockAccumulator,
)
from app.services.orchestration.activity_runtime import (
    ActivityHandlerRegistry,
    ActivityRuntime,
)
from app.services.orchestration.stream_block_assembler import StreamBlockAssemblyMixin
from app.services.orchestration.terminal_finalization import StreamTerminalStateMixin
from app.services.orchestration.tool_call_registry import (
    PendingToolCompletion,
    StreamToolRegistryMixin,
)
from app.services.orchestration.trace_observer import StreamTraceObserverMixin

NormalizedBlockObserver = Callable[[str, Mapping[str, object]], Awaitable[None]]
CanonicalItemSink = Callable[[Sequence[CanonicalItemRecord]], Awaitable[None]]
ToolResultSink = Callable[[ToolResultCompletion], Awaitable[CanonicalItemRecord]]
ModelCallRegistrar = Callable[[str, int, str], Awaitable[str | None]]
ModelCallOutcomeSink = Callable[[str, str], Awaitable[None]]


class MessageStreamRuntime(
    StreamBlockAssemblyMixin,
    StreamToolRegistryMixin,
    StreamTerminalStateMixin,
    StreamTraceObserverMixin,
):
    """把模型 chunk 和 AgentLoop 生命周期提交到同一个消息流 writer。"""

    def __init__(
        self,
        writer: MessageStreamWriter | None,
        *,
        normalized_block_observer: NormalizedBlockObserver | None = None,
        activity_registry: ActivityHandlerRegistry | None = None,
        canonical_item_sink: CanonicalItemSink | None = None,
        tool_result_sink: ToolResultSink | None = None,
        canonical_turn_id: str | None = None,
        model_call_registrar: ModelCallRegistrar | None = None,
        model_call_outcome_sink: ModelCallOutcomeSink | None = None,
    ) -> None:
        self.writer = writer
        self.activities = (
            ActivityRuntime(writer, activity_registry or ActivityHandlerRegistry())
            if writer is not None
            else None
        )
        self._normalized_block_observer = normalized_block_observer
        self._canonical_item_sink = canonical_item_sink
        self._tool_result_sink = tool_result_sink
        self._canonical_turn_id = canonical_turn_id
        self._model_call_registrar = model_call_registrar
        self._model_call_outcome_sink = model_call_outcome_sink
        self._canonical_block_accumulator: CanonicalBlockAccumulator | None = None
        self._canonical_block_accumulators: dict[str, CanonicalBlockAccumulator] = {}
        self._sealed_canonical_model_call_ids: set[str] = set()
        self._model_execution_ids: dict[str, str] = {}
        self._pending_model_starts: dict[str, asyncio.Future[None]] = {}
        self._summary_candidate_model_call_ids: set[str] = set()
        self._completed_model_call_ids: set[str] = set()
        self._pending_canonical_blocks: list[dict[str, object]] = []
        self.current_model_call_id: str | None = None
        self.current_attempt = 0
        # dict 保留插入顺序，关闭事件可按激活顺序发出且只遍历当前活动项。
        self._active_block_ids: dict[str, None] = {}
        self._closing_blocks: set[str] = set()
        self._active_block_order: list[str] = []
        self._block_metadata: dict[str, tuple[int, str]] = {}
        self._block_local_seq: dict[str, int] = {}
        self._block_model_call_ids: dict[str, str | None] = {}
        self._normalized_text_by_block: dict[str, str] = {}
        self._normalized_carrier_by_block: dict[str, str] = {}
        self._normalized_block_model_call_ids: dict[str, str | None] = {}
        self._tool_call_ids_by_index: dict[tuple[str | None, int], str] = {}
        self._tool_call_indexes_by_id: dict[str, int] = {}
        self._tool_call_order: list[str] = []
        self._tool_call_names_by_id: dict[str, str] = {}
        self._tool_call_arguments: dict[str, str] = {}
        self._tool_call_arguments_by_id: dict[str, dict[str, object]] = {}
        self._tool_call_arguments_complete: dict[str, bool] = {}
        self._provider_tool_call_ids_by_id: dict[str, str] = {}
        self._tool_call_ids_by_provider_key: dict[tuple[str | None, str], str] = {}
        self._tool_invocation_ids_by_call_id: dict[str, str] = {}
        self._tool_call_model_call_ids: dict[str, str | None] = {}
        self._claimed_tool_call_ids: set[str] = set()
        self._completed_tool_call_ids: set[str] = set()
        self._completed_tool_result_call_ids: set[str] = set()
        self._completed_tool_result_execution_by_call_id: dict[str, str] = {}
        self._completed_tool_execution_ids: set[str] = set()
        self._active_tool_executions: dict[str, tuple[str, str]] = {}
        self._pending_tool_completions: dict[str, PendingToolCompletion] = {}
        self._pending_tool_starts: dict[str, asyncio.Future[tuple[str, str]]] = {}
        self._tool_call_bindings_by_callback_run_id: dict[str, asyncio.Future[str]] = {}
        self._resuming_tool_executions: dict[str, str] = {}
        self._stream_state_lock = asyncio.Lock()
        self._tool_completion_lock = asyncio.Lock()
        self._model_completed = False
        self._interruption_facts_finalized = False

    def register_summary_candidate(self, model_call_id: str) -> None:
        """callback 在首个 delta 前登记；仍由 start_model 提交真实 ModelCall。"""
        self._summary_candidate_model_call_ids.add(model_call_id)

    def is_summary_candidate(self, model_call_id: str | None) -> bool:
        return model_call_id in self._summary_candidate_model_call_ids

    async def restore_pending_model(self, assistant: AIMessage, model_call: ModelCallRecord) -> None:
        """由原 checkpoint carrier 与原 stream 恢复登记镜像，不补模型事件或 item。"""
        model_call_id = model_call.model_call_id
        snapshot = (await self.writer.snapshot())["payload"] if self.writer is not None else None
        if snapshot is not None:
            if snapshot["stream_status"] != "open" or snapshot["current_model_call_id"] != model_call_id:
                raise RuntimeError("恢复 checkpoint 与原 TurnStream 的 ModelCall/状态不一致")
            if snapshot["current_attempt"] != model_call.attempt:
                raise RuntimeError("恢复 checkpoint 与原 TurnStream 的 model attempt 不一致")
            for block in snapshot["blocks"]:
                block_id = block["block_id"]
                self._active_block_order.append(block_id)
                self._block_metadata[block_id] = (block["block_index"], block["carrier_type"])
                self._block_model_call_ids[block_id] = block.get("model_call_id")
                if block["status"] == "running":
                    self._active_block_ids[block_id] = None
        self.current_model_call_id = model_call_id
        self.current_attempt = model_call.attempt
        self._model_execution_ids[model_call_id] = model_call.execution_id
        self._completed_model_call_ids.add(model_call_id)
        self._sealed_canonical_model_call_ids.add(model_call_id)
        identity = ItemProjectionIdentity(projection_message_id=assistant.id, model_call_id=model_call_id)
        for index, call in enumerate(assistant.tool_calls):
            provider_id = provider_tool_call_id(identity, call["id"])
            call_id = self._scoped_tool_call_id(provider_id, model_call_id=model_call_id)
            self._tool_call_order.append(call_id)
            self._tool_call_ids_by_index[(model_call_id, index)] = call_id
            self._tool_call_indexes_by_id[call_id] = index
            self._tool_call_names_by_id[call_id] = call["name"]
            self._tool_call_arguments_by_id[call_id] = dict(call["args"])
            self._tool_call_arguments[call_id] = json.dumps(call["args"], ensure_ascii=False)
            self._tool_call_arguments_complete[call_id] = True
            self._provider_tool_call_ids_by_id[call_id] = provider_id
            self._tool_call_ids_by_provider_key[(model_call_id, provider_id)] = call_id
            self._tool_call_model_call_ids[call_id] = model_call_id
            self._tool_invocation_id_for(call_id)
        if snapshot is not None:
            for call in snapshot["tool_calls"]:
                if call["tool_call_id"] in self._tool_call_model_call_ids and call["status"] == "completed":
                    self._completed_tool_call_ids.add(call["tool_call_id"])
            for execution in snapshot["tool_executions"]:
                call_id = execution["tool_call_id"]
                if call_id in self._tool_call_model_call_ids:
                    execution_id = execution["tool_execution_id"]
                    if call_id in self._resuming_tool_executions:
                        raise RuntimeError("同一 pending tool call 存在多个原 attempt")
                    self._resuming_tool_executions[call_id] = execution_id
                    if execution["status"] == "running":
                        self._active_tool_executions[execution_id] = (call_id, execution["tool_name"])
                    else:
                        self._completed_tool_execution_ids.add(execution_id)
                        self._completed_tool_result_call_ids.add(call_id)
                        self._completed_tool_result_execution_by_call_id[call_id] = execution_id

    def resumed_tool_execution_id(self, tool_call_id: str, callback_run_id: str) -> str:
        """续接原未终态 attempt；新 LangGraph callback ID 只用于本次事件关联。"""
        return self._resuming_tool_executions.get(tool_call_id, callback_run_id)
