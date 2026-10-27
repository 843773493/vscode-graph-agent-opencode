"""Thread 执行队列引用与原 admission 共用唯一 rollout 事务。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import TYPE_CHECKING

from app.core.sqlite_state import utc_now_text
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.domain.itemized.execution_queue import (
    ExecutionCompletion,
    ExecutionCompletionPage,
    ExecutionQueueInput,
    ExecutionQueueRecord,
    ExecutionQueueSnapshot,
)
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )


class RolloutExecutionQueueMixin:
    _storage: RolloutStorage

    def execution_completions(self, session_id: str, thread_id: str, *,
                              after_commit_id: int | None, limit: int = 100) -> ExecutionCompletionPage:
        """以原终态提交顺序增量读取；None只取得新订阅的起点。"""
        if limit < 1 or (after_commit_id is not None and after_commit_id < 0):
            raise ValueError("执行终态游标及页大小非法")
        try:
            with self._storage.read_connection(session_id, thread_id) as connection:
                upper = connection.execute("SELECT COALESCE(MAX(commit_id),0) FROM storage_commits WHERE status='committed'").fetchone()[0]
                if after_commit_id is None:
                    return ExecutionCompletionPage((), upper)
                if after_commit_id > upper:
                    raise RuntimeError("执行终态来源游标超过原owner历史，拒绝重置")
                rows = connection.execute(
                    "WITH terminals AS (SELECT e.execution_id,MIN(c.commit_id) AS first_commit_id,MAX(c.commit_id) AS commit_id FROM executions e "
                    "JOIN storage_commits c ON (c.subject_id=e.execution_id AND e.execution_origin='internal') "
                    "OR (c.subject_id=e.turn_id AND e.execution_origin='user_turn' AND c.idempotency_key IN "
                    "('terminal:'||e.turn_id||':'||e.execution_id||':'||c.outcome,'execution-lost:'||e.turn_id||':'||e.execution_id)) "
                    "WHERE c.status='committed' AND c.commit_kind='terminal_convergence' "
                    "AND e.session_id=? AND e.thread_id=? GROUP BY e.execution_id HAVING MIN(c.commit_id)>?) "
                    "SELECT c.commit_id,e.execution_id,e.job_id,c.outcome,json_extract(q.input_json,'$.request_id'),t.first_commit_id "
                    "FROM terminals t JOIN storage_commits c ON c.commit_id=t.commit_id JOIN executions e ON e.execution_id=t.execution_id "
                    "LEFT JOIN execution_queue q ON q.execution_id=e.execution_id AND q.checkpoint_ns='' "
                    "WHERE c.commit_id<=? "
                    "ORDER BY t.first_commit_id LIMIT ?", (session_id, thread_id, after_commit_id, upper, limit),
                ).fetchall()
                records = tuple(ExecutionCompletion(*row) for row in rows)
                return ExecutionCompletionPage(records, records[-1].cursor_commit_id if len(rows) == limit else upper)
        except RolloutNotMaterializedError:
            if after_commit_id not in {None, 0}:
                raise RuntimeError("已订阅的执行终态来源历史缺失，拒绝重置") from None
            return ExecutionCompletionPage((), 0)

    def publish_execution_queue_input(
        self, connection: sqlite3.Connection, admission: ExecutionAdmission,
        queue_input: ExecutionQueueInput, *, checkpoint_ns: str,
    ) -> ExecutionQueueRecord:
        """只接受原 admission writer 的活动事务；同 identity 不同调度输入拒绝。"""
        if not connection.in_transaction:
            raise RuntimeError("执行队列输入必须与 admission 同事务发布")
        self._validate_execution_queue_input(connection, admission, queue_input)
        cursor = self.execution_admission_cursor(admission, connection=connection, checkpoint_ns=checkpoint_ns)
        payload = asdict(queue_input)
        digest = sha256_jcs(payload)
        row = connection.execute(
            "SELECT input_hash FROM execution_queue WHERE checkpoint_ns=? AND job_id=?",
            (checkpoint_ns, admission.job_id),
        ).fetchone()
        if row is not None:
            if row != (digest,):
                raise ValueError(f"执行队列输入冲突: job_id={admission.job_id}")
        else:
            outcome = connection.execute("SELECT outcome FROM executions WHERE execution_id=?", (admission.execution_id,)).fetchone()[0]
            connection.execute(
                "INSERT INTO execution_queue(checkpoint_ns,job_id,execution_id,admission_commit_id,input_json,input_hash,"
                "delivery_policy,phase,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (checkpoint_ns, admission.job_id, admission.execution_id, cursor.commit_id,
                 canonical_json_text(payload), digest, queue_input.delivery_policy,
                 "queued" if outcome == "unknown" else "terminal", queue_input.created_at, queue_input.created_at),
            )
        return self._execution_queue_record(connection, admission.session_id, admission.thread_id, checkpoint_ns, admission.job_id)

    def _validate_execution_queue_input(self, connection: sqlite3.Connection, admission: ExecutionAdmission, queue_input: ExecutionQueueInput) -> None:
        binding = queue_input.execution_binding
        if binding.admission_idempotency_key != admission.admission_idempotency_key:
            raise ValueError("队列输入与 admission key 不一致")
        if isinstance(binding, UserExecutionIdentity):
            ingress = connection.execute(
                "SELECT accepted_ingress_id FROM turn_acceptances WHERE session_id=? AND thread_id=? AND turn_id=?",
                (admission.session_id, admission.thread_id, admission.turn_id),
            ).fetchone()
            if (admission.execution_origin, admission.job_id, admission.turn_id) != (
                "user_turn", binding.job_id, binding.turn_id,
            ) or ingress != (queue_input.message_id,):
                raise ValueError("队列输入与 user admission identity 不一致")
        elif isinstance(binding, InternalExecutionIntent):
            if admission.execution_origin != "internal" or binding.execution_id != admission.execution_id:
                raise ValueError("队列输入与 internal admission identity 不一致")

    def _execution_queue_record(
        self, connection: sqlite3.Connection, session_id: str, thread_id: str,
        checkpoint_ns: str, job_id: str,
    ) -> ExecutionQueueRecord:
        row = connection.execute(
            "SELECT execution_id,admission_commit_id,input_json,input_hash,delivery_policy,phase,"
            "claim_owner,claim_generation,updated_at,(SELECT outcome FROM executions WHERE execution_id=execution_queue.execution_id) "
            "FROM execution_queue WHERE checkpoint_ns=? AND job_id=?",
            (checkpoint_ns, job_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"执行队列引用不存在: job_id={job_id}")
        admission = self._read_execution_admission_connection(
            connection, session_id=session_id, thread_id=thread_id, execution_id=row[0], checkpoint_ns=checkpoint_ns,
        )
        cursor = self.execution_admission_cursor(admission, connection=connection, checkpoint_ns=checkpoint_ns)
        payload = json.loads(row[2])
        if row[1] != cursor.commit_id or sha256_jcs(payload) != row[3]:
            raise RuntimeError(f"执行队列输入与准入游标/hash 不一致: job_id={job_id}")
        queue_input = ExecutionQueueInput.from_dict(payload)
        self._validate_execution_queue_input(connection, admission, queue_input)
        if admission.job_id != job_id:
            raise RuntimeError("执行队列与 admission Job identity 不一致")
        return ExecutionQueueRecord(cursor, queue_input, row[4], row[5], row[6], row[7], row[8], row[9])

    def validate_execution_queue(self, session_id: str, thread_id: str) -> None:
        """正常调度拒绝缺失冻结输入；历史和迁移可独立读取尚未导入的投影。"""
        try:
            with self._storage.read_connection(session_id, thread_id) as connection:
                # 旧版 active 已从 v5 pending 移出；缺冻结调度输入时不能伪装空队列。
                missing = connection.execute(
                    "SELECT execution.job_id FROM executions execution LEFT JOIN execution_queue queued "
                    "ON queued.execution_id=execution.execution_id AND queued.checkpoint_ns=? "
                    "WHERE execution.session_id=? AND execution.thread_id=? AND execution.outcome='unknown' "
                    "AND queued.execution_id IS NULL LIMIT 1", ("", session_id, thread_id),
                ).fetchone()
                if missing is not None:
                    raise RuntimeError(
                        "执行缺少冻结队列输入；旧 active 须在旧 runtime 结算后再升级，不能从 Trace 猜测重建: "
                        f"session_id={session_id}, thread_id={thread_id}, job_id={missing[0]}"
                    )
        except RolloutNotMaterializedError:
            return

    def execution_queue_snapshot(
        self, session_id: str, thread_id: str, *, checkpoint_ns: str = "",
    ) -> ExecutionQueueSnapshot:
        try:
            with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
                revision = connection.execute("SELECT revision FROM execution_queue_revision WHERE singleton_id=1").fetchone()
                if revision is None:
                    raise RuntimeError("执行队列 revision 缺失")
                ids = connection.execute(
                    "SELECT job_id FROM execution_queue WHERE checkpoint_ns=? AND phase<>'terminal' ORDER BY admission_commit_id",
                    (checkpoint_ns,),
                ).fetchall()
                records = tuple(self._execution_queue_record(connection, session_id, thread_id, checkpoint_ns, row[0]) for row in ids)
                return ExecutionQueueSnapshot(records, revision[0])
        except RolloutNotMaterializedError:
            return ExecutionQueueSnapshot((), 0)

    def update_execution_queue_policy(
        self, session_id: str, thread_id: str, *, job_id: str, policy: str,
        expected_revision: int, checkpoint_ns: str = "",
    ) -> ExecutionQueueSnapshot:
        if policy not in {"after_turn", "after_tool_result", "after_interrupt"}:
            raise ValueError("执行队列 policy 非法")
        with (
            self._storage._lock(thread_id, checkpoint_ns, session_id=session_id),
            self._storage._connect(thread_id, checkpoint_ns, session_id=session_id) as connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            revision = connection.execute("SELECT revision FROM execution_queue_revision WHERE singleton_id=1").fetchone()
            if revision != (expected_revision,):
                raise RuntimeError("执行队列快照已过期")
            changed = connection.execute(
                "UPDATE execution_queue SET delivery_policy=?,updated_at=? WHERE checkpoint_ns=? AND job_id=? AND phase='queued'",
                (policy, utc_now_text(), checkpoint_ns, job_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("执行队列项已不再等待")
            self._storage._commit_connection(connection)
        return self.execution_queue_snapshot(session_id, thread_id, checkpoint_ns=checkpoint_ns)

    def execution_queue_record(
        self, session_id: str, thread_id: str, job_id: str, *, checkpoint_ns: str = "",
    ) -> ExecutionQueueRecord:
        with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            return self._execution_queue_record(connection, session_id, thread_id, checkpoint_ns, job_id)

    def claim_execution_queue(
        self, session_id: str, thread_id: str, *, job_id: str, claim_owner: str,
        previous_generation: int, checkpoint_ns: str = "",
    ) -> ExecutionQueueRecord:
        """调用方已独占精确 Thread holder；同事务核验 FIFO 与旧 generation。"""
        if not isinstance(claim_owner, str) or not claim_owner:
            raise ValueError("claim_owner 必须是非空字符串")
        with (
            self._storage._lock(thread_id, checkpoint_ns, session_id=session_id),
            self._storage._connect(thread_id, checkpoint_ns, session_id=session_id) as connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            record = self._execution_queue_record(connection, session_id, thread_id, checkpoint_ns, job_id)
            if record.phase == "terminal":
                raise RuntimeError(f"不能领取终态执行: job_id={job_id}")
            if record.claim_generation != previous_generation:
                raise RuntimeError(f"执行领取 generation 冲突: job_id={job_id}")
            head = connection.execute(
                "SELECT job_id FROM execution_queue WHERE checkpoint_ns=? AND phase<>'terminal' "
                "ORDER BY admission_commit_id LIMIT 1", (checkpoint_ns,),
            ).fetchone()
            if head != (job_id,):
                raise RuntimeError(f"只能领取同 Thread 的原始 FIFO 队首: job_id={job_id}")
            connection.execute(
                "UPDATE execution_queue SET phase='claimed',claim_owner=?,claim_generation=claim_generation+1,updated_at=? "
                "WHERE checkpoint_ns=? AND job_id=? AND claim_generation=?",
                (claim_owner, utc_now_text(), checkpoint_ns, job_id, previous_generation),
            )
            claimed = self._execution_queue_record(connection, session_id, thread_id, checkpoint_ns, job_id)
            self._storage._commit_connection(connection)
            return claimed

    def start_execution_queue(
        self, session_id: str, thread_id: str, *, job_id: str, claim_owner: str,
        claim_generation: int, checkpoint_ns: str = "",
    ) -> None:
        """保留 claim 到任务/终态结算；旧 owner 的迟到启动必须失败。"""
        with (
            self._storage._lock(thread_id, checkpoint_ns, session_id=session_id),
            self._storage._connect(thread_id, checkpoint_ns, session_id=session_id) as connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE execution_queue SET phase='started',updated_at=? WHERE checkpoint_ns=? AND job_id=? "
                "AND phase IN ('claimed','started') AND claim_owner=? AND claim_generation=?",
                (utc_now_text(), checkpoint_ns, job_id, claim_owner, claim_generation),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"执行启动 claim 已失效: job_id={job_id}")
            self._storage._commit_connection(connection)

    def import_execution_queue(self, session_id: str, thread_id: str,
                               inputs: tuple[tuple[str, ExecutionQueueInput], ...]) -> None:
        """显式迁移既有 v5 引用；每个 Thread 校验原准入顺序后一次提交。"""
        with self._execution_input_transaction(session_id, thread_id) as connection:
            previous_cursor = 0
            for job_id, queue_input in inputs:
                row = connection.execute(
                    "SELECT execution_id FROM executions WHERE session_id=? AND thread_id=? AND job_id=?",
                    (session_id, thread_id, job_id),
                ).fetchone()
                if row is None:
                    raise RuntimeError("迁移队列缺少 canonical admission")
                admission = self._read_execution_admission_connection(
                    connection, session_id=session_id, thread_id=thread_id, execution_id=row[0], checkpoint_ns="",
                )
                record = self.publish_execution_queue_input(connection, admission, queue_input, checkpoint_ns="")
                if record.cursor.commit_id <= previous_cursor:
                    raise RuntimeError("迁移队列顺序与原 admission commit cursor 冲突")
                previous_cursor = record.cursor.commit_id

    def execution_queue_before_dispatch(self, record: ExecutionQueueRecord) -> bool:
        admission = record.cursor.admission
        with self._storage.read_connection(admission.session_id, admission.thread_id) as connection:
            current = self._execution_queue_record(connection, admission.session_id, admission.thread_id, "", admission.job_id)
            if current.claim_owner != record.claim_owner or current.claim_generation != record.claim_generation:
                raise RuntimeError("恢复检查的 execution claim 已失效")
            return connection.execute(
                "SELECT 1 FROM model_calls WHERE execution_id=? UNION ALL "
                "SELECT 1 FROM context_assemblies WHERE execution_id=? LIMIT 1",
                (admission.execution_id, admission.execution_id),
            ).fetchone() is None
