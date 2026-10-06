"""sealed assembly manifest 的纯边界校验。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.serialization import ordered_selection
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    optional_detail_ref_key,
)
from app.services.infrastructure.rollout_context.assembly.detail_registry import (
    validate_assembly_details,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    read_registration,
)
from app.services.infrastructure.rollout_context.assembly.validation import (
    exact_row,
    json_text,
    optional_text,
    required_text,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)


def validate_sealed_selection(entries: Iterable[object]) -> tuple[object, ...]:
    """确认 selection 由 Saver 预先编号，禁止 assembly 层重新排序。"""
    return ordered_selection(entries)

def _persisted_manifest_rows(
    rows: Iterable[tuple[object, ...]],
) -> tuple[tuple[object, ...], ...]:
    """按 snapshot_json 的 JCS 边界恢复 SQL 字段，不触碰 omitted source 正文。"""
    # domain 可持有 str Enum，持久 JSON/SQLite 只持有字符串；转换实际比较
    # 的 manifest 字段，不放宽 exact_row 类型检查，也不序列化工具定义。
    return tuple(tuple(row) for row in json.loads(_json(list(rows))))


def _frozen_source_refs(
    snapshot: ContextAssemblySnapshot,
) -> tuple[dict[str, tuple[str, str]], frozenset[str]]:
    """从真实 prefix/root selection 取得精确来源 ref/revision，忽略 wire 位置。"""
    binding = snapshot.seal_binding
    if binding is None:
        return {}, frozenset()
    contributions = {source.contribution_id: source for source in snapshot.contributions}
    selected = {
        entry.ref.ref_id: (source.source_binding.source_ref, source.source_revision)
        for entry in snapshot.selection
        if entry.included and entry.ref.ref_type == "request_only"
        and entry.availability == "available"
        and (source := contributions.get(entry.contribution_id)) is not None
        and source.source_binding is not None
    }
    sources: dict[str, tuple[str, str]] = {}
    roots = set()
    for provenance in (() if binding.root is None else binding.root.sources):
        matches = [
            ref_id for ref_id, identity in selected.items()
            if identity == (provenance.source_identity, provenance.revision)
            and contributions[next(
                entry.contribution_id for entry in snapshot.selection
                if entry.ref.ref_id == ref_id
            )].root_placement == "root_eligible"
        ]
        if len(matches) != 1:
            raise RuntimeError("source-cut-frontier-conflict: root 缺少唯一冻结 source selection")
        roots.add(matches[0])
        sources[matches[0]] = selected[matches[0]]
    for frame in binding.prefix.state.frames:
        ref = frame.item_ref
        if ref.ref_type != "source_item" or frame.role == "system":
            continue
        identity = (ref.source_identity, ref.source_revision)
        if frame.role != "user" or selected.get(ref.ref_id) != identity or ref.ref_id in sources:
            raise RuntimeError("source-cut-frontier-conflict: source frame 与冻结 selection 不一致")
        sources[ref.ref_id] = selected[ref.ref_id]
    if set(sources) != set(selected):
        raise RuntimeError("source-cut-frontier-conflict: 存在没有真实注入的 source selection")
    return sources, frozenset(roots)


class ContextAssemblyManifestMixin:
    """只验证 sealed assembly 的派生 SQLite manifest。"""

    def _context_source_cut_frontiers(
        self,
        connection: sqlite3.Connection,
        snapshot: ContextAssemblySnapshot,
        *,
        checkpoint_ns: str,
    ) -> dict[str, tuple[str, str | None]]:
        """seal 冻结 source 与 canonical 输入边界；恢复只核验已写的同行字段。

        root 是 wire 排版，不代表来源先于用户激活。新来源绑定本次冻结的
        canonical 输入前沿；已有精确 ref/revision 和提升 root 的精确 revision
        继承已封存 selection 的语义位置，绝不按 source ordinal 重定位。
        """
        sources, roots = _frozen_source_refs(snapshot)
        if not sources:
            return {}
        canonical_ids = {
            entry.ref.ref_id for entry in snapshot.selection
            if entry.included and entry.ref.ref_type == "canonical_item"
        }
        view_rows = connection.execute(
            "SELECT item_id, source_kind FROM context_view_items "
            "WHERE view_id = ? AND visible = 1 ORDER BY logical_item_ordinal",
            (snapshot.active_view_id,),
        ).fetchall()
        visible = {required_text(row[0], field="source-cut.item_id") for row in view_rows}
        for item_id in canonical_ids - visible:
            pending = connection.execute(
                "SELECT semantic_kind, turn_scope, turn_id FROM item_catalog WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            # pending notice 是显式下一次请求输入，未消费时尚无 history
            # membership；它不能替代来源位置的 canonical Turn 输入锚点。
            if pending != ("runtime_notice", "pending_next_turn", None):
                raise RuntimeError("source-cut-frontier-conflict: 冻结 canonical selection 缺少真实 view membership")
        canonical = [
            required_text(item_id, field="source-cut.item_id")
            for item_id, source_kind in view_rows
            if item_id in canonical_ids and source_kind == "canonical"
        ]
        if snapshot.turn_id is not None:
            admission_root = connection.execute(
                "SELECT root_input_item_id FROM turn_records "
                "WHERE session_id = ? AND thread_id = ? AND turn_id = ?",
                (snapshot.session_id, snapshot.thread_id, snapshot.turn_id),
            ).fetchone()
            if admission_root is None or admission_root[0] not in canonical:
                raise RuntimeError("source-cut-frontier-conflict: 来源 freeze 缺少真实 admission root input")
        new_frontier = ("after_canonical", canonical[-1]) if canonical else ("start", None)
        binding = snapshot.seal_binding
        predecessor_id = binding.previous_assembly_id
        transition = binding.prefix.consumed_transition
        if transition is not None:
            inherited = connection.execute(
                "SELECT header.source_assembly_id FROM context_epoch_transitions AS transition "
                "JOIN checkpoint_context_source_snapshots AS header "
                "ON header.checkpoint_id = transition.source_checkpoint_id "
                "AND header.checkpoint_ns = transition.checkpoint_ns "
                "WHERE transition.transition_id = ? AND transition.checkpoint_ns = ? "
                "AND header.session_id = ? AND header.thread_id = ?",
                (transition.transition_id, checkpoint_ns, snapshot.session_id, snapshot.thread_id),
            ).fetchone()
            if inherited is None:
                raise RuntimeError("source-cut-frontier-conflict: rebuild 缺少精确 checkpoint source selection")
            predecessor_id = optional_text(inherited[0], field="source-cut.source_assembly_id")
        previous_sources: dict[str, tuple[str, str]] = {}
        previous_frontiers: dict[str, tuple[str, str | None]] = {}
        if predecessor_id is not None:
            row = connection.execute(
                "SELECT snapshot_json FROM context_assemblies WHERE assembly_id = ? "
                "AND session_id = ? AND thread_id = ? AND status IN ('sealed','terminal')",
                (predecessor_id, snapshot.session_id, snapshot.thread_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("source-cut-frontier-conflict: 缺少真实 predecessor assembly")
            value = json_text(row[0], field="source-cut.snapshot_json")
            if not isinstance(value, Mapping):
                raise TypeError("source-cut-frontier-conflict: predecessor snapshot 必须是 object")
            predecessor = ContextAssemblySnapshot.from_dict(value)
            previous_sources, _ = _frozen_source_refs(predecessor)
            rows = connection.execute(
                "SELECT ref_id, source_revision, source_cut_position_kind, source_cut_item_id "
                "FROM context_assembly_selections WHERE assembly_id = ? "
                "AND included = 1 AND ref_type = 'request_only'",
                (predecessor_id,),
            ).fetchall()
            for ref_id, revision, kind, item_id in rows:
                if ref_id not in previous_sources:
                    continue
                if previous_sources[ref_id][1] != revision or kind not in {"start", "after_canonical"}:
                    raise RuntimeError("source-cut-frontier-conflict: predecessor 来源位置缺失或 revision 不一致")
                item_id = optional_text(item_id, field="source-cut.source_cut_item_id")
                if (kind == "start") != (item_id is None):
                    raise RuntimeError("source-cut-frontier-conflict: predecessor 来源位置字段不一致")
                if item_id is not None and connection.execute(
                    "SELECT 1 FROM item_catalog WHERE item_id = ?", (item_id,),
                ).fetchone() is None:
                    raise RuntimeError("source-cut-frontier-conflict: predecessor canonical frontier 不存在")
                previous_frontiers[ref_id] = (kind, item_id)
            if set(previous_frontiers) != set(previous_sources):
                raise RuntimeError("source-cut-frontier-conflict: predecessor 精确来源位置不完整")
        frontiers = {}
        for ref_id, identity in sources.items():
            if previous_sources.get(ref_id) == identity:
                frontiers[ref_id] = previous_frontiers[ref_id]
                continue
            promoted = [
                previous_ref for previous_ref, previous_identity in previous_sources.items()
                if ref_id in roots and previous_identity == identity
            ]
            if len(promoted) > 1:
                raise RuntimeError("source-cut-frontier-conflict: root revision 对应多个冻结来源 ref")
            frontiers[ref_id] = previous_frontiers[promoted[0]] if promoted else new_frontier
        return frontiers

    def _validate_context_assembly_manifest(
        self,
        connection: sqlite3.Connection,
        snapshot: ContextAssemblySnapshot,
        *,
        header_detail_ref: str | None = None,
        checkpoint_ns: str = "",
    ) -> None:
        """校验 snapshot JSON 与 assembly manifest 表的逐字段一致性。

        snapshot_json 是 sealed view 的便携副本，manifest 表是 SQLite 的可检索
        索引；两者不是两个可选择的事实源。恢复时任何缺行、重复、legacy
        alias 分歧或 hash/ordinal 不一致都必须失败。
        """
        # plan registry 是来源的唯一 owner，同时核验 refs/contributions/tools
        # 和已封存 snapshot 的 hash；本层不读取 draft 或重新解释 omitted 正文。
        registration = read_registration(
            connection,
            snapshot.session_id,
            snapshot.thread_id,
            snapshot.plan_id,
        )
        if registration.plan_state != "sealed":
            raise RuntimeError(
                "source-mismatch: context assembly 的 plan 尚未 sealed: "
                f"{snapshot.plan_id}"
            )
        if registration.assembly_id != snapshot.assembly_id:
            raise RuntimeError(
                "source-mismatch: context assembly 与 plan registry binding 不一致: "
                f"{snapshot.plan_id}/{snapshot.assembly_id}"
            )

        ref_rows = connection.execute(
            "SELECT ref_ordinal, ref_type, ref_id, semantic_kind, payload_kind, status, content_hash, source_revision, content_length, redacted_stable_digest, detail_ref, contribution_id, visibility, protection, availability FROM assembly_item_refs WHERE assembly_id = ? ORDER BY ref_ordinal",
            (snapshot.assembly_id,),
        ).fetchall()
        expected_refs = [
            (
                ordinal,
                ref.ref_type,
                ref.ref_id,
                ref.semantic_kind,
                ref.payload_kind,
                ref.status,
                ref.content_hash,
                ref.source_revision,
                ref.content_length,
                ref.redacted_stable_digest,
                optional_detail_ref_key(entry.detail_ref) if entry else None,
                entry.contribution_id if entry else None,
                ref.visibility,
                ref.protection,
                ref.availability,
            )
            for ordinal, ref in enumerate(snapshot.refs)
            for entry in [
                next(
                    (
                        candidate
                        for candidate in snapshot.selection
                        if (candidate.ref.ref_type, candidate.ref.ref_id)
                        == (ref.ref_type, ref.ref_id)
                    ),
                    None,
                )
            ]
        ]
        exact_row(
            tuple(ref_rows),
            _persisted_manifest_rows(expected_refs),
            field=f"context assembly refs manifest: {snapshot.assembly_id}",
        )

        contribution_rows = connection.execute(
            "SELECT contribution_ordinal, contribution_id, source_kind, source_revision, content_hash, content_length, redacted_stable_digest, request_only, contribution_kind, visibility, protection, binding_manifest_json FROM context_assembly_contributions WHERE assembly_id = ? ORDER BY contribution_ordinal",
            (snapshot.assembly_id,),
        ).fetchall()
        expected_contributions = []
        for ordinal, contribution in enumerate(
            sorted(
                snapshot.contributions,
                key=lambda item: int(item.contribution_ordinal or 0),
            )
        ):
            expected_ordinal = (
                contribution.contribution_ordinal
                if contribution.contribution_ordinal is not None
                else ordinal
            )
            expected_contributions.append(
                (
                    expected_ordinal,
                    contribution.contribution_id,
                    contribution.source_kind,
                    contribution.source_revision,
                    contribution.content_hash,
                    contribution.content_length,
                    contribution.redacted_stable_digest,
                    int(contribution.request_only),
                    contribution.contribution_kind,
                    contribution.visibility,
                    contribution.protection,
                    _json(contribution.binding_manifest()),
                )
            )
        exact_row(
            tuple(contribution_rows),
            _persisted_manifest_rows(expected_contributions),
            field=f"context assembly contribution manifest: {snapshot.assembly_id}",
        )
        source_frontiers = self._context_source_cut_frontiers(
            connection, snapshot, checkpoint_ns=checkpoint_ns,
        )
        selection_rows = connection.execute(
            "SELECT plan_ordinal, selection_kind, ref_type, ref_id, included, omission_reason, loss_json, source_revision, content_length, content_hash, redacted_stable_digest, visibility, protection, availability, base_delta_role, source_overlay_epoch, overlay_from_revision, overlay_to_revision, overlay_diff_hash, detail_ref, contribution_id, contribution_ordinal, source_cut_position_kind, source_cut_item_id FROM context_assembly_selections WHERE assembly_id = ? ORDER BY plan_ordinal",
            (snapshot.assembly_id,),
        ).fetchall()
        expected_selection = [
            (
                entry.plan_ordinal,
                entry.selection_kind,
                entry.ref.ref_type,
                entry.ref.ref_id,
                int(entry.included),
                entry.omission_reason,
                _json(list(entry.loss)),
                entry.source_revision,
                entry.content_length,
                entry.content_hash,
                entry.redacted_stable_digest,
                entry.visibility,
                entry.protection,
                entry.availability,
                entry.base_delta_role,
                entry.source_overlay_epoch,
                entry.overlay_from_revision,
                entry.overlay_to_revision,
                entry.overlay_diff_hash,
                optional_detail_ref_key(entry.detail_ref),
                entry.contribution_id,
                entry.contribution_ordinal,
                *source_frontiers.get(entry.ref.ref_id, (None, None)),
            )
            for entry in snapshot.selection
        ]
        exact_row(
            tuple(selection_rows),
            _persisted_manifest_rows(expected_selection),
            field=f"context assembly selection manifest: {snapshot.assembly_id}",
        )

        validate_assembly_details(
            connection, snapshot, checkpoint_ns, header_detail_ref
        )


__all__ = ["ContextAssemblyManifestMixin", "validate_sealed_selection"]
