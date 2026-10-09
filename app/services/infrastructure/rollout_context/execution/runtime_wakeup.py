"""ContextStore runtime wakeup owner：仅保存请求引用与调度状态。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict

from app.core.sqlite_state import utc_now_text
from app.domain.itemized.mutation_intents import ApplySourceLifecycleDecision
from app.domain.itemized.records import CanonicalItemRecord, InternalDisplayProvenance
from app.domain.itemized.runtime_wakeup import RuntimeExecutionWakeup
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)

_COLUMNS = tuple(RuntimeExecutionWakeup.__dataclass_fields__)


class RuntimeWakeupStorageMixin:
    """source/admission/wakeup 使用同一 ContextStore transaction。"""

    def commit_runtime_source_wakeup(
        self,
        decision: ApplySourceLifecycleDecision,
        item: CanonicalItemRecord,
        wakeup: RuntimeExecutionWakeup,
    ) -> RuntimeExecutionWakeup:
        if (
            wakeup.session_id != decision.owner.session_id
            or wakeup.thread_id != decision.owner.thread_id
            or wakeup.source_item_id != item.item_id
            or wakeup.source_id != decision.source_id
            or decision.execution_id != wakeup.execution_id
            or decision.decision_kind != "observe_pending"
            or not decision.pending_only
        ):
            raise ValueError("runtime source/wakeup owner、input 或 execution identity 不一致")
        with self._execution_input_transaction(wakeup.session_id, wakeup.thread_id) as connection:
            values = asdict(wakeup)
            existing = connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM runtime_execution_wakeups "
                "WHERE admission_idempotency_key = ?", (wakeup.admission_idempotency_key,),
            ).fetchone()
            if existing is not None:
                frozen = RuntimeExecutionWakeup(**dict(zip(_COLUMNS, existing, strict=True)))
                # Trace 与 Agent 由首次准入冻结；正文和 source 仍必须完全相同。
                for column in _COLUMNS:
                    if column not in {"gateway_id", "request_id", "agent_id"} and getattr(frozen, column) != values[column]:
                        raise ValueError("runtime wakeup 幂等请求 identity 漂移")
                self._admit_internal_execution_transaction(
                    connection, wakeup.session_id, thread_id=wakeup.thread_id,
                    execution_id=wakeup.execution_id, job_id=wakeup.job_id,
                    admission_idempotency_key=wakeup.admission_idempotency_key,
                    runtime_notice_item=item,
                    request_input={"kind": "runtime_notice", "payload": item.payload},
                )
                return frozen
            self._admit_internal_execution_transaction(
                connection, wakeup.session_id, thread_id=wakeup.thread_id,
                execution_id=wakeup.execution_id, job_id=wakeup.job_id,
                admission_idempotency_key=wakeup.admission_idempotency_key,
                runtime_notice_item=item,
                request_input={"kind": "runtime_notice", "payload": item.payload},
            )
            self._context.checkpoints._apply_source_lifecycle_decision_transaction(connection, decision, item)
            expected = tuple(values[column] for column in _COLUMNS)
            connection.execute(
                f"INSERT INTO runtime_execution_wakeups ({','.join(_COLUMNS)},state,updated_at) "
                f"VALUES ({','.join('?' for _ in _COLUMNS)},'pending',?)",
                (*expected, utc_now_text()),
            )
            return wakeup

    def runtime_wakeup_for_admission(
        self, session_id: str, thread_id: str, admission_idempotency_key: str,
    ) -> RuntimeExecutionWakeup | None:
        """按准入键读取首次冻结的 wakeup，不从当前配置补造执行参数。"""
        try:
            with self._storage.read_connection(session_id, thread_id, "") as connection:
                self._storage._require_v2_runtime(connection)
                row = connection.execute(
                    f"SELECT {','.join(_COLUMNS)} FROM runtime_execution_wakeups "
                    "WHERE session_id=? AND thread_id=? AND admission_idempotency_key=?",
                    (session_id, thread_id, admission_idempotency_key),
                ).fetchone()
        except RolloutNotMaterializedError:
            return None
        return None if row is None else RuntimeExecutionWakeup(**dict(zip(_COLUMNS, row, strict=True)))

    def pending_runtime_wakeups(
        self, session_id: str, thread_id: str,
    ) -> tuple[RuntimeExecutionWakeup, ...]:
        try:
            with self._storage.read_connection(session_id, thread_id, "") as connection:
                rows = connection.execute(
                    f"SELECT {','.join(_COLUMNS)} FROM runtime_execution_wakeups "
                    "WHERE state = 'pending' OR execution_id IN "
                    "(SELECT execution_id FROM executions WHERE outcome = 'unknown') "
                    "ORDER BY message_created_at, admission_idempotency_key",
                ).fetchall()
        except RolloutNotMaterializedError:
            return ()
        return tuple(RuntimeExecutionWakeup(**dict(zip(_COLUMNS, row, strict=True))) for row in rows)

    def runtime_wakeup_input(self, wakeup: RuntimeExecutionWakeup) -> CanonicalItemRecord:
        with self._storage.read_connection(wakeup.session_id, wakeup.thread_id, "") as connection:
            self._storage._require_v2_runtime(connection)
            return self._storage._read_canonical_item_from_catalog(
                connection, session_id=wakeup.session_id, thread_id=wakeup.thread_id,
                checkpoint_ns="", item_id=wakeup.source_item_id,
            )

    def internal_execution_request_input(self, session_id: str, thread_id: str, execution_id: str) -> dict[str, object]:
        with self._storage.read_connection(session_id, thread_id, "") as connection:
            row = connection.execute(
                "SELECT job_id,admission_idempotency_key,admission_payload_hash FROM executions "
                "WHERE session_id=? AND thread_id=? AND execution_id=? "
                "AND execution_origin='internal' AND turn_id IS NULL",
                (session_id, thread_id, execution_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"internal execution 不存在: {execution_id}")
            preimage = self._context.forks._validate_fork_internal_admission(
                connection, session_id=session_id, thread_id=thread_id, checkpoint_ns="",
                execution_id=execution_id, job_id=row[0], admission_key=row[1], admission_hash=row[2],
            )
            return dict(preimage["input"])

    def execution_output(self, session_id: str, thread_id: str, execution_id: str) -> tuple[str, InternalDisplayProvenance]:
        with self._storage.read_connection(session_id, thread_id, "") as connection:
            self._storage._require_v2_runtime(connection)
            if connection.execute(
                "SELECT 1 FROM executions WHERE session_id=? AND thread_id=? AND execution_id=?",
                (session_id, thread_id, execution_id),
            ).fetchone() is None:
                raise KeyError(f"execution 输出来源不存在: {execution_id}")
            rows = connection.execute(
                "SELECT item_id FROM item_catalog WHERE execution_id = ? "
                "AND semantic_kind = 'assistant_output' ORDER BY item_sequence", (execution_id,),
            ).fetchall()
            items = tuple(self._storage._read_canonical_item_from_catalog(
                connection, session_id=session_id, thread_id=thread_id, checkpoint_ns="", item_id=str(row[0]),
            ) for row in rows)
        codec = self._storage._codec()
        return (
            "\n".join(codec.visible_text(codec.project_message((item,))) for item in items),
            InternalDisplayProvenance(
                source_session_id=session_id, source_thread_id=thread_id,
                source_execution_id=execution_id,
                source_item_id=items[-1].item_id if items else None,
            ),
        )

    def cold_execution_observations(self, session_id: str, thread_id: str, *, job_ids: list[str] | None = None, turn_id: str | None = None) -> list[dict[str, str]]:
        """只读 execution owner；不 initialize、不物化 Agent，不列举 JobService。"""
        root = self._storage.root(session_id, thread_id=thread_id)
        if not (root / "index.sqlite").is_file():
            return []
        clauses = ["session_id=?", "thread_id=?"]
        params: list[object] = [session_id, thread_id]
        if job_ids is not None:
            if not job_ids:
                return []
            clauses.append("job_id IN (" + ",".join("?" for _ in job_ids) + ")")
            params.extend(job_ids)
        if turn_id is not None:
            clauses.append("turn_id=?")
            params.append(turn_id)
        with self._storage._connect(thread_id, "", session_id=session_id, read_only=True) as connection:
            self._storage._require_v2_runtime(connection)
            rows = connection.execute(
                "SELECT job_id,execution_id,outcome,updated_at,last_model_call_id FROM executions WHERE " + " AND ".join(clauses) + " ORDER BY execution_ordinal DESC,attempt DESC,created_at DESC,execution_id", params,
            ).fetchall()
        if turn_id is not None:
            rows = rows[:1]
        states = {"pending": "pending", "running": "running", "completed": "completed", "completed_empty": "completed", "failed": "failed", "cancelled": "cancelled", "interrupted": "cancelled", "execution_lost": "failed"}
        result = []
        for job_id, execution_id, outcome, updated_at, last_model_call_id in rows:
            if outcome == "unknown":
                state = "pending" if last_model_call_id is None else "running"
            else:
                state = states.get(outcome)
            if state is None:
                raise RuntimeError(f"未知 execution wait outcome: {outcome!r}")
            result.append({"job_id": job_id, "execution_id": execution_id, "state": state, "revision": f"{execution_id}:{outcome}:{updated_at}:{last_model_call_id}"})
        return result

    def runtime_execution_outcome(self, session_id: str, thread_id: str, execution_id: str) -> str:
        with self._storage._connect(thread_id, "", session_id=session_id, read_only=True) as connection:
            row = connection.execute(
                "SELECT outcome FROM executions WHERE session_id = ? AND thread_id = ? AND execution_id = ?",
                (session_id, thread_id, execution_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"runtime execution admission 不存在: {execution_id}")
            return str(row[0])

    def claim_runtime_wakeup(self, wakeup: RuntimeExecutionWakeup) -> int:
        with self._execution_input_transaction(wakeup.session_id, wakeup.thread_id) as connection:
            cursor = connection.execute(
                "UPDATE runtime_execution_wakeups SET dispatch_generation = dispatch_generation + 1 "
                "WHERE admission_idempotency_key = ? AND execution_id = ? AND job_id = ?",
                (wakeup.admission_idempotency_key, wakeup.execution_id, wakeup.job_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("runtime wakeup claim 没有命中冻结 identity")
            return int(connection.execute(
                "SELECT dispatch_generation FROM runtime_execution_wakeups WHERE admission_idempotency_key = ?",
                (wakeup.admission_idempotency_key,),
            ).fetchone()[0])

    def finish_runtime_wakeup(
        self, wakeup: RuntimeExecutionWakeup, *, dispatch_generation: int,
        receipt: Mapping[str, object] | None = None, error: str | None = None,
    ) -> None:
        with self._execution_input_transaction(wakeup.session_id, wakeup.thread_id) as connection:
            cursor = connection.execute(
                "UPDATE runtime_execution_wakeups SET state = ?, last_error = ?, updated_at = ?, "
                "dispatch_receipt_json = COALESCE(?, dispatch_receipt_json) "
                "WHERE admission_idempotency_key = ? AND execution_id = ? AND job_id = ? "
                "AND dispatch_generation = ?",
                ("pending" if error is not None else "dispatched", error, utc_now_text(),
                 json.dumps(dict(receipt), ensure_ascii=False, sort_keys=True) if receipt is not None else None,
                 wakeup.admission_idempotency_key, wakeup.execution_id, wakeup.job_id, dispatch_generation),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("runtime wakeup dispatch callback 已失效（generation CAS 失败）")


__all__ = ["RuntimeWakeupStorageMixin"]
