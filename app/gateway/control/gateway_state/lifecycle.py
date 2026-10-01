"""Gateway 全局状态库的 legacy 秘密迁移垂直链路。

承载 legacy ``gateway_config`` KV 行与 active snapshot 中秘密引用的迁移；
连接生命周期与 KV 读写方法保留在 facade ``__init__.py``。

错误分类沿用 gateway_state 约定：``ValueError`` 输入形态非法、
``ConfigConflictError`` CAS/并发冲突、``RuntimeError`` 事务后读取失败。
"""

from __future__ import annotations

import json

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    build_secret_binding_summary,
    dump_json,
    migrate_legacy_secret_payload,
)


class GatewayLifecycleMixin:
    """legacy 秘密迁移方法族（唯一实现点）。"""

    def migrate_legacy_config_secrets(self, config_key: str) -> tuple[str, ...]:
        """升级旧 Gateway 配置表和 source layer 中的秘密引用。

        字面量 key 保留原文（已受支持）；只有旧版本写入的不可逆
        ``literal-sha256:`` 摘要才会被记为阻断路径。
        """

        connection = self._database.connection()
        blocked: set[str] = set()
        try:
            connection.execute("BEGIN IMMEDIATE")
            legacy_row = connection.execute(
                "SELECT payload_json FROM gateway_config WHERE config_key = ?",
                (config_key,),
            ).fetchone()
            if legacy_row is not None:
                migrated, paths = migrate_legacy_secret_payload(
                    json.loads(str(legacy_row[0]))
                )
                blocked.update(paths)
                connection.execute(
                    "UPDATE gateway_config SET payload_json = ?, updated_at = ? WHERE config_key = ?",
                    (dump_json(migrated), utc_now_text(), config_key),
                )
            source_row = connection.execute(
                """
                SELECT payload_json, previous_payload_json
                FROM config_source_layers WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
            if source_row is not None:
                migrated_payload = None
                migrated_previous = None
                if source_row[0] is not None:
                    migrated_payload, paths = migrate_legacy_secret_payload(
                        json.loads(str(source_row[0]))
                    )
                    blocked.update(paths)
                if source_row[1] is not None:
                    migrated_previous, paths = migrate_legacy_secret_payload(
                        json.loads(str(source_row[1]))
                    )
                    blocked.update(paths)
                connection.execute(
                    """
                    UPDATE config_source_layers
                    SET payload_json = ?, previous_payload_json = ?, updated_at = ?
                    WHERE config_key = ?
                    """,
                    (
                        dump_json(migrated_payload)
                        if migrated_payload is not None
                        else None,
                        dump_json(migrated_previous)
                        if migrated_previous is not None
                        else None,
                        utc_now_text(),
                        config_key,
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return tuple(sorted(blocked))

    def migrate_legacy_active_snapshot_secrets(
        self,
        *,
        config_domain: str,
    ) -> tuple[str, ...]:
        """升级旧 Gateway active payload，并显式标记无法恢复的旧摘要秘密。"""

        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json FROM config_active_snapshot WHERE config_domain = ?",
                (config_domain,),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return ()
            migrated, blocked = migrate_legacy_secret_payload(json.loads(str(row[0])))
            connection.execute(
                """
                UPDATE config_active_snapshot
                SET payload_json = ?, secret_bindings_json = ?,
                    state = CASE WHEN ? = 1 THEN 'recovery_required' ELSE state END,
                    last_error = CASE WHEN ? = 1 THEN ? ELSE last_error END
                WHERE config_domain = ?
                """,
                (
                    dump_json(migrated),
                    dump_json(build_secret_binding_summary(migrated)),
                    int(bool(blocked)),
                    int(bool(blocked)),
                    "旧 Gateway active snapshot 含无法恢复的秘密摘要，需重新导入引用",
                    config_domain,
                ),
            )
            connection.execute("COMMIT")
            return blocked
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
