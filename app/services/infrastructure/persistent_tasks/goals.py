"""同一任务 owner 的 Goal 应用状态；main 归属仅由 catalog 派生。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import SessionCreationRecord
from app.domain.itemized.hashing import sha256_jcs
from app.schemas.internal_v2.goal import (
    GoalStatus,
    SessionGoalStateDTO,
)
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

_COMMON_FIELDS = {"goal_id", "session_id", "thread_id", "created_at", "updated_at", "objective", "token_budget"}


class GoalOperationReplayError(RuntimeError):
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result


class TaskGoals:
    _store: WorkspaceStateStore
    _paths: SessionCatalogPathResolver | None
    workspace_id: str

    def _transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        raise NotImplementedError

    def _goal_paths(self) -> SessionCatalogPathResolver:
        if self._paths is None:
            raise RuntimeError("Goal 任务必须装配权威 Session catalog")
        return self._paths

    def _goal_thread(self, session_id: str, creation: SessionCreationRecord | None) -> str:
        paths = self._goal_paths()
        if creation is None:
            return paths.main_thread_id(session_id)
        if creation.session_id != session_id or creation.purpose != "context_fork" or creation.state != "preparing":
            raise RuntimeError("fork Goal 缺少精确 preparing main creation capability")
        # 同时校验当前 journal、稳定 locator 与物理节点，不信任调用方自造 record。
        paths.resolve_prepared_session_node(creation)
        return creation.main_thread_id

    @staticmethod
    def _goal(connection: sqlite3.Connection, workspace_id: str, session_id: str,
              thread_id: str) -> SessionGoalStateDTO | None:
        row = connection.execute(
            "SELECT t.*,d.configuration_json FROM persistent_tasks t LEFT JOIN persistent_task_definitions d "
            "ON d.task_id=t.task_id AND d.definition_revision=t.definition_revision "
            "WHERE t.workspace_id=? AND t.owner_session_id=? AND t.application_type='goal' AND t.retired_at IS NULL",
            (workspace_id, session_id),
        ).fetchone()
        if row is None:
            return None
        if row["configuration_json"] is None:
            raise RuntimeError("Goal 当前任务缺少不可变定义")
        definition = json.loads(row["configuration_json"])
        if definition.pop("type_id") != "builtin.goal" or definition.pop("version") != "1":
            raise RuntimeError("不支持的 Goal 应用定义")
        return SessionGoalStateDTO.model_validate({
            **json.loads(row["application_json"]), **definition,
            "goal_id": row["task_id"], "session_id": session_id, "thread_id": thread_id,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        })

    @staticmethod
    def _slot_control_revision(connection: sqlite3.Connection, workspace_id: str, session_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(CAST(json_extract(application_json,'$.control_revision') AS INTEGER)),0) "
            "FROM persistent_tasks WHERE workspace_id=? AND owner_session_id=? AND application_type='goal'",
            (workspace_id, session_id),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _slot_sync_version(connection: sqlite3.Connection, workspace_id: str, session_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(CAST(json_extract(application_json,'$.sync_version') AS INTEGER)),0) "
            "FROM persistent_tasks WHERE workspace_id=? AND owner_session_id=? AND application_type='goal'",
            (workspace_id, session_id),
        ).fetchone()
        return int(row[0])

    def goal_control_revision(self, session_id: str) -> int:
        with closing(self._store.connection()) as connection:
            return self._slot_control_revision(connection, self.workspace_id, session_id)

    def goal_sync_version(self, session_id: str) -> int:
        with closing(self._store.connection()) as connection:
            return self._slot_sync_version(connection, self.workspace_id, session_id)

    def goal_operation(self, session_id: str, operation_id: str, request_hash: str | None = None) -> dict[str, object] | None:
        with closing(self._store.connection()) as connection:
            row = connection.execute(
                "SELECT request_hash,result_json FROM session_goal_operations WHERE workspace_id=? AND session_id=? AND operation_id=?",
                (self.workspace_id, session_id, operation_id),
            ).fetchone()
        if row is None:
            return None
        if request_hash is not None and row["request_hash"] != request_hash:
            from app.abstractions.state_conflict import ClientStateConflictError

            raise ClientStateConflictError("Goal 操作身份已绑定不同载荷")
        return json.loads(row["result_json"])

    @staticmethod
    def _record_goal_operation(
        connection: sqlite3.Connection, workspace_id: str, session_id: str, operation_id: str,
        request_hash: str, result: dict[str, object], created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO session_goal_operations VALUES (?,?,?,?,?,?)",
            (workspace_id, session_id, operation_id, request_hash,
             json.dumps(result, ensure_ascii=False, sort_keys=True), created_at),
        )

    def goal_read(self, session_id: str, *, creation: SessionCreationRecord | None = None) -> SessionGoalStateDTO | None:
        thread_id = self._goal_thread(session_id, creation)
        with closing(self._store.connection()) as connection:
            return self._goal(connection, self.workspace_id, session_id, thread_id)

    def _write_goal(self, connection: sqlite3.Connection, goal: SessionGoalStateDTO) -> None:
        definition = {"type_id": "builtin.goal", "version": "1", "objective": goal.objective, "token_budget": goal.token_budget}
        definition_json = json.dumps(definition, ensure_ascii=False, sort_keys=True)
        state_json = goal.model_dump_json(exclude=_COMMON_FIELDS)
        lifecycle = {GoalStatus.active: "active", GoalStatus.paused: "paused", GoalStatus.complete: "complete"}.get(goal.status, "needs_attention")
        row = connection.execute(
            "SELECT t.owner_session_id,t.definition_revision,d.configuration_json FROM persistent_tasks t "
            "JOIN persistent_task_definitions d ON d.task_id=t.task_id AND d.definition_revision=t.definition_revision "
            "WHERE t.task_id=? AND t.workspace_id=? AND t.application_type='goal' AND t.retired_at IS NULL",
            (goal.goal_id, self.workspace_id),
        ).fetchone()
        if row is None:
            # 替换只切换当前指针；原任务、定义及导入身份保留，旧文件重试不能复活它。
            connection.execute("UPDATE persistent_tasks SET retired_at=? WHERE workspace_id=? AND owner_session_id=? "
                               "AND application_type='goal' AND retired_at IS NULL",
                               (goal.updated_at.isoformat(), self.workspace_id, goal.session_id))
            connection.execute(
                "INSERT INTO persistent_tasks (task_id,workspace_id,creation_key,creation_hash,revision,definition_revision,"
                "lifecycle,created_at,updated_at,application_type,owner_session_id,application_json) VALUES (?,?,?,?,1,1,?,?,?,'goal',?,?)",
                (goal.goal_id, self.workspace_id, f"goal:{goal.goal_id}", sha256_jcs(goal.model_dump(mode="json", exclude={"thread_id"})),
                 lifecycle, goal.created_at.isoformat(), goal.updated_at.isoformat(), goal.session_id, state_json),
            )
            revision = 1
        else:
            if row["owner_session_id"] != goal.session_id:
                raise RuntimeError("Goal 任务身份属于其它 Session")
            revision = row["definition_revision"] + (row["configuration_json"] != definition_json)
            connection.execute("UPDATE persistent_tasks SET revision=revision+1,definition_revision=?,lifecycle=?,application_json=?,updated_at=? WHERE task_id=?",
                               (revision, lifecycle, state_json, goal.updated_at.isoformat(), goal.goal_id))
        if row is None or revision != row["definition_revision"]:
            connection.execute("INSERT INTO persistent_task_definitions VALUES (?,?,?)", (goal.goal_id, revision, definition_json))

    def goal_write(
        self, goal: SessionGoalStateDTO, *, creation: SessionCreationRecord | None = None,
        expected_control_revision: int | None = None,
        operation: tuple[str, str, dict[str, object]] | None = None,
        retiring_goal: SessionGoalStateDTO | None = None,
    ) -> None:
        if goal.thread_id != self._goal_thread(goal.session_id, creation):
            raise ValueError("Goal 只能写入权威 main Thread")
        with self._transaction() as connection:
            if operation is not None:
                operation_id, request_hash, _ = operation
                row = connection.execute(
                    "SELECT request_hash,result_json FROM session_goal_operations WHERE workspace_id=? AND session_id=? AND operation_id=?",
                    (self.workspace_id, goal.session_id, operation_id),
                ).fetchone()
                if row is not None:
                    if row["request_hash"] != request_hash:
                        from app.abstractions.state_conflict import (
                            ClientStateConflictError,
                        )

                        raise ClientStateConflictError("Goal 操作身份已绑定不同载荷")
                    raise GoalOperationReplayError(json.loads(row["result_json"]))
            current_revision = self._slot_control_revision(connection, self.workspace_id, goal.session_id)
            if expected_control_revision is not None and current_revision != expected_control_revision:
                from app.abstractions.state_conflict import ClientStateConflictError

                raise ClientStateConflictError(
                    f"Goal 控制版本冲突: expected={expected_control_revision}, actual={current_revision}"
                )
            if expected_control_revision is not None and goal.control_revision not in {
                expected_control_revision, expected_control_revision + 1
            }:
                raise ValueError("Goal 控制版本只能保持或从期望版本推进一次")
            if goal.control_revision < current_revision:
                raise RuntimeError("Goal 写入不能回退槽位控制版本")
            if retiring_goal is not None:
                self._write_goal(connection, retiring_goal)
            self._write_goal(connection, goal)
            if operation is not None:
                operation_id, request_hash, result = operation
                self._record_goal_operation(
                    connection, self.workspace_id, goal.session_id, operation_id,
                    request_hash, result, goal.updated_at.isoformat(),
                )

    def goal_clear_versioned(
        self, session_id: str, expected_control_revision: int,
        operation: tuple[str, str, dict[str, object]] | None = None,
        settled_goal: SessionGoalStateDTO | None = None,
    ) -> tuple[bool, int, int]:
        with self._transaction() as connection:
            if operation is not None:
                operation_id, request_hash, _ = operation
                row = connection.execute(
                    "SELECT request_hash,result_json FROM session_goal_operations WHERE workspace_id=? AND session_id=? AND operation_id=?",
                    (self.workspace_id, session_id, operation_id),
                ).fetchone()
                if row is not None:
                    if row["request_hash"] != request_hash:
                        from app.abstractions.state_conflict import (
                            ClientStateConflictError,
                        )

                        raise ClientStateConflictError("Goal 操作身份已绑定不同载荷")
                    raise GoalOperationReplayError(json.loads(row["result_json"]))
            actual = self._slot_control_revision(connection, self.workspace_id, session_id)
            if expected_control_revision is not None and actual != expected_control_revision:
                from app.abstractions.state_conflict import ClientStateConflictError

                raise ClientStateConflictError(
                    f"Goal 控制版本冲突: expected={expected_control_revision}, actual={actual}"
                )
            row = connection.execute(
                "SELECT task_id,application_json FROM persistent_tasks WHERE workspace_id=? AND owner_session_id=? "
                "AND application_type='goal' AND retired_at IS NULL",
                (self.workspace_id, session_id),
            ).fetchone()
            if row is None:
                if operation is not None:
                    operation_id, request_hash, result = operation
                    result.update({"status": "already_missing", "control_revision": actual,
                                   "sync_version": self._slot_sync_version(connection, self.workspace_id, session_id)})
                    self._record_goal_operation(connection, self.workspace_id, session_id, operation_id,
                                                request_hash, result, datetime.now(UTC).isoformat())
                return False, actual, self._slot_sync_version(connection, self.workspace_id, session_id)
            if settled_goal is not None and (settled_goal.goal_id != row["task_id"] or settled_goal.session_id != session_id):
                raise ValueError("冻结进度与待清除 Goal 身份不一致")
            state = (settled_goal.model_dump(mode="json", exclude=_COMMON_FIELDS)
                     if settled_goal is not None else json.loads(row["application_json"]))
            state["control_revision"] = actual + 1
            state["sync_version"] = self._slot_sync_version(connection, self.workspace_id, session_id) + 1
            connection.execute(
                "UPDATE persistent_tasks SET retired_at=?,application_json=?,updated_at=? WHERE task_id=?",
                (datetime.now(UTC).isoformat(), json.dumps(state, ensure_ascii=False),
                 datetime.now(UTC).isoformat(), row["task_id"]),
            )
            if operation is not None:
                operation_id, request_hash, result = operation
                result.update({"status": "applied", "control_revision": actual + 1,
                               "sync_version": state["sync_version"]})
                self._record_goal_operation(connection, self.workspace_id, session_id, operation_id,
                                            request_hash, result, datetime.now(UTC).isoformat())
            return True, actual + 1, state["sync_version"]

    def goal_clear(self, session_id: str, *, expected_control_revision: int | None = None) -> bool:
        if expected_control_revision is not None:
            return self.goal_clear_versioned(session_id, expected_control_revision)[0]
        with closing(self._store.connection()) as connection:
            expected = self._slot_control_revision(connection, self.workspace_id, session_id)
        return self.goal_clear_versioned(session_id, expected)[0]

    def goal_list(self) -> list[SessionGoalStateDTO]:
        # 发布可见性归原 catalog；preparing、aborted、deleting 均不能被恢复调度。
        active = {node.node_id: node.main_thread_id for node in self._goal_paths().list_nodes(kind="session")
                  if node.state == "active"}
        with closing(self._store.connection()) as connection:
            sessions = connection.execute("SELECT owner_session_id FROM persistent_tasks WHERE workspace_id=? "
                                          "AND application_type='goal' AND retired_at IS NULL", (self.workspace_id,)).fetchall()
            return [goal for row in sessions if row[0] in active
                    if (goal := self._goal(connection, self.workspace_id, row[0], active[row[0]])) is not None]

    def goal_import(self, goal: SessionGoalStateDTO, source_hash: str, *, creation: SessionCreationRecord | None = None) -> None:
        if goal.thread_id != self._goal_thread(goal.session_id, creation):
            raise ValueError("Goal 迁移归属与权威 main Thread 不一致")
        with self._transaction() as connection:
            key = f"goal.json:{goal.session_id}"
            imported = connection.execute("SELECT source_hash,task_id FROM persistent_task_imports WHERE workspace_id=? AND source_key=?",
                                          (self.workspace_id, key)).fetchone()
            if imported is not None:
                if tuple(imported) != (source_hash, goal.goal_id):
                    raise RuntimeError("Goal 旧文件与已导入指纹冲突")
                return
            current = self._goal(connection, self.workspace_id, goal.session_id, goal.thread_id)
            if current is not None and current != goal:
                raise RuntimeError("Goal 旧文件与统一任务 owner 冲突")
            if current is None:
                self._write_goal(connection, goal)
            connection.execute("INSERT INTO persistent_task_imports VALUES (?,?,?,?)", (self.workspace_id, key, source_hash, goal.goal_id))

    def goal_abort_fork(self, record: SessionCreationRecord) -> None:
        # capture 失败可能发生在物理节点建立前；撤销只认原 journal，不能依赖目录存在。
        stored = self._goal_paths().catalog_store.get_creation_record(record.session_creation_idempotency_key)
        if stored != record or record.state != "preparing" or record.purpose != "context_fork" or record.workspace_id != self.workspace_id:
            raise RuntimeError("fork Goal 撤销缺少精确 preparing creation capability")
        self.goal_clear(record.session_id)
