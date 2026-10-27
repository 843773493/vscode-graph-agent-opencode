"""checkpoint 控制状态版本、view rebuild 与 transition 消费的同库持久边界。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING

from app.core.sqlite_state import utc_now_text
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.prefix_epoch import EpochReason
from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.domain.itemized.mutation_intents import RebuildContextEpochIntent
from app.services.infrastructure.rollout_context.assembly.manifest import (
    _frozen_source_refs,
)
from app.services.infrastructure.rollout_context.assembly.validation import json_text
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    _normalize_skill_name,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.tool_definitions import (
    is_tool_definition_source,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.store import RolloutContextStore

_SOURCE_COLUMNS = (
    "session_id, thread_id, source_id, source_kind, name, binding_revision, "
    "tracking_status, latest_visible_committed_revision, latest_revision, "
    "state_revision, created_at, updated_at, selection_sequence, latest_visible_selection_sequence"
)


class ContextSourceVersionsStorageMixin:
    """只参加调用方已有 SQLite 事务，不自行提交、不追加 source 正文。"""

    _context: RolloutContextStore

    def _sealed_skill_selection(
        self, connection: sqlite3.Connection, owner: ContextSourceOwnerKey, name: str,
        source_ids: frozenset[str],
    ) -> str | None:
        """旧库选择次序为零时，从当前 view 的封存来源取得唯一基准。"""
        pending = self._pending_context_epoch_row(connection, "")
        row = None if pending is None else connection.execute(
            "SELECT source_assembly_id FROM checkpoint_context_source_snapshots WHERE checkpoint_id=?",
            (pending[3],),
        ).fetchone()
        assembly = (
            self._context.assemblies._read_context_assembly_connection(
                connection, owner.session_id, owner.thread_id, assembly_id=row[0], checkpoint_ns="",
            )[0] if row is not None and row[0] is not None else
            self._context.assemblies._latest_context_assembly_connection(
                connection, owner.session_id, owner.thread_id, checkpoint_ns="",
            ) if pending is None else None
        )
        if assembly is None or assembly.seal_binding is None:
            return None
        binding = assembly.seal_binding
        selected = None
        for source in (() if binding.root is None else binding.root.sources):
            if source.source_kind == "skill" and _normalize_skill_name(source.name) == _normalize_skill_name(name):
                selected = source.source_identity
        for frame in binding.prefix.state.frames:
            if frame.role == "user" and frame.source_protocol is not None:
                for source in parse_source_blocks(frame.content, expected_version=frame.source_protocol):
                    if source.source_kind == "skill" and _normalize_skill_name(source.name) == _normalize_skill_name(name):
                        selected = source.source_identity
            elif frame.role == "user":
                # 统一来源协议之前的封存帧已有 typed 来源身份，不能重读当前文件补名。
                for ref in frame.item_refs:
                    if ref.source_identity in source_ids:
                        selected = ref.source_identity
        return selected

    def consume_context_epoch_rebuild_intent(
        self,
        intent: RebuildContextEpochIntent,
        connection: sqlite3.Connection,
    ) -> None:
        """核验真实 view/control 后在同一事务恢复版本并登记 pending。"""
        if not connection.in_transaction:
            raise RuntimeError("epoch-rebuild-transaction-required: 必须传入边界的活动 SQLite 事务")
        self._storage._require_v2_runtime(connection)
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
        if header is not None:
            require_historical_scope(
                connection, *header,
                current_owner=(intent.owner.session_id, intent.owner.thread_id),
            )
        if (
            view is None or view[:3] != (intent.epoch_reason, namespace[0] if namespace else None, intent.control_revision)
            or active != (intent.target_view_id,)
            or checkpoint != (intent.checkpoint_ns,)
            or header is None
        ):
            raise RuntimeError(
                "epoch-rebuild-boundary-conflict: owner/namespace/checkpoint/active view 与 intent 不一致: "
                f"checkpoint_ns={intent.checkpoint_ns!r}, checkpoint={intent.source_checkpoint_id!r}, "
                f"view={intent.target_view_id!r}"
            )
        control = connection.execute(
            "SELECT control_kind, branch_id, view_id, checkpoint_id, payload_json "
            "FROM control_events WHERE control_sequence=?",
            (intent.control_revision,),
        ).fetchone()
        allowed_kinds = {intent.epoch_reason}
        if intent.epoch_reason == "compaction":
            allowed_kinds.add("checkpoint_created")
        if control is None or control[0] not in allowed_kinds or control[1:4] != (
            view[1], intent.target_view_id, intent.source_checkpoint_id,
        ):
            raise RuntimeError("epoch-rebuild-control-conflict: intent 未绑定同一事务真实控制事件")
        payload = json_text(control[4], field="epoch-rebuild.control.payload_json")
        if not isinstance(payload, Mapping):
            raise TypeError("epoch-rebuild-control-conflict: control payload 必须是 object")
        state_checkpoint_id = (
            intent.source_checkpoint_id if control[0] == "checkpoint_created"
            else payload.get("state_header_checkpoint_id")
        )
        if state_checkpoint_id != intent.state_header_checkpoint_id:
            raise RuntimeError("epoch-rebuild-control-conflict: intent 与冻结控制态 header 不一致")
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
                state_header_checkpoint_id=intent.state_header_checkpoint_id,
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
        source_view_id: str | None = None,
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
            "JOIN context_views view ON view.view_id=transition.target_view_id "
            "JOIN checkpoint_namespace_state namespace ON namespace.checkpoint_ns=transition.checkpoint_ns "
            "AND namespace.active_branch_id=view.branch_id "
            "WHERE transition.checkpoint_ns = ? AND transition.status = 'pending' "
            "AND transition.reason IN ('rewind','compaction')",
            (checkpoint_ns,),
        ).fetchone()
        if source_view_id is not None:
            assembly = connection.execute(
                "SELECT call.assembly_id FROM context_view_items member "
                "JOIN item_catalog item ON item.item_id=member.item_id "
                "JOIN model_calls call ON call.model_call_id=json_extract(item.producer_ref_json,'$.model_call_id') "
                "WHERE member.view_id=? AND member.visible=1 AND call.assembly_id IS NOT NULL "
                "ORDER BY member.logical_item_ordinal DESC LIMIT 1", (source_view_id,),
            ).fetchone()
        else:
            assembly = pending_assembly if pending_assembly is not None else connection.execute(
                "SELECT assembly_id FROM context_assemblies WHERE session_id = ? AND thread_id = ? "
                "AND status IN ('sealed','terminal') "
                "AND json_extract(snapshot_json,'$.selection_policy') <> 'summary_candidate' "
                "ORDER BY rowid DESC LIMIT 1",
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
        state_header_checkpoint_id: str,
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
        if header is None:
            raise RuntimeError(
                "checkpoint-context-state-unavailable: 缺少目标 checkpoint 的精确 owner 控制状态版本"
            )
        require_historical_scope(connection, *header[:2], current_owner=(session_id, thread_id))
        state_header = connection.execute(
            "SELECT snapshot.session_id, snapshot.thread_id, snapshot.source_count "
            "FROM checkpoint_context_source_snapshots snapshot JOIN checkpoints checkpoint "
            "ON checkpoint.checkpoint_id=snapshot.checkpoint_id AND checkpoint.checkpoint_ns=snapshot.checkpoint_ns "
            "WHERE snapshot.checkpoint_id=? AND snapshot.checkpoint_ns=? AND checkpoint.status='active'",
            (state_header_checkpoint_id, checkpoint_ns),
        ).fetchone()
        if state_header is None:
            raise RuntimeError("checkpoint-context-state-unavailable: 缺少目标时间边界的追踪控制态")
        require_historical_scope(connection, *state_header[:2], current_owner=(session_id, thread_id))
        states = connection.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM checkpoint_context_source_states "
            "WHERE checkpoint_id = ? AND session_id = ? AND thread_id = ?",
            (state_header_checkpoint_id, *state_header[:2]),
        ).fetchall()
        if len(states) != strict_non_negative_int(state_header[2], field="checkpoint.source_count"):
            raise RuntimeError("checkpoint-context-state-conflict: 目标控制状态版本不完整")
        pending = self._pending_context_epoch_row(connection, checkpoint_ns)
        if pending is None or pending[3] != checkpoint_id:
            raise RuntimeError("checkpoint-context-state-conflict: restore 缺少同一 boundary transition")
        assembly_id = strict_optional_text(header[3], field="checkpoint.source_assembly_id")
        source_assembly = None
        if assembly_id is not None:
            source_assembly = self._context.assemblies._read_context_assembly_connection(
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
            (session_id, thread_id, *row[2:7],
             visible.get(row[2]) if row[6] == "untracked" else row[7], *row[8:12],
             row[12] if row[6] == "tracked" or row[2] in visible else 0,
             row[13] if row[6] == "tracked" or row[2] in visible else 0)
            for row in states
        ]
        # AGENTS 是自动装配的工作区来源，首次 prepare 才登记；回到输入前仍保留
        # 其跟踪声明，但清空后来的观察与 applied。显式激活来源仍冻结为不跟踪，
        # 不删除审计 identity、不带入后来正文，也不由 register 偷偷恢复 tracking。
        connection.execute(
            "UPDATE context_source_control_states SET tracking_status = "
            "CASE WHEN source_kind='workspace_agents' THEN tracking_status ELSE 'untracked' END, "
            "latest_visible_committed_revision = NULL, latest_revision = NULL, "
            "selection_sequence = 0, latest_visible_selection_sequence = 0, "
            "state_revision = state_revision + 1, updated_at = ? "
            "WHERE session_id = ? AND thread_id = ? AND source_id NOT IN "
            "(SELECT source_id FROM checkpoint_context_source_states WHERE checkpoint_id = ?)",
            (timestamp, session_id, thread_id, state_header_checkpoint_id),
        )
        connection.executemany(
            "INSERT INTO context_source_control_states "
            f"({_SOURCE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id, thread_id, source_id) DO UPDATE SET "
            "source_kind = excluded.source_kind, name = excluded.name, "
            "binding_revision = excluded.binding_revision, "
            "tracking_status = excluded.tracking_status, "
            "latest_visible_committed_revision = excluded.latest_visible_committed_revision, "
            "latest_revision = excluded.latest_revision, "
            "selection_sequence = excluded.selection_sequence, "
            "latest_visible_selection_sequence = excluded.latest_visible_selection_sequence, "
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
            "WHERE checkpoint_ns = ? AND status = 'pending' AND reason IN ('rewind','compaction') AND target_view_id IN "
            "(SELECT view.view_id FROM context_views view JOIN checkpoint_namespace_state namespace "
            "ON namespace.active_branch_id=view.branch_id WHERE namespace.checkpoint_ns=?)",
            (checkpoint_ns, checkpoint_ns),
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
        rows = connection.execute(
            "SELECT transition.transition_id,transition.reason,transition.target_view_id,"
            "transition.source_checkpoint_id,transition.control_sequence,"
            "transition.canonical_cut_kind,transition.canonical_cut_item_id "
            "FROM context_epoch_transitions transition JOIN context_views view "
            "ON view.view_id=transition.target_view_id JOIN checkpoint_namespace_state namespace "
            "ON namespace.active_branch_id=view.branch_id AND namespace.checkpoint_ns=transition.checkpoint_ns "
            "WHERE transition.checkpoint_ns=? AND transition.status='pending' "
            "AND transition.reason IN ('rewind','compaction') LIMIT 2",
            (checkpoint_ns,),
        ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("当前 branch 存在多个 pending context epoch")
        return rows[0] if rows else None

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
        try:
            with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
                self._require_context_source_control_runtime(connection)
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
                if header is None:
                    raise RuntimeError("checkpoint-context-state-conflict: rebuild 缺少目标 checkpoint source selection")
                require_historical_scope(connection, *header[:2], current_owner=(session_id, thread_id))
                assembly_id = strict_optional_text(header[2], field="checkpoint.source_assembly_id")
                source_assembly = None
                if assembly_id is not None:
                    source_assembly = self._context.assemblies._read_context_assembly_connection(
                        connection, session_id, thread_id,
                        assembly_id=assembly_id, checkpoint_ns=checkpoint_ns,
                    )[0]
                    if source_assembly.seal_binding is None:
                        raise RuntimeError("checkpoint-context-state-conflict: 目标 source assembly 缺少 immutable prefix")
                retained_refs = self._retained_context_source_refs(
                    connection, pending, states, source_assembly,
                )
                return tuple(pending), tuple(tuple(row) for row in states), source_assembly, retained_refs
        except RolloutNotMaterializedError:
            return None

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
            and (source.source_binding.source_ref in untracked
                 or is_tool_definition_source(source.source_binding.source_ref))
        }
        frontiers = {
            ref_id: (kind, item_id)
            for ref_id, revision, kind, item_id in connection.execute(
                "SELECT ref_id, source_revision, source_cut_position_kind, source_cut_item_id "
                "FROM context_assembly_selections WHERE assembly_id = ? AND included = 1 "
                "AND ref_type = 'request_only' AND ref_thread_id = ?",
                (source_assembly.assembly_id, source_assembly.thread_id),
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
        # cut 锚点属于冻结历史前缀；压缩后未被 Provider 选择的旧 item
        # 仍可成为合法锚点。来源是否保留由其真实注入前沿决定。
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
        # 这里比较历史 membership，不能用 context_selected 拒绝旧历史锚点。
        source_view = strict_text(payload.get("source_view_id"), field="source-cut.source_view_id")
        anchor = strict_optional_text(payload.get("source_anchor"), field="source-cut.source_anchor")
        source_turn = strict_optional_text(payload.get("source_turn_id"), field="source-cut.source_turn_id")
        mode = strict_text(payload.get("anchor_mode"), field="source-cut.anchor_mode")
        if mode not in {"before", "inclusive"} or (anchor is not None and source_turn is not None):
            raise RuntimeError("source-cut-frontier-conflict: control anchor 声明非法")
        if anchor is None and source_turn is None:
            source_sequences = self._storage._view_message_sequences_from_connection(connection, source_view)
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
        *,
        previous: ContextAssemblySnapshot | None,
    ) -> None:
        binding = snapshot.seal_binding
        if binding is None:
            return
        admission = self._context.executions.execution_admission(
            snapshot.session_id, thread_id=snapshot.thread_id, execution_id=snapshot.execution_id,
            checkpoint_ns=checkpoint_ns, writer_connection=connection,
        )
        notice_ref, _selection = self._context.executions.runtime_notice_acceptance(admission, connection=connection)
        if binding.notice_acceptance != notice_ref:
            raise RuntimeError("runtime-notice-acceptance-conflict: seal 未绑定精确冻结 acceptance")
        outcome = connection.execute(
            "SELECT outcome FROM executions WHERE execution_id=?", (snapshot.execution_id,),
        ).fetchone()[0]
        if outcome in {"cancelled", "failed"}:
            raise RuntimeError("runtime-notice-owner-terminal: cancelled/failed execution 不得恢复 seal")
        confirmation = None if notice_ref is None else connection.execute(
            "SELECT transition_id,notice_selection_hash FROM context_epoch_transitions "
            "WHERE notice_consumption_acceptance_id=?", (notice_ref.acceptance_commit_id,),
        ).fetchone()
        if confirmation is not None and confirmation[1] != notice_ref.selection_hash:
            raise RuntimeError("runtime-notice-acceptance-conflict: 已消费 selection hash 不一致")
        frozen_ids = self._context.executions.runtime_notice_excluded_item_ids(admission, connection=connection)
        if binding.previous_assembly_id != (None if previous is None else previous.assembly_id):
            raise RuntimeError("context-epoch-transition-conflict: seal predecessor 与同事务最新 assembly 不一致")
        prior_ids = set() if previous is None or previous.seal_binding is None else {
            frame.item_refs[0].ref_id for frame in previous.seal_binding.prefix.state.frames
            if frame.item_refs[0].ref_type == "canonical_item"
        }
        current_ids = {frame.item_refs[0].ref_id for frame in binding.prefix.state.frames
                       if frame.item_refs[0].ref_type == "canonical_item"}
        removed = (prior_ids & frozen_ids) - current_ids
        confirm_notice = notice_ref is not None and bool(removed) and confirmation is None
        consumed = binding.prefix.consumed_transition if binding is not None else None
        source_consumed = consumed if consumed is not None and consumed.reason in {
            EpochReason.REWIND, EpochReason.COMPACTION,
        } else None
        pending = self._pending_context_epoch_row(connection, checkpoint_ns)
        if pending is None:
            if source_consumed is not None:
                raise RuntimeError("context-epoch-transition-conflict: seal 携带不存在的 pending transition")
        elif source_consumed is None or not source_consumed.consumed or (
            source_consumed.transition_id != pending[0] or source_consumed.reason.value != pending[1]
        ):
            raise RuntimeError("context-epoch-transition-conflict: 首个 rebuild seal 必须消费精确 pending transition")
        active_branch, _epoch = self._storage._namespace_state(connection, checkpoint_ns)
        if pending is not None:
            row = connection.execute("SELECT branch_id FROM context_views WHERE view_id=?", (pending[2],)).fetchone()
            if row is None or row[0] != active_branch:
                raise RuntimeError("context-epoch-transition-conflict: pending rebuild 已不属于 active view branch")
            cursor = connection.execute(
                "UPDATE context_epoch_transitions SET status='consumed',consumed_assembly_id=?,consumed_at=?,"
                "notice_consumption_acceptance_id=?,notice_selection_hash=? WHERE transition_id=? AND status='pending'",
                (snapshot.assembly_id, utc_now_text(),
                 notice_ref.acceptance_commit_id if confirm_notice else None,
                 notice_ref.selection_hash if confirm_notice else None, pending[0]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("context-epoch-transition-conflict: pending transition 消费未命中")
        elif confirm_notice:
            view = connection.execute("SELECT head_view_id FROM branches WHERE branch_id=?", (active_branch,)).fetchone()
            connection.execute(
                "INSERT INTO context_epoch_transitions (transition_id,checkpoint_ns,target_view_id,reason,status,"
                "consumed_assembly_id,created_at,consumed_at,notice_consumption_acceptance_id,notice_selection_hash) "
                "VALUES (?,?,?,'runtime_notice_consumed','consumed',?,?,?,?,?)",
                (f"epoch-notice:{notice_ref.acceptance_commit_id}", checkpoint_ns, view[0], snapshot.assembly_id,
                 utc_now_text(), utc_now_text(), notice_ref.acceptance_commit_id, notice_ref.selection_hash),
            )
        if consumed is not None and consumed.reason == EpochReason.RUNTIME_NOTICE_CONSUMED and (
            not confirm_notice or pending is not None or consumed.notice_acceptance != notice_ref
            or consumed.transition_id != f"epoch-notice:{notice_ref.acceptance_commit_id}"
        ):
            raise RuntimeError("runtime-notice-epoch-conflict: notice epoch 未绑定首次消费的冻结 owner")


__all__ = ["ContextSourceVersionsStorageMixin"]
