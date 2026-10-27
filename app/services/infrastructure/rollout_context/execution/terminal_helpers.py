"""terminal convergence 使用的 canonical item 与 active view helper。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import (
    provider_tool_call_id,
    software_tool_operation_id,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )


def execution_lost_tool_results(
    storage: RolloutStorage, admission: ExecutionAdmission, connection: sqlite3.Connection, *,
    turn_id: str | None, checkpoint_ns: str,
    items: Sequence[CanonicalItemRecord],
) -> tuple[CanonicalItemRecord, ...]:
    """闭合已提交但没有结果的声明；恢复通知不能冒充真实工具 attempt。"""
    session_id, thread_id, execution_id = admission.session_id, admission.thread_id, admission.execution_id
    if admission.turn_id != turn_id:
        raise ValueError("工具丢失通知与原 execution/Turn 不一致")
    rows = connection.execute(
        "SELECT item_id FROM item_catalog WHERE execution_id=? "
        "AND semantic_kind IN ('tool_call','tool_result') ORDER BY item_sequence",
        (execution_id,),
    ).fetchall()
    stored = storage.read_items(
        session_id, thread_id, checkpoint_ns=checkpoint_ns,
        item_ids=tuple(row[0] for row in rows), connection=connection,
    ) if rows else ()
    declarations: dict[tuple[str, str], str] = {}
    results: dict[tuple[str, str], CanonicalItemRecord] = {}
    for item in (*stored, *items):
        if item.status != "completed" or item.semantic_kind not in {"tool_call", "tool_result"}:
            continue
        identity = item.projection_identity
        if identity is None or identity.model_call_id is None:
            raise RuntimeError("已提交工具声明/结果缺少原 ModelCall 归属")
        model_call = identity.model_call_id
        payload = item.payload
        if not isinstance(payload, Mapping):
            raise TypeError("工具丢失恢复需要完整 canonical 声明/结果")
        if item.semantic_kind == "tool_result":
            call_id = strict_text(item.tool_call_id, field="tool_result.tool_call_id")
            results[(model_call, provider_tool_call_id(identity, call_id))] = item
            continue
        calls = payload.get("tool_calls", [payload])
        if not isinstance(calls, list):
            raise TypeError("工具丢失恢复的 tool_calls 必须是数组")
        for call in calls:
            if not isinstance(call, Mapping):
                raise TypeError("工具丢失恢复的声明必须是对象")
            call_id = strict_text(call.get("id", call.get("tool_call_id")), field="tool_call.id")
            name = strict_text(call.get("name"), field="tool_call.name")
            key = (model_call, provider_tool_call_id(identity, call_id))
            previous = declarations.setdefault(key, name)
            if previous != name:
                raise RuntimeError("工具丢失恢复的原调用声明冲突")
    recovered: list[CanonicalItemRecord] = []
    incoming = {item.item_id for item in items}
    for (model_call, call_id), name in declarations.items():
        recovery_id = software_tool_operation_id(
            "execution_lost_tool_result", session_id, thread_id, model_call, call_id,
        )
        item_id = f"item-{recovery_id}"
        previous = results.get((model_call, call_id))
        if previous is not None:
            # 重复终态提交必须使用原通知正文与时间，不重新生成或改写真实结果。
            if previous.item_id == item_id and item_id not in incoming:
                recovered.append(previous)
            continue
        recovered.append(CanonicalItemRecord.create(
            item_sequence=1, item_id=item_id,
            semantic_kind="tool_result", payload_kind="tool_result", status="completed",
            producer_ref={"producer_kind": "runtime", "producer_id": execution_id, "invocation_id": model_call},
            payload={
                "tool_call_id": call_id, "result_id": recovery_id, "name": name,
                "content": "工作区后端重启，原工具调用没有已确认结果；执行是否发生未知，禁止自动重试。",
                "tool_outcome": "unknown", "execution_confirmed": False,
            },
            projection_identity=ItemProjectionIdentity(model_call_id=model_call),
            turn_id=turn_id, turn_scope="turn_member" if turn_id is not None else None,
            message_group_id=f"message-{recovery_id}", wire_role="tool",
        ))
    return (*items, *recovered)


class RolloutTerminalProjectionMixin:
    """只负责 terminal projection 所需的 durable item/view 辅助读取。"""

    def _ensure_active_view_turn(
        self,
        connection: sqlite3.Connection,
        *,
        checkpoint_ns: str,
        turn_id: str,
    ) -> None:
        """让没有先经过 checkpoint view 的 terminal Turn 也拥有 active 索引。"""
        branch = connection.execute(
            "SELECT active_branch_id FROM checkpoint_namespace_state "
            "WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if branch is None:
            raise RuntimeError(
                f"terminal convergence 缺少 active branch: {checkpoint_ns!r}"
            )
        branch_id = strict_text(
            branch[0], field="checkpoint_namespace_state.active_branch_id"
        )
        view = connection.execute(
            "SELECT head_view_id FROM branches "
            "WHERE branch_id = ? AND status = 'active'",
            (branch_id,),
        ).fetchone()
        turn = connection.execute(
            "SELECT user_message_sequence, final_message_sequence FROM turns "
            "WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        root = connection.execute(
            "SELECT root_input_item_id FROM turn_records WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if view is None or turn is None or root is None:
            raise RuntimeError(f"terminal convergence 无法建立 active Turn view: {turn_id}")
        view_id = strict_text(view[0], field="branches.head_view_id")
        user_sequence = strict_optional_non_negative_int(
            turn[0], field="turns.user_message_sequence"
        )
        final_sequence = strict_optional_non_negative_int(
            turn[1], field="turns.final_message_sequence"
        )
        root_item_id = strict_text(
            root[0], field="turn_records.root_input_item_id"
        )
        existing = connection.execute(
            "SELECT user_message_sequence, final_message_sequence, root_input_item_id "
            "FROM context_view_turns WHERE view_id = ? AND turn_id = ?",
            (view_id, turn_id),
        ).fetchone()
        if existing == (user_sequence, final_sequence, root_item_id):
            return
        view_id = self._storage._writable_active_context_view_id(connection, checkpoint_ns, branch_id)
        if existing is not None:
            connection.execute(
                "UPDATE context_view_turns SET user_message_sequence = ?, "
                "final_message_sequence = ?, root_input_item_id = ? "
                "WHERE view_id = ? AND turn_id = ?",
                (user_sequence, final_sequence, root_item_id, view_id, turn_id),
            )
            return
        ordinal = strict_non_negative_int(
            connection.execute(
                "SELECT COALESCE(MAX(logical_turn_ordinal), 0) + 1 "
                "FROM context_view_turns WHERE view_id = ?",
                (view_id,),
            ).fetchone()[0],
            field="context_view_turns.logical_turn_ordinal",
        )
        connection.execute(
            "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, "
            "user_message_sequence, final_message_sequence, root_input_item_id, "
            "fork_lineage_json) VALUES (?, ?, ?, ?, ?, ?, '{}')",
            (
                view_id,
                turn_id,
                ordinal,
                user_sequence,
                final_sequence,
                root_item_id,
            ),
        )


__all__ = ["RolloutTerminalProjectionMixin"]
