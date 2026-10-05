"""canonical catalog 的 active context view item membership。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Literal

from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


class RolloutViewMembershipMixin:
    """集中维护 view-local item ordinal，避免各写入入口各自分配顺序。"""

    @staticmethod
    def _active_context_view_id(
        connection: sqlite3.Connection,
        checkpoint_ns: str,
        branch_id: str | None = None,
    ) -> str | None:
        checkpoint_ns = strict_text(
            checkpoint_ns,
            field="checkpoint_namespace_state.checkpoint_ns",
            allow_empty=True,
        )
        branch_id = (
            strict_text(branch_id, field="branches.branch_id")
            if branch_id is not None
            else None
        )
        if branch_id is None:
            branch_row = connection.execute(
                "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
                (checkpoint_ns,),
            ).fetchone()
            if branch_row is None:
                return None
            branch_id = strict_text(
                branch_row[0],
                field="checkpoint_namespace_state.active_branch_id",
            )
        view_row = connection.execute(
            "SELECT head_view_id FROM branches WHERE branch_id = ? AND status = 'active'",
            (branch_id,),
        ).fetchone()
        if view_row is None or view_row[0] is None:
            return None
        return strict_text(view_row[0], field="branches.head_view_id")

    def _append_context_view_items(
        self,
        connection: sqlite3.Connection,
        *,
        checkpoint_ns: str,
        item_ids: Iterable[str],
        branch_id: str | None = None,
        require_view: bool = False,
        source_kind: Literal["canonical", "display_only"] = "canonical",
    ) -> bool:
        """把 canonical item 幂等加入 active view，并集中分配 item ordinal。

        ``context_view_items`` 是 view 的派生 membership；canonical item 的
        物理 ``item_sequence`` 只用于本次新成员的稳定候选排序，不能覆盖已
        存在的 view-local ordinal。缺失 catalog 或 ordinal 冲突直接报错，不能
        用 ``INSERT OR IGNORE`` 把索引损坏伪装成成功。
        """
        values = tuple(
            dict.fromkeys(
                strict_text(item_id, field="context_view_items.item_id")
                for item_id in item_ids
            )
        )
        if source_kind not in {"canonical", "display_only"}:
            raise ValueError(f"context view item source_kind 非法: {source_kind!r}")
        if not values:
            return False
        view_id = self._active_context_view_id(connection, checkpoint_ns, branch_id)
        if view_id is None:
            if require_view:
                raise RuntimeError(
                    f"rollout namespace 缺少 active context view: {checkpoint_ns!r}"
                )
            return False
        placeholders = ",".join("?" for _ in values)
        catalog_rows = connection.execute(
            f"SELECT item_id, semantic_kind, turn_id, turn_scope, execution_id "
            f"FROM item_catalog WHERE item_id IN ({placeholders}) "
            "ORDER BY item_sequence, item_id",
            values,
        ).fetchall()
        found = {
            strict_text(row[0], field="item_catalog.item_id") for row in catalog_rows
        }
        missing = tuple(item_id for item_id in values if item_id not in found)
        if missing:
            raise RuntimeError(
                "active view membership 缺少 canonical catalog item: "
                + ",".join(missing)
            )
        next_ordinal = strict_non_negative_int(
            connection.execute(
                "SELECT COALESCE(MAX(logical_item_ordinal), -1) + 1 "
                "FROM context_view_items WHERE view_id = ?",
                (view_id,),
            ).fetchone()[0],
            field="context_view_items.logical_item_ordinal",
        )
        inserted = False
        for item_id, semantic_kind, turn_id, turn_scope, execution_id in catalog_rows:
            item_id = strict_text(item_id, field="item_catalog.item_id")
            semantic_kind = strict_text(
                semantic_kind, field=f"item_catalog.semantic_kind: {item_id}"
            )
            turn_id = strict_optional_text(
                turn_id, field=f"item_catalog.turn_id: {item_id}"
            )
            turn_scope = strict_optional_text(
                turn_scope, field=f"item_catalog.turn_scope: {item_id}"
            )
            if turn_scope not in {
                "turn_root",
                "turn_member",
                "ambient",
                "pending_next_turn",
            }:
                raise RuntimeError(f"item_catalog.turn_scope 非法: {item_id}")
            execution_id = strict_optional_text(
                execution_id, field=f"item_catalog.execution_id: {item_id}"
            )
            if source_kind == "canonical":
                if turn_scope not in {"turn_root", "turn_member"}:
                    continue
            elif (
                execution_id is None
                or turn_id is not None
                or (
                    semantic_kind == "runtime_notice"
                    and turn_scope not in {"ambient", "pending_next_turn"}
                )
                or (
                    semantic_kind != "runtime_notice"
                    and turn_scope is not None
                )
            ):
                raise RuntimeError(
                    "display_only membership 必须引用已绑定 execution 的 Turn-less item: "
                    f"item_id={item_id}, semantic_kind={semantic_kind}, "
                    f"turn_scope={turn_scope}"
                )
            existing = connection.execute(
                "SELECT logical_item_ordinal, visible, source_kind FROM context_view_items "
                "WHERE view_id = ? AND item_id = ?",
                (view_id, item_id),
            ).fetchone()
            if existing is not None:
                strict_non_negative_int(
                    existing[0],
                    field=f"context_view_items.logical_item_ordinal: {item_id}",
                )
                if (
                    existing[1] != 1
                    or strict_text(
                        existing[2], field=f"context_view_items.source_kind: {item_id}"
                    )
                    != source_kind
                ):
                    raise RuntimeError(
                        "context view item 已有不同 membership 身份: "
                        f"view_id={view_id}, item_id={item_id}, source_kind={source_kind}"
                    )
                continue
            try:
                cursor = connection.execute(
                    "INSERT INTO context_view_items(view_id, item_id, logical_item_ordinal, visible, source_kind) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (view_id, item_id, next_ordinal, source_kind),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("active view item membership 插入未产生一条记录")
            except sqlite3.IntegrityError as error:
                raise RuntimeError(
                    "active view item ordinal 冲突，拒绝静默跳过: "
                    f"view_id={view_id}, item_id={item_id}, ordinal={next_ordinal}"
                ) from error
            inserted = True
            next_ordinal += 1
        return inserted


__all__ = ["RolloutViewMembershipMixin"]
