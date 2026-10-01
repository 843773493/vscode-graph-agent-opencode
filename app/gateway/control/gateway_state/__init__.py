"""Gateway 全局状态库 facade（单点实现，按垂直链路拆包）。

原先的单文件 ``app/gateway/control/gateway_state.py`` 已按垂直链路拆入本包：
连接生命周期与 KV 读写（facade 本文件）、legacy 秘密迁移（``lifecycle.py``）、
active snapshot 与 pending candidate（``active_snapshot.py``）、active 提升
（``apply_promotion.py``）、config apply journal（``apply_journal.py``）、
config apply claim（``apply_claim.py``）、gateway restart intent
（``restart_intent.py``）、restart apply 与失败记录（``restart_apply.py``），
schema 迁移序列与一次性镜像行迁移收敛在 ``_schema.py``。

本 facade 保留 ``GatewayConfigRecord``、``GatewayStateStore`` 类声明（原有四个
兄弟子包 mixin + 本包七个 mixin）与连接生命周期 / KV 读写方法，并按原名再导出
``_GATEWAY_MIGRATIONS``；对外契约与导入路径保持不变：
``app.gateway.control.gateway_state.GatewayStateStore``。

错误分类约定：``ValueError`` 输入形态非法、``KeyError`` 目标记录缺失、
``ConfigConflictError`` CAS/幂等冲突、``RuntimeError`` 事务后读取失败。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.core.sqlite_state import (
    SQLiteDiagnostics,
    SQLiteStateDatabase,
    utc_now_text,
)
from app.gateway.control.gateway_config_events import GatewayConfigEventMixin
from app.gateway.control.gateway_config_source import GatewayConfigSourceMixin
from app.gateway.control.gateway_registry import GatewayRegistryMixin
from app.gateway.control.gateway_runtime_generation import (
    GatewayRuntimeGenerationMixin,
)
from app.gateway.control.gateway_state._schema import (
    _GATEWAY_MIGRATIONS as _GATEWAY_MIGRATIONS,
)
from app.gateway.control.gateway_state.active_snapshot import (
    ConfigActiveSnapshotMixin,
)
from app.gateway.control.gateway_state.apply_claim import ConfigApplyClaimMixin
from app.gateway.control.gateway_state.apply_journal import (
    ConfigApplyJournalMixin,
)
from app.gateway.control.gateway_state.apply_promotion import (
    ConfigApplyPromotionMixin,
)
from app.gateway.control.gateway_state.lifecycle import GatewayLifecycleMixin
from app.gateway.control.gateway_state.restart_apply import (
    GatewayRestartApplyMixin,
)
from app.gateway.control.gateway_state.restart_intent import (
    GatewayRestartIntentMixin,
)
from app.services.infrastructure.config.state import (
    dump_json,
    prepare_config_for_persistence,
)


@dataclass(frozen=True, slots=True)
class GatewayConfigRecord:
    config_key: str
    config_version: int
    payload: dict[str, object]


class GatewayStateStore(
    GatewayRegistryMixin,
    GatewayConfigSourceMixin,
    GatewayConfigEventMixin,
    GatewayRuntimeGenerationMixin,
    GatewayLifecycleMixin,
    ConfigActiveSnapshotMixin,
    ConfigApplyPromotionMixin,
    ConfigApplyJournalMixin,
    ConfigApplyClaimMixin,
    GatewayRestartIntentMixin,
    GatewayRestartApplyMixin,
):
    def __init__(self, *, path: Path, allow_shared_processes: bool = False) -> None:
        self._database = SQLiteStateDatabase(
            path=path,
            schema_version=len(_GATEWAY_MIGRATIONS),
            migrations=_GATEWAY_MIGRATIONS,
            # 只有 supervisor 管理的 pending successor 才允许共享 WAL；
            # 普通 Gateway 继续保留单进程 ownership lock。
            allow_shared_processes=allow_shared_processes,
        )

    @property
    def path(self) -> Path:
        return self._database.path

    def diagnostics(self) -> SQLiteDiagnostics:
        return self._database.diagnostics()

    def connection(self) -> sqlite3.Connection:
        return self._database.connection()

    def set_config(
        self,
        *,
        config_key: str,
        config_version: int,
        payload: dict[str, object],
    ) -> None:
        connection = self._database.connection()
        try:
            connection.execute(
                """
                INSERT INTO gateway_config(config_key, config_version, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(config_key) DO UPDATE SET
                    config_version=excluded.config_version,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    config_key,
                    config_version,
                    dump_json(prepare_config_for_persistence(payload)),
                    utc_now_text(),
                ),
            )
        finally:
            connection.close()

    def get_config(self, config_key: str) -> GatewayConfigRecord | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                "SELECT config_key, config_version, payload_json FROM gateway_config WHERE config_key = ?",
                (config_key,),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(str(row[2]))
            if not isinstance(payload, dict):
                raise ValueError(f"Gateway SQLite 配置不是对象: key={config_key}")
            return GatewayConfigRecord(
                config_key=str(row[0]),
                config_version=int(row[1]),
                payload=payload,
            )
        finally:
            connection.close()

    def close(self) -> None:
        self._database.close()

    def __enter__(self) -> GatewayStateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
