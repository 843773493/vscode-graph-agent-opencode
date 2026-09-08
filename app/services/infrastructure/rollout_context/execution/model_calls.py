"""Rollout v2 Turn/execution/model-call 持久化 owner。

这些 mixin 只依赖 RolloutStorage 提供的 SQLite、JSONL transaction 和 domain
ports；它们不承担 LangChain/provider projection，也不读取 v1 数据。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app.domain.itemized.enums import (
    ControlOutcome,
    TurnStatus,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


_V2_TO_HISTORY_STATUS = {
    TurnStatus.OPEN.value: "accepted",
    TurnStatus.ACTIVE.value: "running",
    TurnStatus.COMPLETED.value: "completed",
    TurnStatus.COMPLETED_EMPTY.value: "completed",
    TurnStatus.INTERRUPTED.value: "timed_out",
    TurnStatus.CANCELLED.value: "cancelled",
    TurnStatus.FAILED.value: "failed",
    TurnStatus.UNKNOWN.value: "failed",
}


class RolloutModelCallsMixin:
    """Provider model-call identity/outcome owner。"""

    def register_model_call(
        self,
        thread_id: str,
        *,
        execution_id: str,
        model_call_id: str,
        attempt: int,
        provider: str,
        provider_request_id: str | None = None,
        retry_of_model_call_id: str | None = None,
        assembly_id: str | None = None,
        dispatch_state: str = "ready",
        checkpoint_ns: str = "",
    ) -> None:
        """登记 provider call；provider request id 只作审计，不进入 request hash。"""
        model_call_id = strict_text(model_call_id, field="model_call_id")
        execution_id = strict_text(execution_id, field="execution_id")
        provider = strict_text(provider, field="model_call.provider")
        provider_request_id = strict_optional_text(
            provider_request_id, field="model_call.provider_request_id"
        )
        retry_of_model_call_id = strict_optional_text(
            retry_of_model_call_id, field="model_call.retry_of_model_call_id"
        )
        assembly_id = strict_text(assembly_id, field="model_call.assembly_id")
        dispatch_state = strict_text(
            dispatch_state, field="model_call.dispatch_state"
        )
        if (
            not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt <= 0
        ):
            raise ValueError("model call identity/provider/attempt 非法")
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                existing = connection.execute(
                    "SELECT execution_id, attempt, provider, provider_request_id, "
                    "retry_of_model_call_id, assembly_id, dispatch_state, outcome "
                    "FROM model_calls WHERE model_call_id = ?",
                    (model_call_id,),
                ).fetchone()
                if existing is not None:
                    existing_execution_id = strict_text(
                        existing[0], field=f"model_calls.execution_id: {model_call_id}"
                    )
                    existing_attempt = strict_non_negative_int(
                        existing[1], field=f"model_calls.attempt: {model_call_id}"
                    )
                    if existing_attempt == 0:
                        raise RuntimeError(
                            f"model_calls.attempt 必须为正数: {model_call_id}"
                        )
                    existing_provider = strict_text(
                        existing[2], field=f"model_calls.provider: {model_call_id}"
                    )
                    existing_provider_request_id = strict_optional_text(
                        existing[3],
                        field=f"model_calls.provider_request_id: {model_call_id}",
                    )
                    existing_retry_id = strict_optional_text(
                        existing[4],
                        field=f"model_calls.retry_of_model_call_id: {model_call_id}",
                    )
                    existing_assembly_id = strict_text(
                        existing[5], field=f"model_calls.assembly_id: {model_call_id}"
                    )
                    strict_text(
                        existing[6], field=f"model_calls.dispatch_state: {model_call_id}"
                    )
                    strict_text(
                        existing[7], field=f"model_calls.outcome: {model_call_id}"
                    )
                    if (
                        existing_execution_id,
                        existing_attempt,
                        existing_provider,
                        existing_provider_request_id,
                        existing_retry_id,
                        existing_assembly_id,
                    ) == (
                        execution_id,
                        attempt,
                        provider,
                        provider_request_id,
                        retry_of_model_call_id,
                        assembly_id,
                    ):
                        return
                    raise ValueError(
                        "model_call_id 已存在但 execution/attempt/provider/"
                        "request/retry/assembly identity 冲突"
                    )
                if dispatch_state not in {
                    "ready",
                    "dispatched",
                    "completed",
                    "failed",
                    "unknown",
                }:
                    raise ValueError(
                        f"未知 model call dispatch_state: {dispatch_state}"
                    )
                execution = connection.execute(
                    """
                    SELECT e.turn_id, tr.status
                    FROM executions AS e
                    JOIN turn_records AS tr ON tr.turn_id = e.turn_id
                    WHERE e.execution_id = ?
                    """,
                    (execution_id,),
                ).fetchone()
                if execution is None:
                    raise KeyError(f"execution 不存在: {execution_id}")
                execution_turn_id = strict_text(
                    execution[0], field=f"executions.turn_id: {execution_id}"
                )
                execution_status = strict_text(
                    execution[1], field=f"turn_records.status: {execution_id}"
                )
                if execution_status not in {
                    TurnStatus.OPEN.value,
                    TurnStatus.ACTIVE.value,
                }:
                    raise ValueError(
                        "turn_not_resumable: model call 不能绑定 terminal Turn: "
                        f"status={execution[1]}"
                    )
                assembly = connection.execute(
                    "SELECT session_id, turn_id, execution_id, status, model_call_id FROM context_assemblies WHERE assembly_id = ?",
                    (assembly_id,),
                ).fetchone()
                if assembly is None:
                    raise KeyError(f"sealed assembly 不存在: {assembly_id}")
                assembly_session_id = strict_text(
                    assembly[0], field=f"context_assemblies.session_id: {assembly_id}"
                )
                assembly_turn_id = strict_text(
                    assembly[1], field=f"context_assemblies.turn_id: {assembly_id}"
                )
                assembly_execution_id = strict_text(
                    assembly[2], field=f"context_assemblies.execution_id: {assembly_id}"
                )
                assembly_status = strict_text(
                    assembly[3], field=f"context_assemblies.status: {assembly_id}"
                )
                assembly_model_call_id = strict_optional_text(
                    assembly[4], field=f"context_assemblies.model_call_id: {assembly_id}"
                )
                if (
                    assembly_session_id != thread_id
                    or assembly_turn_id != execution_turn_id
                    or assembly_execution_id != execution_id
                    or assembly_status != "sealed"
                    or assembly_model_call_id not in {None, model_call_id}
                ):
                    raise ValueError("model_call 与 sealed assembly 关联冲突")
                if retry_of_model_call_id is not None:
                    retry = connection.execute(
                        "SELECT execution_id, attempt FROM model_calls WHERE model_call_id = ?",
                        (retry_of_model_call_id,),
                    ).fetchone()
                    if retry is None:
                        raise ValueError(
                            "retry_of_model_call_id 不是同一 execution 的旧 attempt"
                        )
                    retry_execution_id = strict_text(
                        retry[0],
                        field=f"model_calls.execution_id: {retry_of_model_call_id}",
                    )
                    retry_attempt = strict_non_negative_int(
                        retry[1],
                        field=f"model_calls.attempt: {retry_of_model_call_id}",
                    )
                    if retry_execution_id != execution_id or retry_attempt >= attempt:
                        raise ValueError(
                            "retry_of_model_call_id 不是同一 execution 的旧 attempt"
                        )
                timestamp = _now()
                try:
                    inserted = connection.execute(
                        "INSERT INTO model_calls(model_call_id, execution_id, attempt, attempt_ordinal, provider, provider_request_id, retry_of_model_call_id, assembly_id, dispatch_state, outcome, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?)",
                        (
                            model_call_id,
                            execution_id,
                            attempt,
                            attempt,
                            provider,
                            provider_request_id,
                            retry_of_model_call_id,
                            assembly_id,
                            dispatch_state,
                            timestamp,
                        ),
                    )
                    if inserted.rowcount != 1:
                        raise RuntimeError("model_calls 插入未产生恰好一条记录")
                except sqlite3.IntegrityError as error:
                    raise ValueError(
                        "同一 execution 的 model-call attempt identity 冲突"
                    ) from error
                execution_update = connection.execute(
                    "UPDATE executions SET first_model_call_id = COALESCE(first_model_call_id, ?), last_model_call_id = ? WHERE execution_id = ?",
                    (model_call_id, model_call_id, execution_id),
                )
                if execution_update.rowcount != 1:
                    raise RuntimeError("execution 的 model-call 指针更新失败")
                assembly_update = connection.execute(
                    "UPDATE context_assemblies SET model_call_id = ? WHERE assembly_id = ?",
                    (model_call_id, assembly_id),
                )
                if assembly_update.rowcount != 1:
                    raise RuntimeError("assembly 的 model-call 指针更新失败")
                connection.commit()

    def update_model_call_outcome(
        self,
        thread_id: str,
        *,
        model_call_id: str,
        outcome: str,
        dispatch_state: str | None = None,
        checkpoint_ns: str = "",
    ) -> None:
        """原子更新已登记 model call 的控制 outcome。"""
        model_call_id = strict_text(model_call_id, field="model_call_id")
        outcome = strict_text(outcome, field="model_call.outcome")
        dispatch_state = strict_optional_text(
            dispatch_state, field="model_call.dispatch_state"
        )
        if outcome not in {value.value for value in ControlOutcome}:
            raise ValueError(f"未知 model call outcome: {outcome}")
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                row = connection.execute(
                    "SELECT outcome, dispatch_state FROM model_calls WHERE model_call_id = ?",
                    (model_call_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"model_call 不存在: {model_call_id}")
                stored_outcome = strict_text(
                    row[0], field=f"model_calls.outcome: {model_call_id}"
                )
                stored_dispatch = strict_text(
                    row[1], field=f"model_calls.dispatch_state: {model_call_id}"
                )
                target_dispatch = dispatch_state or stored_dispatch
                if target_dispatch not in {
                    "ready",
                    "dispatched",
                    "completed",
                    "failed",
                    "unknown",
                }:
                    raise ValueError(
                        f"未知 model call dispatch_state: {target_dispatch}"
                    )
                allowed_dispatch = {
                    ControlOutcome.COMPLETED.value: {"completed"},
                    ControlOutcome.COMPLETED_EMPTY.value: {"completed"},
                    ControlOutcome.FAILED.value: {"failed"},
                    ControlOutcome.INTERRUPTED.value: {"failed", "unknown"},
                    ControlOutcome.CANCELLED.value: {"failed"},
                    ControlOutcome.EXECUTION_LOST.value: {"unknown"},
                    ControlOutcome.UNKNOWN.value: {"unknown", "ready", "dispatched"},
                }
                if target_dispatch not in allowed_dispatch[outcome]:
                    raise ValueError(
                        "model_call outcome/dispatch_state 不匹配: "
                        f"outcome={outcome}, dispatch_state={target_dispatch}"
                    )
                if stored_outcome not in {"unknown", outcome}:
                    raise ValueError("model_call outcome 已收敛且不可逆")
                updated = connection.execute(
                    "UPDATE model_calls SET outcome = ?, dispatch_state = ? WHERE model_call_id = ?",
                    (outcome, target_dispatch, model_call_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("model_call outcome 更新失败")
                connection.commit()
