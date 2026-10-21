"""基于 rollout 增量日志的 LangGraph CheckpointSaver。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Self

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.execution_admission_context import require_current_execution_admission
from app.core.history_loading import HistoryLoadingConfig
from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.mutation_intents import (
    MutationIntentOwner,
)
from app.domain.itemized.parts import ContentPart, ContentPartAnchor
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.runtime import ProvenanceEdge
from app.schemas.internal_v2.turn import (
    TurnHistoryLoadRequest,
    TurnHistoryPageDTO,
    TurnSummaryDTO,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.node_debug.session.fork import (
    NodeDebugWorkspaceForkConfig,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.rollout_context.activation.lineage import (
    ActivationLineageBodyStore,
)
from app.services.infrastructure.rollout_context.activation.store import (
    ResourceActivationStore,
)
from app.services.infrastructure.rollout_context.checkpoint.fork_copy import (
    CheckpointForkCopy,
)
from app.services.infrastructure.rollout_context.checkpoint.fork_owner import (
    CheckpointForkOwner,
)
from app.services.infrastructure.rollout_context.checkpoint.fork_recovery import (
    CheckpointForkRecovery,
)
from app.services.infrastructure.rollout_context.checkpoint.fork_source import (
    CheckpointForkSource,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.mutation.mutation_intents import (
    SessionThreadMutationIntents,
)
from app.services.infrastructure.rollout_context.checkpoint.mutation.saver_mutation_intents import (
    RolloutMutationIntentPortMixin,
    _append_intent_for,
    _AppendIntentBatch,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_epoch_transition import (
    ContextEpochTransitionOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_owner import (
    ContextOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_plan_registry import (
    ContextPlanRegistryOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_reconciliation import (
    ContextReconciliationMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_replay import (
    ContextReplayMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_source import (
    ContextSourceOverlayMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_source_control_port import (
    ContextSourceControlOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.runtime_execution import (
    RuntimeExecutionOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.checkpoint.runtime.async_api import (
    RolloutLangGraphAsyncMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.runtime.langgraph_api import (
    RolloutLangGraphCheckpointMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.writer import (
    RolloutAppendWriter,
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
from app.services.infrastructure.rollout_context.store import (
    RolloutContextStore,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader


class RolloutCheckpointSaver(
    RuntimeExecutionOwnerMixin,
    RolloutMutationIntentPortMixin,
    ContextPlanRegistryOwnerMixin,
    ContextOwnerMixin,
    ContextSourceOverlayMixin,
    ContextSourceControlOwnerMixin,
    ContextEpochTransitionOwnerMixin,
    ContextReconciliationMixin,
    ContextReplayMixin,
    RolloutLangGraphAsyncMixin,
    RolloutLangGraphCheckpointMixin,
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
        storage: RolloutContextStore | None = None,
        writer: RolloutAppendWriter | None = None,
        context_reader: RolloutContextReader | None = None,
        history_reader: RolloutHistoryReader | None = None,
        detail_store: ContextPlanDetailStore | None = None,
        node_debug_store: NodeDebugSessionStore | None = None,
        node_debug_workspace_config: Callable[[], NodeDebugWorkspaceForkConfig]
        | None = None,
        attachment_store: AttachmentBlobStore | None = None,
    ) -> None:
        super().__init__(serde=serde)
        self._serde = serde or JsonPlusSerializer()
        self._attachment_store = attachment_store
        self._storage = storage or RolloutContextStore(
            sessions_dir,
            serde=self._serde,
            message_codec=LangChainMessageCodec(),
            attachment_ref_projector=attachment_store.project_attachment if attachment_store is not None else None,
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
        self._detail_store = detail_store or ContextPlanDetailStore(
            sessions_dir,
        )
        self._resource_activation_store = ResourceActivationStore(
            self._storage, ActivationLineageBodyStore(self._storage, self._detail_store),
        )
        self._node_debug_store = node_debug_store or NodeDebugSessionStore(
            get_session_path_resolver(Path(sessions_dir).resolve())
        )
        self._context_plan_composers: dict[
            tuple[str, str, str], ContextPlanComposer
        ] = {}
        # 这是当前进程的 context view 变化记录，不是第二份持久化事实。
        # storage manifest/source_overlays 仍是恢复后的权威状态；每次对外暴露
        # reconciliation 前都会用已提交 manifest 校准内存记录。
        self._context_reconciliations: dict[
            tuple[str, str, str], ContextReconciliation
        ] = {}
        # request-only 正文只能存在当前进程的实时 view；SQLite/JSONL 只保留
        # contribution 的 identity、revision 和 hash。该缓存不会进入
        # ContextAssemblySnapshot 或任何 canonical history。
        self._request_only_content: dict[tuple[str, str, str, str], object] = {}
        # draft 修订和不同 plan 的同名贡献不能污染全局 source cache。
        # 此 view 只服务于尚未 sealed 的 plan，最终正文仍由 detail store 持有。
        self._plan_request_content: dict[tuple[str, str, str, str, str], object] = {}
        # ModelRequest middleware 在 provider handler 之前封存 assembly，但
        # LangChain 的 model-start event 才会给出真实 model_call_id。按
        # session/namespace/execution 暂存已 sealed plan，event 到达后只消费同一
        # assembly；这不是第二份事实，SQLite snapshot 才是权威。
        self._prepared_dispatches: dict[
            tuple[str, str, str, str], list[dict[str, object]]
        ] = {}
        # SessionThread mutation intent 的 per-owner facade 注册表；saver 是多
        # session 单例，facade 只服务单个 (session_id, thread_id) owner。
        self._mutation_intent_facades: dict[
            MutationIntentOwner, SessionThreadMutationIntents
        ] = {}
        # 正在经 intent 端口消费的 append 批；由 append_items 持锁注册/清理。
        self._append_intent_batches: dict[MutationIntentOwner, _AppendIntentBatch] = {}
        self._lock = threading.RLock()
        fork_source = CheckpointForkSource(
            checkpoint=self, storage=self._storage, context_reader=self._context_reader,
            attachment_store=attachment_store,
            node_debug_store=self._node_debug_store,
            node_debug_workspace_config=node_debug_workspace_config,
        )
        self.forks = CheckpointForkOwner(
            storage=self._storage, source=fork_source,
            copy=CheckpointForkCopy(
                checkpoint=self, source=fork_source, storage=self._storage,
                context_reader=self._context_reader,
            ),
            recovery=CheckpointForkRecovery(storage=self._storage, source=fork_source),
        )


    def append_items(
        self,
        session_id: str,
        items: Sequence[object],
        *,
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> tuple[int, ...]:
        """由 Saver owner 以 AppendCanonicalItemIntent 追加 canonical items。

        真实 append 调用点在此表达为 typed intent，经唯一 SessionThread
        mutation owner 校验（owner 一致 + 幂等键去重）后进入同一 storage
        事务；重复消费、owner 不匹配、intent 构造失败都显式抛出。
        """
        if not all(isinstance(item, CanonicalItemRecord) for item in items):
            raise TypeError("canonical item sink 只接受 CanonicalItemRecord")
        typed_items = tuple(items)
        if not typed_items:
            raise ValueError("append_items 至少需要一个 canonical item")
        if checkpoint_ns:
            # SessionThread owner 身份由调用方显式提供；checkpoint_ns 只标识 rollout namespace。
            raise ValueError(
                "canonical append intent 不接受非空 checkpoint_ns: "
                f"{checkpoint_ns!r} (session_id={session_id!r})"
            )
        admission = require_current_execution_admission(langgraph_thread_id=thread_id)
        if (admission.session_id, admission.thread_id) != (session_id, thread_id):
            raise RuntimeError(
                "canonical append owner 与 execution admission 不一致: "
                f"append=({session_id},{thread_id}), "
                f"admission=({admission.session_id},{admission.thread_id})"
            )
        owner = MutationIntentOwner(session_id=session_id, thread_id=thread_id)
        if any(item.turn_id != admission.turn_id for item in typed_items):
            raise RuntimeError(
                "canonical append item Turn attribution 与 execution admission 不一致: "
                f"execution_id={admission.execution_id}"
            )
        # 先构造全部 intent：任一字段不合法都在触碰 storage 前显式失败。
        intents = tuple(
            _append_intent_for(owner, item, execution_id=admission.execution_id)
            for item in typed_items
        )
        with self._lock:
            facade = self._mutation_intent_facade(owner)
            batch = _AppendIntentBatch(
                owner=owner,
                checkpoint_ns=checkpoint_ns,
                execution_id=admission.execution_id,
                records=typed_items,
                item_ids=frozenset(item.item_id for item in typed_items),
            )
            self._append_intent_batches[owner] = batch
            try:
                for intent in intents:
                    facade.consume(intent)
            finally:
                self._append_intent_batches.pop(owner, None)
        if batch.commit_ids is None:
            raise RuntimeError("append intent 批未被 owner 端口提交")
        return batch.commit_ids


    def safe_compaction_prefix_cutoffs(
        self,
        session_id: str,
        thread_id: str,
        *,
        checkpoint_ns: str,
        state_messages: Sequence[object],
        cutoff_indexes: Sequence[int],
    ) -> frozenset[int]:
        """把 model 任务命名空间交给同一持久 context owner 做只读 preflight。"""
        return self._storage.checkpoints.safe_compaction_prefix_cutoffs(
            session_id,
            thread_id,
            checkpoint_ns=(
                self.context_owner_namespace(checkpoint_ns)
                if isinstance(checkpoint_ns, str)
                else checkpoint_ns
            ),
            state_messages=state_messages,
            cutoff_indexes=cutoff_indexes,
        )


    def get_context_assembly(
        self,
        session_id: str,
        thread_id: str,
        *,
        assembly_id: str,
        checkpoint_ns: str = "",
    ) -> ContextAssemblySnapshot:
        """由 Saver 提供已提交 assembly，屏蔽底层 SQLite 读取。"""
        return self._storage.assemblies.get_context_assembly(
            session_id,
            thread_id,
            assembly_id=assembly_id,
            checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
        )


    def list_context_assemblies(
        self,
        session_id: str,
        thread_id: str,
        *,
        turn_id: str | None = None,
    ) -> tuple[ContextAssemblySnapshot, ...]:
        """由 Saver 提供已提交 assembly 列表。"""
        return self._storage.assemblies.list_context_assemblies(
            session_id,
            thread_id,
            turn_id=turn_id,
        )


    def register_content_part(
        self,
        session_id: str,
        *,
        thread_id: str,
        item_id: str,
        part: ContentPart,
        locator: Mapping[str, object],
        checkpoint_ns: str = "",
    ) -> None:
        """通过 Saver 持久化 canonical item 的 content-part locator。"""
        self._storage.storage.register_content_part(
            session_id,
            thread_id=thread_id,
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
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> None:
        """通过 Saver 持久化可恢复的 content-part 操作锚点。"""
        self._storage.storage.register_content_part_anchor(
            session_id,
            anchor,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
        )

    def resolve_content_part_anchor(
        self,
        session_id: str,
        *,
        thread_id: str,
        anchor_id: str,
        checkpoint_ns: str = "",
    ) -> ContentPartAnchor:
        """通过 Saver 恢复并校验 durable content-part anchor。"""
        return self._storage.storage.resolve_content_part_anchor(
            session_id,
            thread_id=thread_id,
            anchor_id=anchor_id,
            checkpoint_ns=checkpoint_ns,
        )

    def register_provenance_edge(
        self,
        session_id: str,
        thread_id: str,
        edge: ProvenanceEdge,
        *,
        checkpoint_ns: str = "",
    ) -> None:
        """通过 Saver 持久化 item/replay/fork provenance。"""
        self._storage.storage.register_provenance_edge(
            session_id,
            thread_id,
            edge,
            checkpoint_ns=checkpoint_ns,
        )

    def history_read_lease(
        self,
        session_id: str,
        thread_id: str,
    ) -> AbstractAsyncContextManager[None]:
        """历史异步入口借用同一 reader 准入，未物化状态仍交给原历史规则。"""
        return self._context_reader.read_lease(
            session_id, thread_id, allow_unmaterialized=True,
        )

    def bootstrap_history(
        self,
        session_id: str,
        thread_id: str,
        *,
        policy: HistoryLoadingConfig | None = None,
    ) -> tuple[TurnSummaryDTO | None, str | None, int]:
        """通过内部历史 reader 生成会话 bootstrap。"""
        latest, older_cursor, projection_epoch = self._history_reader.bootstrap(
            session_id,
            thread_id,
            policy=policy,
        )
        return latest, older_cursor, projection_epoch

    def mark_turn_terminal_status(
        self,
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
        status: str,
    ) -> bool:
        """通过 Saver 统一持久化失败、取消和超时等 Turn 终态。"""
        return self._writer.mark_turn_terminal_status(
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            status=status,
        )

    def mark_job_terminal_status(
        self,
        *,
        session_id: str,
        thread_id: str,
        job_id: str,
        status: str,
    ) -> bool:
        return self._writer.mark_job_terminal_status(
            session_id=session_id,
            thread_id=thread_id,
            job_id=job_id,
            status=status,
        )

    def load_history(
        self,
        session_id: str,
        thread_id: str,
        request: TurnHistoryLoadRequest,
        *,
        policy: HistoryLoadingConfig | None = None,
    ) -> TurnHistoryPageDTO:
        """通过内部历史 reader 读取 projection/detail 历史页面。"""
        return self._history_reader.load(
            session_id,
            thread_id,
            request,
            policy=policy,
        )

    def has_committed_tool_result(self, session_id: str, thread_id: str, model_call_id: str, tool_call_id: str) -> bool:
        return self._storage.storage.has_committed_tool_result(session_id, thread_id, model_call_id, tool_call_id)

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
