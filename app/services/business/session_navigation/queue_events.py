"""会话目录导航事件 outbox（``navigation_events``）的追加与读取链路。

承载整条「事件表」垂直链：单调 ``event_seq`` 分配、终态事件追加（与 operation
terminal 在**同一个** SQLite 写事务提交）、``event_seq`` cursor 分页读取与水位。
由 ``NavigationMutationQueueStore`` 继承（宿主必须提供 ``_store`` 与
``_event_from_row``），不反向依赖顶层 ``queue_store.py``。
"""

from __future__ import annotations

import json
import sqlite3

from app.core.session_catalog_store import CatalogMaintenanceRequiredError
from app.services.business.session_navigation.queue_records import (
    _EVENT_COLUMNS,
    NavigationEventRecord,
    NavigationMutationRecord,
)


class NavigationEventOutboxMixin:
    """事件 outbox 的方法族（由 ``NavigationMutationQueueStore`` 继承）。"""

    def _event_in(
        self, connection: sqlite3.Connection, row: sqlite3.Row, *, workspace_id: str,
    ) -> NavigationEventRecord:
        """在原快照核事件和保留operation的冻结身份，不查询当前导航节点。"""
        try:
            event = self._event_from_row(row)
            if event.workspace_id != workspace_id:
                raise ValueError(f"事件workspace归属不符: expected={workspace_id!r}")
            operation = self._fetch_record(
                connection, event.gateway_id, event.workspace_id, event.actor, event.operation_id,
            )
            if operation is None:
                raise ValueError("事件对应的保留operation不存在")
            for field, value in (
                ("queue_seq", event.queue_seq), ("kind", event.kind),
                ("state", event.result_state),
                ("committed_catalog_revision", event.committed_catalog_revision),
            ):
                if operation[field] != value:
                    raise ValueError(f"事件{field}与保留operation不符")
            # 删除的后续settlement可以改变回执错误；原终态事件不随之重写。
            return event
        except (TypeError, ValueError) as error:
            raise CatalogMaintenanceRequiredError(
                "navigation event 原始记录损坏，拒绝读取或恢复: "
                f"path={self._store.database_path}, event_seq={row['event_seq']!r}, "
                f"operation_id={row['operation_id']!r}, detail={error}"
            ) from error

    @staticmethod
    def _next_event_seq(connection: sqlite3.Connection, workspace_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(event_seq), 0) + 1 FROM navigation_events "
            "WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        return int(row[0])

    def append_event(
        self,
        connection: sqlite3.Connection,
        *,
        record: NavigationMutationRecord,
        affected_node_ids: list[str],
        now: str,
    ) -> NavigationEventRecord:
        """在**同一** catalog 写事务内追加终态事件（与 operation terminal 同事务）。"""
        if not record.is_terminal:
            raise RuntimeError(
                f"只允许为终态 operation 追加导航事件: {record.operation_id}"
            )
        event_seq = self._next_event_seq(connection, record.workspace_id)
        connection.execute(
            "INSERT INTO navigation_events ("
            "workspace_id, event_seq, operation_id, gateway_id, actor, queue_seq, "
            "kind, result_state, committed_catalog_revision, "
            "affected_node_ids_json, error_code, error_detail, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.workspace_id,
                event_seq,
                record.operation_id,
                record.gateway_id,
                record.actor,
                record.queue_seq,
                record.kind,
                record.state,
                record.committed_catalog_revision,
                json.dumps(sorted(set(affected_node_ids))),
                record.error_code,
                record.error_detail,
                now,
            ),
        )
        row = connection.execute(
            f"SELECT {_EVENT_COLUMNS} FROM navigation_events "
            "WHERE workspace_id = ? AND event_seq = ?",
            (record.workspace_id, event_seq),
        ).fetchone()
        return self._event_in(connection, row, workspace_id=record.workspace_id)

    def list_events(
        self,
        *,
        workspace_id: str,
        after: int,
        limit: int,
    ) -> tuple[list[NavigationEventRecord], int]:
        """返回 ``(event_seq > after)`` 的前 ``limit`` 条事件与当前水位。

        cursor 由单调 ``event_seq`` 承载：重复的 ``after`` 会重复返回相同集合
        （客户端按 ``event_seq``/``operation_id`` 去重即可），不存在「跳过未读
        事件」的窗口。水位在同一次只读事务内取得。
        """
        with self._store.read_transaction() as connection:
            rows = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM navigation_events "
                "WHERE workspace_id = ? AND event_seq > ? ORDER BY event_seq LIMIT ?",
                (workspace_id, after, limit),
            ).fetchall()
            watermark = self._event_watermark(connection, workspace_id)
            return [self._event_in(connection, row, workspace_id=workspace_id) for row in rows], watermark

    def event_watermark_in(self, connection: sqlite3.Connection, workspace_id: str) -> int:
        """在调用方只读事务内取事件水位（供 snapshot 与 revision 同一快照）。"""
        return self._event_watermark(connection, workspace_id)

    @staticmethod
    def _event_watermark(connection: sqlite3.Connection, workspace_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(event_seq), 0) FROM navigation_events "
            "WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        return int(row[0])
