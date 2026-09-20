"""基于 rollout 增量日志的 LangGraph CheckpointSaver。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
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
from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_control_store import SessionControlStore
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.enums import CanonicalItemStatus, SemanticKind, TurnScope
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import (
    AppendCanonicalItemIntent,
    ApplySourceLifecycleDecision,
    ContextMutationIntent,
    MutationIntentOwner,
    SwitchToolSetIntent,
)
from app.domain.itemized.parts import ContentPart, ContentPartAnchor
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.runtime import ProvenanceEdge
from app.schemas.internal_v2.turn import (
    TurnHistoryLoadRequest,
    TurnHistoryPageDTO,
    TurnSummaryDTO,
)
from app.services.infrastructure.node_debug_fork import NodeDebugWorkspaceForkConfig
from app.services.infrastructure.node_debug_session_store import NodeDebugSessionStore
from app.services.infrastructure.node_debug_thread_owner import MAIN_THREAD_ID
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
from app.services.infrastructure.rollout_context.checkpoint.mutation_intents import (
    MutationIntentOwnerMismatch,
    MutationIntentPortError,
    SessionThreadMutationIntents,
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


# SemanticKind → AppendCanonicalItemIntent.item_kind 的闭合映射；
# runtime_notice/compaction_summary/extension 不属于 canonical append intent，
# 走各自 owner 端口。
_APPEND_ITEM_KINDS: Mapping[str, str] = {
    SemanticKind.USER_INPUT: "user_message",
    SemanticKind.ATTACHMENT: "attachment",
    SemanticKind.ASSISTANT_OUTPUT: "assistant_message",
    SemanticKind.REASONING: "reasoning",
    SemanticKind.TOOL_CALL: "tool_call",
    SemanticKind.TOOL_RESULT: "tool_result",
}


def _append_intent_for(
    owner: MutationIntentOwner,
    item: CanonicalItemRecord,
) -> AppendCanonicalItemIntent:
    """从 canonical record 构造 append intent；字段不完整时显式失败。"""
    item_kind = _APPEND_ITEM_KINDS.get(item.semantic_kind)
    if item_kind is None:
        raise ValueError(
            "semantic_kind 不在 append intent 闭合集内: "
            f"{item.semantic_kind!r} (item_id={item.item_id!r})"
        )
    tool_call_id: str | None = None
    if item_kind in {"tool_call", "tool_result"}:
        tool_call_id = _tool_call_id_for_intent(item)
    if not item.turn_id:
        raise ValueError(f"append intent 需要非空 origin turn_id: {item.item_id!r}")
    return AppendCanonicalItemIntent(
        owner=owner,
        item_id=item.item_id,
        item_kind=item_kind,
        origin_turn_id=item.turn_id,
        tool_call_id=tool_call_id,
    )


def _tool_call_id_for_intent(item: CanonicalItemRecord) -> str:
    """提取 tool_call/tool_result 与 intent 配对的 tool_call_id。

    - tool_call 接受单 call 形态与单元素 tool_calls 列表（checkpoint
      shadow 投影形态）；多元素列表无法映射为单个 append intent 的
      配对身份，显式拒绝。
    - tool_result 的身份按 v2 合同位于 typed payload；text payload 的
      身份由 metadata 携带，二者都支持。
    """
    payload = item.payload if isinstance(item.payload, Mapping) else {}
    if item.semantic_kind == SemanticKind.TOOL_CALL:
        if isinstance(payload.get("tool_calls"), list):
            calls = payload["tool_calls"]
            if len(calls) != 1:
                raise ValueError(
                    "多 call 批式 tool_call 不能构造 append intent: "
                    f"{item.item_id!r} (tool_calls={len(calls)})"
                )
            call = calls[0] if isinstance(calls[0], Mapping) else {}
            raw_tool_call_id = call.get("id")
        else:
            raw_tool_call_id = payload.get("tool_call_id")
    else:
        raw_tool_call_id = payload.get("tool_call_id")
        if raw_tool_call_id is None:
            raw_tool_call_id = item.metadata.get("tool_call_id")
    if not isinstance(raw_tool_call_id, str) or not raw_tool_call_id:
        raise ValueError(
            f"{item.semantic_kind} 缺少配对 tool_call_id: {item.item_id!r}"
        )
    return raw_tool_call_id


@dataclass(slots=True)
class _AppendIntentBatch:
    """一次 append_items 调用的 in-flight 批上下文。

    批内 intent 先全部构造、再逐个经 facade 消费；首个 intent 消费时由
    owner 端口单事务提交整批，后续 intent 只做校验，保持 storage 既有的
    同批原子提交边界。
    """

    owner: MutationIntentOwner
    checkpoint_ns: str
    records: tuple[CanonicalItemRecord, ...]
    item_ids: frozenset[str]
    commit_ids: tuple[int, ...] | None = None


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
        node_debug_store: NodeDebugSessionStore | None = None,
        node_debug_workspace_config: Callable[[], NodeDebugWorkspaceForkConfig]
        | None = None,
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
        self._node_debug_store = node_debug_store or NodeDebugSessionStore(
            get_session_path_resolver(Path(sessions_dir).resolve())
        )
        self._node_debug_workspace_config = node_debug_workspace_config
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
        # SessionThread mutation intent 的 per-owner facade 注册表；saver 是多
        # session 单例，facade 只服务单个 (session_id, thread_id) owner。
        self._mutation_intent_facades: dict[
            MutationIntentOwner, SessionThreadMutationIntents
        ] = {}
        # 正在经 intent 端口消费的 append 批；由 append_items 持锁注册/清理。
        self._append_intent_batches: dict[
            MutationIntentOwner, _AppendIntentBatch
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
            # SessionThread owner 身份是 (session_id, thread_id)；生产 canonical
            # append 只发生在 main thread，checkpoint_ns 不得参与 owner 身份。
            raise ValueError(
                "canonical append intent 不接受非空 checkpoint_ns: "
                f"{checkpoint_ns!r} (session_id={session_id!r})"
            )
        owner = MutationIntentOwner(session_id=session_id, thread_id=MAIN_THREAD_ID)
        # 先构造全部 intent：任一字段不合法都在触碰 storage 前显式失败。
        intents = tuple(_append_intent_for(owner, item) for item in typed_items)
        with self._lock:
            facade = self._mutation_intent_facade(owner)
            batch = _AppendIntentBatch(
                owner=owner,
                checkpoint_ns=checkpoint_ns,
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

    def consume_mutation_intent(self, intent: ContextMutationIntent) -> None:
        """SessionThreadMutationIntentPort 的 append/toolset 分支生产实现。

        TODO(OpenSpec 2.3-B4)：epoch 分支（rewind/compaction rebuild）的
        生产接线属后续切片，当前显式拒绝，不静默降级。
        """
        if isinstance(intent, AppendCanonicalItemIntent):
            self._consume_append_intent(intent)
            return
        if isinstance(intent, ApplySourceLifecycleDecision):
            self._consume_source_lifecycle_intent(intent)
            return
        if isinstance(intent, SwitchToolSetIntent):
            self._consume_switch_tool_set_intent(intent)
            return
        raise NotImplementedError(
            "TODO(OpenSpec 2.3-B4): mutation intent 分支尚未接线: "
            + type(intent).__name__
        )

    def _consume_source_lifecycle_intent(
        self,
        intent: ApplySourceLifecycleDecision,
    ) -> None:
        """source 分支：正文与 control state 在同一 owner transaction 提交。"""
        item: CanonicalItemRecord | None = None
        if intent.content is not None:
            if intent.item_id is None:
                raise MutationIntentPortError(
                    "source lifecycle intent 缺少 item_id: "
                    f"source_id={intent.source_id!r}"
                )
            message_id = (
                intent.item_id.removeprefix("item-")
                if intent.item_id.startswith("item-")
                else intent.item_id
            )
            metadata = dict(intent.metadata)
            metadata.setdefault("projection_message_id", message_id)
            metadata.setdefault("wire_role", "user")
            metadata.setdefault("execution_confirmed", True)
            metadata.setdefault("internal", True)
            metadata.setdefault("context_source_kind", intent.source_kind)
            metadata.setdefault("context_source_id", intent.source_id)
            metadata.setdefault("context_source_name", intent.name)
            metadata.setdefault("context_wire_role", "user")
            if intent.revision is not None:
                metadata.setdefault("context_revision", intent.revision)
            item = CanonicalItemRecord.create(
                item_sequence=1,
                item_id=intent.item_id,
                semantic_kind=SemanticKind.RUNTIME_NOTICE,
                payload_kind="text",
                status=CanonicalItemStatus.COMPLETED,
                producer_ref={
                    "producer_kind": "runtime",
                    "producer_id": intent.source_id,
                    "invocation_id": intent.idempotency_key,
                },
                payload=intent.content,
                metadata=metadata,
                turn_scope=TurnScope(intent.turn_scope),
                message_group_id=f"message-{message_id}",
                wire_role="user",
            )
        apply = getattr(self._storage, "apply_source_lifecycle_decision", None)
        if not callable(apply):
            raise MutationIntentPortError(
                "Saver 缺少 apply_source_lifecycle_decision owner 端口"
            )
        apply(intent, item)

    def _consume_append_intent(self, intent: AppendCanonicalItemIntent) -> None:
        """append 分支：批内首个 intent 消费时单事务提交整批。"""
        batch = self._append_intent_batches.get(intent.owner)
        if batch is None:
            raise MutationIntentOwnerMismatch(
                "mutation-intent-owner-mismatch: append intent 没有对应的"
                "活动 owner 批: expected="
                + ",".join(
                    f"({owner.session_id},{owner.thread_id})"
                    for owner in self._append_intent_batches
                )
                + f" actual=({intent.owner.session_id},{intent.owner.thread_id})"
            )
        if intent.item_id not in batch.item_ids:
            raise MutationIntentPortError(
                "append intent 不属于当前 owner 批: "
                f"item_id={intent.item_id!r}, "
                f"batch=({batch.owner.session_id},{batch.owner.thread_id})"
            )
        if batch.commit_ids is None:
            batch.commit_ids = self._storage.append_items(
                intent.owner.session_id,
                batch.records,
                checkpoint_ns=batch.checkpoint_ns,
            )

    def _consume_switch_tool_set_intent(self, intent: SwitchToolSetIntent) -> None:
        """toolset 分支：把 desired ToolSet 状态应用到 main thread owner binding。

        TODO(OpenSpec 5.4)：outstanding tool call 收敛与 toolset_changed
        epoch bump 属后续切片；本分支只推进 durable desired/applied 状态。
        """
        if intent.owner.thread_id != MAIN_THREAD_ID:
            raise MutationIntentOwnerMismatch(
                "mutation-intent-owner-mismatch: ToolSet 切换当前只接 main "
                f"thread owner: expected=(*,{MAIN_THREAD_ID}) actual=("
                f"{intent.owner.session_id},{intent.owner.thread_id})"
            )
        control_path, main_thread_id = self._resolve_main_thread_control(
            intent.owner.session_id
        )
        store = SessionControlStore(control_path)
        try:
            binding = store.ensure_thread_owner_binding(thread_id=main_thread_id)
            if binding.toolset_compatibility_key == intent.desired_revision:
                # 跨进程同 identity 重放：applied 历史不可覆盖，不产生新 revision。
                return
            next_revision = (binding.applied_toolset_revision or 0) + 1
            store.update_thread_owner_binding(
                main_thread_id,
                desired_toolset_revision=next_revision,
                applied_toolset_revision=next_revision,
                toolset_compatibility_key=intent.desired_revision,
            )
        finally:
            store.close()

    def _mutation_intent_facade(
        self, owner: MutationIntentOwner
    ) -> SessionThreadMutationIntents:
        """取得（或建立）owner 的 mutation intent facade；调用方需持 self._lock。"""
        facade = self._mutation_intent_facades.get(owner)
        if facade is None:
            facade = SessionThreadMutationIntents(owner=owner, port=self)
            self._mutation_intent_facades[owner] = facade
        return facade

    def _switch_tool_set_if_needed(
        self,
        session_id: str,
        *,
        tool_snapshot: Sequence[Mapping[str, object]],
    ) -> None:
        """model-call 安全边界：desired ToolSet 变化时经 intent 端口切换。

        比较基准是 durable applied 状态（跨进程一致）；相同时不构造
        intent，已 sealed 的在飞请求不受影响。
        """
        desired_revision = sha256_jcs(
            {"tools": [dict(tool) for tool in tool_snapshot]}
        )
        if self._applied_toolset_key(session_id) == desired_revision:
            return
        owner = MutationIntentOwner(session_id=session_id, thread_id=MAIN_THREAD_ID)
        intent = SwitchToolSetIntent(
            owner=owner,
            desired_revision=desired_revision,
            tool_set_snapshot_id="tool-set:" + desired_revision,
        )
        with self._lock:
            self._mutation_intent_facade(owner).consume(intent)

    def _applied_toolset_key(self, session_id: str) -> str | None:
        """只读读取 main thread 当前 applied ToolSet compatibility key。"""
        control_path, main_thread_id = self._resolve_main_thread_control(session_id)
        store = SessionControlStore(control_path)
        try:
            binding = store.get_thread_owner_binding(main_thread_id)
        except KeyError:
            # owner binding 行尚未建立：等价于从未 applied。
            return None
        finally:
            store.close()
        return binding.toolset_compatibility_key

    def _resolve_main_thread_control(self, session_id: str) -> tuple[Path, str]:
        """解析 main thread 的 session-control 路径与 catalog main_thread_id。"""
        resolver = get_session_path_resolver(self._storage.sessions_dir)
        if not isinstance(resolver, SessionCatalogPathResolver):
            raise RuntimeError(  # noqa: TRY004 —— 运行时模式错误，非参数类型错误
                "ToolSet owner 状态要求 catalog resolver（当前 legacy resolver，"
                f"fail closed）: sessions_dir={self._storage.sessions_dir}"
            )
        node = resolver.catalog_store.get_node(session_id)
        if node.main_thread_id is None:
            raise RuntimeError(
                "catalog 节点缺 main_thread_id（fail closed）: "
                f"session_id={session_id!r}"
            )
        session_dir = resolver.resolve_session_node(session_id)
        return session_dir / "session-control.sqlite", node.main_thread_id

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
