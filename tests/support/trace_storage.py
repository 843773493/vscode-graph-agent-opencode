"""Trace 存储故障注入与原始证据读取，仅供测试使用。"""

from pathlib import Path

from app.services.infrastructure.turn_history.trace_database import TraceDatabase


def trace_bytes(path: Path) -> bytes:
    with TraceDatabase(path).connection() as connection:
        if connection is None:
            return b""
        return b"".join(
            row[0].encode("utf-8")
            for row in connection.execute("SELECT raw FROM trace_events ORDER BY start")
        )


def inject_trace_record(path: Path, raw: str, *, event_id: str | None = None) -> None:
    """绕过协议校验注入坏正文；生成列与 SQLite 索引仍由数据库维护。"""
    with TraceDatabase(path).connection(write=True) as connection:
        if event_id is None:
            tail = connection.execute(
                "SELECT end FROM trace_events ORDER BY start DESC LIMIT 1"
            ).fetchone()
            connection.execute(
                "INSERT INTO trace_events(start,raw) VALUES (?,?)",
                (tail[0] if tail else 0, raw),
            )
        else:
            changed = connection.execute(
                "UPDATE trace_events SET raw=? WHERE event_id=?", (raw, event_id)
            ).rowcount
            assert changed == 1
