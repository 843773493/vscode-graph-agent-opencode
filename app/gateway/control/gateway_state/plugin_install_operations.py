"""Gateway 插件安装操作的唯一持久状态 owner。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from app.core.sqlite_state import SQLiteStateDatabase, utc_now_text
from app.services.infrastructure.config.state import ConfigConflictError


def _plugin_install_operation(row: sqlite3.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "gateway_id": str(row[0]),
        "operation_id": str(row[1]),
        "request": json.loads(str(row[2])),
        "artifact": json.loads(str(row[3])),
        "status": str(row[4]),
        "cancel_requested": bool(row[5]),
        "error": str(row[6]) if row[6] is not None else None,
        "owner_pid": int(row[7]) if row[7] is not None else None,
        "owner_created_at": float(row[8]) if row[8] is not None else None,
    }


class PluginInstallOperationMixin:
    """安装操作状态与发布裁决；包正文不进入此表。"""

    _database: SQLiteStateDatabase

    def create_plugin_install_operation(
        self,
        *,
        gateway_id: str,
        operation_id: str,
        request_json: str,
        artifact_json: str,
        owner_pid: int,
        owner_created_at: float,
    ) -> tuple[dict[str, object], bool]:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT gateway_id,operation_id,request_json,artifact_json,status,cancel_requested,error,owner_pid,owner_created_at "
                "FROM plugin_install_operations WHERE gateway_id=? AND operation_id=?",
                (gateway_id, operation_id),
            ).fetchone()
            if row is not None:
                if str(row[2]) != request_json:
                    raise ConfigConflictError(f"插件安装 operation_id 已绑定不同输入: {operation_id}")
                result = _plugin_install_operation(row)
                assert result is not None
                connection.execute("COMMIT")
                return result, False
            connection.execute(
                "INSERT INTO plugin_install_operations(gateway_id,operation_id,request_json,artifact_json,status,owner_pid,owner_created_at,updated_at) "
                "VALUES(?,?,?,?, 'running',?,?,?)",
                (gateway_id, operation_id, request_json, artifact_json, owner_pid, owner_created_at, utc_now_text()),
            )
            result = {
                "gateway_id": gateway_id, "operation_id": operation_id,
                "request": json.loads(request_json), "artifact": json.loads(artifact_json),
                "status": "running", "cancel_requested": False, "error": None,
                "owner_pid": owner_pid, "owner_created_at": owner_created_at,
            }
            connection.execute("COMMIT")
            return result, True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_plugin_install_operation(self, *, gateway_id: str, operation_id: str) -> dict[str, object] | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                "SELECT gateway_id,operation_id,request_json,artifact_json,status,cancel_requested,error,owner_pid,owner_created_at "
                "FROM plugin_install_operations WHERE gateway_id=? AND operation_id=?",
                (gateway_id, operation_id),
            ).fetchone()
            return _plugin_install_operation(row)
        finally:
            connection.close()

    def request_plugin_install_cancel(self, *, gateway_id: str, operation_id: str) -> dict[str, object]:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE plugin_install_operations SET cancel_requested=1, status=CASE WHEN status='pending' THEN 'cancelled' ELSE status END, updated_at=? "
                "WHERE gateway_id=? AND operation_id=? AND status IN ('pending','running')",
                (utc_now_text(), gateway_id, operation_id),
            )
            row = connection.execute(
                "SELECT gateway_id,operation_id,request_json,artifact_json,status,cancel_requested,error,owner_pid,owner_created_at "
                "FROM plugin_install_operations WHERE gateway_id=? AND operation_id=?",
                (gateway_id, operation_id),
            ).fetchone()
            result = _plugin_install_operation(row)
            if result is None:
                raise KeyError(f"插件安装操作不存在: {operation_id}")
            connection.execute("COMMIT")
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def commit_plugin_install_publication(
        self,
        *,
        gateway_id: str,
        operation_id: str,
        owner_pid: int,
        owner_created_at: float,
        publish: Callable[[], None],
    ) -> bool:
        """在短事务内裁决取消与原子包发布，成功状态随发布一同提交。"""
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,cancel_requested,owner_pid,owner_created_at FROM plugin_install_operations WHERE gateway_id=? AND operation_id=?",
                (gateway_id, operation_id),
            ).fetchone()
            if row is None or str(row[0]) != "running" or int(row[2]) != owner_pid or float(row[3]) != owner_created_at:
                raise RuntimeError(f"插件安装执行 owner 已失效: {operation_id}")
            if bool(row[1]):
                connection.execute(
                    "UPDATE plugin_install_operations SET status='cancelled',updated_at=? WHERE gateway_id=? AND operation_id=?",
                    (utc_now_text(), gateway_id, operation_id),
                )
                connection.execute("COMMIT")
                return False
            publish()
            connection.execute(
                "UPDATE plugin_install_operations SET status='succeeded',cancel_requested=0,error=NULL,updated_at=? WHERE gateway_id=? AND operation_id=?",
                (utc_now_text(), gateway_id, operation_id),
            )
            connection.execute("COMMIT")
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finish_plugin_install_operation(
        self, *, gateway_id: str, operation_id: str, owner_pid: int,
        owner_created_at: float, status: str, error: str | None,
    ) -> None:
        if status not in {"failed", "cancelled", "unknown"}:
            raise ValueError(f"不支持插件安装终态: {status}")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE plugin_install_operations SET status=?,error=?,updated_at=? "
                "WHERE gateway_id=? AND operation_id=? AND status='running' AND owner_pid=? AND owner_created_at=?",
                (status, error, utc_now_text(), gateway_id, operation_id, owner_pid, owner_created_at),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
