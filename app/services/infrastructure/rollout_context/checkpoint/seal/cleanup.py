"""独立 seal 失败审计；detail 文件回滚只由唯一 storage seal owner 执行。"""

from __future__ import annotations


def record_failed_seal(
    owner,
    session_id: str,
    thread_id: str,
    plan_id: str,
    seal_key: str,
    checkpoint_ns: str,
    error: BaseException,
) -> None:
    registered = owner.get_context_plan_registration(
        session_id,
        thread_id=thread_id,
        plan_id=plan_id,
        checkpoint_ns=checkpoint_ns,
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
    owner._storage.assemblies.record_context_plan_seal_failure(
        session_id,
        thread_id,
        plan_id=plan_id,
        seal_idempotency_key=seal_key,
        error_code=error_code,
        checkpoint_ns=checkpoint_ns,
    )
