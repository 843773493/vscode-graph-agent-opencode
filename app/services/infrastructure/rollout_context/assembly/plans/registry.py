"""独立 plan identity 的创建、修订和恢复；不拥有连接或提交。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    canonical_json_bytes_and_hash,
)
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.serde.plan import unsealed_context_plan_from_dict
from app.services.infrastructure.rollout_context.assembly.content_store import (
    load_snapshot,
)
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    _creation_hash_manifest,
    draft_manifest,
    persisted_runtime_seal_hash,
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
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as json_text,
)

_ValidatedPredecessors = dict[
    tuple[str, str, str], tuple[tuple[object, ...], tuple[object, ...]],
]


def require_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeError("context plan 写入必须属于 storage owner 的显式事务")


def require_owner(
    connection: sqlite3.Connection, session_id: str, thread_id: str
) -> None:
    row = connection.execute(
        "SELECT session_id, thread_id, database_state FROM database_meta WHERE singleton_id = 1",
    ).fetchone()
    if row != (session_id, thread_id, "active"):
        raise ValueError(
            "source-mismatch: plan session/thread owner 不一致或数据库不是 active"
        )


def read_registration(
    connection: sqlite3.Connection,
    session_id: str,
    thread_id: str,
    plan_id: str,
    *,
    validated_predecessors: _ValidatedPredecessors | None = None,
) -> ContextPlanRegistration:
    require_historical_scope(connection, session_id, thread_id)
    row = connection.execute(
        "SELECT draft_json, draft_hash, creation_hash, revision, plan_state, assembly_id, "
        "seal_idempotency_key, seal_hash, plan_creation_idempotency_key, creation_json, "
        "seal_input_hash, created_at, updated_at "
        "FROM context_plans WHERE session_id = ? AND thread_id = ? AND plan_id = ?",
        (session_id, thread_id, plan_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"context plan 不存在: {plan_id}")
    if (row[4] == "unsealed" and row[10] is not None) or (
        row[4] == "sealed" and (not isinstance(row[10], str) or not row[10].strip())
    ):
        raise ValueError("source-mismatch: plan seal input binding 不一致")
    value = json.loads(row[0])
    manifest_bytes, manifest_hash = canonical_json_bytes_and_hash(value)
    if row[0] != manifest_bytes.decode("utf-8") or row[1] != manifest_hash:
        raise ValueError("source-mismatch: plan draft manifest hash 不一致")
    plan = unsealed_context_plan_from_dict(value)
    if any(item.body is not None for item in plan.contributions):
        raise ValueError("source-mismatch: plan registry 不得包含 inline body")
    if (plan.session_id, plan.thread_id, plan.plan_id, plan.plan_creation_idempotency_key) != (
        session_id,
        thread_id,
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
    same_json = row[0] == row[9]
    if same_json:
        # 同一 SQL 行的相同文本已完整冷校验；只在本次读取中复用 typed 值和投影。
        initial = plan
        initial_manifest = plan.to_dict(include_plan_hash=False)
    else:
        initial_value = json.loads(row[9])
        if row[9] != json_text(initial_value):
            raise ValueError("source-mismatch: 初始 creation manifest 不是规范 JSON")
        initial = unsealed_context_plan_from_dict(initial_value)
        if any(item.body is not None for item in initial.contributions):
            raise ValueError("source-mismatch: creation registry 不得包含 inline body")
        initial_manifest = initial.to_dict(include_plan_hash=False)
    if (initial.session_id, initial.thread_id, initial.plan_id, initial.plan_creation_idempotency_key) != (
        session_id,
        thread_id,
        plan_id,
        row[8],
    ) or row[2] != _creation_hash_manifest(initial_manifest):
        raise ValueError("source-mismatch: plan creation preimage 不一致")
    if row[3] == 0 and not same_json and initial_manifest != plan.to_dict(include_plan_hash=False):
        raise ValueError(
            "source-mismatch: 未修订 draft 与初始 creation manifest 不一致"
        )
    validate_registry(connection, plan, row[5])
    snapshot = None
    if row[4] == "sealed":
        assembly = connection.execute(
            "SELECT snapshot_json, detail_ref, status, assembly_id, session_id, thread_id, "
            "plan_id, plan_hash, request_hash, turn_id, execution_id, history_view_revision, "
            "source_overlay_epoch, model_call_id, outcome, created_at, sealed_at, terminal_at "
            "FROM context_assemblies "
            "WHERE session_id = ? AND thread_id = ? AND plan_id = ? AND assembly_id = ?",
            (session_id, thread_id, plan_id, row[5]),
        ).fetchone()
        if assembly is None or assembly[2] not in {"sealed", "terminal"}:
            raise ValueError("source-mismatch: sealed plan 缺少同一 assembly")
        snapshot_value = load_snapshot(connection, assembly[0])
        snapshot = ContextAssemblySnapshot.from_dict(snapshot_value)
        key = (session_id, thread_id, plan_id)
        persisted_rows = (tuple(row), tuple(assembly))
        # 仅在本次 prepare 复用完全相同的 SQL 原文与 scalar，不缓存 typed 对象。
        # 初读和 writer 分属不同事务；行有变化时必须重新核验 plan/request 与 seal。
        if validated_predecessors is None or validated_predecessors.get(key) != persisted_rows:
            validate_sealed_plan(plan, snapshot)
            seal_hash = persisted_runtime_seal_hash(
                canonical_json_bytes(snapshot_value).decode("utf-8"), assembly[1], row[10],
            )
            if row[7] != seal_hash:
                raise ValueError("source-mismatch: sealed plan preimage 与 assembly 不一致")
            if validated_predecessors is not None:
                validated_predecessors[key] = persisted_rows
    return ContextPlanRegistration(
        session_id=session_id,
        thread_id=thread_id,
        draft=plan,
        creation_hash=row[2],
        draft_hash=row[1], draft_json=row[0],
        revision=row[3],
        plan_state=row[4],
        assembly_id=row[5],
        seal_idempotency_key=row[6],
        seal_hash=row[7],
        seal_input_hash=row[10],
        sealed_snapshot=snapshot,
    )


def create_registration(
    connection: sqlite3.Connection, plan: ContextRequestPlan
) -> ContextPlanRegistration:
    require_transaction(connection)
    require_owner(connection, plan.session_id, plan.thread_id)
    manifest = draft_manifest(plan)
    input_hash = _creation_hash_manifest(manifest)
    existing = connection.execute(
        "SELECT plan_id FROM context_plans WHERE session_id = ? AND thread_id = ? AND plan_creation_idempotency_key = ?",
        (plan.session_id, plan.thread_id, plan.plan_creation_idempotency_key),
    ).fetchone()
    if existing is not None:
        registration = read_registration(
            connection, plan.session_id, plan.thread_id, existing[0]
        )
        if registration.creation_hash != input_hash:
            raise ValueError("plan-idempotency-conflict: 同一创建键的初始输入不一致")
        return registration
    if connection.execute(
        "SELECT 1 FROM context_plans WHERE session_id = ? AND thread_id = ? AND plan_id = ?",
        (plan.session_id, plan.thread_id, plan.plan_id),
    ).fetchone():
        raise ValueError("plan-idempotency-conflict: plan_id 已被另一创建键占用")
    timestamp = datetime.now(UTC).isoformat()
    manifest_bytes, manifest_hash = canonical_json_bytes_and_hash(manifest)
    manifest_json = manifest_bytes.decode("utf-8")
    connection.execute(
        "INSERT INTO context_plans(session_id, thread_id, plan_id, plan_creation_idempotency_key, "
        "creation_hash, creation_json, draft_json, draft_hash, revision, plan_state, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'unsealed', ?, ?)",
        (
            plan.session_id,
            plan.thread_id,
            plan.plan_id,
            plan.plan_creation_idempotency_key,
            input_hash,
            manifest_json,
            manifest_json,
            manifest_hash,
            timestamp,
            timestamp,
        ),
    )
    draft = replace(plan, contributions=tuple(
        replace(item, body=None) for item in plan.contributions
    ), tool_snapshot=())
    write_registry(connection, draft, timestamp)
    return ContextPlanRegistration(
        session_id=plan.session_id, thread_id=plan.thread_id, draft=draft,
        creation_hash=input_hash, draft_hash=manifest_hash, draft_json=manifest_json,
        revision=0, plan_state="unsealed",
        assembly_id=None, seal_idempotency_key=None, seal_hash=None,
    )


def revise_registration(
    connection: sqlite3.Connection,
    plan: ContextRequestPlan,
    *,
    expected_revision: int,
) -> ContextPlanRegistration:
    require_transaction(connection)
    require_owner(connection, plan.session_id, plan.thread_id)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("plan-revision-conflict: expected_revision 必须是非负整数")
    manifest = draft_manifest(plan)
    existing = read_registration(
        connection, plan.session_id, plan.thread_id, plan.plan_id
    )
    if existing.plan_state != "unsealed":
        raise ValueError("assembly-idempotency-conflict: sealed plan 不可修订")
    if (
        existing.draft.plan_creation_idempotency_key
        != plan.plan_creation_idempotency_key
    ):
        raise ValueError("plan-idempotency-conflict: 修订不能改变 creation key")
    if existing.revision != expected_revision:
        raise ValueError("plan-revision-conflict: draft 已被其它写入修订")
    if existing.draft.to_dict() == manifest:
        return existing
    timestamp = datetime.now(UTC).isoformat()
    delete_unsealed_registry(connection, existing.draft)
    draft = replace(plan, contributions=tuple(
        replace(item, body=None) for item in plan.contributions
    ), tool_snapshot=())
    write_registry(connection, draft, timestamp)
    manifest_bytes, manifest_hash = canonical_json_bytes_and_hash(manifest)
    cursor = connection.execute(
        "UPDATE context_plans SET draft_json = ?, draft_hash = ?, revision = revision + 1, updated_at = ? "
        "WHERE session_id = ? AND thread_id = ? AND plan_id = ? AND revision = ? AND plan_state = 'unsealed' "
        "AND draft_hash = ? AND draft_json = ?",
        (
            manifest_bytes.decode("utf-8"),
            manifest_hash,
            timestamp,
            plan.session_id,
            plan.thread_id,
            plan.plan_id,
            expected_revision,
            existing.draft_hash, existing.draft_json,
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("plan-revision-conflict: draft CAS 写入失败")
    return replace(
        existing, draft=draft, draft_hash=manifest_hash,
        draft_json=manifest_bytes.decode("utf-8"), revision=expected_revision + 1,
    )
