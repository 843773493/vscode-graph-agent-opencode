"""跨 session fork 的 canonical item 复制与 identity remap owner。

本模块只消费 v2 item/catalog，并在 target rollout 重新分配本地 identity；不提供
v1 fork fallback。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from app.domain.itemized.enums import SemanticKind, TurnScope
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.transaction import strict_text


class ForkItemCopyMixin:
    def copy_v2_items_for_fork(
        self,
        *,
        source_thread_id: str,
        target_thread_id: str,
        checkpoint_ns: str = "",
        fork_id: str | None = None,
    ) -> tuple[str, ...]:
        """把 target checkpoint 已包含 Turn 的 v2 item 补齐到 target。

        ``aput`` 负责兼容 LangChain message channel，但 provider block、tool
        result 和 reasoning item 可能没有对应的 message sequence。跨 session
        fork 因此先以 target 已物化的 Turn root/member 集合确定复制范围，再
        从 source 的 immutable catalog/JSONL 读取正文，在 target 重新分配 item
        与 Turn identity；target 的 item_sequence/offset 由 ``append_items``
        重新分配，正文不从 source 文件建立运行时读取依赖。没有 v2
        v1 rollout 不能从此路径读取；调用方必须先显式执行一次性的
        ``legacy_import_v1_to_v2``，再对 v2 target 执行 fork。此方法不提供
        legacy fork fallback，也不从 v1 message 表恢复 TurnRecord。
        """
        source_thread_id = strict_text(
            source_thread_id,
            field="fork.source_thread_id",
        )
        target_thread_id = strict_text(
            target_thread_id,
            field="fork.target_thread_id",
        )
        if source_thread_id == target_thread_id:
            raise ValueError("fork target_thread_id 不能与 source_thread_id 相同")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("fork checkpoint_ns 必须是字符串")
        fork_id = (
            strict_text(fork_id, field="fork.fork_id") if fork_id is not None else None
        )
        source_items = self.read_items(source_thread_id, checkpoint_ns=checkpoint_ns)
        self.initialize(target_thread_id, checkpoint_ns)
        with self._connect(
            target_thread_id, checkpoint_ns, read_only=True
        ) as connection:
            self._require_v2_runtime(connection)
            target_turn_ids = {
                strict_text(row[0], field="item_catalog.turn_id")
                for row in connection.execute(
                    "SELECT DISTINCT turn_id FROM item_catalog WHERE turn_id IS NOT NULL"
                ).fetchall()
            }
            target_turn_ids.update(
                strict_text(row[0], field="messages.turn_id")
                for row in connection.execute(
                    "SELECT DISTINCT turn_id FROM messages WHERE turn_id IS NOT NULL"
                ).fetchall()
            )
        selected = tuple(
            item
            for item in source_items
            if (item.turn_id is not None and item.turn_id in target_turn_ids)
            or item.turn_scope in {TurnScope.AMBIENT, TurnScope.PENDING_NEXT_TURN}
        )
        if not selected:
            return ()
        fork_token = (
            fork_id
            or sha256_jcs(
                {
                    "source_session_id": source_thread_id,
                    "target_session_id": target_thread_id,
                    "selected_item_ids": [item.item_id for item in selected],
                }
            ).split(":")[-1][:32]
        )
        source_turn_ids = {
            item.turn_id for item in selected if item.turn_id is not None
        }
        target_turn_map = {
            source_turn_id: (
                f"fork-turn:{target_thread_id}:"
                f"{hashlib.sha256((fork_token + ':' + source_turn_id).encode('utf-8')).hexdigest()[:32]}"
            )
            for source_turn_id in sorted(source_turn_ids)
        }
        item_id_map = {
            item.item_id: (
                f"fork-item:{target_thread_id}:"
                f"{hashlib.sha256((fork_token + ':' + item.item_id).encode('utf-8')).hexdigest()[:32]}"
            )
            for item in selected
        }
        remapped: list[CanonicalItemRecord] = []

        def remap_fork_identity(entity_type: str, value: object) -> object:
            if not isinstance(value, str) or not value:
                return value
            return (
                f"fork-{entity_type}:{target_thread_id}:"
                f"{hashlib.sha256((fork_token + ':' + value).encode('utf-8')).hexdigest()[:32]}"
            )

        def remap_payload(value: object) -> object:
            if isinstance(value, Mapping):
                result: dict[str, object] = {}
                for key, child in value.items():
                    if not isinstance(key, str) or not key:
                        raise ValueError("fork payload object key 必须是非空字符串")
                    key_text = key
                    if key_text in {"tool_call_id", "call_id"}:
                        child = remap_fork_identity("tool-call", child)
                    elif key_text in {"tool_invocation_id", "tool_attempt_id"}:
                        child = remap_fork_identity(key_text.removesuffix("_id"), child)
                    elif key_text == "result_id":
                        child = remap_fork_identity("tool-result", child)
                    elif (
                        key_text == "id"
                        and isinstance(child, str)
                        and "name" in value
                        and "args" in value
                    ):
                        # 只在工具 call/result payload 的已知位置重写；其它
                        # 业务 payload 的通用 id 不是 rollout identity。
                        child = remap_fork_identity("tool-call", child)
                    result[key_text] = remap_payload(child)
                return result
            if isinstance(value, list):
                return [remap_payload(child) for child in value]
            if isinstance(value, tuple):
                return [remap_payload(child) for child in value]
            return value

        for item in selected:
            target_item_id = item_id_map[item.item_id]
            metadata = {
                **dict(item.metadata),
                "fork_source_session_id": source_thread_id,
                "fork_source_item_id": item.item_id,
                "fork_source_turn_id": item.turn_id,
                "projection_message_id": target_item_id,
            }
            producer = dict(item.producer_ref)
            producer_id = producer.get("producer_id")
            if isinstance(producer_id, str) and producer_id:
                producer["producer_id"] = (
                    f"fork-producer:{target_thread_id}:"
                    f"{hashlib.sha256((fork_token + ':' + producer_id).encode('utf-8')).hexdigest()[:32]}"
                )
            if isinstance(producer.get("invocation_id"), str):
                producer["invocation_id"] = remap_fork_identity(
                    "execution", producer["invocation_id"]
                )
            remapped_payload = (
                remap_payload(item.payload)
                if item.semantic_kind
                in {SemanticKind.TOOL_CALL, SemanticKind.TOOL_RESULT}
                else item.payload
            )
            remapped.append(
                CanonicalItemRecord.create(
                    item_sequence=item.item_sequence,
                    item_id=target_item_id,
                    semantic_kind=item.semantic_kind,
                    payload_kind=item.payload_kind,
                    status=item.status,
                    producer_ref=producer,
                    payload=remapped_payload,
                    created_at=item.created_at,
                    metadata=metadata,
                    turn_id=(
                        target_turn_map[item.turn_id]
                        if item.turn_id is not None
                        else None
                    ),
                    turn_scope=item.turn_scope,
                    message_group_id=(
                        remap_fork_identity("message-group", item.message_group_id)
                        if item.message_group_id is not None
                        else None
                    ),
                    wire_role=item.wire_role,
                )
            )
        self.append_items(
            target_thread_id,
            tuple(remapped),
            checkpoint_ns=checkpoint_ns,
        )
        return tuple(item.item_id for item in remapped)
