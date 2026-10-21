"""Workspace 控制面 SQLite 状态库的装配点与 lifecycle/config-kv 方法族。

WorkspaceStateStore 组合 workspace activity、config event、config source 以及本模块
导入的 apply / snapshot / restart 各族 mixin；本模块显式加载 workspace_state_sql/ 迁移、
一次性来源层迁移回调与 lifecycle / config-kv 方法。"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from app.core.config_sources import config_revision
from app.core.sqlite_state import (
    SQLiteDiagnostics,
    SQLiteStateDatabase,
    load_sql,
    utc_now_text,
)
from app.services.infrastructure.config.state import (
    build_secret_binding_summary,
    dump_json,
    load_json_object,
    migrate_legacy_secret_payload,
)
from app.services.infrastructure.workspace_activity.workspace_activity import (
    WorkspaceActivityCursorGoneError,
    WorkspaceActivityMixin,
    WorkspaceActivityRecord,
    WorkspaceActivityService,
)
from app.services.infrastructure.workspace_config_events.workspace_config_events import (
    WorkspaceConfigEventMixin,
)
from app.services.infrastructure.workspace_config_source.workspace_config_source import (
    WorkspaceConfigSourceMixin,
)
from app.services.infrastructure.workspace_state_store_apply import (
    WorkspaceStateStoreApplyMixin,
)
from app.services.infrastructure.workspace_state_store_pending import (
    WorkspaceStateStorePendingMixin,
)
from app.services.infrastructure.workspace_state_store_restart import (
    WorkspaceStateStoreRestartMixin,
)
from app.services.infrastructure.workspace_state_store_snapshot import (
    WorkspaceStateStoreSnapshotMixin,
)

__all__ = [
    "WorkspaceActivityCursorGoneError",
    "WorkspaceActivityRecord",
    "WorkspaceActivityService",
    "WorkspaceStateStore",
]

logger = logging.getLogger(__name__)


def _migrate_workspace_config_into_source_layers(
    connection: sqlite3.Connection,
) -> None:
    """一次性显式迁移：把 legacy ``workspace_config`` 残留行并入权威 layer 表后删表。

    - 只有当 ``workspace_config`` 有行、``config_source_layers`` 无同 key 行时，
      才补一条等价 present layer 行（``layer_revision``/``source_generation``/``uri``
      按下标初始化，digest 由 payload 计算）——这是运行时覆盖读从旧表迁到权威表的
      唯一活兼容来源，必须还原语义。
    - 同 key 两表都在时必须逐字等价，冲突即 fail-closed，绝不静默择一。
    - 迁移后物理 DROP 该表；不双读、不留别名、不扫描重建。
    """

    exists = connection.execute(
        load_sql(__package__, "workspace_state_sql/legacy_config/exists.sql")
    ).fetchone()
    if exists is None:
        return
    rows = connection.execute(
        load_sql(__package__, "workspace_state_sql/legacy_config/read_mirror.sql")
    ).fetchall()
    for row in rows:
        config_key = str(row[0])
        config_version = int(row[1])
        payload_json = str(row[2])
        updated_at = str(row[3])
        try:
            payload = load_json_object(
                payload_json, field=f"workspace_config payload (key={config_key})"
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "legacy workspace_config 迁移失败：payload 非法，无法并入 "
                f"config_source_layers: key={config_key}: {error}"
            ) from error
        layer = connection.execute(
            load_sql(__package__, "workspace_state_sql/legacy_config/read_source.sql"),
            (config_key,),
        ).fetchone()
        if layer is not None:
            layer_payload = (
                load_json_object(str(layer[1]), field="source layer payload")
                if layer[1] is not None
                else None
            )
            if str(layer[0]) != "present" or layer_payload != payload:
                raise RuntimeError(
                    "legacy workspace_config 与权威 config_source_layers 同 key 但"
                    f"语义不一致，拒绝静默择一: key={config_key}"
                )
            continue
        connection.execute(
            load_sql(__package__, "workspace_state_sql/legacy_config/insert_source.sql"),
            (
                config_key,
                config_version,
                payload_json,
                config_revision(payload),
                updated_at,
            ),
        )
        logger.warning(
            "legacy workspace_config 行已一次性并入 config_source_layers: key=%s",
            config_key,
        )
    connection.execute(load_sql(__package__, "workspace_state_sql/legacy_config/drop_mirror.sql"))


_WORKSPACE_MIGRATIONS = (
    load_sql(__package__, "workspace_state_sql/001.sql"),
    load_sql(__package__, "workspace_state_sql/002.sql"),
    load_sql(__package__, "workspace_state_sql/003.sql"),
    load_sql(__package__, "workspace_state_sql/004.sql"),
    load_sql(__package__, "workspace_state_sql/005.sql"),
    load_sql(__package__, "workspace_state_sql/006.sql"),
    load_sql(__package__, "workspace_state_sql/007.sql"),
    load_sql(__package__, "workspace_state_sql/008.sql"),
    load_sql(__package__, "workspace_state_sql/009.sql"),
    load_sql(__package__, "workspace_state_sql/010.sql"),
    load_sql(__package__, "workspace_state_sql/011.sql"),
    load_sql(__package__, "workspace_state_sql/012.sql"),
    # 配置来源位置改以 URI 表达（real path 不持久化）：丢弃旧真实路径列、新增 URI 列。
    # 旧值一律不迁移（列本身即被删除）；新列可空，sqlite 层不可寻址即写 NULL。
    load_sql(__package__, "workspace_state_sql/013.sql"),
    # 一次性显式迁移：把 legacy workspace_config 残留行并入权威 config_source_layers
    # 后物理 DROP 该镜像表（彻底根除表级双轨）。遇同 key 语义冲突 fail-closed。
    _migrate_workspace_config_into_source_layers,
)


class WorkspaceStateStore(
    WorkspaceActivityMixin,
    WorkspaceConfigEventMixin,
    WorkspaceConfigSourceMixin,
    WorkspaceStateStoreApplyMixin,
    WorkspaceStateStoreSnapshotMixin,
    WorkspaceStateStorePendingMixin,
    WorkspaceStateStoreRestartMixin,
):
    def __init__(self, *, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.expanduser().resolve()
        self._database = SQLiteStateDatabase(
            path=self.workspace_root / ".boxteam" / "state" / "workspace.sqlite",
            schema_version=len(_WORKSPACE_MIGRATIONS),
            migrations=_WORKSPACE_MIGRATIONS,
        )

    @property
    def path(self) -> Path:
        return self._database.path

    def connection(self) -> sqlite3.Connection:
        return self._database.connection()

    def diagnostics(self) -> SQLiteDiagnostics:
        return self._database.diagnostics()

    def migrate_legacy_config_secrets(self, config_key: str) -> tuple[str, ...]:
        """升级权威 layer 表中的秘密引用。

        字面量 key 保留原文（已受支持）；只有旧版本写入的不可逆
        ``literal-sha256:`` 摘要才会被记为阻断路径。
        """

        connection = self._database.connection()
        blocked: set[str] = set()
        try:
            connection.execute("BEGIN IMMEDIATE")
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
                        dump_json(migrated_payload) if migrated_payload is not None else None,
                        dump_json(migrated_previous) if migrated_previous is not None else None,
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
        """升级旧 active payload，并对无法恢复的旧摘要秘密建立恢复态。"""

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
            migrated, blocked = migrate_legacy_secret_payload(
                json.loads(str(row[0]))
            )
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
                    "旧 active snapshot 含无法恢复的秘密摘要，需重新导入引用",
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

    def _next_config_revision(
        self,
        connection,
        *,
        config_domain: str,
    ) -> int:
        row = connection.execute(
            "SELECT next_revision FROM config_revision_meta WHERE config_domain = ?",
            (config_domain,),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO config_revision_meta(config_domain, next_revision) VALUES (?, ?)",
                (config_domain, 2),
            )
            return 1
        revision = int(row[0])
        connection.execute(
            "UPDATE config_revision_meta SET next_revision = ? WHERE config_domain = ?",
            (revision + 1, config_domain),
        )
        return revision

    def close(self) -> None:
        self._database.close()

    def __enter__(self) -> WorkspaceStateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
