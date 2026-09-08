"""sealed assembly manifest 的纯边界校验。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import canonical_json_bytes
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
)


def validate_sealed_selection(entries: Iterable[object]) -> tuple[object, ...]:
    """确认 selection 由 Saver 预先编号，禁止 assembly 层重新排序。"""
    return ordered_selection(entries)


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _persisted_manifest_rows(
    rows: Iterable[tuple[object, ...]],
) -> tuple[tuple[object, ...], ...]:
    """按 snapshot_json 的 JCS 边界恢复 SQL 字段，不触碰 omitted source 正文。"""
    # domain 可持有 str Enum，持久 JSON/SQLite 只持有字符串；转换实际比较
    # 的 manifest 字段，不放宽 exact_row 类型检查，也不序列化工具定义。
    return tuple(tuple(row) for row in json.loads(_json(list(rows))))


class ContextAssemblyManifestMixin:
    """只验证 sealed assembly 的派生 SQLite manifest。"""

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
            connection, snapshot.session_id, snapshot.plan_id
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
            "SELECT contribution_ordinal, contribution_id, source_kind, source_revision, content_hash, content_length, redacted_stable_digest, request_only, contribution_kind, visibility, protection, metadata_json FROM context_assembly_contributions WHERE assembly_id = ? ORDER BY contribution_ordinal",
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
                    _json(dict(contribution.metadata)),
                )
            )
        exact_row(
            tuple(contribution_rows),
            _persisted_manifest_rows(expected_contributions),
            field=f"context assembly contribution manifest: {snapshot.assembly_id}",
        )
        selection_rows = connection.execute(
            "SELECT plan_ordinal, selection_kind, ref_type, ref_id, included, omission_reason, loss_json, source_revision, content_length, content_hash, redacted_stable_digest, visibility, protection, availability, base_delta_role, source_overlay_epoch, overlay_from_revision, overlay_to_revision, overlay_diff_hash, detail_ref, contribution_id, contribution_ordinal FROM context_assembly_selections WHERE assembly_id = ? ORDER BY plan_ordinal",
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
