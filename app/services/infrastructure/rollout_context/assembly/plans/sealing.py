"""封存事务中的 plan CAS 与显式失败控制记录；不创建第二个提交。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    runtime_seal_bytes_and_hash,
    seal_snapshot_prefix,
    validate_sealed_plan,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    ContextPlanRegistration,
    read_registration,
    require_owner,
    require_transaction,
)
from app.services.infrastructure.rollout_context.assembly.plans.rows import (
    validate_registry,
)
from app.services.infrastructure.rollout_context.assembly.plans.types import (
    ContextPlanSeal,
)


def preflight_seal(
    connection: sqlite3.Connection,
    snapshot: ContextAssemblySnapshot,
    *,
    seal_idempotency_key: str,
    seal_input_hash: str,
    detail_key: str | None,
) -> ContextPlanSeal:
    require_owner(connection, snapshot.session_id, snapshot.thread_id)
    if not isinstance(seal_idempotency_key, str) or not seal_idempotency_key:
        raise ValueError("assembly-idempotency-conflict: seal 必须有非空幂等键")
    sealed_bytes, input_bound_hash = runtime_seal_bytes_and_hash(
        snapshot, detail_key, seal_input_hash,
    )
    registered = read_registration(
        connection, snapshot.session_id, snapshot.thread_id, snapshot.plan_id
    )
    if registered.plan_state == "sealed" and (
        registered.assembly_id != snapshot.assembly_id
        or registered.seal_idempotency_key != seal_idempotency_key
        or registered.seal_input_hash != seal_input_hash
        or registered.seal_hash != input_bound_hash
    ):
        raise ValueError("assembly-idempotency-conflict: 同一 plan 已封存为不同请求")
    validate_sealed_plan(registered.draft, snapshot)
    # RFC 8785 将固定成员排为 detail_ref、schema、snapshot；最后一个 value
    # 就是本次已编码的 snapshot。只在 writer 准入成功后取其字节，冷读只核
    # envelope hash；前缀转义仍交给唯一 JCS 编码器，不另行拼装 JSON。
    prefix_length = len(seal_snapshot_prefix(detail_key))
    return ContextPlanSeal(
        registered=registered, snapshot=snapshot,
        snapshot_json=sealed_bytes[prefix_length:-1].decode("utf-8"),
        seal_idempotency_key=seal_idempotency_key, seal_input_hash=seal_input_hash,
        detail_key=detail_key, seal_hash=input_bound_hash,
    )


def bind_sealed_registration(
    connection: sqlite3.Connection,
    prepared: ContextPlanSeal,
) -> ContextPlanRegistration:
    """消费同一事务的 preflight 结果，在 snapshot/selection 写入后绑定 plan。"""
    require_transaction(connection)
    snapshot, registered = prepared.snapshot, prepared.registered
    require_owner(connection, snapshot.session_id, snapshot.thread_id)
    row = connection.execute(
        "SELECT snapshot_json, detail_ref FROM context_assemblies "
        "WHERE session_id = ? AND thread_id = ? AND plan_id = ? AND assembly_id = ?",
        (snapshot.session_id, snapshot.thread_id, snapshot.plan_id, snapshot.assembly_id),
    ).fetchone()
    if (
        row is None
        or row != (prepared.snapshot_json, prepared.detail_key)
    ):
        raise ValueError(
            "source-mismatch: plan seal 必须与同事务 assembly snapshot 相符"
        )
    if registered.plan_state == "sealed":
        return registered
    cursor = connection.execute(
        "UPDATE context_plans SET plan_state = 'sealed', assembly_id = ?, "
        "seal_idempotency_key = ?, seal_hash = ?, seal_input_hash = ?, updated_at = ? "
        "WHERE session_id = ? AND thread_id = ? AND plan_id = ? AND plan_state = 'unsealed' AND revision = ? "
        "AND draft_hash = ? AND draft_json = ? AND creation_hash = ?",
        (
            snapshot.assembly_id,
            prepared.seal_idempotency_key,
            prepared.seal_hash,
            prepared.seal_input_hash,
            datetime.now(UTC).isoformat(),
            snapshot.session_id,
            snapshot.thread_id,
            snapshot.plan_id,
            registered.revision,
            registered.draft_hash, registered.draft_json, registered.creation_hash,
        ),
    )
    if cursor.rowcount != 1:
        current = connection.execute(
            "SELECT revision, draft_hash, draft_json FROM context_plans "
            "WHERE session_id = ? AND thread_id = ? AND plan_id = ?",
            (snapshot.session_id, snapshot.thread_id, snapshot.plan_id),
        ).fetchone()
        if current is not None and current[0] == registered.revision and current[1:] != (
            registered.draft_hash, registered.draft_json,
        ):
            raise ValueError("source-mismatch: plan draft manifest hash/文本在 seal preflight 后变化")
        raise RuntimeError("assembly-idempotency-conflict: plan seal CAS 失败")
    cursor = connection.execute(
        "UPDATE tool_set_snapshots SET assembly_id = ? "
        "WHERE session_id = ? AND thread_id = ? AND plan_id = ? AND assembly_id IS NULL",
        (snapshot.assembly_id, snapshot.session_id, snapshot.thread_id, snapshot.plan_id),
    )
    if cursor.rowcount != len(registered.draft.tool_set_refs):
        raise RuntimeError("source-mismatch: draft ToolSetSnapshot 绑定行数不一致")
    validate_registry(connection, registered.draft, snapshot.assembly_id)
    return replace(
        registered, plan_state="sealed", assembly_id=snapshot.assembly_id,
        seal_idempotency_key=prepared.seal_idempotency_key,
        seal_hash=prepared.seal_hash, seal_input_hash=prepared.seal_input_hash,
        sealed_snapshot=snapshot,
    )


def record_seal_failure(
    connection: sqlite3.Connection,
    session_id: str,
    thread_id: str,
    plan_id: str,
    *,
    seal_idempotency_key: str,
    error_code: str,
) -> str:
    """失败仅记录闭合的错误分类，禁止把含敏感正文的异常信息落盘。"""
    require_transaction(connection)
    require_owner(connection, session_id, thread_id)
    registered = read_registration(connection, session_id, thread_id, plan_id)
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
        "INSERT INTO context_plan_seal_failures(failure_id, session_id, thread_id, plan_id, revision, "
        "seal_idempotency_key, error_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            failure_id,
            session_id,
            thread_id,
            plan_id,
            registered.revision,
            seal_idempotency_key,
            error_code,
            datetime.now(UTC).isoformat(),
        ),
    )
    return failure_id
