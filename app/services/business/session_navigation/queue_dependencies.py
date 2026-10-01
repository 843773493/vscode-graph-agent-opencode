"""会话目录队列的依赖解析与失败传播链路。

承载「队列表」中与依赖相关的一条垂直链：

- ``_require_dependencies_resolvable`` / ``_require_referenced_operation``：enqueue
  期判定引用可解析（同批引用本批 ``client_operation_id``，跨批引用已 durable 接受的
  ``created_by_operation_id``）；
- ``_dependencies_terminal``：判定某条 queued record 的全部前置是否已 terminal；
- ``mark_dependency_failed_successors``：把失败 operation 的传递后继从 ``queued``
  直接终结为 ``dependency_failed``（无业务副作用）。

由 ``NavigationMutationQueueStore`` 继承（宿主必须提供 ``_record_from_row``），
不反向依赖顶层 ``queue_store.py``。
"""

from __future__ import annotations

import sqlite3

from app.schemas.internal_v2.session_navigation.operations import (
    NAVIGATION_MUTATION_TERMINAL_STATES,
    NavigationMutationIntentDTO,
)
from app.services.business.session_navigation.queue_records import (
    _RECORD_COLUMNS,
    NavigationMutationConflictError,
    NavigationMutationRecord,
)


class NavigationDependencyMixin:
    """依赖解析与失败传播的方法族（由 ``NavigationMutationQueueStore`` 继承）。"""

    def _require_dependencies_resolvable(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        intent: NavigationMutationIntentDTO,
        batch_ids: set[str],
    ) -> None:
        for dependency_id in intent.depends_on:
            if dependency_id in batch_ids:
                continue
            self._require_referenced_operation(
                connection, workspace_id, dependency_id, intent
            )
        if intent.created_by_operation_id is not None:
            if intent.created_by_operation_id == intent.client_operation_id:
                raise ValueError("intent 不能引用自身作为 created_by_operation_id")
            if intent.created_by_operation_id not in batch_ids:
                self._require_referenced_operation(
                    connection,
                    workspace_id,
                    intent.created_by_operation_id,
                    intent,
                )

    def _require_referenced_operation(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        operation_id: str,
        intent: NavigationMutationIntentDTO,
    ) -> None:
        row = connection.execute(
            "SELECT queue_seq FROM navigation_mutation_records "
            "WHERE workspace_id = ? AND operation_id = ? LIMIT 1",
            (workspace_id, operation_id),
        ).fetchone()
        if row is None:
            raise NavigationMutationConflictError(
                "跨批依赖引用的 operation 尚未被 durable 接受: "
                f"intent={intent.client_operation_id}, dependency={operation_id}"
            )

    def _dependencies_terminal(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        record: NavigationMutationRecord,
    ) -> bool:
        for dependency_id in record.depends_on:
            row = connection.execute(
                "SELECT state FROM navigation_mutation_records "
                "WHERE workspace_id = ? AND operation_id = ? LIMIT 1",
                (workspace_id, dependency_id),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "已 durable 接受的依赖 operation 缺失（catalog 被外部改动）: "
                    f"operation={record.operation_id}, dependency={dependency_id}"
                )
            if str(row[0]) not in NAVIGATION_MUTATION_TERMINAL_STATES:
                return False
        if record.created_by_operation_id is not None:
            row = connection.execute(
                "SELECT state FROM navigation_mutation_records "
                "WHERE workspace_id = ? AND operation_id = ? LIMIT 1",
                (workspace_id, record.created_by_operation_id),
            ).fetchone()
            if row is None or str(row[0]) not in NAVIGATION_MUTATION_TERMINAL_STATES:
                return False
        return True

    def mark_dependency_failed_successors(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        failed_operation_id: str,
        now: str,
    ) -> list[NavigationMutationRecord]:
        """把 ``failed_operation_id`` 的传递后继从 ``queued`` 直接终结为
        ``dependency_failed``（无任何业务副作用）。

        只处理仍为 ``queued`` 的行：已 ``running``/终态的行不动（不重排、不
        伪造结果）。返回被终结的记录，供同事务写事件 outbox。
        """
        queued_rows = connection.execute(
            f"SELECT {_RECORD_COLUMNS} FROM navigation_mutation_records "
            "WHERE workspace_id = ? AND state = 'queued' ORDER BY queue_seq",
            (workspace_id,),
        ).fetchall()
        queued = {row["operation_id"]: self._record_from_row(row) for row in queued_rows}
        failed: list[NavigationMutationRecord] = []
        poisoned = {failed_operation_id}
        changed = True
        while changed:
            changed = False
            for operation_id, record in queued.items():
                if operation_id in poisoned:
                    continue
                references = set(record.depends_on)
                if record.created_by_operation_id is not None:
                    references.add(record.created_by_operation_id)
                if references & poisoned:
                    poisoned.add(operation_id)
                    failed.append(record)
                    changed = True
        for record in failed:
            connection.execute(
                "UPDATE navigation_mutation_records SET state = 'dependency_failed', "
                "error_code = 'dependency_failed', error_detail = ?, "
                "receipt_revision = receipt_revision + 1, updated_at = ? "
                "WHERE gateway_id = ? AND workspace_id = ? AND actor = ? "
                "AND operation_id = ? AND state = 'queued'",
                (
                    f"前置 operation 未成功: {failed_operation_id}",
                    now,
                    record.gateway_id,
                    record.workspace_id,
                    record.actor,
                    record.operation_id,
                ),
            )
        return failed
