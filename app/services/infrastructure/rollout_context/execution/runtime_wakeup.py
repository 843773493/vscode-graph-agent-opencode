"""ContextStore runtime wakeup owner：仅保存请求引用与调度状态。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict

from app.core.sqlite_state import utc_now_text
from app.domain.itemized.mutation_intents import ApplySourceLifecycleDecision
from app.domain.itemized.records import CanonicalItemRecord, InternalDisplayProvenance
from app.domain.itemized.runtime_wakeup import RuntimeExecutionWakeup
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
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
                    "WHERE session_id=? AND thread_id=? AND (state = 'pending' OR execution_id IN "
                    "(SELECT execution_id FROM executions WHERE outcome = 'unknown'))",
                    (session_id, thread_id),
                ).fetchall()
                ordered = []
                for row in rows:
                    wakeup = RuntimeExecutionWakeup(**dict(zip(_COLUMNS, row, strict=True)))
                    admission = self.execution_admission(
                        session_id, thread_id=thread_id, execution_id=wakeup.execution_id,
                        writer_connection=connection,
                    )
                    if (admission.job_id != wakeup.job_id
                            or admission.admission_idempotency_key != wakeup.admission_idempotency_key):
                        raise RuntimeError("runtime wakeup 与 canonical admission identity 不一致")
                    cursor = self.execution_admission_cursor(admission, connection=connection)
                    ordered.append((cursor.commit_id, wakeup))
        except RolloutNotMaterializedError:
            return ()
        return tuple(wakeup for _commit_id, wakeup in sorted(ordered, key=lambda entry: entry[0]))

    def runtime_wakeup_before_provider(
        self, session_id: str, thread_id: str, *, execution_id: str,
        job_id: str, admission_idempotency_key: str,
    ) -> RuntimeExecutionWakeup | None:
        """仅恢复尚无 Provider assembly 的原准入，不能据延迟的模型事件判断未派发。"""
        try:
            with self._storage.read_connection(session_id, thread_id, "") as connection:
                row = connection.execute(
                    f"SELECT {','.join(_COLUMNS)} FROM runtime_execution_wakeups "
                    "WHERE session_id=? AND thread_id=? AND admission_idempotency_key=?",
                    (session_id, thread_id, admission_idempotency_key),
                ).fetchone()
                if row is None:
                    return None
                wakeup = RuntimeExecutionWakeup(**dict(zip(_COLUMNS, row, strict=True)))
                admission = self.execution_admission(
                    session_id, thread_id=thread_id, execution_id=execution_id,
                    writer_connection=connection,
                )
                if (
                    (wakeup.execution_id, wakeup.job_id) != (execution_id, job_id)
                    or admission.execution_origin != "internal"
                    or (admission.job_id, admission.admission_idempotency_key)
                    != (job_id, admission_idempotency_key)
                ):
                    raise RuntimeError("未派发 runtime wakeup 与原 Job/admission identity 不一致")
                self.execution_admission_cursor(admission, connection=connection)
                state = connection.execute(
                    "SELECT outcome,first_model_call_id,last_model_call_id,"
                    "EXISTS(SELECT 1 FROM model_calls WHERE execution_id=?),"
                    "EXISTS(SELECT 1 FROM context_assemblies WHERE execution_id=?) "
                    "FROM executions WHERE execution_id=?",
                    (execution_id, execution_id, execution_id),
                ).fetchone()
                # assembly 在真实 handler 前封存；model-start 的消费可晚于网络请求。
                return wakeup if state == ("unknown", None, None, 0, 0) else None
        except RolloutNotMaterializedError:
            return None

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
                "SELECT session_id,thread_id,job_id,admission_idempotency_key,admission_payload_hash FROM executions "
                "WHERE execution_id=? "
                "AND execution_origin='internal' AND turn_id IS NULL",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"internal execution 不存在: {execution_id}")
            require_historical_scope(connection, row[0], row[1], current_owner=(session_id, thread_id))
            preimage = self.internal_execution_admission_preimage(
                connection, session_id=row[0], thread_id=row[1], checkpoint_ns="",
                execution_id=execution_id, job_id=row[2], admission_key=row[3], admission_hash=row[4],
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
        """只读当前 owner 的可观察执行；显式导入的合成历史不伪装成待运行 Job。"""
        root = self._storage.root(session_id, thread_id=thread_id)
        if not (root / "index.sqlite").is_file():
            return []
        clauses = [
            "execution.session_id=?", "execution.thread_id=?",
            (
                "NOT EXISTS(SELECT 1 FROM turn_acceptances acceptance "
                "JOIN turn_records turn ON turn.session_id=acceptance.session_id "
                "AND turn.thread_id=acceptance.thread_id AND turn.turn_id=acceptance.turn_id "
                "WHERE acceptance.session_id=execution.session_id "
                "AND acceptance.thread_id=execution.thread_id "
                "AND acceptance.identity_origin='legacy_synthetic' "
                "AND turn.initial_execution_id=execution.execution_id)"
            ),
            (
                "NOT EXISTS(SELECT 1 FROM item_catalog notice "
                "JOIN json_each(notice.projection_manifest_json,'$.provenance_relations') source "
                "WHERE execution.execution_origin='internal' "
                "AND notice.execution_id=execution.execution_id AND notice.semantic_kind='runtime_notice' "
                "AND json_extract(source.value,'$.relation')='derived_from' "
                "AND json_extract(source.value,'$.target_kind')='legacy_message')"
            ),
        ]
        params: list[object] = [session_id, thread_id]
        if job_ids is not None:
            if not job_ids:
                return []
            clauses.append("execution.job_id IN (" + ",".join("?" for _ in job_ids) + ")")
            params.extend(job_ids)
        if turn_id is not None:
            clauses.append("execution.turn_id=?")
            params.append(turn_id)
        with self._storage._connect(thread_id, "", session_id=session_id, read_only=True) as connection:
            self._storage._require_v2_runtime(connection)
            rows = connection.execute(
                "SELECT execution.job_id,execution.execution_id,execution.outcome,"
                "execution.updated_at,execution.last_model_call_id FROM executions execution WHERE "
                + " AND ".join(clauses)
                + " ORDER BY execution.execution_ordinal DESC,execution.attempt DESC,"
                "execution.created_at DESC,execution.execution_id", params,
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
