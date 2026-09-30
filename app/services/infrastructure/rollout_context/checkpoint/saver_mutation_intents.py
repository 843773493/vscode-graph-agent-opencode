"""RolloutCheckpointSaver 的 mutation intent owner 端口实现（由 Saver 组合）。

consume_mutation_intent 及其 append/toolset 分支、facade registry 与 main-thread
control 解析。方法体逐字平移，self 语义不变；仅供 RolloutCheckpointSaver 继承。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_control_store import SessionControlStore
from app.domain.itemized.enums import CanonicalItemStatus, SemanticKind, TurnScope
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import (
    AppendCanonicalItemIntent,
    ApplySourceLifecycleDecision,
    ContextMutationIntent,
    MutationIntentOwner,
    SwitchToolSetIntent,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.node_debug.session.thread_owner import MAIN_THREAD_ID
from app.services.infrastructure.rollout_context.checkpoint.mutation_intents import (
    MutationIntentOwnerMismatch,
    MutationIntentPortError,
    SessionThreadMutationIntents,
)


class RolloutMutationIntentPortMixin:
    """SessionThreadMutationIntentPort 的 append/toolset 分支 owner 端口。"""

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
                "ToolSet owner 状态要求 catalog resolver（当前解析器不满足要求，"
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


__all__ = ["RolloutMutationIntentPortMixin"]
