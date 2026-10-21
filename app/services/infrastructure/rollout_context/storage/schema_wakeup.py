"""runtime wakeup 的 SQLite schema；不依赖执行 owner。"""

from app.core.sqlite_state import load_sql

RUNTIME_WAKEUP_SCHEMA_SQL = load_sql(__package__, "sql/runtime_wakeup.sql")
