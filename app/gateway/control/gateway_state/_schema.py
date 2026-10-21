"""Gateway 全局状态库的 schema 迁移序列与一次性镜像行迁移。

承载 ``_GATEWAY_MIGRATIONS``（按序号逐条执行的建表/加列/索引 DDL，序号即
``SQLiteStateDatabase.schema_version`` 的权威来源）与一次性显式迁移
``_migrate_gateway_config_into_source_layers``。迁移 DDL 从同包 SQL 资产加载，
链路 mixin 不重复声明表结构，也不改动迁移序号。

错误分类沿用 gateway_state 约定：``ValueError`` 输入形态非法、
``ConfigConflictError`` CAS/并发冲突、``RuntimeError`` 事务后读取失败。
"""

from __future__ import annotations

import logging
import sqlite3

from app.core.config_sources import config_revision
from app.core.sqlite_state import load_sql
from app.services.infrastructure.config.state import load_json_object

# 日志器名固定为 facade 模块名，搬迁前后同一条日志走同一 logger（语义零变更）。
logger = logging.getLogger("app.gateway.control.gateway_state")


def _migrate_gateway_config_into_source_layers(
    connection: sqlite3.Connection,
) -> None:
    """一次性显式迁移：把 legacy ``gateway_config`` 中的 config 来源层镜像行并入权威
    ``config_source_layers`` 后物理删除这些镜像行；控制面独有 KV
    （``workspace_registry_meta``/``gateway_connection_ids``）MUST NOT 被迁移或删除。

    - 只有镜像 key（``gateway_*_mutable_override``）参与迁移；其余 key 一律保留。
    - 只有当镜像 key 在 ``gateway_config`` 有行、``config_source_layers`` 无同 key 行
      时才补一条等价 present layer 行；同 key 两处都在时必须逐字等价，冲突 fail-closed。
    - 只删镜像 key 行，不 DROP 表、不迁移控制面 KV。
    """

    exists = connection.execute(
        load_sql(__package__, "sql/legacy_config/exists.sql")
    ).fetchone()
    if exists is None:
        return
    mirror_keys = (
        "gateway_mutable_override",
        "gateway_local_mutable_override",
    )
    for config_key in mirror_keys:
        row = connection.execute(
            load_sql(__package__, "sql/legacy_config/read_mirror.sql"),
            (config_key,),
        ).fetchone()
        if row is None:
            continue
        config_version = int(row[0])
        payload_json = str(row[1])
        updated_at = str(row[2])
        try:
            payload = load_json_object(
                payload_json, field=f"gateway_config payload (key={config_key})"
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "legacy gateway_config 镜像行 payload 非法，无法并入权威 "
                f"config_source_layers: key={config_key}: {error}"
            ) from error
        layer = connection.execute(
            load_sql(__package__, "sql/legacy_config/read_source.sql"),
            (config_key,),
        ).fetchone()
        if layer is not None:
            layer_payload = (
                load_json_object(str(layer[1]), field="Gateway source layer payload")
                if layer[1] is not None
                else None
            )
            if str(layer[0]) != "present" or layer_payload != payload:
                raise RuntimeError(
                    "legacy gateway_config 镜像行与权威 config_source_layers 同 key 但"
                    f"语义不一致，拒绝静默择一: key={config_key}"
                )
        else:
            connection.execute(
                load_sql(__package__, "sql/legacy_config/insert_source.sql"),
                (
                    config_key,
                    config_version,
                    payload_json,
                    config_revision(payload),
                    updated_at,
                ),
            )
            logger.warning(
                "legacy gateway_config 镜像行已一次性并入 config_source_layers: key=%s",
                config_key,
            )
        connection.execute(
            load_sql(__package__, "sql/legacy_config/delete_mirror.sql"), (config_key,)
        )


_GATEWAY_MIGRATIONS = (
    load_sql(__package__, "sql/001.sql"),
    load_sql(__package__, "sql/002.sql"),
    load_sql(__package__, "sql/003.sql"),
    load_sql(__package__, "sql/004.sql"),
    load_sql(__package__, "sql/005.sql"),
    load_sql(__package__, "sql/006.sql"),
    load_sql(__package__, "sql/007.sql"),
    load_sql(__package__, "sql/008.sql"),
    load_sql(__package__, "sql/009.sql"),
    load_sql(__package__, "sql/010.sql"),
    load_sql(__package__, "sql/011.sql"),
    load_sql(__package__, "sql/012.sql"),
    load_sql(__package__, "sql/013.sql"),
    load_sql(__package__, "sql/014.sql"),
    # 配置来源位置改以 URI 表达（real path 不持久化）：丢弃旧真实路径列、新增 URI 列。
    load_sql(__package__, "sql/015.sql"),
    # 一次性显式迁移：把 legacy gateway_config 中的 config 来源层镜像行并入权威
    # config_source_layers 后物理删除这些镜像行；控制面独有 KV 必须留在该表。
    _migrate_gateway_config_into_source_layers,
    load_sql(__package__, "sql/017.sql"),
)
