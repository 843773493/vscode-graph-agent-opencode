"""checkpoint 控制状态版本、view rebuild 与 transition 消费的同库持久边界。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from app.core.sqlite_state import utc_now_text
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.mutation_intents import RebuildContextEpochIntent
from app.services.infrastructure.rollout_context.assembly.manifest import (
    _frozen_source_refs,
)
from app.services.infrastructure.rollout_context.assembly.validation import json_text
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)

_SOURCE_COLUMNS = (
    "session_id, thread_id, source_id, source_kind, name, binding_revision, "
    "tracking_status, latest_visible_committed_revision, latest_revision, "
    "state_revision, created_at, updated_at"
)


class ContextSourceVersionsStorageMixin:
    """只参加调用方已有 SQLite 事务，不自行提交、不追加 source 正文。"""

    def consume_context_epoch_rebuild_intent(
        self,
        intent: RebuildContextEpochIntent,
        connection: sqlite3.Connection,
    ) -> None:
        """核验真实 view/control 后在同一事务恢复版本并登记 pending。"""
        if not connection.in_transaction:
            raise RuntimeError("epoch-rebuild-transaction-required: 必须传入边界的活动 SQLite 事务")
        self._require_v2_runtime(connection)
        meta = connection.execute(
            "SELECT session_id, thread_id, history_view_revision, last_control_sequence "
            "FROM database_meta WHERE singleton_id=1"
        ).fetchone()
        expected = (
            intent.owner.session_id, intent.owner.thread_id,
            intent.view_revision, intent.control_revision,
        )
        if meta != expected:
            raise RuntimeError(f"epoch-rebuild-owner-revision-conflict: expected={expected!r}, actual={meta!r}")
        view = connection.execute(
            "SELECT view_kind, branch_id, control_sequence, created_at FROM context_views WHERE view_id=?",
            (intent.target_view_id,),
        ).fetchone()
        namespace = connection.execute(
            "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns=?",
            (intent.checkpoint_ns,),
        ).fetchone()
        active = None if namespace is None else connection.execute(
            "SELECT head_view_id FROM branches WHERE branch_id=? AND status='active'", namespace,
        ).fetchone()
        checkpoint = connection.execute(
            "SELECT checkpoint_ns FROM checkpoints WHERE checkpoint_id=?", (intent.source_checkpoint_id,),
        ).fetchone()
        header = connection.execute(
            "SELECT session_id, thread_id FROM checkpoint_context_source_snapshots "
            "WHERE checkpoint_id=? AND checkpoint_ns=?",
            (intent.source_checkpoint_id, intent.checkpoint_ns),
        ).fetchone()
        if (
            view is None or view[:3] != (intent.epoch_reason, namespace[0] if namespace else None, intent.control_revision)
            or active != (intent.target_view_id,)
            or checkpoint != (intent.checkpoint_ns,)
            or header != (intent.owner.session_id, intent.owner.thread_id)
        ):
            raise RuntimeError(
                "epoch-rebuild-boundary-conflict: owner/namespace/checkpoint/active view 与 intent 不一致: "
                f"checkpoint_ns={intent.checkpoint_ns!r}, checkpoint={intent.source_checkpoint_id!r}, "
                f"view={intent.target_view_id!r}"
            )
        control = connection.execute(
            "SELECT control_kind, branch_id, view_id, checkpoint_id FROM control_events WHERE control_sequence=?",
            (intent.control_revision,),
        ).fetchone()
        allowed_kinds = {intent.epoch_reason}
        if intent.epoch_reason == "compaction":
            allowed_kinds.add("checkpoint_created")
        if control is None or control[0] not in allowed_kinds or control[1:] != (
            view[1], intent.target_view_id, intent.source_checkpoint_id,
        ):
            raise RuntimeError("epoch-rebuild-control-conflict: intent 未绑定同一事务真实控制事件")
        transition_id = f"epoch-rebuild:{intent.target_view_id}:{intent.control_revision}"
        if connection.execute(
            "SELECT 1 FROM context_epoch_transitions WHERE transition_id=?", (transition_id,),
        ).fetchone() is not None:
            raise RuntimeError(f"mutation-intent-duplicate: epoch rebuild 已消费: {intent.idempotency_key}")
        timestamp = strict_text(view[3], field="context_views.created_at")
        self._record_context_epoch_transition(
            connection, checkpoint_ns=intent.checkpoint_ns,
            source_checkpoint_id=intent.source_checkpoint_id, target_view_id=intent.target_view_id,
            reason=intent.epoch_reason, control_sequence=intent.control_revision, timestamp=timestamp,
        )
        if intent.epoch_reason == "rewind":
            self._restore_checkpoint_context_sources(
                connection, checkpoint_id=intent.source_checkpoint_id,
                checkpoint_ns=intent.checkpoint_ns, session_id=intent.owner.session_id,
                thread_id=intent.owner.thread_id, timestamp=timestamp,
            )

    @staticmethod
    def _capture_checkpoint_context_sources(
        connection: sqlite3.Connection,
        *,
        checkpoint_id: str,
        checkpoint_ns: str,
        session_id: str,
        thread_id: str,
        timestamp: str,
    ) -> None:
        count = connection.execute(
            "SELECT count(*) FROM context_source_control_states "
            "WHERE session_id = ? AND thread_id = ?",
            (session_id, thread_id),
        ).fetchone()[0]
        pending_assembly = connection.execute(
            "SELECT checkpoint.source_assembly_id FROM context_epoch_transitions AS transition "
            "JOIN checkpoint_context_source_snapshots AS checkpoint "
            "ON checkpoint.checkpoint_id = transition.source_checkpoint_id "
            "AND checkpoint.checkpoint_ns = transition.checkpoint_ns "
            "WHERE transition.checkpoint_ns = ? AND transition.status = 'pending'",
            (checkpoint_ns,),
        ).fetchone()
        assembly = pending_assembly if pending_assembly is not None else connection.execute(
            "SELECT assembly_id FROM context_assemblies WHERE session_id = ? AND thread_id = ? "
            "AND status IN ('sealed','terminal') ORDER BY rowid DESC LIMIT 1",
            (session_id, thread_id),
        ).fetchone()
        connection.execute(
            "INSERT INTO checkpoint_context_source_snapshots "
            "(checkpoint_id, checkpoint_ns, session_id, thread_id, source_count, source_assembly_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (checkpoint_id, checkpoint_ns, session_id, thread_id, count,
             assembly[0] if assembly is not None else None, timestamp),
        )
        connection.execute(
            "INSERT INTO checkpoint_context_source_states "
            f"(checkpoint_id, {_SOURCE_COLUMNS}) SELECT ?, {_SOURCE_COLUMNS} "
            "FROM context_source_control_states WHERE session_id = ? AND thread_id = ?",
            (checkpoint_id, session_id, thread_id),
        )

    def _restore_checkpoint_context_sources(
        self,
        connection: sqlite3.Connection,
        *,
        checkpoint_id: str,
        checkpoint_ns: str,
        session_id: str,
        thread_id: str,
        timestamp: str,
    ) -> None:
        header = connection.execute(
            "SELECT session_id, thread_id, source_count, source_assembly_id "
            "FROM checkpoint_context_source_snapshots "
            "WHERE checkpoint_id = ? AND checkpoint_ns = ?",
            (checkpoint_id, checkpoint_ns),
        ).fetchone()
        if header is None or header[:2] != (session_id, thread_id):
            raise RuntimeError(
                "checkpoint-context-state-unavailable: 缺少目标 checkpoint 的精确 owner 控制状态版本"
            )
        states = connection.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM checkpoint_context_source_states "
            "WHERE checkpoint_id = ? AND session_id = ? AND thread_id = ?",
            (checkpoint_id, session_id, thread_id),
        ).fetchall()
        if len(states) != strict_non_negative_int(header[2], field="checkpoint.source_count"):
            raise RuntimeError("checkpoint-context-state-conflict: 目标控制状态版本不完整")
        pending = self._pending_context_epoch_row(connection, checkpoint_ns)
        if pending is None or pending[3] != checkpoint_id:
            raise RuntimeError("checkpoint-context-state-conflict: restore 缺少同一 boundary transition")
        assembly_id = strict_optional_text(header[3], field="checkpoint.source_assembly_id")
        source_assembly = None
        if assembly_id is not None:
            source_assembly = self._read_context_assembly_connection(
                connection, session_id, thread_id,
                assembly_id=assembly_id, checkpoint_ns=checkpoint_ns,
            )[0]
            if source_assembly.seal_binding is None:
                raise RuntimeError("checkpoint-context-state-conflict: 目标 source assembly 缺少 immutable prefix")
        retained = self._retained_context_source_refs(connection, pending, states, source_assembly)
        frozen_sources = {} if source_assembly is None else _frozen_source_refs(source_assembly)[0]
        # 冻结 root backing 后接真实 prefix tail，最后保留的精确 revision 才是
        # untracked visible 基准；observed 与 checkpoint 版本不参与这次裁剪。
        visible = {
            source_id: revision for ref_id, (source_id, revision) in frozen_sources.items()
            if (ref_id, revision) in retained
        }
        restored = [
            (*row[:7], visible.get(row[2]) if row[6] == "untracked" else row[7], *row[8:])
            for row in states
        ]
        # 目标 checkpoint 之后建立的 registration 冻结为不跟踪，保留单调 CAS，
        # 不删除审计 identity，也不把后来观察到的正文带入目标 tracking 状态。
        connection.execute(
            "UPDATE context_source_control_states SET tracking_status = 'untracked', "
            "latest_visible_committed_revision = NULL, latest_revision = NULL, "
            "state_revision = state_revision + 1, updated_at = ? "
            "WHERE session_id = ? AND thread_id = ? AND source_id NOT IN "
            "(SELECT source_id FROM checkpoint_context_source_states WHERE checkpoint_id = ?)",
            (timestamp, session_id, thread_id, checkpoint_id),
        )
        connection.executemany(
            "INSERT INTO context_source_control_states "
            f"({_SOURCE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id, thread_id, source_id) DO UPDATE SET "
            "source_kind = excluded.source_kind, name = excluded.name, "
            "binding_revision = excluded.binding_revision, "
            "tracking_status = excluded.tracking_status, "
            "latest_visible_committed_revision = excluded.latest_visible_committed_revision, "
            "latest_revision = excluded.latest_revision, "
            "state_revision = max(context_source_control_states.state_revision, excluded.state_revision) + 1, "
            "updated_at = ?",
            ((*row, timestamp) for row in restored),
        )

    def _record_context_epoch_transition(
        self,
        connection: sqlite3.Connection,
        *,
        checkpoint_ns: str,
        source_checkpoint_id: str,
        target_view_id: str,
        reason: str,
        control_sequence: int,
        timestamp: str,
    ) -> str:
        if reason not in {"rewind", "compaction"}:
            raise ValueError("pending epoch transition 只能来自 rewind/compaction view")
        control = connection.execute(
            "SELECT payload_json FROM control_events WHERE control_sequence = ?", (control_sequence,),
        ).fetchone()
        if control is None:
            raise RuntimeError("source-cut-frontier-conflict: boundary 缺少真实 control")
        payload = json_text(control[0], field="source-cut.control.payload_json")
        if not isinstance(payload, Mapping):
            raise TypeError("source-cut-frontier-conflict: control payload 必须是 object")
        cut_kind, cut_item = self._context_source_boundary_cut(
            connection, reason=reason, target_view_id=target_view_id, payload=payload,
        )
        transition_id = f"epoch-rebuild:{target_view_id}:{control_sequence}"
        connection.execute(
            "UPDATE context_epoch_transitions SET status = 'superseded' "
            "WHERE checkpoint_ns = ? AND status = 'pending'",
            (checkpoint_ns,),
        )
        connection.execute(
            "INSERT INTO context_epoch_transitions "
            "(transition_id, checkpoint_ns, source_checkpoint_id, target_view_id, "
            "reason, control_sequence, canonical_cut_kind, canonical_cut_item_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (transition_id, checkpoint_ns, source_checkpoint_id, target_view_id,
             reason, control_sequence, cut_kind, cut_item, timestamp),
        )
        return transition_id

    @staticmethod
    def _pending_context_epoch_row(
        connection: sqlite3.Connection,
        checkpoint_ns: str,
    ) -> tuple[object, ...] | None:
        return connection.execute(
            "SELECT transition_id, reason, target_view_id, source_checkpoint_id, control_sequence, "
            "canonical_cut_kind, canonical_cut_item_id "
            "FROM context_epoch_transitions WHERE checkpoint_ns = ? AND status = 'pending'",
            (checkpoint_ns,),
        ).fetchone()

    def read_context_source_rebuild_rows(
        self,
        session_id: str,
        thread_id: str,
        *,
        checkpoint_ns: str,
    ) -> tuple[
        tuple[object, ...], tuple[tuple[object, ...], ...], ContextAssemblySnapshot | None,
        frozenset[tuple[str, str]],
    ] | None:
        thread_id = self._context_source_rollout_owner_or_none(session_id, thread_id)
        if thread_id is None:
            return None
        if not self._context_source_control_index_ready(session_id, thread_id):
            return None
        with self._connect(
            thread_id, checkpoint_ns, session_id=session_id, read_only=True,
        ) as connection:
            self._require_context_source_control_runtime(connection)
            self._validate_schema_state(connection)
            connection.execute("BEGIN")
            states = connection.execute(
                f"SELECT {_SOURCE_COLUMNS} FROM context_source_control_states "
                "WHERE session_id = ? AND thread_id = ? ORDER BY created_at, source_id",
                (session_id, thread_id),
            ).fetchall()
            pending = self._pending_context_epoch_row(connection, checkpoint_ns)
            if pending is None:
                return None
            header = connection.execute(
                "SELECT session_id, thread_id, source_assembly_id "
                "FROM checkpoint_context_source_snapshots WHERE checkpoint_id = ? AND checkpoint_ns = ?",
                (pending[3], checkpoint_ns),
            ).fetchone()
            if header is None or header[:2] != (session_id, thread_id):
                raise RuntimeError("checkpoint-context-state-conflict: rebuild 缺少目标 checkpoint source selection")
            assembly_id = strict_optional_text(header[2], field="checkpoint.source_assembly_id")
            source_assembly = None
            if assembly_id is not None:
                source_assembly = self._read_context_assembly_connection(
                    connection, session_id, thread_id,
                    assembly_id=assembly_id, checkpoint_ns=checkpoint_ns,
                )[0]
                if source_assembly.seal_binding is None:
                    raise RuntimeError("checkpoint-context-state-conflict: 目标 source assembly 缺少 immutable prefix")
            retained_refs = self._retained_context_source_refs(
                connection, pending, states, source_assembly,
            )
            return tuple(pending), tuple(tuple(row) for row in states), source_assembly, retained_refs

    def _retained_context_source_refs(
        self,
        connection: sqlite3.Connection,
        pending: tuple[object, ...],
        states: list[tuple[object, ...]],
        source_assembly: ContextAssemblySnapshot | None,
    ) -> frozenset[tuple[str, str]]:
        """由真实 boundary owner 决定已封存来源的 canonical cut membership。"""
        if source_assembly is None:
            return frozenset()
        untracked = {
            strict_text(row[2], field="context_source_control_states.source_id")
            for row in states if row[6] == "untracked" and row[7] is not None
        }
        contributions = {
            source.contribution_id: source for source in source_assembly.contributions
        }
        selected = {
            entry.ref.ref_id: strict_text(entry.source_revision, field="source-cut.source_revision")
            for entry in source_assembly.selection
            if entry.included and entry.ref.ref_type == "request_only"
            and entry.availability == "available"
            and (source := contributions.get(entry.contribution_id)) is not None
            and source.source_binding is not None
            and source.source_binding.source_ref in untracked
        }
        frontiers = {
            ref_id: (kind, item_id)
            for ref_id, revision, kind, item_id in connection.execute(
                "SELECT ref_id, source_revision, source_cut_position_kind, source_cut_item_id "
                "FROM context_assembly_selections WHERE assembly_id = ? AND included = 1 "
                "AND ref_type = 'request_only'",
                (source_assembly.assembly_id,),
            ).fetchall()
            if ref_id in selected and revision == selected[ref_id]
        }
        if set(frontiers) != set(selected) or any(
            kind not in {"start", "after_canonical"} for kind, _item_id in frontiers.values()
        ):
            raise RuntimeError("source-cut-frontier-conflict: rebuild source selection 缺少精确位置")
        control = connection.execute(
            "SELECT control_kind, view_id, checkpoint_id, payload_json "
            "FROM control_events WHERE control_sequence = ?", (pending[4],),
        ).fetchone()
        if control is None or control[1:3] != (pending[2], pending[3]) or control[0] not in {
            pending[1], "checkpoint_created" if pending[1] == "compaction" else pending[1],
        }:
            raise RuntimeError("source-cut-frontier-conflict: rebuild 未绑定真实 control boundary")
        payload = json_text(control[3], field="source-cut.control.payload_json")
        if not isinstance(payload, Mapping):
            raise TypeError("source-cut-frontier-conflict: control payload 必须是 object")
        cut_kind = strict_text(pending[5], field="source-cut.canonical_cut_kind")
        if cut_kind == "complete":
            if pending[6] is not None:
                raise RuntimeError("source-cut-frontier-conflict: complete cut 不得携带 canonical anchor")
            return frozenset(selected.items())
        if cut_kind not in {"before_canonical", "after_canonical"}:
            raise RuntimeError("source-cut-frontier-conflict: canonical cut kind 非法")
        anchor_item = strict_text(pending[6], field="source-cut.canonical_cut_item_id")
        source_view = strict_text(payload.get("source_view_id"), field="source-cut.source_view_id")
        ordinals = {
            strict_text(item_id, field="source-cut.item_id"): strict_non_negative_int(
                ordinal, field="context_view_items.logical_item_ordinal",
            )
            for item_id, ordinal in connection.execute(
                "SELECT item_id, logical_item_ordinal FROM context_view_items "
                "WHERE view_id = ? AND visible = 1 AND source_kind = 'canonical'",
                (source_view,),
            ).fetchall()
        }
        if anchor_item not in ordinals:
            raise RuntimeError("source-cut-frontier-conflict: anchor 不属于真实 canonical source view")
        retained = set()
        for ref_id, (kind, item_id) in frontiers.items():
            if kind == "start":
                retained.add((ref_id, selected[ref_id]))
                continue
            if item_id not in ordinals:
                raise RuntimeError("source-cut-frontier-conflict: 来源前沿不属于 canonical source view")
            # after_canonical 严格位于该 item 之后：inclusive 只保留 anchor
            # 本身，before 则连 anchor 也排除；两者均不能带回其后注入来源。
            if ordinals[item_id] < ordinals[anchor_item]:
                retained.add((ref_id, selected[ref_id]))
        return frozenset(retained)

    def _context_source_boundary_cut(
        self,
        connection: sqlite3.Connection,
        *,
        reason: str,
        target_view_id: str,
        payload: Mapping[str, object],
    ) -> tuple[str, str | None]:
        """在真实 boundary 事务内冻结 cut，避免后来 admission 改写 target membership。"""
        if reason == "compaction":
            return "complete", None
        source_view = strict_text(payload.get("source_view_id"), field="source-cut.source_view_id")
        anchor = strict_optional_text(payload.get("source_anchor"), field="source-cut.source_anchor")
        source_turn = strict_optional_text(payload.get("source_turn_id"), field="source-cut.source_turn_id")
        mode = strict_text(payload.get("anchor_mode"), field="source-cut.anchor_mode")
        if mode not in {"before", "inclusive"} or (anchor is not None and source_turn is not None):
            raise RuntimeError("source-cut-frontier-conflict: control anchor 声明非法")
        if anchor is None and source_turn is None:
            source_sequences = self._view_message_sequences_from_connection(connection, source_view)
            if payload.get("cutoff_message_sequence") != (source_sequences[-1] if source_sequences else None):
                raise RuntimeError("source-cut-frontier-conflict: 部分 canonical cut 缺少显式 anchor")
            return "complete", None
        if source_turn is not None and mode == "before":
            root = connection.execute(
                "SELECT root_input_item_id FROM turn_records "
                "WHERE turn_id = ?",
                (source_turn,),
            ).fetchone()
            if root is None:
                raise RuntimeError("source-cut-frontier-conflict: before Turn 缺少真实 root input")
            anchor_item = strict_text(root[0], field="source-cut.anchor_item_id")
        else:
            if anchor is None:
                message = connection.execute(
                    "SELECT message_id FROM messages WHERE message_sequence = ?",
                    (payload.get("cutoff_message_sequence"),),
                ).fetchone()
                if message is None:
                    raise RuntimeError("source-cut-frontier-conflict: inclusive Turn 缺少真实 message anchor")
                anchor = strict_text(message[0], field="source-cut.anchor_message_id")
            matches = connection.execute(
                "SELECT item_id FROM item_catalog WHERE item_id = ? "
                "OR json_extract(projection_manifest_json, '$.projection_identity.projection_message_id') = ?",
                (f"item-{anchor}", anchor),
            ).fetchall()
            if len(matches) != 1:
                raise RuntimeError("source-cut-frontier-conflict: message anchor 缺少唯一 canonical item")
            anchor_item = strict_text(matches[0][0], field="source-cut.anchor_item_id")
        ordinals = {
            strict_text(item_id, field="source-cut.item_id"): strict_non_negative_int(
                ordinal, field="context_view_items.logical_item_ordinal",
            )
            for item_id, ordinal in connection.execute(
                "SELECT item_id, logical_item_ordinal FROM context_view_items "
                "WHERE view_id = ? AND visible = 1 AND source_kind = 'canonical'",
                (source_view,),
            ).fetchall()
        }
        if anchor_item not in ordinals:
            raise RuntimeError("source-cut-frontier-conflict: anchor 不属于真实 canonical source view")
        target_items = {
            strict_text(row[0], field="source-cut.target_item_id")
            for row in connection.execute(
                "SELECT item_id FROM context_view_items "
                "WHERE view_id = ? AND visible = 1 AND source_kind = 'canonical'",
                (target_view_id,),
            ).fetchall()
        }
        if not target_items.issubset(ordinals):
            raise RuntimeError("source-cut-frontier-conflict: target canonical membership 脱离 source view")
        if source_turn is not None and mode == "inclusive":
            if not target_items or anchor_item not in target_items:
                raise RuntimeError("source-cut-frontier-conflict: inclusive Turn 缺少真实 canonical 终点")
            anchor_item = max(target_items, key=ordinals.__getitem__)
        elif (anchor_item in target_items) != (mode == "inclusive"):
            raise RuntimeError("source-cut-frontier-conflict: target canonical membership 与 anchor mode 不一致")
        return ("before_canonical" if mode == "before" else "after_canonical"), anchor_item

    def _consume_context_epoch_transition(
        self,
        connection: sqlite3.Connection,
        snapshot: ContextAssemblySnapshot,
        checkpoint_ns: str,
    ) -> None:
        binding = snapshot.seal_binding
        consumed = binding.prefix.consumed_transition if binding is not None else None
        pending = self._pending_context_epoch_row(connection, checkpoint_ns)
        if pending is None:
            if consumed is not None:
                raise RuntimeError("context-epoch-transition-conflict: seal 携带不存在的 pending transition")
            return
        if consumed is None or not consumed.consumed or (
            consumed.transition_id != pending[0] or consumed.reason.value != pending[1]
        ):
            raise RuntimeError("context-epoch-transition-conflict: 首个 rebuild seal 必须消费精确 pending transition")
        active_branch, _epoch = self._namespace_state(connection, checkpoint_ns)
        row = connection.execute(
            "SELECT branch_id FROM context_views WHERE view_id = ?", (pending[2],),
        ).fetchone()
        if row is None or row[0] != active_branch:
            raise RuntimeError("context-epoch-transition-conflict: pending rebuild 已不属于 active view branch")
        cursor = connection.execute(
            "UPDATE context_epoch_transitions SET status = 'consumed', "
            "consumed_assembly_id = ?, consumed_at = ? "
            "WHERE transition_id = ? AND status = 'pending'",
            (snapshot.assembly_id, utc_now_text(),
             strict_text(pending[0], field="transition_id")),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("context-epoch-transition-conflict: pending transition 消费未命中")


__all__ = ["ContextSourceVersionsStorageMixin"]
