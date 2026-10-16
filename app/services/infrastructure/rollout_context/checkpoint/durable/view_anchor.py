"""v2 checkpoint/view/anchor durable owner。

这里只调用 RolloutContextStore 提供的 SQLite、JSONL 和 domain ports，不创建
LangChain message，也不读取 v1 数据。
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutReadSnapshot,
        RolloutTurnAnchor,
    )




def _v2_json_line(value: object) -> bytes:
    from app.services.infrastructure.rollout_context.storage.serialization import (
        canonical_json_line,
    )

    return canonical_json_line(value)


_VISIBLE_NORMAL_TURN_PREDICATE = (
    "EXISTS (SELECT 1 FROM messages AS visible_user_message "
    "WHERE visible_user_message.turn_id = t.turn_id "
    "AND visible_user_message.role = 'user' "
    "AND visible_user_message.visibility = 'visible')"
)


class RolloutViewAnchorMixin:
    """active context view、Turn anchor 与 item 读取 owner。"""


    def read_items_for_view(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
    ) -> list[CanonicalItemRecord]:
        """按 active view 的 item 索引读取 canonical item，不扫描物理前缀。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        connection = self._storage._snapshot_connection(snapshot)
        rows = connection.execute(
            "SELECT item_id FROM context_view_items WHERE view_id = ? AND visible = 1 ORDER BY logical_item_ordinal",
            (view_id,),
        ).fetchall()
        item_ids = tuple(
            strict_text(row[0], field="context_view_items.item_id") for row in rows
        )
        items = self._storage.read_items(
            snapshot.session_id,
            snapshot.thread_id,
            checkpoint_ns=snapshot.checkpoint_ns,
            item_ids=item_ids,
            snapshot=snapshot,
        )
        by_id = {item.item_id: item for item in items}
        missing = tuple(item_id for item_id in item_ids if item_id not in by_id)
        if missing:
            raise RuntimeError(
                "context view 引用的 canonical item 未能完整恢复: "
                + ",".join(missing)
            )
        return [by_id[item_id] for item_id in item_ids]

    def resolve_turn_anchor(
        self,
        snapshot: RolloutReadSnapshot,
        turn_id: str,
        *,
        anchor_mode: str = "inclusive",
        require_completed: bool = False,
    ) -> RolloutTurnAnchor:
        """从 active head 沿 view lineage 解析用户可见的 Turn 锚点。

        `context_view_turns` 只提供候选完整 Turn，真正的选择顺序由 active
        head 的祖先链决定。这样同一个 Turn 出现在多个 branch/view 时，不会
        因为物理 sequence 或全局 control sequence 较大而选错上下文。
        """
        return self._resolve_turn_anchor_connection(
            self._storage._snapshot_connection(snapshot),
            turn_id,
            checkpoint_ns=snapshot.checkpoint_ns,
            anchor_mode=anchor_mode,
            require_completed=require_completed,
        )

    def resolve_latest_completed_turn_anchor(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        anchor_mode: str = "inclusive",
    ) -> RolloutTurnAnchor | None:
        """从 active view lineage 找到最近一个已完成 Turn 的锚点。

        最新物理消息可能属于尚未完成的 Turn，不能用它作为前端默认 fork
        起点。这里先按 active head 到祖先 view 的逻辑顺序查找最近完成的
        normal Turn，再复用同一个 Turn resolver 取得 source view/checkpoint。
        """
        connection = self._storage._snapshot_connection(snapshot)
        active = connection.execute(
            "SELECT b.head_view_id FROM branches b WHERE b.branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?)",
            (snapshot.checkpoint_ns,),
        ).fetchone()
        active_view_id = (
            strict_optional_text(active[0], field="branches.head_view_id")
            if active is not None
            else None
        )
        if active_view_id is None:
            has_turn = connection.execute(
                f"SELECT 1 FROM turns AS t WHERE t.turn_kind = 'normal' AND t.user_message_sequence IS NOT NULL AND {_VISIBLE_NORMAL_TURN_PREDICATE} LIMIT 1"
            ).fetchone()
            if has_turn is None:
                return None
            raise ValueError("当前会话没有已完成的 Turn，无法创建 fork")

        current_view_id = active_view_id
        visited: set[str] = set()
        while current_view_id:
            if current_view_id in visited:
                raise RuntimeError(f"context view 父链成环: {current_view_id}")
            visited.add(current_view_id)
            row = connection.execute(
                f"SELECT cvt.turn_id FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id WHERE cvt.view_id = ? AND t.turn_kind = 'normal' AND t.user_message_sequence IS NOT NULL AND t.final_message_sequence IS NOT NULL AND t.status IN ('completed', 'succeeded') AND {_VISIBLE_NORMAL_TURN_PREDICATE} ORDER BY cvt.logical_turn_ordinal DESC LIMIT 1",
                (current_view_id,),
            ).fetchone()
            if row is not None:
                return self._resolve_turn_anchor_connection(
                    connection,
                    strict_text(row[0], field="context_view_turns.turn_id"),
                    checkpoint_ns=snapshot.checkpoint_ns,
                    anchor_mode=anchor_mode,
                    require_completed=True,
                )
            parent = connection.execute(
                "SELECT parent_view_id FROM context_views WHERE view_id = ?",
                (current_view_id,),
            ).fetchone()
            if parent is None:
                raise RuntimeError(f"context view 不存在: {current_view_id}")
            current_view_id = strict_optional_text(
                parent[0], field="context_views.parent_view_id"
            ) or ""

        running = connection.execute(
            f"SELECT 1 FROM turns AS t WHERE t.turn_kind = 'normal' AND t.user_message_sequence IS NOT NULL AND (t.final_message_sequence IS NULL OR t.status NOT IN ('completed', 'succeeded')) AND {_VISIBLE_NORMAL_TURN_PREDICATE} LIMIT 1"
        ).fetchone()
        if running is not None:
            raise ValueError("当前会话没有已完成的 Turn，无法创建 fork")
        return None

    def _resolve_turn_anchor_connection(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
        *,
        checkpoint_ns: str,
        anchor_mode: str,
        require_completed: bool = False,
    ) -> RolloutTurnAnchor:
        from app.services.infrastructure.rollout_context.storage.primitives import (
            RolloutTurnAnchor,
        )

        turn_id = strict_text(turn_id, field="turns.turn_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        if anchor_mode not in {"inclusive", "before"}:
            raise ValueError("Turn anchor_mode 必须是 inclusive 或 before")
        turn = connection.execute(
            "SELECT turn_id, turn_kind, first_message_sequence, user_message_sequence, last_message_sequence, final_message_sequence, status FROM turns WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if turn is None:
            raise KeyError(f"Turn 不存在: {turn_id}")
        stored_turn_id = strict_text(turn[0], field="turns.turn_id")
        turn_kind = strict_text(turn[1], field="turns.turn_kind")
        first_sequence = strict_non_negative_int(
            turn[2], field=f"turns.first_message_sequence: {turn_id}"
        )
        user_sequence = strict_optional_non_negative_int(
            turn[3], field=f"turns.user_message_sequence: {turn_id}"
        )
        last_sequence = strict_non_negative_int(
            turn[4], field=f"turns.last_message_sequence: {turn_id}"
        )
        final_sequence = strict_optional_non_negative_int(
            turn[5], field=f"turns.final_message_sequence: {turn_id}"
        )
        turn_status = strict_text(turn[6], field=f"turns.status: {turn_id}")
        if stored_turn_id != turn_id:
            raise RuntimeError(f"Turn 索引 identity 不一致: {turn_id}")
        if first_sequence == 0 or last_sequence == 0 or last_sequence < first_sequence:
            raise RuntimeError(f"Turn message sequence 索引非法: {turn_id}")
        if turn_kind != "normal" or user_sequence is None:
            raise KeyError(f"Turn 不是可定位的 normal Turn: {turn_id}")
        if require_completed and (
            final_sequence is None or turn_status not in {"completed", "succeeded"}
        ):
            raise ValueError(f"运行中的 Turn 不支持 fork: turn_id={turn_id}")
        checkpoint_sequence_limit = None
        checkpoint_commit_limit = None
        if require_completed:
            checkpoint_sequence_limit = (
                final_sequence if anchor_mode == "inclusive" else user_sequence - 1
            )
            boundary_item = "final_item_id" if anchor_mode == "inclusive" else "root_input_item_id"
            boundary = connection.execute(
                f"SELECT i.commit_id FROM turn_records AS t JOIN item_catalog AS i ON i.item_id = t.{boundary_item} WHERE t.turn_id = ?",
                (turn_id,),
            ).fetchone()
            if boundary is None:
                raise RuntimeError(f"source-mismatch: 完成 Turn 缺少 canonical 边界: {turn_id}")
            # 后继 admission 的 input checkpoint 可能尚未追加消息；消息序号相同
            # 不能证明非消息状态仍属于该 Turn，header 必须留在同一提交边界。
            checkpoint_commit_limit = strict_non_negative_int(
                boundary[0], field="turn_anchor.boundary_commit_id"
            ) - (1 if anchor_mode == "before" else 0)

        candidates = {
            strict_text(row[0], field="context_views.view_id"): {
                "branch_id": strict_text(row[1], field="context_views.branch_id"),
                "logical_turn_ordinal": strict_non_negative_int(
                    row[2], field="context_view_turns.logical_turn_ordinal"
                ),
            }
            for row in connection.execute(
                "SELECT cvt.view_id, cv.branch_id, cvt.logical_turn_ordinal FROM context_view_turns cvt JOIN context_views cv ON cv.view_id = cvt.view_id WHERE cvt.turn_id = ?",
                (turn_id,),
            ).fetchall()
        }
        active = connection.execute(
            "SELECT b.branch_id, b.head_view_id FROM branches b WHERE b.branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?)",
            (checkpoint_ns,),
        ).fetchone()
        active_view_id = (
            strict_optional_text(active[1], field="branches.head_view_id")
            if active is not None
            else None
        )
        if active_view_id is None:
            raise RuntimeError(f"会话没有可用的 active context view: turn_id={turn_id}")

        current_view_id = active_view_id
        visited: set[str] = set()
        while current_view_id:
            if current_view_id in visited:
                raise RuntimeError(f"context view 父链成环: {current_view_id}")
            visited.add(current_view_id)
            candidate = candidates.get(current_view_id)
            if candidate is not None:
                checkpoint_id = self._source_checkpoint_for_view(
                    connection,
                    current_view_id,
                    checkpoint_ns=checkpoint_ns,
                    max_message_sequence=checkpoint_sequence_limit,
                    max_commit_id=checkpoint_commit_limit,
                )
                if checkpoint_id is not None:
                    state_header_checkpoint_id = checkpoint_id
                    if anchor_mode == "before" and not require_completed:
                        state_header_checkpoint_id = self._checkpoint_for_turn_prefix(
                            connection, checkpoint_ns, checkpoint_id, user_sequence,
                        )
                    cutoff = (
                        last_sequence
                        if anchor_mode == "inclusive"
                        else user_sequence - 1
                    )
                    return RolloutTurnAnchor(
                        turn_id=stored_turn_id,
                        view_id=current_view_id,
                        checkpoint_id=checkpoint_id,
                        state_header_checkpoint_id=state_header_checkpoint_id,
                        branch_id=strict_text(
                            candidate["branch_id"],
                            field="context_views.branch_id",
                        ),
                        logical_turn_ordinal=strict_non_negative_int(
                            candidate["logical_turn_ordinal"],
                            field="context_view_turns.logical_turn_ordinal",
                        ),
                        first_message_sequence=first_sequence,
                        user_message_sequence=user_sequence,
                        last_message_sequence=last_sequence,
                        final_message_sequence=final_sequence,
                        anchor_mode=anchor_mode,
                        cutoff_message_sequence=cutoff,
                    )
            parent = connection.execute(
                "SELECT parent_view_id FROM context_views WHERE view_id = ?",
                (current_view_id,),
            ).fetchone()
            if parent is None:
                raise RuntimeError(f"context view 不存在: {current_view_id}")
            current_view_id = strict_optional_text(
                parent[0], field="context_views.parent_view_id"
            ) or ""

        raise KeyError(
            f"当前 active view lineage 不包含可恢复的完整 Turn: turn_id={turn_id}"
        )

    def _source_checkpoint_for_view(
        self,
        connection: sqlite3.Connection,
        view_id: str,
        *,
        checkpoint_ns: str,
        max_message_sequence: int | None = None,
        max_commit_id: int | None = None,
    ) -> str | None:
        view_id = strict_text(view_id, field="context_views.view_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        max_message_sequence = (
            strict_non_negative_int(
                max_message_sequence, field="max_message_sequence"
            )
            if max_message_sequence is not None
            else None
        )
        max_commit_id = (
            strict_non_negative_int(max_commit_id, field="max_commit_id")
            if max_commit_id is not None
            else None
        )
        row = connection.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE checkpoint_ns = ? AND view_id = ? AND status = 'active' AND (? IS NULL OR message_sequence <= ?) AND (? IS NULL OR commit_id <= ?) ORDER BY commit_id DESC LIMIT 1",
            (checkpoint_ns, view_id, max_message_sequence, max_message_sequence, max_commit_id, max_commit_id),
        ).fetchone()
        if row is not None:
            return strict_text(row[0], field="checkpoints.checkpoint_id")
        row = connection.execute(
            "SELECT b.head_checkpoint_id FROM branches b JOIN checkpoints c ON c.checkpoint_id = b.head_checkpoint_id WHERE c.checkpoint_ns = ? AND b.head_view_id = ? AND b.head_checkpoint_id IS NOT NULL AND c.status = 'active' AND (? IS NULL OR c.message_sequence <= ?) AND (? IS NULL OR c.commit_id <= ?) ORDER BY b.updated_at DESC LIMIT 1",
            (checkpoint_ns, view_id, max_message_sequence, max_message_sequence, max_commit_id, max_commit_id),
        ).fetchone()
        if row is not None:
            return strict_text(row[0], field="checkpoints.checkpoint_id")
        row = connection.execute(
            "SELECT ce.checkpoint_id FROM control_events ce JOIN checkpoints c ON c.checkpoint_id = ce.checkpoint_id WHERE c.checkpoint_ns = ? AND ce.view_id = ? AND ce.checkpoint_id IS NOT NULL AND c.status = 'active' AND (? IS NULL OR c.message_sequence <= ?) AND (? IS NULL OR c.commit_id <= ?) ORDER BY ce.control_sequence DESC LIMIT 1",
            (checkpoint_ns, view_id, max_message_sequence, max_message_sequence, max_commit_id, max_commit_id),
        ).fetchone()
        return (
            strict_text(row[0], field="control_events.checkpoint_id")
            if row is not None
            else None
        )

    def _checkpoint_for_turn_prefix(
        self, connection: sqlite3.Connection, checkpoint_ns: str,
        checkpoint_id: str, user_sequence: int,
    ) -> str:
        """canonical anchor 负责截断；冻结输入边界选择对应的非消息状态 header。"""
        current = self._checkpoint_row(connection, checkpoint_ns, checkpoint_id)
        if current is None:
            raise RuntimeError(f"Turn prefix 缺少持久 checkpoint header: {checkpoint_id}")
        visited: set[str] = set()
        while True:
            checkpoint_id = strict_text(current[0], field="checkpoints.checkpoint_id")
            if checkpoint_id in visited:
                raise RuntimeError(f"checkpoint header 父链成环: {checkpoint_id}")
            visited.add(checkpoint_id)
            channel = connection.execute(
                "SELECT context_view_id FROM checkpoint_channels WHERE checkpoint_id=? AND channel_name='messages'",
                (checkpoint_id,),
            ).fetchone()
            if channel is None:
                raise RuntimeError(f"Turn prefix checkpoint 缺少冻结 messages channel: {checkpoint_id}")
            messages_view_id = self._require_checkpoint_messages_view(
                connection, checkpoint_id=checkpoint_id,
                history_view_id=strict_text(current[6], field="checkpoints.view_id"),
                branch_id=strict_text(current[7], field="checkpoints.branch_id"),
                messages_view_id=strict_text(channel[0], field="checkpoint_channels.context_view_id"),
            )
            if user_sequence not in self._storage._view_message_sequences_from_connection(connection, messages_view_id):
                return checkpoint_id
            parent_id = strict_optional_text(current[5], field="checkpoints.parent_checkpoint_id")
            parent = self._checkpoint_row(connection, checkpoint_ns, parent_id) if parent_id is not None else None
            if parent is None:
                # 首次 checkpoint 的内存初始父节点没有持久 header，保留首次状态。
                return checkpoint_id
            current = parent
