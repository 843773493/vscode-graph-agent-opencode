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

from app.abstractions.resource_file_content import ResourceFileContent
from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.core.execution_admission_context import require_current_execution_admission
from app.core.history_loading import HistoryLoadingConfig
from app.core.path_utils import get_session_path_resolver
from app.core.resource_uri import parse_session_artifact_uri
from app.core.sqlite_state import utc_now_text
from app.core.trace_middleware import get_current_gateway_id
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
)
from app.domain.itemized.identity.item_projection import ItemProvenanceRelation
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.mutation_intents import (
    MutationIntentOwner,
)
from app.domain.itemized.parts import ContentPart, ContentPartAnchor
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.runtime import ModelCallRecord, ProvenanceEdge
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
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    _ValidatedPredecessors,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    ToolProtocolBoundaryConflict,
    validate_tool_protocol_closure,
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











    def ensure_request_items(
        self, session_id: str, thread_id: str, *, turn_id: str | None,
        execution_id: str, messages: Sequence[object], checkpoint_ns: str = "",
    ) -> tuple[str, ...]:
        """模型请求先映射完整 canonical group，再交唯一 storage owner 原子提交。"""
        codec = self._storage.storage._codec()
        eligible = []
        for index, message in enumerate(messages):
            role = codec.message_role(message)
            if role == "tool" or codec.tool_calls(message):
                eligible.append((index, message, codec.message_id(message, index), role))
        with self._storage.storage._lock(thread_id, checkpoint_ns, session_id=session_id):
            with self._storage.storage._connect(
                thread_id, checkpoint_ns, session_id=session_id, read_only=True,
            ) as connection:
                existing_groups = self._storage.storage.read_request_groups(
                    session_id, thread_id, turn_id=turn_id, execution_id=execution_id,
                    message_ids=tuple(value[2] for value in eligible), checkpoint_ns=checkpoint_ns,
                    writer_connection=connection,
                )
            pending = []
            for (index, message, message_id, role), stored in zip(eligible, existing_groups, strict=True):
                model_call_id = (
                    codec.tool_message_model_call_id(message, messages[:index]) if role == "tool"
                    else codec.model_call_id(message) if role == "assistant" else None
                )
                group = tuple(codec.items_for_message(
                    message, item_sequence=stored.items[0].item_sequence if stored.items else 1, message_id=message_id,
                    turn_id=stored.turn_id, execution_id=stored.execution_id,
                    timestamp=utc_now_text(), model_call_id=model_call_id,
                    canonical_items=stored.items,
                ))
                if stored.items:
                    if (
                        len(group) != len(stored.items)
                        or codec.project_message(group) != codec.project_message(stored.items)
                        or any(
                            item.item_id != saved.item_id or item.semantic_kind != saved.semantic_kind
                            or item.content_hash != saved.content_hash or item.turn_id != saved.turn_id
                            or item.turn_scope != saved.turn_scope
                            for item, saved in zip(group, stored.items, strict=True)
                        )
                    ):
                        raise RuntimeError(f"request 与既有 canonical message group 不一致: {message_id}")
                else:
                    pending.append(group)
            return self._storage.executions.append_request_groups(
                session_id, thread_id, groups=pending, execution_id=execution_id, checkpoint_ns=checkpoint_ns,
            )

    def prepare_model_call_boundary(
        self, session_id: str, thread_id: str, *, turn_id: str | None,
        execution_id: str, messages: Sequence[object], checkpoint_ns: str = "",
        validated_predecessors: _ValidatedPredecessors,
    ) -> tuple[ToolProtocolBoundaryConflict | None, ContextAssemblySnapshot | None]:
        """先提交 canonical group 并检查闭合；预期冲突不撤销已提交事实。"""
        admission = require_current_execution_admission(langgraph_thread_id=thread_id)
        if (admission.session_id, admission.thread_id, admission.execution_id, admission.turn_id) != (
            session_id, thread_id, execution_id, turn_id,
        ):
            raise RuntimeError("model-call boundary 与当前 execution admission 不一致")
        checkpoint_ns = self.context_owner_namespace(checkpoint_ns)
        # Provider prepare 已持有请求级校验作用域，不能在 boundary 内重置它。
        # TODO：独立 boundary 准入的校验复用留待后续性能优化。
        with self._lock:
            if turn_id is not None:
                self._storage.checkpoints.ensure_active_view_contains_turn_root(
                    session_id, thread_id, turn_id=turn_id, checkpoint_ns=checkpoint_ns,
                )
            self.ensure_request_items(
                session_id, thread_id, turn_id=turn_id, execution_id=execution_id,
                messages=messages, checkpoint_ns=checkpoint_ns,
            )
            with self._context_reader.open_snapshot(session_id, thread_id, checkpoint_ns) as snapshot:
                predecessor = self._storage.assemblies.latest_context_assembly(
                    snapshot, validated_predecessors=validated_predecessors,
                )
                items = self._storage.checkpoints.committed_context_items(
                    snapshot, predecessor=predecessor,
                )
                sequences = self._storage.checkpoints.selected_context_message_sequences(snapshot, items=items)
                try:
                    validate_tool_protocol_closure(snapshot.connection, sequences, len(sequences))
                except ToolProtocolBoundaryConflict as conflict:
                    return conflict, predecessor
        return None, predecessor

    def accept_turn(
        self,
        session_id: str,
        *,
        thread_id: str,
        accepted_ingress_id: str,
        acceptance_idempotency_key: str,
        job_id: str,
        admission_idempotency_key: str,
        payload: object,
        payload_kind: str = "text",
        checkpoint_ns: str = "",
        branch_id: str | None = None,
        turn_id: str | None = None,
        turn_ordinal: int | None = None,
        root_item_id: str | None = None,
        initial_execution_id: str | None = None,
        extensions: Mapping[str, object] | None = None,
        source_session_id: str | None = None,
        source_accepted_ingress_id: str | None = None,
        source_acceptance_idempotency_key: str | None = None,
        provenance_relations: tuple[ItemProvenanceRelation, ...] = (),
        replay_of_turn_id: str | None = None,
        identity_origin: str = "runtime",
    ) -> dict[str, object]:
        """在 checkpoint owner 内接受一个真实用户 Turn。"""
        return self._storage.executions.accept_turn(
            session_id,
            thread_id=thread_id,
            accepted_ingress_id=accepted_ingress_id,
            acceptance_idempotency_key=acceptance_idempotency_key,
            job_id=job_id,
            admission_idempotency_key=admission_idempotency_key,
            payload=payload,
            payload_kind=payload_kind,
            checkpoint_ns=checkpoint_ns,
            branch_id=branch_id,
            turn_id=turn_id,
            turn_ordinal=turn_ordinal,
            root_item_id=root_item_id,
            initial_execution_id=initial_execution_id,
            extensions=extensions,
            source_session_id=source_session_id,
            source_accepted_ingress_id=source_accepted_ingress_id,
            source_acceptance_idempotency_key=source_acceptance_idempotency_key,
            provenance_relations=provenance_relations,
            replay_of_turn_id=replay_of_turn_id,
            identity_origin=identity_origin,
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

    def commit_tool_result(
        self, *, session_id: str, thread_id: str, job_id: str,
        completion: ToolResultCompletion, recovery: bool = False,
        checkpoint_ns: str = "",
    ) -> CanonicalItemRecord:
        """工具终态始终解析原 Job 的 durable admission，不借用后继请求上下文。"""
        if checkpoint_ns:
            raise ValueError(f"canonical 工具终态不接受非空 checkpoint_ns: {checkpoint_ns!r}")
        admission = self.execution_admission_for_job(
            session_id, thread_id=thread_id, job_id=job_id, checkpoint_ns=checkpoint_ns,
        )
        return self._storage.executions.commit_tool_result(
            admission, completion, recovery=recovery, checkpoint_ns=checkpoint_ns,
        )

    def execution_for_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> str:
        return self._storage.executions.execution_for_turn(
            session_id,
            turn_id=turn_id,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
        )

    def execution_admission_for_job(
        self,
        session_id: str,
        *,
        thread_id: str,
        job_id: str,
        checkpoint_ns: str = "",
    ) -> ExecutionAdmission:
        return self._storage.executions.execution_admission_for_job(
            session_id,
            thread_id=thread_id,
            job_id=job_id,
            checkpoint_ns=checkpoint_ns,
        )

    def read_resumable_model_call(self, admission: ExecutionAdmission, model_call_id: str, *, carrier_items: tuple[CanonicalItemRecord, ...]) -> tuple[ModelCallRecord, str] | None:
        return self._storage.executions.read_resumable_model_call(admission, model_call_id, carrier_items=carrier_items)

    def execution_admission(
        self,
        session_id: str,
        *,
        thread_id: str,
        execution_id: str,
        checkpoint_ns: str = "",
    ) -> ExecutionAdmission:
        return self._storage.executions.execution_admission(
            session_id,
            thread_id=thread_id,
            execution_id=execution_id,
            checkpoint_ns=checkpoint_ns,
        )

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

    def dispatch_replay(
        self,
        session_id: str,
        *,
        thread_id: str,
        turn_id: str,
        checkpoint_ns: str = "",
    ) -> str:
        """通过 Saver 绑定原 Turn dispatch，不创建新的 Turn。"""
        return self._storage.executions.dispatch_replay(
            session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            checkpoint_ns=checkpoint_ns,
        )

    def register_model_call(
        self,
        session_id: str,
        *,
        thread_id: str,
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
        self._storage.executions.register_model_call(
            session_id,
            thread_id=thread_id,
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
        thread_id: str,
        model_call_id: str,
        outcome: str,
        dispatch_state: str | None = None,
        checkpoint_ns: str = "",
    ) -> None:
        self._storage.executions.update_model_call_outcome(
            session_id,
            thread_id=thread_id,
            model_call_id=model_call_id,
            outcome=outcome,
            dispatch_state=dispatch_state,
            checkpoint_ns=checkpoint_ns,
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

    def read_tool_call_attachment(
        self, session_id: str, *, thread_id: str, tool_call_id: str,
        model_call_id: str, file_id: str, checkpoint_ns: str = "",
    ) -> ResourceFileContent:
        """附件 capability 只来自 emitting model-call 的已封存 selection。"""
        from app.services.mapping.itemized.selection import resolve_selected_item

        if self._attachment_store is None:
            raise RuntimeError("附件工具未装配唯一 AttachmentBlobStore")
        target = parse_session_artifact_uri(file_id, local_gateway_id=get_current_gateway_id()).display_uri
        assembly = self.load_tool_call_context_assembly(
            session_id, thread_id=thread_id, tool_call_id=tool_call_id,
            model_call_id=model_call_id, checkpoint_ns=checkpoint_ns,
        )
        entries = tuple(
            entry for entry in assembly.selection
            if entry.included and isinstance(entry.ref, ContextRef)
            and entry.ref.ref_type == "canonical_item"
        )
        items = self._storage.storage.read_ref_items(
            session_id, thread_id, checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
            refs=tuple(entry.ref for entry in entries),
        )
        for entry in entries:
            item = resolve_selected_item(entry, items)
            payload = self._storage.storage.project_attachment_payload(session_id, thread_id, item.payload)
            if not isinstance(payload, Mapping):
                continue
            attachments = payload.get("attachments", ())
            if any(isinstance(ref, Mapping) and isinstance(ref.get("file_id"), str)
                   and "://" in ref["file_id"]
                   and parse_session_artifact_uri(ref["file_id"], local_gateway_id=get_current_gateway_id()).display_uri == target
                   for ref in attachments):
                return self._attachment_store.read(session_id, file_id, owner_thread_id=thread_id)
        raise PermissionError("附件未属于当前工具原始 assembly 的 item/view membership")

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

    def converge_execution(
        self,
        session_id: str,
        *,
        thread_id: str,
        turn_id: str | None,
        execution_id: str,
        outcome: str,
        turn_status: str | None,
        items: Sequence[CanonicalItemRecord] = (),
        final_item_id: str | None = None,
        assembly_id: str | None = None,
        checkpoint_ns: str = "",
        loss_reason: str | None = None,
    ) -> int:
        """通过 Saver 原子收敛 execution、output item、assembly 与 Turn。"""
        return self._storage.executions.converge_execution(
            session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            execution_id=execution_id,
            outcome=outcome,
            turn_status=turn_status,
            items=items,
            final_item_id=final_item_id,
            assembly_id=assembly_id,
            checkpoint_ns=checkpoint_ns,
            loss_reason=loss_reason,
        )

    def resume_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        thread_id: str,
        job_id: str,
        checkpoint_ns: str = "",
    ) -> dict[str, object]:
        return self._storage.executions.resume_turn(
            session_id,
            turn_id=turn_id,
            thread_id=thread_id,
            job_id=job_id,
            checkpoint_ns=checkpoint_ns,
        )

    def mark_execution_lost(
        self,
        session_id: str,
        *,
        turn_id: str | None,
        thread_id: str,
        execution_id: str | None = None,
        checkpoint_ns: str = "",
        reason: str = "provider_result_commit_missing",
    ) -> dict[str, object]:
        """通过 Saver 记录 provider dispatch 后丢失的 execution。"""
        return self._storage.executions.mark_execution_lost(
            session_id,
            turn_id=turn_id,
            thread_id=thread_id,
            execution_id=execution_id,
            checkpoint_ns=checkpoint_ns,
            reason=reason,
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
