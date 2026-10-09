"""rollout active context view 的一致性与 durable turn root 绑定。"""

from __future__ import annotations

from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


def _turn_root_json(value: object) -> str:
    import rfc8785

    return rfc8785.dumps(value).decode("utf-8")


__all__ = ["RolloutActiveViewMixin"]


class RolloutActiveViewMixin:
    def ensure_active_view_contains_turn_root(
        self,
        session_id: str,
        thread_id: str,
        *,
        turn_id: str,
        checkpoint_ns: str = "",
    ) -> bool:
        """把已接受 Turn root 幂等补入当前 active view 的 item 索引。

        acceptance 可能先于 LangGraph 创建初始化 view；因此 acceptance 事务
        本身没有可更新的 view。provider dispatch 前再次执行这个 owner-side
        同步，避免首个 assembly 在 active view 已建立后仍看不到 root。只更新
        SQLite view membership；checkpoint 引用的 head 写前切换 successor。
        """
        session_id = strict_text(session_id, field="session_id")
        thread_id = strict_text(thread_id, field="thread_id")
        turn_id = strict_text(turn_id, field="turn_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        if not session_id or not thread_id or not turn_id:
            raise ValueError("ensure active view root 缺少 session_id/thread_id/turn_id")
        with self._storage._lock(
            thread_id, checkpoint_ns, session_id=session_id
        ):
            self._storage.initialize(session_id, checkpoint_ns, thread_id=thread_id)
            with self._storage._connect(
                thread_id, checkpoint_ns, session_id=session_id
            ) as connection:
                self._storage._require_v2_runtime(connection)
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
                item_added = self._storage._append_context_view_items(
                    connection,
                    checkpoint_ns=checkpoint_ns,
                    item_ids=(item_id,),
                )
                if not item_added:
                    return False
                view_id = self._storage._active_context_view_id(connection, checkpoint_ns)
                if view_id is None:
                    raise RuntimeError("Turn root 写入后缺少 active context view")
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
