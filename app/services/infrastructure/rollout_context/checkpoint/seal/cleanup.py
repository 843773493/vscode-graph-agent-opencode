"""失败 control 与未提交 detail 清理；不删除任何已提交 selection 的正文。"""

from __future__ import annotations

from collections.abc import Sequence

from app.services.infrastructure.rollout_context.runtime.detail_store import (
    DetailRecord,
)


def release_uncommitted_details(
    owner, session_id: str, checkpoint_ns: str, records: Sequence[DetailRecord]
) -> None:
    for record in records:
        if owner._storage.context_assembly_references_detail(
            session_id,
            assembly_id=record.assembly_id,
            detail_ref=record.detail_ref,
            checkpoint_ns=checkpoint_ns,
        ):
            continue
        try:
            owner._storage.get_context_plan_detail(
                session_id, detail_ref=record.detail_ref, checkpoint_ns=checkpoint_ns
            )
        except KeyError:
            # detail 写成功但注册失败；不存在 registry 行时只清理本次文件。
            pass
        else:
            # tombstone owner 在同一写锁内再检查引用；若刚完成 seal，抛错
            # 留存文件，不能依赖上方可能已经过期的查询结果继续删除。
            owner._storage.mark_context_plan_details_unavailable(
                session_id,
                detail_refs=(record.detail_ref,),
                checkpoint_ns=checkpoint_ns,
            )
        owner._detail_store.remove(session_id=session_id, record=record)


def record_failed_seal(
    owner,
    session_id: str,
    plan_id: str,
    seal_key: str,
    checkpoint_ns: str,
    error: BaseException,
) -> None:
    registered = owner.get_context_plan_registration(
        session_id, plan_id=plan_id, checkpoint_ns=checkpoint_ns
    )
    # 提交成功后返回路径抛错不等于 seal 失败，不能改写已提交结果。
    if registered.plan_state == "sealed":
        return
    error_code = str(error).split(":", 1)[0]
    if error_code not in {
        "source-mismatch",
        "detail-unavailable",
        "plan-order-integrity",
        "plan-hash-mismatch",
        "request-hash-mismatch",
    }:
        error_code = "seal-storage-failure"
    owner._storage.record_context_plan_seal_failure(
        session_id,
        plan_id=plan_id,
        seal_idempotency_key=seal_key,
        error_code=error_code,
        checkpoint_ns=checkpoint_ns,
    )
