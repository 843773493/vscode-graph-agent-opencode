"""连接操作复用唯一 WorkspaceStateStore，不创建另一份数据库或队列。"""

from __future__ import annotations

import time
from contextlib import closing

from app.schemas.internal_v2.mcp import McpConnectionOperationDTO
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


class McpConnectionOperationStore:
    def __init__(self, store: WorkspaceStateStore) -> None:
        self._store = store

    def get(self, operation_id: str) -> McpConnectionOperationDTO:
        with closing(self._store.connection()) as connection:
            row = connection.execute("SELECT o.* FROM mcp_connection_operations o JOIN mcp_connection_operation_aliases a USING (operation_id) WHERE a.intent_id = ?", (operation_id,)).fetchone()
        if row is None:
            raise LookupError("MCP 连接操作不存在")
        return self._operation(dict(row))

    @staticmethod
    def _operation(row: dict[str, object]) -> McpConnectionOperationDTO:
        return McpConnectionOperationDTO.model_validate({key: value for key, value in row.items() if key != "configuration_ref"})

    def configuration_ref(self, operation_id: str) -> str:
        with closing(self._store.connection()) as connection:
            row = connection.execute("SELECT o.configuration_ref FROM mcp_connection_operations o JOIN mcp_connection_operation_aliases a USING (operation_id) WHERE a.intent_id = ?", (operation_id,)).fetchone()
        if row is None:
            raise LookupError("MCP 连接操作不存在")
        return str(row[0])

    def active(self) -> tuple[McpConnectionOperationDTO, ...]:
        with closing(self._store.connection()) as connection:
            rows = connection.execute("SELECT * FROM mcp_connection_operations WHERE status IN ('pending', 'waiting_user', 'verifying') ORDER BY created_at").fetchall()
        return tuple(self._operation(dict(row)) for row in rows)

    def begin(self, *, operation_id: str, connection_id: str, server_id: str, action: str,
              configuration_ref: str, deadline: float) -> tuple[McpConnectionOperationDTO, bool]:
        """同一意图重放返回原操作；同连接同时 connect 加入原有效操作。"""
        now = time.time()
        with closing(self._store.connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT o.* FROM mcp_connection_operations o JOIN mcp_connection_operation_aliases a USING (operation_id) WHERE a.intent_id = ?", (operation_id,)).fetchone()
                if row is not None:
                    operation = self._operation(dict(row))
                    if (operation.connection_id, operation.server_id, operation.action) != (connection_id, server_id, action):
                        raise ValueError("MCP operation_id 已被不同连接意图使用")
                    connection.commit()
                    return operation, False
                row = connection.execute("SELECT * FROM mcp_connection_operations WHERE connection_id = ? AND status IN ('pending', 'waiting_user', 'verifying')", (connection_id,)).fetchone()
                if row is not None:
                    operation = self._operation(dict(row))
                    if action != operation.action:
                        raise RuntimeError("MCP 连接已有进行中的相反操作")
                    connection.execute("INSERT INTO mcp_connection_operation_aliases VALUES (?, ?)", (operation_id, operation.operation_id))
                    connection.commit()
                    return operation, False
                connection.execute(
                    "INSERT INTO mcp_connection_operations VALUES (?, ?, ?, ?, ?, 'pending', 'queued', ?, ?, ?, NULL)",
                    (operation_id, connection_id, server_id, action, configuration_ref, now, deadline, now),
                )
                connection.execute("INSERT INTO mcp_connection_operation_aliases VALUES (?, ?)", (operation_id, operation_id))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get(operation_id), True

    def advance(self, operation_id: str, *, status: str, stage: str, error_code: str | None = None) -> McpConnectionOperationDTO:
        """终态不可被晚回调改写；真实目录提交前的资格由同一行核实。"""
        with closing(self._store.connection()) as connection:
            connection.execute(
                "UPDATE mcp_connection_operations SET status = ?, stage = ?, error_code = ?, updated_at = ? "
                "WHERE operation_id = ? AND status IN ('pending', 'waiting_user', 'verifying')",
                (status, stage, error_code, time.time(), operation_id),
            )
        return self.get(operation_id)

    def record_cleanup_failure(self, operation_id: str, error_code: str) -> None:
        """取消／过期不恢复准入；清理失败作为同一终态的诊断补全。"""
        with closing(self._store.connection()) as connection:
            connection.execute(
                "UPDATE mcp_connection_operations SET stage = 'cleanup', error_code = ?, updated_at = ? "
                "WHERE operation_id = ? AND status IN ('cancelled', 'expired')",
                (error_code, time.time(), operation_id),
            )
