from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence

from app.domain.itemized.records import CanonicalItemRecord
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
from app.services.orchestration.tool_call_registry import StreamToolRegistryMixin
from app.services.orchestration.trace_observer import StreamTraceObserverMixin

NormalizedBlockObserver = Callable[[str, Mapping[str, object]], Awaitable[None]]
CanonicalItemSink = Callable[[Sequence[CanonicalItemRecord]], Awaitable[None]]
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
        writer: MessageStreamWriter,
        *,
        normalized_block_observer: NormalizedBlockObserver | None = None,
        activity_registry: ActivityHandlerRegistry | None = None,
        canonical_item_sink: CanonicalItemSink | None = None,
        canonical_turn_id: str | None = None,
        model_call_registrar: ModelCallRegistrar | None = None,
        model_call_outcome_sink: ModelCallOutcomeSink | None = None,
    ) -> None:
        self.writer = writer
        self.activities = ActivityRuntime(
            writer,
            activity_registry or ActivityHandlerRegistry(),
        )
        self._normalized_block_observer = normalized_block_observer
        self._canonical_item_sink = canonical_item_sink
        self._canonical_turn_id = canonical_turn_id
        self._model_call_registrar = model_call_registrar
        self._model_call_outcome_sink = model_call_outcome_sink
        self._canonical_block_accumulator: CanonicalBlockAccumulator | None = None
        self._canonical_block_accumulators: dict[str, CanonicalBlockAccumulator] = {}
        self._sealed_canonical_model_call_ids: set[str] = set()
        self._model_execution_ids: dict[str, str] = {}
        self._completed_model_call_ids: set[str] = set()
        self._pending_canonical_blocks: list[dict[str, object]] = []
        self.current_model_call_id: str | None = None
        self.current_attempt = 0
        self._active_blocks: set[str] = set()
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
        self._started_tool_execution_ids: set[str] = set()
        self._active_tool_executions: dict[str, tuple[str, str]] = {}
        self._stream_state_lock = asyncio.Lock()
        self._tool_completion_lock = asyncio.Lock()
        self._model_completed = False
        self._interruption_facts_finalized = False
