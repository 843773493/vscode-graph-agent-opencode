"""基于 rollout 增量日志的 LangGraph CheckpointSaver。"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.history_loading import HistoryLoadingConfig
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.parts import ContentPart, ContentPartAnchor
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.runtime import ProvenanceEdge
from app.schemas.internal_v2.turn import (
    TurnHistoryLoadRequest,
    TurnHistoryPageDTO,
    TurnSummaryDTO,
)
from app.services.infrastructure.rollout_context.checkpoint.async_api import (
    RolloutLangGraphAsyncMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.context_owner import (
    ContextOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.context_plan_registry import (
    ContextPlanRegistryOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.context_reconciliation import (
    ContextReconciliationMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.context_replay import (
    ContextReplayMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.context_source import (
    ContextSourceOverlayMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.context_source_control import (
    ContextSourceControlOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.fork_compaction import (
    ForkCompactionMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.langgraph_api import (
    RolloutLangGraphCheckpointMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.runtime.reconciliation import (
    ContextReconciliation,
)
from app.services.infrastructure.rollout_context.storage.append_writer import (
    RolloutAppendWriter,
)
from app.services.infrastructure.rollout_context.storage.service import (
    RolloutStorage,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader


class RolloutCheckpointSaver(
    ContextPlanRegistryOwnerMixin,
    ContextOwnerMixin,
    ContextSourceOverlayMixin,
    ContextSourceControlOwnerMixin,
    ContextReconciliationMixin,
    ContextReplayMixin,
    RolloutLangGraphAsyncMixin,
    RolloutLangGraphCheckpointMixin,
    ForkCompactionMixin,
    BaseCheckpointSaver[str],
    AbstractContextManager,
    AbstractAsyncContextManager,
):
    """将 messages channel 作为 rollout 增量记录，其它 channel 随 checkpoint 保存。"""

    def __init__(
        self,
        sessions_dir: str | Path,
        *,
        serde: JsonPlusSerializer | None = None,
        storage: RolloutStorage | None = None,
        writer: RolloutAppendWriter | None = None,
        context_reader: RolloutContextReader | None = None,
        history_reader: RolloutHistoryReader | None = None,
        detail_store: ContextPlanDetailStore | None = None,
        protected_detail_key: bytes | None = None,
    ) -> None:
        super().__init__(serde=serde)
        self._serde = serde or JsonPlusSerializer()
        self._storage = storage or RolloutStorage(
            sessions_dir,
            serde=self._serde,
            message_codec=LangChainMessageCodec(),
        )
        self._writer = writer or RolloutAppendWriter(
            sessions_dir,
            storage=self._storage,
        )
        self._context_reader = context_reader or RolloutContextReader(self._storage)
        # 历史 DTO 读取器属于 Saver 内部实现，业务层只依赖本类。
        if history_reader is None:
            from app.services.infrastructure.rollout_history_reader import (
                RolloutHistoryReader,
            )

            history_reader = RolloutHistoryReader(self._context_reader)
        self._history_reader = history_reader
        if detail_store is not None and protected_detail_key is not None:
            raise ValueError(
                "detail_store 与 protected_detail_key 不能同时传入"
            )
        self._detail_store = detail_store or ContextPlanDetailStore(
            sessions_dir,
            protected_key=protected_detail_key,
        )
        self._context_plan_composers: dict[tuple[str, str], ContextPlanComposer] = {}
        # 这是当前进程的 context view 变化记录，不是第二份持久化事实。
        # storage manifest/source_overlays 仍是恢复后的权威状态；每次对外暴露
        # reconciliation 前都会用已提交 manifest 校准内存记录。
        self._context_reconciliations: dict[tuple[str, str], ContextReconciliation] = {}
        # request-only 正文只能存在当前进程的实时 view；SQLite/JSONL 只保留
        # contribution 的 identity、revision 和 hash。该缓存不会进入
        # ContextAssemblySnapshot 或任何 canonical history。
        self._request_only_content: dict[tuple[str, str, str], object] = {}
        # draft 修订和不同 plan 的同名贡献不能污染全局 source cache。
        # 此 view 只服务于尚未 sealed 的 plan，最终正文仍由 detail store 持有。
        self._plan_request_content: dict[tuple[str, str, str, str], object] = {}
        # ModelRequest middleware 在 provider handler 之前封存 assembly，但
        # LangChain 的 model-start event 才会给出真实 model_call_id。按
        # session/namespace/turn 暂存已 sealed plan，event 到达后只消费同一
        # assembly；这不是第二份事实，SQLite snapshot 才是权威。
        self._prepared_dispatches: dict[
            tuple[str, str, str], list[dict[str, object]]
        ] = {}
        self._lock = threading.RLock()

    def upgrade_rollout_schema(
        self, session_id: str, *, checkpoint_ns: str = "",
    ) -> None:
        """显式升级当前会话索引；普通启动、读取和请求路径不得自动调用。"""
        # 只有显式升级加载旧 artifact parser；正常 Saver 构造和读取路径不加载它。
        from app.services.infrastructure.rollout_context.migration.schema_v3 import (
            prepare_schema_v3_upgrade,
            resume_schema_v3_upgrade_audits,
        )
        from app.services.infrastructure.rollout_context.migration.schema_v4 import (
            prepare_schema_v4_upgrade,
        )

        def prepare_artifacts(connection):
            return prepare_schema_v3_upgrade(
                connection, rollout_root=self._storage.root(session_id, checkpoint_ns),
                session_id=session_id, checkpoint_ns=checkpoint_ns,
                detail_capability=self._detail_store.schema_v3_detail_capability(),
            )

        def resume_artifacts(connection):
            return resume_schema_v3_upgrade_audits(
                connection, rollout_root=self._storage.root(session_id, checkpoint_ns),
                checkpoint_ns=checkpoint_ns,
            )

        def prepare_plans(connection):
            return prepare_schema_v4_upgrade(
                connection, session_id=session_id, checkpoint_ns=checkpoint_ns,
            )

        with self._storage.upgrade_v2_schema(
            session_id, checkpoint_ns=checkpoint_ns,
            prepare_artifact_upgrade=prepare_artifacts,
            resume_artifact_upgrade=resume_artifacts,
            prepare_plan_upgrade=prepare_plans,
        ):
            pass

    def accept_turn(
        self,
        session_id: str,
        *,
        accepted_ingress_id: str,
        acceptance_idempotency_key: str,
        payload: object,
        payload_kind: str = "text",
        checkpoint_ns: str = "",
        branch_id: str | None = None,
        turn_id: str | None = None,
        turn_ordinal: int | None = None,
        root_item_id: str | None = None,
        initial_execution_id: str | None = None,
        acceptance_metadata: Mapping[str, object] | None = None,
        replay_of_turn_id: str | None = None,
        identity_origin: str = "runtime",
    ) -> dict[str, object]:
        """在 checkpoint owner 内接受一个真实用户 Turn。"""
        return self._storage.accept_turn(
            session_id,
            accepted_ingress_id=accepted_ingress_id,
            acceptance_idempotency_key=acceptance_idempotency_key,
            payload=payload,
            payload_kind=payload_kind,
            checkpoint_ns=checkpoint_ns,
            branch_id=branch_id,
            turn_id=turn_id,
            turn_ordinal=turn_ordinal,
            root_item_id=root_item_id,
            initial_execution_id=initial_execution_id,
            acceptance_metadata=acceptance_metadata,
            replay_of_turn_id=replay_of_turn_id,
            identity_origin=identity_origin,
        )

    def append_items(
        self,
        session_id: str,
        items: Sequence[object],
        *,
        checkpoint_ns: str = "",
    ) -> tuple[int, ...]:
        """由 Saver owner 追加 provider/tool canonical items。"""
        if not all(isinstance(item, CanonicalItemRecord) for item in items):
            raise TypeError("canonical item sink 只接受 CanonicalItemRecord")
        return self._storage.append_items(
            session_id,
            tuple(item for item in items if isinstance(item, CanonicalItemRecord)),
            checkpoint_ns=checkpoint_ns,
        )

    def execution_for_turn(
        self, session_id: str, *, turn_id: str, checkpoint_ns: str = ""
    ) -> str:
        return self._storage.execution_for_turn(
            session_id, turn_id=turn_id, checkpoint_ns=checkpoint_ns
        )

    def safe_compaction_prefix_cutoffs(
        self,
        session_id: str,
        *,
        checkpoint_ns: str,
        state_messages: Sequence[object],
        cutoff_indexes: Sequence[int],
    ) -> frozenset[int]:
        """compaction preflight 只读端口；唯一实现由 storage owner 提供。"""
        return self._storage.safe_compaction_prefix_cutoffs(
            session_id,
            checkpoint_ns=checkpoint_ns,
            state_messages=state_messages,
            cutoff_indexes=cutoff_indexes,
        )

    def dispatch_replay(
        self, session_id: str, *, turn_id: str, checkpoint_ns: str = ""
    ) -> str:
        """通过 Saver 绑定原 Turn dispatch，不创建新的 Turn。"""
        return self._storage.dispatch_replay(
            session_id,
            turn_id=turn_id,
            checkpoint_ns=checkpoint_ns,
        )

    def register_model_call(
        self,
        session_id: str,
        *,
        execution_id: str,
        model_call_id: str,
        attempt: int,
        provider: str,
        provider_request_id: str | None = None,
        retry_of_model_call_id: str | None = None,
        assembly_id: str | None = None,
        dispatch_state: str = "ready",
        checkpoint_ns: str = "",
    ) -> None:
        self._storage.register_model_call(
            session_id,
            execution_id=execution_id,
            model_call_id=model_call_id,
            attempt=attempt,
            provider=provider,
            provider_request_id=provider_request_id,
            retry_of_model_call_id=retry_of_model_call_id,
            assembly_id=assembly_id,
            dispatch_state=dispatch_state,
            checkpoint_ns=checkpoint_ns,
        )

    def update_model_call_outcome(
        self,
        session_id: str,
        *,
        model_call_id: str,
        outcome: str,
        dispatch_state: str | None = None,
        checkpoint_ns: str = "",
    ) -> None:
        self._storage.update_model_call_outcome(
            session_id,
            model_call_id=model_call_id,
            outcome=outcome,
            dispatch_state=dispatch_state,
            checkpoint_ns=checkpoint_ns,
        )

    def get_context_assembly(
        self,
        session_id: str,
        *,
        assembly_id: str,
    ) -> ContextAssemblySnapshot:
        """由 Saver 提供已提交 assembly，屏蔽底层 SQLite 读取。"""
        return self._storage.get_context_assembly(
            session_id,
            assembly_id=assembly_id,
        )

    def list_context_assemblies(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
    ) -> tuple[ContextAssemblySnapshot, ...]:
        """由 Saver 提供已提交 assembly 列表。"""
        return self._storage.list_context_assemblies(
            session_id,
            turn_id=turn_id,
        )

    def converge_execution(
        self,
        session_id: str,
        *,
        turn_id: str,
        execution_id: str,
        outcome: str,
        turn_status: str,
        items: Sequence[CanonicalItemRecord] = (),
        final_item_id: str | None = None,
        assembly_id: str | None = None,
        checkpoint_ns: str = "",
    ) -> int:
        """通过 Saver 原子收敛 execution、output item、assembly 与 Turn。"""
        return self._storage.converge_execution(
            session_id,
            turn_id=turn_id,
            execution_id=execution_id,
            outcome=outcome,
            turn_status=turn_status,
            items=items,
            final_item_id=final_item_id,
            assembly_id=assembly_id,
            checkpoint_ns=checkpoint_ns,
        )

    def resume_turn(
        self, session_id: str, *, turn_id: str, checkpoint_ns: str = ""
    ) -> dict[str, object]:
        return self._storage.resume_turn(
            session_id, turn_id=turn_id, checkpoint_ns=checkpoint_ns
        )

    def mark_execution_lost(
        self,
        session_id: str,
        *,
        turn_id: str,
        execution_id: str | None = None,
        checkpoint_ns: str = "",
        reason: str = "provider_result_commit_missing",
    ) -> dict[str, object]:
        """通过 Saver 记录 provider dispatch 后丢失的 execution。"""
        return self._storage.mark_execution_lost(
            session_id,
            turn_id=turn_id,
            execution_id=execution_id,
            checkpoint_ns=checkpoint_ns,
            reason=reason,
        )

    def register_content_part(
        self,
        session_id: str,
        *,
        item_id: str,
        part: ContentPart,
        locator: Mapping[str, object],
        checkpoint_ns: str = "",
    ) -> None:
        """通过 Saver 持久化 canonical item 的 content-part locator。"""
        self._storage.register_content_part(
            session_id,
            item_id=item_id,
            part=part,
            locator=locator,
            checkpoint_ns=checkpoint_ns,
        )

    def register_content_part_anchor(
        self,
        session_id: str,
        anchor: ContentPartAnchor,
        *,
        checkpoint_ns: str = "",
    ) -> None:
        """通过 Saver 持久化可恢复的 content-part 操作锚点。"""
        self._storage.register_content_part_anchor(
            session_id,
            anchor,
            checkpoint_ns=checkpoint_ns,
        )

    def resolve_content_part_anchor(
        self,
        session_id: str,
        *,
        anchor_id: str,
        checkpoint_ns: str = "",
    ) -> ContentPartAnchor:
        """通过 Saver 恢复并校验 durable content-part anchor。"""
        return self._storage.resolve_content_part_anchor(
            session_id,
            anchor_id=anchor_id,
            checkpoint_ns=checkpoint_ns,
        )

    def register_provenance_edge(
        self,
        session_id: str,
        edge: ProvenanceEdge,
        *,
        checkpoint_ns: str = "",
    ) -> None:
        """通过 Saver 持久化 item/replay/fork provenance。"""
        self._storage.register_provenance_edge(
            session_id,
            edge,
            checkpoint_ns=checkpoint_ns,
        )

    def bootstrap_history(
        self,
        session_id: str,
        *,
        policy: HistoryLoadingConfig | None = None,
    ) -> tuple[TurnSummaryDTO | None, str | None, int]:
        """通过内部历史 reader 生成会话 bootstrap。"""
        latest, older_cursor, projection_epoch = self._history_reader.bootstrap(
            session_id,
            policy=policy,
        )
        return latest, older_cursor, projection_epoch

    def mark_turn_terminal_status(
        self,
        *,
        session_id: str,
        turn_id: str,
        status: str,
    ) -> bool:
        """通过 Saver 统一持久化失败、取消和超时等 Turn 终态。"""
        return self._writer.mark_turn_terminal_status(
            session_id=session_id,
            turn_id=turn_id,
            status=status,
        )

    def load_history(
        self,
        session_id: str,
        request: TurnHistoryLoadRequest,
        *,
        policy: HistoryLoadingConfig | None = None,
    ) -> TurnHistoryPageDTO:
        """通过内部历史 reader 读取 projection/detail 历史页面。"""
        return self._history_reader.load(session_id, request, policy=policy)

    def get_next_version(self, current: str | None, channel: None) -> str:
        if current is None:
            current_version = 0
        elif isinstance(current, int):
            current_version = current
        else:
            current_version = int(current.split(".", 1)[0])
        return f"{current_version + 1:032}.{__import__('os').urandom(4).hex()}"

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def _tuple_from_index(self, thread_id: str, index: Any) -> CheckpointTuple:
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": index.checkpoint_ns,
                "checkpoint_id": index.checkpoint_id,
            }
        }
        value = self.get_tuple(config)
        if value is None:
            raise RuntimeError(
                f"checkpoint 索引无法 materialize: {index.checkpoint_id}"
            )
        return value

    def _decode_metadata(self, index: Any) -> CheckpointMetadata:
        return self._storage.metadata(index)


def _checkpoint_identity(config: RunnableConfig) -> tuple[str, str]:
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        raise TypeError("checkpoint config 缺少 configurable")
    thread_id = configurable.get("thread_id")
    checkpoint_ns = configurable.get("checkpoint_ns", "")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("checkpoint config 缺少 thread_id")
    if not isinstance(checkpoint_ns, str):
        raise TypeError("checkpoint config checkpoint_ns 必须是字符串")
    return thread_id, checkpoint_ns


def _messages_from_checkpoint(value: CheckpointTuple | None) -> list[BaseMessage]:
    if value is None:
        return []
    channel_values = value.checkpoint.get("channel_values", {})
    if not isinstance(channel_values, dict):
        raise TypeError("checkpoint channel_values 必须是 dict")
    messages = channel_values.get("messages", [])
    if not isinstance(messages, list) or not all(
        isinstance(message, BaseMessage) for message in messages
    ):
        raise TypeError("checkpoint messages 必须是 BaseMessage 列表")
    return messages
