"""rollout active context view 的一致性与 turn root 修复。"""

from __future__ import annotations

from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)

_VISIBLE_NORMAL_TURN_PREDICATE = (
    "EXISTS (SELECT 1 FROM messages AS visible_user_message "
    "WHERE visible_user_message.turn_id = t.turn_id "
    "AND visible_user_message.role = 'user' "
    "AND visible_user_message.visibility = 'visible')"
)

def _turn_root_json(value: object) -> str:
    import rfc8785

    return rfc8785.dumps(value).decode("utf-8")


__all__ = ["RolloutActiveViewMixin"]


class RolloutActiveViewMixin:
    def repair_active_context_view(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> bool:
        """修复 active view 的 Turn 索引，不重写 canonical 消息文件。

        旧版本按全局消息序号连续性判断 Turn 完整性。并发执行时不同 Turn
        的消息会交错，导致 view 的消息范围存在但 ``context_view_turns`` 被
        错误删空。这里依据每个 Turn 自身的消息集合重新计算索引；只有索引
        与规范结果不一致时才写 SQLite，避免普通只读请求产生文件监听噪声。
        """
        with self._host._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._host._connect(thread_id, checkpoint_ns) as connection:
                self._host._require_v2_runtime(connection)
                namespace = self._namespace_state(connection, checkpoint_ns)
                branch_row = connection.execute(
                    "SELECT head_view_id FROM branches WHERE branch_id = ? AND status = 'active'",
                    (namespace[0],),
                ).fetchone()
                if branch_row is None:
                    return False
                view_id = strict_optional_text(
                    branch_row[0], field="branches.head_view_id"
                )
                if view_id is None:
                    return False
                visible_sequences = set(
                    self._host._view_message_sequences_from_connection(connection, view_id)
                )
                expected: list[tuple[str, int, int | None, int | None]] = []
                turn_rows = connection.execute(
                    f"SELECT turn_id, first_message_sequence, last_message_sequence, user_message_sequence, final_message_sequence FROM turns AS t WHERE t.turn_kind = 'normal' AND t.user_message_sequence IS NOT NULL AND {_VISIBLE_NORMAL_TURN_PREDICATE} ORDER BY t.turn_ordinal"
                ).fetchall()
                for row in turn_rows:
                    turn_id = strict_text(row[0], field="turns.turn_id")
                    first_sequence = strict_non_negative_int(
                        row[1], field=f"turns.first_message_sequence:{turn_id}"
                    )
                    last_sequence = strict_non_negative_int(
                        row[2], field=f"turns.last_message_sequence:{turn_id}"
                    )
                    user_sequence = strict_optional_non_negative_int(
                        row[3], field=f"turns.user_message_sequence:{turn_id}"
                    )
                    final_sequence = strict_optional_non_negative_int(
                        row[4], field=f"turns.final_message_sequence:{turn_id}"
                    )
                    if (
                        first_sequence == 0
                        or last_sequence == 0
                        or last_sequence < first_sequence
                        or user_sequence is None
                    ):
                        raise RuntimeError(
                            f"active view repair Turn message range 非法: {turn_id}"
                        )
                    turn_sequences = {
                        strict_non_negative_int(
                            message_row[0],
                            field=f"messages.message_sequence:{turn_id}",
                        )
                        for message_row in connection.execute(
                            "SELECT message_sequence FROM messages WHERE turn_id = ?",
                            (turn_id,),
                        ).fetchall()
                    }
                    if not turn_sequences:
                        raise RuntimeError(
                            f"active view repair Turn 没有 message: {turn_id}"
                        )
                    if turn_sequences.issubset(visible_sequences):
                        expected.append(
                            (
                                turn_id,
                                first_sequence,
                                user_sequence,
                                final_sequence,
                            )
                        )
                current = [
                    (
                        strict_text(row[0], field="context_view_turns.turn_id"),
                        strict_non_negative_int(
                            row[1], field="context_view_turns.logical_turn_ordinal"
                        ),
                        strict_optional_non_negative_int(
                            row[2], field="context_view_turns.user_message_sequence"
                        ),
                        strict_optional_non_negative_int(
                            row[3], field="context_view_turns.final_message_sequence"
                        ),
                    )
                    for row in connection.execute(
                        "SELECT turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence FROM context_view_turns WHERE view_id = ? ORDER BY logical_turn_ordinal",
                        (view_id,),
                    ).fetchall()
                ]
                normalized_expected = [
                    (turn_id, ordinal, user_sequence, final_sequence)
                    for ordinal, (
                        turn_id,
                        _first,
                        user_sequence,
                        final_sequence,
                    ) in enumerate(expected, start=1)
                ]
                view_header = connection.execute(
                    "SELECT head_turn_id, head_message_sequence, logical_turn_count FROM context_views WHERE view_id = ?",
                    (view_id,),
                ).fetchone()
                if view_header is None:
                    raise RuntimeError(f"active context view 不存在: {view_id}")
                stored_head_turn_id = strict_optional_text(
                    view_header[0], field="context_views.head_turn_id"
                )
                stored_head_sequence = strict_non_negative_int(
                    view_header[1], field="context_views.head_message_sequence"
                )
                stored_turn_count = strict_non_negative_int(
                    view_header[2], field="context_views.logical_turn_count"
                )
                expected_head = (
                    normalized_expected[-1][0] if normalized_expected else None
                )
                expected_sequence = max(visible_sequences, default=0)
                if (
                    current == normalized_expected
                    and stored_head_turn_id == expected_head
                    and stored_head_sequence == expected_sequence
                    and stored_turn_count == len(normalized_expected)
                ):
                    return False
                delete_result = connection.execute(
                    "DELETE FROM context_view_turns WHERE view_id = ?",
                    (view_id,),
                )
                if delete_result.rowcount != len(current):
                    raise RuntimeError(
                        f"active view repair 删除 Turn 行数不一致: {view_id}"
                    )
                insert_result = connection.executemany(
                    "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence) VALUES (?, ?, ?, ?, ?)",
                    (
                        (view_id, turn_id, ordinal, user_sequence, final_sequence)
                        for ordinal, (
                            turn_id,
                            _first,
                            user_sequence,
                            final_sequence,
                        ) in enumerate(expected, start=1)
                    ),
                )
                if insert_result.rowcount != len(normalized_expected):
                    raise RuntimeError(
                        f"active view repair 插入 Turn 行数不一致: {view_id}"
                    )
                header_result = connection.execute(
                    "UPDATE context_views SET head_turn_id = ?, head_message_sequence = ?, logical_turn_count = ? WHERE view_id = ?",
                    (
                        expected_head,
                        expected_sequence,
                        len(normalized_expected),
                        view_id,
                    ),
                )
                if header_result.rowcount != 1:
                    raise RuntimeError(f"active view repair header 更新失败: {view_id}")
                connection.commit()
                return True

    def ensure_active_view_contains_turn_root(
        self,
        thread_id: str,
        *,
        turn_id: str,
        checkpoint_ns: str = "",
    ) -> bool:
        """把已接受 Turn root 幂等补入当前 active view 的 item 索引。

        acceptance 可能先于 LangGraph 创建初始化 view；因此 acceptance 事务
        本身没有可更新的 view。provider dispatch 前再次执行这个 owner-side
        同步，避免首个 assembly 在 active view 已建立后仍看不到 root。只更新
        SQLite view membership，不改变 canonical JSONL 或 view head。
        """
        thread_id = strict_text(thread_id, field="thread_id")
        turn_id = strict_text(turn_id, field="turn_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        if not thread_id or not turn_id:
            raise ValueError("ensure active view root 缺少 thread_id/turn_id")
        with self._host._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._host._connect(thread_id, checkpoint_ns) as connection:
                self._host._require_v2_runtime(connection)
                branch_row = connection.execute(
                    "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
                    (checkpoint_ns,),
                ).fetchone()
                if branch_row is None:
                    raise RuntimeError(
                        f"rollout namespace 缺少 active branch: {checkpoint_ns!r}"
                    )
                active_branch_id = strict_text(
                    branch_row[0],
                    field="checkpoint_namespace_state.active_branch_id",
                )
                view_row = connection.execute(
                    "SELECT head_view_id FROM branches WHERE branch_id = ? AND status = 'active'",
                    (active_branch_id,),
                ).fetchone()
                if view_row is None:
                    raise RuntimeError(
                        f"rollout active branch 不存在: {active_branch_id}"
                    )
                view_id = strict_optional_text(
                    view_row[0], field="branches.head_view_id"
                )
                if view_id is None:
                    return False
                root_row = connection.execute(
                    "SELECT root_input_item_id FROM turn_records WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                if root_row is None:
                    raise KeyError(f"Turn root 不存在: {turn_id}")
                item_id = strict_text(
                    root_row[0], field=f"turn_records.root_input_item_id:{turn_id}"
                )
                if (
                    connection.execute(
                        "SELECT 1 FROM item_catalog WHERE item_id = ?",
                        (item_id,),
                    ).fetchone()
                    is None
                ):
                    raise RuntimeError(f"Turn root catalog 缺失: {item_id}")
                item_added = self._host._append_context_view_items(
                    connection,
                    checkpoint_ns=checkpoint_ns,
                    item_ids=(item_id,),
                )
                if not item_added:
                    return False
                view_turn = connection.execute(
                    "SELECT 1 FROM context_view_turns WHERE view_id = ? AND turn_id = ?",
                    (view_id, turn_id),
                ).fetchone()
                if view_turn is None:
                    ordinal_row = connection.execute(
                        "SELECT COALESCE(MAX(logical_turn_ordinal), 0) + 1 FROM context_view_turns WHERE view_id = ?",
                        (view_id,),
                    ).fetchone()
                    if ordinal_row is None:
                        raise RuntimeError(
                            f"active view Turn ordinal 无法读取: {view_id}"
                        )
                    logical_turn_ordinal = strict_non_negative_int(
                        ordinal_row[0],
                        field="context_view_turns.next_logical_turn_ordinal",
                    )
                    if logical_turn_ordinal == 0:
                        raise RuntimeError(
                            f"active view Turn ordinal 不能为 0: {view_id}"
                        )
                    turn_result = connection.execute(
                        "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence, root_input_item_id, fork_lineage_json) VALUES (?, ?, ?, NULL, NULL, ?, ?)",
                        (
                            view_id,
                            turn_id,
                            logical_turn_ordinal,
                            item_id,
                            _turn_root_json({"source": "turn_root_reconciliation"}),
                        ),
                    )
                    if turn_result.rowcount != 1:
                        raise RuntimeError(
                            f"active view Turn root 写入失败: {view_id}/{turn_id}"
                        )
                    header_result = connection.execute(
                        "UPDATE context_views SET head_turn_id = ?, logical_turn_count = MAX(logical_turn_count, ?) WHERE view_id = ?",
                        (turn_id, logical_turn_ordinal, view_id),
                    )
                    if header_result.rowcount != 1:
                        raise RuntimeError(f"active view header 更新失败: {view_id}")
                connection.commit()
                return True
