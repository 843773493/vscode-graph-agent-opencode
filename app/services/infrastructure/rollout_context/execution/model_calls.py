"""Rollout v2 Turn/execution/model-call 持久化 owner。

这些 mixin 只依赖 RolloutContextStore 提供的 SQLite、JSONL transaction 和 domain
ports；它们不承担 LangChain/provider projection，也不读取 v1 数据。
"""

from __future__ import annotations

import json
import sqlite3

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.enums import (
    ControlOutcome,
    TurnStatus,
)
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.runtime import ModelCallRecord
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)

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

    def read_resumable_model_call(
        self, admission: ExecutionAdmission, model_call_id: str, *, carrier_items: tuple[CanonicalItemRecord, ...],
    ) -> tuple[ModelCallRecord, str] | None:
        """只读尚无 terminal convergence 的原调用及 committed completed carrier。"""
        if not carrier_items:
            return None
        with self._storage.read_connection(admission.session_id, admission.thread_id, "") as connection:
            self._storage._require_v2_runtime(connection)
            row = connection.execute(
                "SELECT e.outcome,e.last_model_call_id,m.attempt,m.provider,m.outcome,"
                "m.assembly_id,a.status,a.execution_id,a.model_call_id,m.dispatch_state,"
                "a.outcome,a.terminal_at,a.request_hash,tr.status,tr.last_execution_id,"
                "e.job_id,e.turn_id,e.admission_idempotency_key,e.execution_origin,a.turn_id "
                "FROM executions e JOIN model_calls m USING(session_id,thread_id,execution_id) "
                "JOIN context_assemblies a USING(session_id,thread_id,assembly_id) "
                "LEFT JOIN turn_records tr ON tr.session_id=e.session_id AND tr.thread_id=e.thread_id AND tr.turn_id=e.turn_id "
                "WHERE e.session_id=? AND e.thread_id=? AND e.execution_id=? AND m.model_call_id=?",
                (admission.session_id, admission.thread_id, admission.execution_id, model_call_id),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"pending tool 的 ModelCall/assembly 绑定缺失: {model_call_id}")
            if row[15:20] != (admission.job_id, admission.turn_id, admission.admission_idempotency_key, admission.execution_origin, admission.turn_id):
                raise RuntimeError(f"pending tool 的 admission/assembly owner 漂移: {model_call_id}")
            terminal = connection.execute(
                "SELECT 1 FROM storage_commits WHERE commit_kind='terminal_convergence' AND status='committed' "
                "AND subject_id IN (?,?) LIMIT 1", (admission.execution_id, admission.turn_id),
            ).fetchone()
            if terminal is not None or row[0] != "unknown" or row[1] != model_call_id or row[4] != "unknown":
                return None
            if row[6:9] != ("sealed", admission.execution_id, model_call_id):
                raise RuntimeError(f"pending tool 的 sealed assembly owner 漂移: {model_call_id}")
            if row[9] != "dispatched" or row[10] not in {None, "unknown"} or row[11] is not None:
                return None
            strict_text(row[12], field="context_assemblies.request_hash")
            if admission.turn_id is not None and (row[13] not in {"open", "active"} or row[14] != admission.execution_id):
                return None
            # 固定 checkpoint 声明的消息组必须全员匹配；同 call 的 stream sink
            # 不是该 checkpoint 的成员，不能混入比较或用传入的 ID 子集自证完整。
            carrier = connection.execute(
                "SELECT i.semantic_kind,i.status,i.execution_id,i.turn_id,i.item_id,c.status,i.content_hash,i.projection_manifest_json "
                "FROM item_catalog i JOIN storage_commits c USING(commit_id) "
                "WHERE json_extract(i.projection_manifest_json,'$.projection_identity.model_call_id')=? "
                "AND i.message_group_id=? "
                "AND i.semantic_kind IN ('reasoning','assistant_output','tool_call') ORDER BY i.item_sequence",
                (model_call_id, carrier_items[-1].message_group_id),
            ).fetchall()
            if not carrier or any(value[1:4] != ("completed", admission.execution_id, admission.turn_id) or value[5] != "committed" for value in carrier):
                return None
            expected = tuple((item.semantic_kind, item.item_id, item.content_hash, item.projection_group.to_dict() if item.projection_group is not None else None) for item in carrier_items)
            committed = tuple((value[0], value[4], value[6], json.loads(value[7]).get("projection_group")) for value in carrier)
            if expected != committed or not any(item.semantic_kind == "tool_call" for item in carrier_items):
                return None
        return ModelCallRecord(
            session_id=admission.session_id, thread_id=admission.thread_id,
            model_call_id=model_call_id, execution_id=admission.execution_id,
            attempt=row[2], provider=row[3], outcome=row[4],
        ), strict_text(row[5], field="model_calls.assembly_id")

    def register_model_call(
        self,
        session_id: str,
        *,
        thread_id: str,
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
        session_id = strict_text(session_id, field="session_id")
        owner_thread_id = self._storage.fork_paths.thread_identity(session_id, strict_text(thread_id, field="thread_id"))
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
        with self._storage._lock(owner_thread_id, checkpoint_ns, session_id=session_id):
            self._storage.initialize(
                session_id, checkpoint_ns, thread_id=owner_thread_id
            )
            with self._storage._connect(
                owner_thread_id, checkpoint_ns, session_id=session_id
            ) as connection:
                self._storage._require_v2_runtime(connection)
                existing = connection.execute(
                    "SELECT execution_id, attempt, provider, provider_request_id, "
                    "retry_of_model_call_id, assembly_id, dispatch_state, outcome "
                    "FROM model_calls WHERE session_id = ? AND thread_id = ? AND model_call_id = ?",
                    (session_id, owner_thread_id, model_call_id),
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
                    SELECT e.turn_id, e.execution_origin, e.outcome, tr.status
                    FROM executions AS e
                    LEFT JOIN turn_records AS tr
                      ON tr.session_id = e.session_id
                     AND tr.thread_id = e.thread_id
                     AND tr.turn_id = e.turn_id
                    WHERE e.session_id = ? AND e.thread_id = ? AND e.execution_id = ?
                    """,
                    (session_id, owner_thread_id, execution_id),
                ).fetchone()
                if execution is None:
                    raise KeyError(f"execution 不存在: {execution_id}")
                execution_turn_id = strict_optional_text(
                    execution[0], field=f"executions.turn_id: {execution_id}"
                )
                execution_origin = strict_text(
                    execution[1], field=f"executions.execution_origin: {execution_id}"
                )
                if execution_origin == "user_turn":
                    if execution_turn_id is None:
                        raise RuntimeError(
                            f"user_turn execution 缺少 Turn: {execution_id}"
                        )
                    execution_status = strict_text(
                        execution[3],
                        field=f"turn_records.status: {execution_id}",
                    )
                    if execution_status not in {
                        TurnStatus.OPEN.value,
                        TurnStatus.ACTIVE.value,
                    }:
                        raise ValueError(
                            "turn_not_resumable: model call 不能绑定 terminal Turn: "
                            f"status={execution_status}"
                        )
                elif execution_origin == "internal":
                    if execution_turn_id is not None:
                        raise RuntimeError(
                            f"internal execution 不得绑定 Turn: {execution_id}"
                        )
                    if strict_text(
                        execution[2], field=f"executions.outcome: {execution_id}"
                    ) != "unknown":
                        raise ValueError(
                            "internal execution 已终态，不能登记新的 model call: "
                            f"execution_id={execution_id}"
                        )
                else:
                    raise RuntimeError(
                        f"execution_origin 非法: {execution_origin!r}"
                    )
                retry_assembly_id: str | None = None
                retry_outcome: str | None = None
                retry_dispatch_state: str | None = None
                if retry_of_model_call_id is not None:
                    retry = connection.execute(
                        "SELECT execution_id, attempt, assembly_id, outcome, dispatch_state "
                        "FROM model_calls WHERE session_id = ? AND thread_id = ? AND model_call_id = ?",
                        (session_id, owner_thread_id, retry_of_model_call_id),
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
                    retry_assembly_id = strict_text(
                        retry[2],
                        field=f"model_calls.assembly_id: {retry_of_model_call_id}",
                    )
                    retry_outcome = strict_text(
                        retry[3],
                        field=f"model_calls.outcome: {retry_of_model_call_id}",
                    )
                    retry_dispatch_state = strict_text(
                        retry[4],
                        field=(
                            "model_calls.dispatch_state: "
                            f"{retry_of_model_call_id}"
                        ),
                    )
                    if retry_execution_id != execution_id or retry_attempt >= attempt:
                        raise ValueError(
                            "retry_of_model_call_id 不是同一 execution 的旧 attempt"
                        )
                assembly = connection.execute(
                    "SELECT session_id, thread_id, turn_id, execution_id, status, model_call_id "
                    "FROM context_assemblies WHERE session_id = ? AND thread_id = ? AND assembly_id = ?",
                    (session_id, owner_thread_id, assembly_id),
                ).fetchone()
                if assembly is None:
                    raise KeyError(f"sealed assembly 不存在: {assembly_id}")
                assembly_session_id = strict_text(
                    assembly[0], field=f"context_assemblies.session_id: {assembly_id}"
                )
                assembly_turn_id = strict_optional_text(
                    assembly[2], field=f"context_assemblies.turn_id: {assembly_id}"
                )
                assembly_execution_id = strict_text(
                    assembly[3], field=f"context_assemblies.execution_id: {assembly_id}"
                )
                assembly_status = strict_text(
                    assembly[4], field=f"context_assemblies.status: {assembly_id}"
                )
                assembly_model_call_id = strict_optional_text(
                    assembly[5], field=f"context_assemblies.model_call_id: {assembly_id}"
                )
                reuses_failed_retry_assembly = (
                    retry_of_model_call_id is not None
                    and assembly_model_call_id == retry_of_model_call_id
                    and retry_assembly_id == assembly_id
                    and retry_outcome == "failed"
                    and retry_dispatch_state == "failed"
                )
                if (
                    (assembly_session_id, strict_text(assembly[1], field=f"context_assemblies.thread_id: {assembly_id}"))
                    != (session_id, owner_thread_id)
                    or assembly_turn_id != execution_turn_id
                    or assembly_execution_id != execution_id
                    or assembly_status != "sealed"
                    or (
                        assembly_model_call_id not in {None, model_call_id}
                        and not reuses_failed_retry_assembly
                    )
                ):
                    raise ValueError("model_call 与 sealed assembly 关联冲突")
                timestamp = _now()
                try:
                    inserted = connection.execute(
                        "INSERT INTO model_calls(model_call_id, session_id, thread_id, execution_id, attempt, attempt_ordinal, provider, provider_request_id, retry_of_model_call_id, assembly_id, dispatch_state, outcome, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?)",
                        (
                            model_call_id,
                            session_id,
                            owner_thread_id,
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
                    "UPDATE executions SET first_model_call_id = COALESCE(first_model_call_id, ?), last_model_call_id = ?, updated_at = ? WHERE session_id = ? AND thread_id = ? AND execution_id = ?",
                    (
                        model_call_id,
                        model_call_id,
                        timestamp,
                        session_id,
                        owner_thread_id,
                        execution_id,
                    ),
                )
                if execution_update.rowcount != 1:
                    raise RuntimeError("execution 的 model-call 指针更新失败")
                assembly_update = connection.execute(
                    "UPDATE context_assemblies SET model_call_id = ? WHERE session_id = ? AND thread_id = ? AND assembly_id = ?",
                    (model_call_id, session_id, owner_thread_id, assembly_id),
                )
                if assembly_update.rowcount != 1:
                    raise RuntimeError("assembly 的 model-call 指针更新失败")
                if execution_origin == "user_turn":
                    connection.execute(
                        "UPDATE turn_records SET status='active', updated_at=? "
                        "WHERE session_id=? AND thread_id=? AND turn_id=? AND status='open'",
                        (timestamp, session_id, owner_thread_id, execution_turn_id),
                    )
                    connection.execute(
                        "UPDATE turns SET status='running', updated_at=? WHERE turn_id=? AND status='accepted'",
                        (timestamp, execution_turn_id),
                    )
                connection.commit()

    def update_model_call_outcome(
        self,
        session_id: str,
        *,
        thread_id: str,
        model_call_id: str,
        outcome: str,
        dispatch_state: str | None = None,
        checkpoint_ns: str = "",
    ) -> None:
        """原子更新已登记 model call 的控制 outcome。"""
        session_id = strict_text(session_id, field="session_id")
        owner_thread_id = self._storage.fork_paths.thread_identity(session_id, strict_text(thread_id, field="thread_id"))
        model_call_id = strict_text(model_call_id, field="model_call_id")
        outcome = strict_text(outcome, field="model_call.outcome")
        dispatch_state = strict_optional_text(
            dispatch_state, field="model_call.dispatch_state"
        )
        if outcome not in {value.value for value in ControlOutcome}:
            raise ValueError(f"未知 model call outcome: {outcome}")
        with self._storage._lock(owner_thread_id, checkpoint_ns, session_id=session_id):
            self._storage.initialize(
                session_id, checkpoint_ns, thread_id=owner_thread_id
            )
            with self._storage._connect(
                owner_thread_id, checkpoint_ns, session_id=session_id
            ) as connection:
                self._storage._require_v2_runtime(connection)
                row = connection.execute(
                    "SELECT outcome, dispatch_state FROM model_calls WHERE session_id = ? AND thread_id = ? AND model_call_id = ?",
                    (session_id, owner_thread_id, model_call_id),
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
                    "UPDATE model_calls SET outcome = ?, dispatch_state = ? WHERE session_id = ? AND thread_id = ? AND model_call_id = ?",
                    (outcome, target_dispatch, session_id, owner_thread_id, model_call_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("model_call outcome 更新失败")
                connection.commit()
