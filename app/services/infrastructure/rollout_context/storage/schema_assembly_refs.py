"""assembly ref 的当前 DDL 与显式 18→19 迁移；线程只从冻结 snapshot 取得。"""

from __future__ import annotations

from app.core.sqlite_state import load_sql

ASSEMBLY_REF_SCHEMA_SQL = load_sql(__package__, "sql/assembly_refs.sql")
ASSEMBLY_REF_THREAD_MIGRATION_SQL = load_sql(__package__, "sql/assembly_refs_018_019.sql")
