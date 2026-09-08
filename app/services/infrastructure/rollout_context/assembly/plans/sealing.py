"""封存事务中的 plan CAS 与显式失败控制记录；不创建第二个提交。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    runtime_seal_hash,
    validate_sealed_plan,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    ContextPlanRegistration,
    read_registration,
    require_transaction,
)


def preflight_seal(
    connection: sqlite3.Connection,
    snapshot: ContextAssemblySnapshot,
    *,
    seal_idempotency_key: str,
    seal_input_hash: str,
    detail_key: str | None,
) -> ContextPlanRegistration:
    if not isinstance(seal_idempotency_key, str) or not seal_idempotency_key:
        raise ValueError("assembly-idempotency-conflict: seal 必须有非空幂等键")
    input_bound_hash = runtime_seal_hash(snapshot, detail_key, seal_input_hash)
    registered = read_registration(connection, snapshot.session_id, snapshot.plan_id)
    if registered.draft is None or registered.registration_origin != "runtime":
        raise ValueError("assembly-idempotency-conflict: 导入快照没有运行时 draft，不可再次 seal")
    if registered.plan_state == "sealed" and (
        registered.assembly_id != snapshot.assembly_id
        or registered.seal_idempotency_key != seal_idempotency_key
        or registered.seal_input_hash != seal_input_hash
        or registered.seal_hash != input_bound_hash
    ):
        raise ValueError("assembly-idempotency-conflict: 同一 plan 已封存为不同请求")
    validate_sealed_plan(registered.draft, snapshot)
    return registered


def bind_sealed_registration(
    connection: sqlite3.Connection,
    snapshot: ContextAssemblySnapshot,
    *,
    seal_idempotency_key: str,
    seal_input_hash: str,
    detail_key: str | None,
) -> None:
    """由 assembly owner 在同一事务写入 snapshot/selection 后绑定 plan。"""
    require_transaction(connection)
    registered = preflight_seal(
        connection,
        snapshot,
        seal_idempotency_key=seal_idempotency_key,
        seal_input_hash=seal_input_hash,
        detail_key=detail_key,
    )
    row = connection.execute(
        "SELECT snapshot_json, detail_ref FROM context_assemblies "
        "WHERE session_id = ? AND plan_id = ? AND assembly_id = ?",
        (snapshot.session_id, snapshot.plan_id, snapshot.assembly_id),
    ).fetchone()
    if (
        row is None
        or row[1] != detail_key
        or runtime_seal_hash(
            ContextAssemblySnapshot.from_dict(json.loads(row[0])),
            row[1],
            seal_input_hash,
        )
        != runtime_seal_hash(snapshot, detail_key, seal_input_hash)
    ):
        raise ValueError(
            "source-mismatch: plan seal 必须与同事务 assembly snapshot 相符"
        )
    if registered.plan_state == "sealed":
        return
    cursor = connection.execute(
        "UPDATE context_plans SET plan_state = 'sealed', assembly_id = ?, "
        "seal_idempotency_key = ?, seal_hash = ?, seal_input_hash = ?, updated_at = ? "
        "WHERE session_id = ? AND plan_id = ? AND plan_state = 'unsealed' AND revision = ?",
        (
            snapshot.assembly_id,
            seal_idempotency_key,
            runtime_seal_hash(snapshot, detail_key, seal_input_hash),
            seal_input_hash,
            datetime.now(UTC).isoformat(),
            snapshot.session_id,
            snapshot.plan_id,
            registered.revision,
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("assembly-idempotency-conflict: plan seal CAS 失败")
    cursor = connection.execute(
        "UPDATE tool_set_snapshots SET assembly_id = ? "
        "WHERE session_id = ? AND plan_id = ? AND assembly_id IS NULL",
        (snapshot.assembly_id, snapshot.session_id, snapshot.plan_id),
    )
    if cursor.rowcount != len(registered.draft.tool_set_refs):
        raise RuntimeError("source-mismatch: draft ToolSetSnapshot 绑定行数不一致")
    read_registration(connection, snapshot.session_id, snapshot.plan_id)


def record_seal_failure(
    connection: sqlite3.Connection,
    session_id: str,
    plan_id: str,
    *,
    seal_idempotency_key: str,
    error_code: str,
) -> str:
    """失败仅记录闭合的错误分类，禁止把含敏感正文的异常信息落盘。"""
    require_transaction(connection)
    registered = read_registration(connection, session_id, plan_id)
    if registered.plan_state != "unsealed":
        raise ValueError("assembly-idempotency-conflict: sealed plan 不能新增失败尝试")
    if not isinstance(seal_idempotency_key, str) or not seal_idempotency_key:
        raise ValueError("assembly-idempotency-conflict: seal failure 缺少幂等键")
    if error_code not in {
        "source-mismatch",
        "detail-unavailable",
        "plan-order-integrity",
        "plan-hash-mismatch",
        "request-hash-mismatch",
        "seal-storage-failure",
    }:
        raise ValueError("seal failure error_code 不属于允许的控制分类")
    failure_id = f"seal-failure:{uuid4().hex}"
    connection.execute(
        "INSERT INTO context_plan_seal_failures(failure_id, session_id, plan_id, revision, "
        "seal_idempotency_key, error_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            failure_id,
            session_id,
            plan_id,
            registered.revision,
            seal_idempotency_key,
            error_code,
            datetime.now(UTC).isoformat(),
        ),
    )
    return failure_id
