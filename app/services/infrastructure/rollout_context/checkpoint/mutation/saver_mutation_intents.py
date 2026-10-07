"""RolloutCheckpointSaver 的 mutation intent owner 端口实现（由 Saver 组合）。

consume_mutation_intent 的唯一 owner 端口与 facade registry；epoch rebuild
只消费边界显式传入的 SQLite 事务，ToolSet applied 只由 seal binding 确立。
"""

from __future__ import annotations

import sqlite3

from app.domain.itemized.enums import CanonicalItemStatus, SemanticKind, TurnScope
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemSourceManifest,
)
from app.domain.itemized.mutation_intents import (
    AppendCanonicalItemIntent,
    ApplySourceLifecycleDecision,
    ContextMutationIntent,
    MutationIntentOwner,
    RebuildContextEpochIntent,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.mutation.mutation_intents import (
    MutationIntentOwnerMismatch,
    MutationIntentPortError,
    SessionThreadMutationIntents,
)


class RolloutMutationIntentPortMixin:
    """SessionThreadMutationIntentPort 的唯一 owner 消费端口。"""

    def consume_mutation_intent(
        self, intent: ContextMutationIntent, connection: sqlite3.Connection | None = None,
    ) -> None:
        """epoch rebuild 不另开事务、不经 facade 的提前成功缓存。"""
        if isinstance(intent, RebuildContextEpochIntent):
            if connection is None or not connection.in_transaction:
                raise MutationIntentPortError(
                    "epoch-rebuild-transaction-required: rewind/compaction 必须显式传入"
                    "创建 view/control 的同一活动 SQLite connection"
                )
            self._storage.consume_context_epoch_rebuild_intent(intent, connection)
            return
        if connection is not None:
            raise MutationIntentPortError("只有 epoch rebuild 可以消费边界传入的 SQLite connection")
        if isinstance(intent, AppendCanonicalItemIntent):
            self._consume_append_intent(intent)
            return
        if isinstance(intent, ApplySourceLifecycleDecision):
            self._consume_source_lifecycle_intent(intent)
            return
        raise TypeError("未知 ContextMutationIntent 分支: " + type(intent).__name__)

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
            item = CanonicalItemRecord.create(
                item_sequence=1,
                item_id=intent.item_id,
                semantic_kind=SemanticKind.RUNTIME_NOTICE,
                payload_kind=("structured_content" if intent.runtime_notice_payload is not None else "text"),
                status=CanonicalItemStatus.COMPLETED,
                producer_ref={
                    "producer_kind": "runtime",
                    "producer_id": intent.source_id,
                    "invocation_id": intent.idempotency_key,
                },
                payload=(dict(intent.runtime_notice_payload) if intent.runtime_notice_payload is not None else intent.content),
                projection_identity=ItemProjectionIdentity(projection_message_id=message_id),
                source_manifest=ItemSourceManifest(intent.revision, "internal", "public") if intent.revision is not None else None,
                extensions=intent.extensions,
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
        if intent.execution_id != batch.execution_id:
            raise MutationIntentOwnerMismatch(
                "mutation-intent-owner-mismatch: append intent execution 与当前批不一致: "
                f"intent={intent.execution_id!r}, batch={batch.execution_id!r}"
            )
        if batch.commit_ids is None:
            batch.commit_ids = self._storage.append_items(
                intent.owner.session_id,
                intent.owner.thread_id,
                batch.records,
                execution_id=batch.execution_id,
                checkpoint_ns=batch.checkpoint_ns,
            )

    def _mutation_intent_facade(
        self, owner: MutationIntentOwner
    ) -> SessionThreadMutationIntents:
        """取得（或建立）owner 的 mutation intent facade；调用方需持 self._lock。"""
        facade = self._mutation_intent_facades.get(owner)
        if facade is None:
            facade = SessionThreadMutationIntents(owner=owner, port=self)
            self._mutation_intent_facades[owner] = facade
        return facade

__all__ = ["RolloutMutationIntentPortMixin"]
