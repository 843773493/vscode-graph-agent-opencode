"""状态版本的现行结构与已登记 19→20 SQL 资产。"""

from __future__ import annotations

from app.core.sqlite_state import load_sql

CHANNEL_VALUES_SCHEMA_SQL = load_sql(__package__, "migrations/checkpoint/checkpoint_state_values.sql") + load_sql(__package__, "migrations/checkpoint/checkpoint_channels.sql")
CHANNEL_VALUES_MIGRATION_SQL = (
    load_sql(__package__, "migrations/checkpoint/checkpoint_channels_019_020_prepare.sql")
    + CHANNEL_VALUES_SCHEMA_SQL
    + load_sql(__package__, "migrations/checkpoint/checkpoint_channels_019_020.sql")
)
