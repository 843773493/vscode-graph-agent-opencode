"""v2 checkpoint/view/anchor durable owner。

这里只调用 RolloutStorage 提供的 SQLite、JSONL 和 domain ports，不创建
LangChain message，也不读取 v1 数据。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

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


def _now() -> str:
    return datetime.now(UTC).isoformat()


from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)


def _hash_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


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

    def _create_view(
        self,
        connection: sqlite3.Connection,
        branch_id: str,
        parent_view_id: str | None,
        visible_sequences: Sequence[int],
        timestamp: str,
        *,
        view_kind: str = "checkpoint",
    ) -> str:
        branch_id = strict_text(branch_id, field="context_views.branch_id")
        parent_view_id = strict_optional_text(
            parent_view_id, field="context_views.parent_view_id"
        )
        timestamp = strict_text(timestamp, field="context_views.created_at")
        view_kind = strict_text(view_kind, field="context_views.view_kind")
        view_id = "view-" + uuid4().hex
        sequence_values = tuple(
            dict.fromkeys(
                strict_non_negative_int(value, field="messages.message_sequence")
                for value in visible_sequences
            )
        )
        if any(value == 0 for value in sequence_values):
            raise ValueError("messages.message_sequence 必须为正数")
        materialized_sequences = sequence_values
        if parent_view_id and view_kind == "checkpoint":
            # 新 checkpoint 的 visible_sequences 可能只是本次 delta；Turn
            # 索引也必须基于完整的父链，否则最新 view 会只有 ToolMessage，
            # 历史分页会暂时看不到这条正在执行的 Turn。
            materialized_sequences = tuple(
                dict.fromkeys(
                    [
                        *self._view_message_sequences_from_connection(
                            connection,
                            parent_view_id,
                        ),
                        *sequence_values,
                    ]
                )
            )
        message_rows = (
            connection.execute(
                "SELECT message_sequence, turn_id FROM messages WHERE message_sequence IN ("
                + ",".join("?" for _ in materialized_sequences)
                + ")",
                materialized_sequences,
            ).fetchall()
            if materialized_sequences
            else []
        )
        if len(message_rows) != len(materialized_sequences):
            raise RuntimeError(
                f"context view 引用了不存在的 message: view_id={view_id}"
            )
        turn_by_sequence = {
            strict_non_negative_int(sequence, field="messages.message_sequence"): strict_text(
                turn_id, field="messages.turn_id"
            )
            for sequence, turn_id in message_rows
        }
        turn_ids: list[str] = []
        sequence_set = set(materialized_sequences)
        for sequence in materialized_sequences:
            value = turn_by_sequence.get(sequence)
            if value is None:
                continue
            if value in turn_ids:
                continue
            row = connection.execute(
                "SELECT turn_kind, first_message_sequence, last_message_sequence, user_message_sequence, final_message_sequence FROM turns WHERE turn_id = ?",
                (value,),
            ).fetchone()
            if row is None:
                if value.startswith("internal-"):
                    continue
                raise RuntimeError(f"message Turn projection 缺失: {value}")
            turn_kind = strict_text(row[0], field=f"turns.turn_kind: {value}")
            first_sequence = strict_non_negative_int(
                row[1], field=f"turns.first_message_sequence: {value}"
            )
            last_sequence = strict_non_negative_int(
                row[2], field=f"turns.last_message_sequence: {value}"
            )
            user_sequence = strict_optional_non_negative_int(
                row[3], field=f"turns.user_message_sequence: {value}"
            )
            final_sequence = strict_optional_non_negative_int(
                row[4], field=f"turns.final_message_sequence: {value}"
            )
            if turn_kind != "normal" or user_sequence is None:
                continue
            if (
                first_sequence == 0
                or last_sequence == 0
                or last_sequence < first_sequence
                or final_sequence is not None
                and final_sequence < user_sequence
            ):
                raise RuntimeError(f"Turn message sequence 索引非法: {value}")
            # 不同并发 Turn 的 canonical message sequence 可能交错。不能用
            # first..last 的连续整数判断完整性，否则后来完成的 Turn 会因为
            # 中间插入了其它 Turn 的消息而从 active view 消失，历史读取随后
            # 被错误识别成 stale reference。
            turn_sequences = {
                strict_non_negative_int(
                    message_row[0], field=f"messages.message_sequence: {value}"
                )
                for message_row in connection.execute(
                    "SELECT message_sequence FROM messages WHERE turn_id = ?",
                    (value,),
                ).fetchall()
            }
            if turn_sequences and turn_sequences.issubset(sequence_set):
                turn_ids.append(value)
        rows: list[tuple[str, int, object, object, str | None]] = []
        for ordinal, turn_id in enumerate(turn_ids, start=1):
            row = connection.execute(
                "SELECT user_message_sequence, final_message_sequence FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if row is not None:
                user_sequence_value = strict_optional_non_negative_int(
                    row[0], field=f"turns.user_message_sequence: {turn_id}"
                )
                final_sequence_value = strict_optional_non_negative_int(
                    row[1], field=f"turns.final_message_sequence: {turn_id}"
                )
                root_row = connection.execute(
                    "SELECT root_input_item_id FROM turn_records WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                rows.append(
                    (
                        turn_id,
                        ordinal,
                        user_sequence_value,
                        final_sequence_value,
                        strict_text(root_row[0], field="turn_records.root_input_item_id")
                        if root_row is not None and root_row[0] is not None
                        else None,
                    )
                )
        head_turn = rows[-1][0] if rows else None
        head_message_sequence = (
            max(materialized_sequences) if materialized_sequences else 0
        )
        view_cursor = connection.execute(
            "INSERT INTO context_views(view_id, branch_id, parent_view_id, view_kind, head_turn_id, head_message_sequence, logical_turn_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                view_id,
                branch_id,
                parent_view_id,
                view_kind,
                head_turn,
                head_message_sequence,
                len(rows),
                timestamp,
            ),
        )
        if view_cursor.rowcount != 1:
            raise RuntimeError(f"context view 插入失败: {view_id}")
        ranges: list[tuple[int, int]] = []
        for sequence in sequence_values:
            if not ranges or sequence != ranges[-1][1] + 1:
                ranges.append((sequence, sequence))
            else:
                ranges[-1] = (ranges[-1][0], sequence)
        range_index = 0
        if parent_view_id and view_kind == "checkpoint":
            # 普通 checkpoint 可能只携带本次 LangGraph delta（例如单独的
            # ToolMessage），而不是完整 messages 快照。parent_view_id 仅用于
            # 跳转索引并不能参与消息物化；显式保留父 view，才能让对应的
            # assistant tool call 与后续 ToolMessage 一起进入下一次模型请求。
            range_cursor = connection.execute(
                "INSERT INTO context_view_ranges(view_id, range_index, source_kind, source_view_id, start_message_sequence, end_message_sequence, message_start_sequence, message_end_sequence, range_ordinal, logical_start_turn_ordinal, logical_end_turn_ordinal) VALUES (?, ?, 'view', ?, NULL, NULL, NULL, NULL, ?, NULL, NULL)",
                (view_id, range_index, parent_view_id, range_index),
            )
            if range_cursor.rowcount != 1:
                raise RuntimeError(f"context view parent range 插入失败: {view_id}")
            range_index += 1
        for start_sequence, end_sequence in ranges:
            range_cursor = connection.execute(
                "INSERT INTO context_view_ranges(view_id, range_index, source_kind, start_message_sequence, end_message_sequence, message_start_sequence, message_end_sequence, range_ordinal, logical_start_turn_ordinal, logical_end_turn_ordinal) VALUES (?, ?, 'messages', ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    view_id,
                    range_index,
                    start_sequence,
                    end_sequence,
                    start_sequence,
                    end_sequence,
                    range_index,
                ),
            )
            if range_cursor.rowcount != 1:
                raise RuntimeError(f"context view message range 插入失败: {view_id}")
            range_index += 1
        if parent_view_id:
            jump_cursor = connection.execute(
                "INSERT INTO context_view_jumps(view_id, jump_level, ancestor_view_id, ancestor_depth) VALUES (?, 0, ?, 1)",
                (view_id, parent_view_id),
            )
            if jump_cursor.rowcount != 1:
                raise RuntimeError(f"context view ancestor jump 插入失败: {view_id}")
            parent_by_view = {
                strict_text(row[0], field="context_views.view_id"): strict_optional_text(
                    row[1], field="context_views.parent_view_id"
                )
                for row in connection.execute(
                    "SELECT view_id, parent_view_id FROM context_views"
                ).fetchall()
            }
            level = 1
            while True:
                ancestor = view_id
                for _ in range(2**level):
                    ancestor = parent_by_view.get(ancestor)
                    if ancestor is None:
                        break
                if ancestor is None:
                    break
                jump_cursor = connection.execute(
                    "INSERT INTO context_view_jumps(view_id, jump_level, ancestor_view_id, ancestor_depth) VALUES (?, ?, ?, ?)",
                    (
                        view_id,
                        level,
                        ancestor,
                        2**level,
                    ),
                )
                if jump_cursor.rowcount != 1:
                    raise RuntimeError(
                        f"context view ancestor jump 插入失败: {view_id}/{level}"
                    )
                level += 1
        for turn_id, ordinal, user_sequence, final_sequence, root_item_id in rows:
            turn_cursor = connection.execute(
                "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence, root_input_item_id, fork_lineage_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    view_id,
                    turn_id,
                    ordinal,
                    user_sequence,
                    final_sequence,
                    root_item_id,
                    _json(
                        {
                            "source_view_id": parent_view_id,
                            "source_branch_id": branch_id,
                        }
                    ),
                ),
            )
            if turn_cursor.rowcount != 1:
                raise RuntimeError(
                    f"context view turn 插入失败: {view_id}/{turn_id}"
                )
        item_rows = (
            connection.execute(
                "SELECT m.message_sequence, m.message_id FROM messages AS m WHERE m.message_sequence IN ("
                + ",".join("?" for _ in materialized_sequences)
                + ") ORDER BY m.message_sequence",
                materialized_sequences,
            ).fetchall()
            if materialized_sequences
            else []
        )
        candidate_item_ids: set[str] = set()
        for message_sequence, message_id in item_rows:
            strict_non_negative_int(
                message_sequence, field="messages.message_sequence"
            )
            message_id = strict_text(message_id, field="messages.message_id")
            item_rows_for_message = connection.execute(
                "SELECT item_id FROM item_catalog WHERE item_id = ? "
                "OR json_extract(metadata_json, '$.projection_message_id') = ? "
                "ORDER BY item_sequence",
                (f"item-{message_id}", message_id),
            ).fetchall()
            if not item_rows_for_message:
                raise RuntimeError(
                    f"message projection 缺少 canonical item: {message_id}"
                )
            if len(item_rows_for_message) != 1:
                raise RuntimeError(
                    "message projection 对应多个 canonical item: "
                    f"message_id={message_id}, count={len(item_rows_for_message)}"
                )
            candidate_item_ids.add(
                strict_text(
                    item_rows_for_message[0][0], field="item_catalog.item_id"
                )
            )
        # provider block item 没有独立的 message_sequence；它们仍属于同一
        # Turn 的 active context view。message mirror 与 block item 必须先合并，
        # 再按 canonical item_sequence 排序，不能把 block item 盲目追加到旧
        # LangChain message 顺序之后；否则 assistant tool_call 会落到 tool
        # result 之后，provider/history projector 得到非法因果顺序。
        if turn_ids:
            placeholders = ",".join("?" for _ in turn_ids)
            block_items = connection.execute(
                f"SELECT item_id FROM item_catalog WHERE turn_id IN ({placeholders}) ORDER BY item_sequence, item_id",
                tuple(turn_ids),
            ).fetchall()
            candidate_item_ids.update(
                strict_text(item_id, field="item_catalog.item_id")
                for (item_id,) in block_items
            )
        if candidate_item_ids:
            placeholders = ",".join("?" for _ in candidate_item_ids)
            ordered_item_rows = connection.execute(
                f"SELECT item_id FROM item_catalog WHERE item_id IN ({placeholders}) ORDER BY item_sequence, item_id",
                tuple(candidate_item_ids),
            ).fetchall()
            for logical_item_ordinal, (item_id,) in enumerate(ordered_item_rows):
                item_id = strict_text(item_id, field="item_catalog.item_id")
                item_cursor = connection.execute(
                    "INSERT INTO context_view_items(view_id, item_id, logical_item_ordinal, visible, source_kind) VALUES (?, ?, ?, 1, 'canonical')",
                    (view_id, item_id, logical_item_ordinal),
                )
                if item_cursor.rowcount != 1:
                    raise RuntimeError(
                        f"context view item 插入失败: {view_id}/{item_id}"
                    )
        return view_id

    def read_items_for_view(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
    ) -> list[CanonicalItemRecord]:
        """按 active view 的 item 索引读取 canonical item，不扫描物理前缀。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        connection = self._snapshot_connection(snapshot)
        rows = connection.execute(
            "SELECT item_id FROM context_view_items WHERE view_id = ? AND visible = 1 ORDER BY logical_item_ordinal",
            (view_id,),
        ).fetchall()
        item_ids = tuple(
            strict_text(row[0], field="context_view_items.item_id") for row in rows
        )
        items = self.read_items(
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
            self._snapshot_connection(snapshot),
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
        connection = self._snapshot_connection(snapshot)
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
        if require_completed:
            checkpoint_sequence_limit = (
                final_sequence if anchor_mode == "inclusive" else user_sequence - 1
            )

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
                )
                if checkpoint_id is not None:
                    cutoff = (
                        last_sequence
                        if anchor_mode == "inclusive"
                        else user_sequence - 1
                    )
                    return RolloutTurnAnchor(
                        turn_id=stored_turn_id,
                        view_id=current_view_id,
                        checkpoint_id=checkpoint_id,
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
        row = connection.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE checkpoint_ns = ? AND view_id = ? AND status = 'active' AND (? IS NULL OR message_sequence <= ?) ORDER BY commit_id DESC LIMIT 1",
            (checkpoint_ns, view_id, max_message_sequence, max_message_sequence),
        ).fetchone()
        if row is not None:
            return strict_text(row[0], field="checkpoints.checkpoint_id")
        row = connection.execute(
            "SELECT b.head_checkpoint_id FROM branches b JOIN checkpoints c ON c.checkpoint_id = b.head_checkpoint_id WHERE c.checkpoint_ns = ? AND b.head_view_id = ? AND b.head_checkpoint_id IS NOT NULL AND c.status = 'active' AND (? IS NULL OR c.message_sequence <= ?) ORDER BY b.updated_at DESC LIMIT 1",
            (checkpoint_ns, view_id, max_message_sequence, max_message_sequence),
        ).fetchone()
        if row is not None:
            return strict_text(row[0], field="checkpoints.checkpoint_id")
        row = connection.execute(
            "SELECT ce.checkpoint_id FROM control_events ce JOIN checkpoints c ON c.checkpoint_id = ce.checkpoint_id WHERE c.checkpoint_ns = ? AND ce.view_id = ? AND ce.checkpoint_id IS NOT NULL AND c.status = 'active' AND (? IS NULL OR c.message_sequence <= ?) ORDER BY ce.control_sequence DESC LIMIT 1",
            (checkpoint_ns, view_id, max_message_sequence, max_message_sequence),
        ).fetchone()
        return (
            strict_text(row[0], field="control_events.checkpoint_id")
            if row is not None
            else None
        )

    def materialize_turn_anchor(
        self,
        snapshot: RolloutReadSnapshot,
        anchor: RolloutTurnAnchor,
    ) -> list[object]:
        """读取 anchor view 中截至边界的消息，不读取父 rollout。"""
        connection = self._snapshot_connection(snapshot)
        sequences = self._view_message_sequences_from_connection(
            connection,
            anchor.view_id,
        )
        selected = [
            sequence
            for sequence in sequences
            if sequence <= anchor.cutoff_message_sequence
        ]
        values = self._read_messages(
            snapshot.thread_id,
            snapshot.checkpoint_ns,
            selected,
            connection=connection,
        )
        return [self._codec().from_dict(value) for value in values]
