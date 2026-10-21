"""当前 typed plan registry DDL；由 schema owner 初始化。"""

from __future__ import annotations

from app.core.sqlite_state import load_sql

PLAN_REGISTRY_SCHEMA_SQL = load_sql(__package__, "sql/plan_registry.sql")


TOOL_SET_SNAPSHOT_SCHEMA_SQL = load_sql(__package__, "sql/tool_set_snapshot.sql")
