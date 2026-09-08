"""schema4 full-copy 的真实 draft 保留与 sealed import 事务接线。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.serde.plan import unsealed_context_plan_from_dict
from app.domain.itemized.serde.registry import (
    parse_context_ref,
    parse_contribution,
    parse_tool_set_ref,
)
from app.services.infrastructure.rollout_context.assembly.plans.imported import (
    write_imported_registration,
)
from app.services.infrastructure.rollout_context.assembly.plans.imported_sources import (
    build_source_manifest,
    parse_source_manifest,
    read_source_manifest,
)
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    creation_hash,
    draft_manifest,
    json_text,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    read_registration,
)
from app.services.infrastructure.rollout_context.assembly.plans.rows import (
    write_registry,
)


@dataclass(frozen=True, slots=True)
class CopiedPlanSource:
    plan_id: str
    initial: ContextRequestPlan | None
    draft: ContextRequestPlan | None
    revision: int
    assembly_id: str | None
    snapshot_hash: str | None
    created_at: str
    updated_at: str
    source_manifest: Mapping[str, object]


def collect_plan_identities(
    connection: sqlite3.Connection, collect: Callable[[str, Iterable[object]], None]
) -> None:
    """覆盖未 seal 的 plan 及 initial/current 候选；不扫描或读取 source 正文。"""
    for plan_id, initial, current, imported, session_id in connection.execute(
        "SELECT plan_id, creation_json, draft_json, source_manifest_json, session_id FROM context_plans"
    ).fetchall():
        collect("plan", (plan_id,))
        registries = []
        for raw in (initial, current):
            if raw is None:
                continue
            plan = unsealed_context_plan_from_dict(json.loads(raw))
            registries.append((plan.refs, plan.contributions))
            collect("tool_set", (ref.ref_id for ref in plan.tool_set_refs))
        if imported is not None:
            sources = read_source_manifest(imported, session_id=session_id, plan_id=plan_id)
            registries.append((sources.refs, sources.contributions))
        for refs, contributions in registries:
            for ref in refs:
                collect(
                    "item" if ref.ref_type == "canonical_item" else "request_ref",
                    (ref.ref_id,),
                )
                if isinstance(ref.source_ref, str):
                    collect("request_ref", (ref.source_ref,))
            for contribution in contributions:
                collect("context_contribution", (contribution.contribution_id,))
                alias = contribution.metadata.get("source_ref")
                if isinstance(alias, str):
                    collect("request_ref", (alias,))
                overlay_ref = contribution.metadata.get("overlay_ref")
                if isinstance(overlay_ref, str):
                    collect("request_ref", (overlay_ref,))
    for (ref_id,) in connection.execute(
        "SELECT ref_id FROM context_plan_refs WHERE ref_type='request_only'"
    ).fetchall():
        collect("request_ref", (ref_id,))
    collect(
        "context_contribution",
        (
            row[0]
            for row in connection.execute(
                "SELECT contribution_id FROM context_plan_contributions"
            ).fetchall()
        ),
    )


def detach_copied_plan_registry(state) -> None:
    """仅清理私有 target 副本；全部来源已在 clone 的 source owner 下验证。"""
    connection = state.connection
    sources = []
    for row in connection.execute(
        "SELECT session_id, plan_id, plan_state, creation_json, draft_json, revision, "
        "assembly_id, created_at, updated_at, registration_origin, source_manifest_json FROM context_plans"
    ).fetchall():
        if row[0] != state.source_session_id:
            raise ValueError("source-mismatch: fork plan registry source owner 不一致")
        unsealed = row[2] == "unsealed"
        snapshot = (
            None
            if unsealed
            else connection.execute(
                "SELECT snapshot_json FROM context_assemblies WHERE assembly_id=? AND plan_id=?",
                (row[6], row[1]),
            ).fetchone()
        )
        if not unsealed and snapshot is None:
            raise ValueError("source-mismatch: fork sealed plan 缺少 snapshot")
        if row[9] == "runtime":
            registered = unsealed_context_plan_from_dict(json.loads(row[4]))
            source_manifest = build_source_manifest(
                session_id=row[0], plan_id=row[1], refs=registered.refs,
                contributions=registered.contributions,
            )
        else:
            source_manifest = read_source_manifest(
                row[10], session_id=row[0], plan_id=row[1]
            ).manifest
        sources.append(
            CopiedPlanSource(
                plan_id=row[1],
                initial=unsealed_context_plan_from_dict(json.loads(row[3]))
                if unsealed
                else None,
                draft=unsealed_context_plan_from_dict(json.loads(row[4]))
                if unsealed
                else None,
                revision=row[5],
                assembly_id=row[6],
                snapshot_hash=sha256_jcs(json.loads(snapshot[0])) if snapshot else None,
                created_at=row[7],
                updated_at=row[8],
                source_manifest=source_manifest,
            )
        )
    state.plan_sources = tuple(sources)
    state.plan_failure_rows = tuple(
        connection.execute(
            "SELECT failure_id, plan_id, revision, seal_idempotency_key, error_code, created_at "
            "FROM context_plan_seal_failures WHERE session_id=?",
            (state.source_session_id,),
        ).fetchall()
    )
    # 子表先移除，工具候选与真实 draft 已包含在经过验证的 initial/current 中。
    for table in (
        "context_plan_seal_failures",
        "context_plan_refs",
        "context_plan_contributions",
        "tool_set_snapshots",
        "context_plans",
    ):
        connection.execute(f"DELETE FROM {table}")


def _draft(state, source: ContextRequestPlan) -> ContextRequestPlan:
    from app.services.infrastructure.rollout_context.fork.full_copy.plan_values import (
        localize_plan_value,
    )

    value = localize_plan_value(state, state.remap_json(source.to_dict()))
    return replace(
        source,
        session_id=state.target_session_id,
        plan_id=value["plan_id"],
        refs=tuple(parse_context_ref(ref) for ref in value["refs"]),
        contributions=tuple(
            parse_contribution(item, sealed=False) for item in value["contributions"]
        ),
        tool_set_refs=tuple(parse_tool_set_ref(ref) for ref in value["tool_set_refs"]),
        active_view_id=value["active_view_id"],
        source_overlay_epoch=value["source_overlay_epoch"],
        plan_creation_idempotency_key=state.service._full_copy_identity(
            state.fork_id, state.target_session_id, "plan_creation", source.plan_id
        ),
    )


def write_copied_plan_registry(state) -> None:
    from app.services.infrastructure.rollout_context.fork.full_copy.plan_values import (
        localize_plan_value,
    )

    connection = state.connection
    for source in state.plan_sources:
        target_plan = state.maps["plan"][source.plan_id]
        if source.draft is None:
            assembly_id = state.maps["assembly"][source.assembly_id]
            raw, detail_key = connection.execute(
                "SELECT snapshot_json, detail_ref FROM context_assemblies WHERE assembly_id=? AND plan_id=?",
                (assembly_id, target_plan),
            ).fetchone()
            snapshot = ContextAssemblySnapshot.from_dict(json.loads(raw))
            provenance = {
                "source_session_id": state.source_session_id,
                "source_plan_id": source.plan_id,
                "source_assembly_id": source.assembly_id,
                "source_snapshot_hash": source.snapshot_hash,
                "source_schema_version": 4,
                "audit_id": state.fork_id,
            }
            _record_source_plan(state, source, provenance)
            # 来源只能是 source 已验证的真实 registry，不从 included snapshot 补造。
            sources = parse_source_manifest(
                localize_plan_value(state, state.remap_json(source.source_manifest)),
                session_id=state.target_session_id, plan_id=target_plan,
            )
            write_imported_registration(
                connection,
                snapshot,
                detail_key=detail_key,
                origin="fork_import",
                source_provenance=provenance,
                source_manifest=sources.manifest,
                seal_idempotency_key=state.service._full_copy_identity(
                    state.fork_id, state.target_session_id, "plan_seal", source.plan_id
                ),
            )
        else:
            initial = _draft(state, source.initial)
            draft = _draft(state, source.draft)
            manifest = draft_manifest(draft)
            connection.execute(
                "INSERT INTO context_plans(session_id, plan_id, plan_creation_idempotency_key, creation_hash, "
                "creation_json, draft_json, draft_hash, revision, plan_state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unsealed', ?, ?)",
                (
                    state.target_session_id,
                    target_plan,
                    draft.plan_creation_idempotency_key,
                    creation_hash(initial),
                    json_text(draft_manifest(initial)),
                    json_text(manifest),
                    sha256_jcs(manifest),
                    source.revision,
                    source.created_at,
                    source.updated_at,
                ),
            )
            write_registry(connection, draft, state.timestamp)
            _record_source_plan(
                state,
                source,
                {
                    "source_session_id": state.source_session_id,
                    "source_plan_id": source.plan_id,
                    "source_creation_hash": creation_hash(source.initial),
                    "source_draft_hash": sha256_jcs(draft_manifest(source.draft)),
                    "source_revision": source.revision,
                    "audit_id": state.fork_id,
                },
            )
        read_registration(connection, state.target_session_id, target_plan)
    for (
        failure,
        plan_id,
        revision,
        seal_key,
        error_code,
        created_at,
    ) in state.plan_failure_rows:
        connection.execute(
            "INSERT INTO context_plan_seal_failures(failure_id, session_id, plan_id, revision, seal_idempotency_key, error_code, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                state.service._full_copy_identity(
                    state.fork_id, state.target_session_id, "seal_failure", failure
                ),
                state.target_session_id,
                state.maps["plan"][plan_id],
                revision,
                state.service._full_copy_identity(
                    state.fork_id, state.target_session_id, "failed_seal", seal_key
                ),
                error_code,
                created_at,
            ),
        )


def _record_source_plan(
    state, source: CopiedPlanSource, provenance: Mapping[str, object]
) -> None:
    entity = "assembly" if source.assembly_id is not None else "plan"
    source_id = source.assembly_id if source.assembly_id is not None else source.plan_id
    key = (state.fork_id, entity, source_id)
    row = state.connection.execute(
        "SELECT lineage_json FROM fork_identity_mappings WHERE fork_id=? AND entity_type=? AND source_local_id=?",
        key,
    ).fetchone()
    if row is None:
        raise ValueError("source-mismatch: fork plan 缺少已生成 identity audit")
    lineage = json.loads(row[0])
    lineage["source_snapshot" if source.assembly_id is not None else "source_draft"] = (
        dict(provenance)
    )
    result = state.connection.execute(
        "UPDATE fork_identity_mappings SET lineage_json=? WHERE fork_id=? AND entity_type=? AND source_local_id=?",
        (json_text(lineage), *key),
    )
    if result.rowcount != 1:
        raise RuntimeError("source-mismatch: fork plan audit 写入失败")
