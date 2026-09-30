"""config_source_fanout 逐 generation/逐 workspace 导入结果账本方法族。

``WorkspaceConfigSourceFanoutMixin`` 只承载本族方法，由 ``WorkspaceConfigSourceMixin``
组合装配；依赖宿主提供的 ``_database``。
"""

from __future__ import annotations

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import ConfigConflictError

__all__ = ["WorkspaceConfigSourceFanoutMixin"]


class WorkspaceConfigSourceFanoutMixin:
    def record_config_source_fanout(
        self,
        *,
        source_key: str,
        source_generation: int,
        workspace_id: str,
        status: str,
        layer_revision: int | None = None,
        layer_digest: str | None = None,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        if not workspace_id or not status:
            raise ValueError("fan-out 工作区和状态不能为空")
        if source_generation < 1:
            raise ValueError("fan-out source_generation 必须为正数")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                """
                SELECT 1 FROM config_source_journal
                WHERE source_key = ? AND source_generation = ?
                """,
                (source_key, source_generation),
            ).fetchone() is None:
                raise ConfigConflictError("fan-out 关联的 source journal 不存在")
            connection.execute(
                """
                INSERT INTO config_source_fanout(
                    source_key, source_generation, workspace_id, status,
                    layer_revision, layer_digest, result, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key, source_generation, workspace_id) DO UPDATE SET
                    status=excluded.status,
                    layer_revision=excluded.layer_revision,
                    layer_digest=excluded.layer_digest,
                    result=excluded.result,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    source_key,
                    source_generation,
                    workspace_id,
                    status,
                    layer_revision,
                    layer_digest,
                    result,
                    error,
                    utc_now_text(),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def prepare_config_source_fanout(
        self,
        *,
        source_key: str,
        workspace_id: str,
        after_generation: int = 0,
        limit: int = 100,
    ) -> tuple[dict[str, object], ...]:
        """为停止后重新上线的 Workspace 建立逐 generation 的待导入记录。"""
        if not workspace_id.strip() or after_generation < 0:
            raise ValueError("fan-out workspace 或 high-water 无效")
        records = self.list_config_source_journal(
            source_key=source_key,
            after_generation=after_generation,
            limit=limit,
        )
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now_text()
            for record in records:
                connection.execute(
                    """
                    INSERT INTO config_source_fanout(
                        source_key, source_generation, workspace_id, status,
                        layer_revision, layer_digest, result, error, updated_at
                    ) VALUES (?, ?, ?, 'pending', NULL, NULL, NULL, NULL, ?)
                    ON CONFLICT(source_key, source_generation, workspace_id)
                    DO NOTHING
                    """,
                    (
                        source_key,
                        record.source_generation,
                        workspace_id,
                        now,
                    ),
                )
            # 返回必须反映库中真实状态：ON CONFLICT DO NOTHING 不会把既有
            # applied/conflict 行改回 pending，此处按 workspace 回读权威状态。
            statuses = dict(
                connection.execute(
                    """
                    SELECT source_generation, status FROM config_source_fanout
                    WHERE source_key = ? AND workspace_id = ?
                    """,
                    (source_key, workspace_id),
                ).fetchall()
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return tuple(
            {
                "source_generation": record.source_generation,
                "source_event_id": record.source_event_id,
                "fanout_id": record.fanout_id,
                "status": str(statuses[record.source_generation]),
            }
            for record in records
        )

    def config_source_fanout_summary(
        self,
        *,
        source_key: str,
        source_generation: int,
        workspace_ids: tuple[str, ...],
    ) -> dict[str, object]:
        """汇总 fan-out，明确区分全部完成、进行中和 fanout_partial。"""
        if not workspace_ids or len(workspace_ids) != len(set(workspace_ids)):
            raise ValueError("fan-out workspace_ids 不能为空且不能重复")
        statuses = {
            str(item["workspace_id"]): str(item["status"])
            for item in self.list_config_source_fanout(
                source_key=source_key,
                source_generation=source_generation,
            )
        }
        missing = tuple(
            workspace_id for workspace_id in workspace_ids if workspace_id not in statuses
        )
        failed = tuple(
            workspace_id
            for workspace_id in workspace_ids
            if statuses.get(workspace_id) in {"conflict", "failed"}
        )
        pending = tuple(
            workspace_id
            for workspace_id in workspace_ids
            if workspace_id in statuses
            and statuses[workspace_id] not in {"applied", "conflict", "failed"}
        )
        if failed:
            result = "fanout_partial"
        elif missing or any(
            statuses.get(workspace_id) != "applied" for workspace_id in workspace_ids
        ):
            result = "pending"
        else:
            result = "applied"
        return {
            "source_key": source_key,
            "source_generation": source_generation,
            "result": result,
            "missing_workspace_ids": missing,
            "pending_workspace_ids": pending,
            "failed_workspace_ids": failed,
        }

    def list_config_source_fanout(
        self,
        *,
        source_key: str,
        source_generation: int,
    ) -> tuple[dict[str, object], ...]:
        connection = self._database.connection()
        try:
            rows = connection.execute(
                """
                SELECT workspace_id, status, layer_revision, layer_digest, result,
                       error, updated_at
                FROM config_source_fanout
                WHERE source_key = ? AND source_generation = ?
                ORDER BY workspace_id ASC
                """,
                (source_key, source_generation),
            ).fetchall()
        finally:
            connection.close()
        return tuple(
            {
                "workspace_id": str(row[0]),
                "status": str(row[1]),
                "layer_revision": int(row[2]) if row[2] is not None else None,
                "layer_digest": str(row[3]) if row[3] is not None else None,
                "result": str(row[4]) if row[4] is not None else None,
                "error": str(row[5]) if row[5] is not None else None,
                "updated_at": str(row[6]),
            }
            for row in rows
        )
