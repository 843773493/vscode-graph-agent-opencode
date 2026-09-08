"""独立 plan identity 的创建、修订和恢复；不拥有连接或提交。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.serde.plan import unsealed_context_plan_from_dict
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    creation_hash,
    draft_manifest,
    json_text,
    runtime_seal_hash,
    validate_sealed_plan,
)
from app.services.infrastructure.rollout_context.assembly.plans.rows import (
    delete_unsealed_registry,
    validate_registry,
    write_registry,
)
from app.services.infrastructure.rollout_context.assembly.plans.types import (
    ContextPlanRegistration,
)


def require_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeError("context plan 写入必须属于 storage owner 的显式事务")


def require_owner(connection: sqlite3.Connection, session_id: str) -> None:
    row = connection.execute(
        "SELECT session_id, database_state FROM database_meta WHERE singleton_id = 1",
    ).fetchone()
    if row != (session_id, "active"):
        raise ValueError(
            "source-mismatch: plan session owner 不一致或数据库不是 active"
        )


def read_registration(
    connection: sqlite3.Connection,
    session_id: str,
    plan_id: str,
) -> ContextPlanRegistration:
    require_owner(connection, session_id)
    row = connection.execute(
        "SELECT draft_json, draft_hash, creation_hash, revision, plan_state, assembly_id, "
        "seal_idempotency_key, seal_hash, plan_creation_idempotency_key, creation_json, "
        "registration_origin, source_provenance_json, seal_input_hash, source_manifest_json "
        "FROM context_plans WHERE session_id = ? AND plan_id = ?",
        (session_id, plan_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"context plan 不存在: {plan_id}")
    if row[10] != "runtime":
        from app.services.infrastructure.rollout_context.assembly.plans.imported import (
            read_imported_registration,
        )

        return read_imported_registration(connection, session_id, plan_id)
    if row[11] is not None or row[13] is not None:
        raise ValueError("source-mismatch: runtime draft 不得冒充导入记录")
    if (row[4] == "unsealed" and row[12] is not None) or (
        row[4] == "sealed" and (not isinstance(row[12], str) or not row[12].strip())
    ):
        raise ValueError("source-mismatch: plan seal input binding 不一致")
    value = json.loads(row[0])
    if row[0] != json_text(value) or row[1] != sha256_jcs(value):
        raise ValueError("source-mismatch: plan draft manifest hash 不一致")
    plan = unsealed_context_plan_from_dict(value)
    if any(item.body is not None for item in plan.contributions):
        raise ValueError("source-mismatch: plan registry 不得包含 inline body")
    if (plan.session_id, plan.plan_id, plan.plan_creation_idempotency_key) != (
        session_id,
        plan_id,
        row[8],
    ):
        raise ValueError("source-mismatch: plan header 与 draft owner 不一致")
    if type(row[3]) is not int or row[3] < 0:
        raise ValueError("source-mismatch: plan revision 非法")
    if row[4] not in {"unsealed", "sealed"}:
        raise ValueError("source-mismatch: plan_state 非法")
    binding = row[5:8]
    if (row[4] == "unsealed" and any(item is not None for item in binding)) or (
        row[4] == "sealed"
        and any(not isinstance(item, str) or not item for item in binding)
    ):
        raise ValueError("source-mismatch: plan seal binding 不一致")
    initial_value = json.loads(row[9])
    if row[9] != json_text(initial_value):
        raise ValueError("source-mismatch: 初始 creation manifest 不是规范 JSON")
    initial = unsealed_context_plan_from_dict(initial_value)
    if any(item.body is not None for item in initial.contributions):
        raise ValueError("source-mismatch: creation registry 不得包含 inline body")
    if (initial.session_id, initial.plan_id, initial.plan_creation_idempotency_key) != (
        session_id,
        plan_id,
        row[8],
    ) or row[2] != creation_hash(initial):
        raise ValueError("source-mismatch: plan creation preimage 不一致")
    if row[3] == 0 and draft_manifest(initial) != draft_manifest(plan):
        raise ValueError(
            "source-mismatch: 未修订 draft 与初始 creation manifest 不一致"
        )
    validate_registry(connection, plan, row[5])
    if row[4] == "sealed":
        assembly = connection.execute(
            "SELECT snapshot_json, detail_ref, status FROM context_assemblies "
            "WHERE session_id = ? AND plan_id = ? AND assembly_id = ?",
            (session_id, plan_id, row[5]),
        ).fetchone()
        if assembly is None or assembly[2] not in {"sealed", "terminal"}:
            raise ValueError("source-mismatch: sealed plan 缺少同一 assembly")
        snapshot = ContextAssemblySnapshot.from_dict(json.loads(assembly[0]))
        validate_sealed_plan(plan, snapshot)
        if row[7] != runtime_seal_hash(snapshot, assembly[1], row[12]):
            raise ValueError("source-mismatch: sealed plan preimage 与 assembly 不一致")
    return ContextPlanRegistration(
        plan, row[2], row[3], row[4], row[5], row[6], row[7], seal_input_hash=row[12],
    )


def create_registration(
    connection: sqlite3.Connection, plan: ContextRequestPlan
) -> ContextPlanRegistration:
    require_transaction(connection)
    require_owner(connection, plan.session_id)
    manifest = draft_manifest(plan)
    input_hash = creation_hash(plan)
    existing = connection.execute(
        "SELECT plan_id FROM context_plans WHERE session_id = ? AND plan_creation_idempotency_key = ?",
        (plan.session_id, plan.plan_creation_idempotency_key),
    ).fetchone()
    if existing is not None:
        registration = read_registration(connection, plan.session_id, existing[0])
        if registration.creation_hash != input_hash:
            raise ValueError("plan-idempotency-conflict: 同一创建键的初始输入不一致")
        return registration
    if connection.execute(
        "SELECT 1 FROM context_plans WHERE session_id = ? AND plan_id = ?",
        (plan.session_id, plan.plan_id),
    ).fetchone():
        raise ValueError("plan-idempotency-conflict: plan_id 已被另一创建键占用")
    timestamp = datetime.now(UTC).isoformat()
    connection.execute(
        "INSERT INTO context_plans(session_id, plan_id, plan_creation_idempotency_key, "
        "creation_hash, creation_json, draft_json, draft_hash, revision, plan_state, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'unsealed', ?, ?)",
        (
            plan.session_id,
            plan.plan_id,
            plan.plan_creation_idempotency_key,
            input_hash,
            json_text(manifest),
            json_text(manifest),
            sha256_jcs(manifest),
            timestamp,
            timestamp,
        ),
    )
    write_registry(connection, plan, timestamp)
    return read_registration(connection, plan.session_id, plan.plan_id)


def revise_registration(
    connection: sqlite3.Connection,
    plan: ContextRequestPlan,
    *,
    expected_revision: int,
) -> ContextPlanRegistration:
    require_transaction(connection)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("plan-revision-conflict: expected_revision 必须是非负整数")
    manifest = draft_manifest(plan)
    existing = read_registration(connection, plan.session_id, plan.plan_id)
    if existing.plan_state != "unsealed":
        raise ValueError("assembly-idempotency-conflict: sealed plan 不可修订")
    if existing.draft is None or existing.registration_origin != "runtime":
        raise ValueError("plan-idempotency-conflict: 导入记录没有可修订草稿")
    if (
        existing.draft.plan_creation_idempotency_key
        != plan.plan_creation_idempotency_key
    ):
        raise ValueError("plan-idempotency-conflict: 修订不能改变 creation key")
    if existing.revision != expected_revision:
        raise ValueError("plan-revision-conflict: draft 已被其它写入修订")
    if draft_manifest(existing.draft) == manifest:
        return existing
    timestamp = datetime.now(UTC).isoformat()
    delete_unsealed_registry(connection, existing.draft)
    write_registry(connection, plan, timestamp)
    cursor = connection.execute(
        "UPDATE context_plans SET draft_json = ?, draft_hash = ?, revision = revision + 1, updated_at = ? "
        "WHERE session_id = ? AND plan_id = ? AND revision = ? AND plan_state = 'unsealed'",
        (
            json_text(manifest),
            sha256_jcs(manifest),
            timestamp,
            plan.session_id,
            plan.plan_id,
            expected_revision,
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("plan-revision-conflict: draft CAS 写入失败")
    return read_registration(connection, plan.session_id, plan.plan_id)
