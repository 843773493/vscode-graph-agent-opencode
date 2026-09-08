"""draft registry 的可检索索引；完整 manifest 与索引必须逐字段相等。"""

from __future__ import annotations

import sqlite3

from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    draft_manifest,
    json_text,
)


def tool_rows(
    plan: ContextRequestPlan, assembly_id: str | None
) -> list[tuple[object, ...]]:
    return [
        (
            ref.ref_id,
            assembly_id,
            ref.source_revision,
            ref.tool_set_schema,
            ref.tool_set_schema_version,
            ref.tool_policy_version,
            ref.content_length,
            ref.content_hash,
            ref.redacted_stable_digest,
            ref.protection,
            ref.availability,
            json_text(dict(ref.tool_policy)),
            json_text([dict(tool) for tool in ref.tools]),
        )
        for ref in plan.tool_set_refs
    ]


def write_registry(
    connection: sqlite3.Connection, plan: ContextRequestPlan, timestamp: str
) -> None:
    """调用方持有事务；既有 draft 修订必须先显式移除旧索引。"""
    connection.executemany(
        "INSERT INTO context_plan_refs(session_id, plan_id, ref_type, ref_id, ref_json) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (
                plan.session_id,
                plan.plan_id,
                ref.ref_type,
                ref.ref_id,
                json_text(ref.to_dict()),
            )
            for ref in plan.refs
        ],
    )
    contribution_manifests = draft_manifest(plan)["contributions"]
    connection.executemany(
        "INSERT INTO context_plan_contributions(session_id, plan_id, contribution_id, manifest_json) "
        "VALUES (?, ?, ?, ?)",
        [
            (
                plan.session_id,
                plan.plan_id,
                contribution.contribution_id,
                json_text(manifest),
            )
            for contribution, manifest in zip(
                plan.contributions, contribution_manifests, strict=True
            )
        ],
    )
    connection.executemany(
        "INSERT INTO tool_set_snapshots(session_id, plan_id, tool_set_snapshot_id, assembly_id, "
        "source_revision, tool_set_schema, tool_set_schema_version, tool_policy_version, "
        "content_length, content_hash, redacted_stable_digest, protection, availability, "
        "tool_policy_json, tools_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (plan.session_id, plan.plan_id, *row, timestamp)
            for row in tool_rows(plan, None)
        ],
    )


def validate_registry(
    connection: sqlite3.Connection,
    plan: ContextRequestPlan,
    assembly_id: str | None,
) -> None:
    identity = (plan.session_id, plan.plan_id)
    refs = connection.execute(
        "SELECT ref_type, ref_id, ref_json FROM context_plan_refs WHERE session_id = ? AND plan_id = ?",
        identity,
    ).fetchall()
    if set(refs) != {
        (ref.ref_type, ref.ref_id, json_text(ref.to_dict())) for ref in plan.refs
    }:
        raise ValueError("source-mismatch: plan ContextRef registry 不一致")
    contributions = connection.execute(
        "SELECT contribution_id, manifest_json FROM context_plan_contributions "
        "WHERE session_id = ? AND plan_id = ?",
        identity,
    ).fetchall()
    if set(contributions) != {
        (contribution.contribution_id, json_text(manifest))
        for contribution, manifest in zip(
            plan.contributions,
            draft_manifest(plan)["contributions"],
            strict=True,
        )
    }:
        raise ValueError("source-mismatch: plan contribution registry 不一致")
    tools = connection.execute(
        "SELECT tool_set_snapshot_id, assembly_id, source_revision, tool_set_schema, "
        "tool_set_schema_version, tool_policy_version, content_length, content_hash, "
        "redacted_stable_digest, protection, availability, tool_policy_json, tools_json "
        "FROM tool_set_snapshots WHERE session_id = ? AND plan_id = ?",
        identity,
    ).fetchall()
    if set(tools) != set(tool_rows(plan, assembly_id)):
        raise ValueError("source-mismatch: plan ToolSetSnapshot registry 不一致")


def delete_unsealed_registry(
    connection: sqlite3.Connection, plan: ContextRequestPlan
) -> None:
    """只清理经 header 验证的可修订 draft 索引，不触碰任何 sealed manifest。"""
    identity = (plan.session_id, plan.plan_id)
    for table in ("context_plan_refs", "context_plan_contributions"):
        connection.execute(
            f"DELETE FROM {table} WHERE session_id = ? AND plan_id = ?", identity
        )
    connection.execute(
        "DELETE FROM tool_set_snapshots WHERE session_id = ? AND plan_id = ? AND assembly_id IS NULL",
        identity,
    )
