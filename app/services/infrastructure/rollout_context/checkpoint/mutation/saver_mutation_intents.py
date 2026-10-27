"""RolloutCheckpointSaver 的 mutation intent owner 端口实现（由 Saver 组合）。

consume_mutation_intent 的唯一 owner 端口与 facade registry；epoch rebuild
只消费边界显式传入的 SQLite 事务，ToolSet applied 只由 seal binding 确立。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

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
    *,
    execution_id: str,
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
    return AppendCanonicalItemIntent(
        owner=owner,
        item_id=item.item_id,
        item_kind=item_kind,
        execution_id=execution_id,
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
            raw_tool_call_id = (
                item.tool_result_evidence.tool_call_id
                if item.tool_result_evidence is not None
                else None
            )
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
    execution_id: str
    records: tuple[CanonicalItemRecord, ...]
    item_ids: frozenset[str]
    commit_ids: tuple[int, ...] | None = None


def source_lifecycle_item(intent: ApplySourceLifecycleDecision) -> CanonicalItemRecord | None:
    """所有 source 提交共用唯一 canonical item 构造。"""
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
    return item


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
            self._storage.checkpoints.consume_context_epoch_rebuild_intent(intent, connection)
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
        item = source_lifecycle_item(intent)
        self._storage.checkpoints.apply_source_lifecycle_decision(intent, item)

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
            batch.commit_ids = self._storage.executions.append_items(
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
