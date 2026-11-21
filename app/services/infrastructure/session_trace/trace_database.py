"""Trace 正文及结构化索引的唯一持久化 owner。"""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.atomic_fs import fsync_directory, fsync_file, native_io_path
from app.core.sqlite_state import SQLiteStateDatabase, load_sql, sqlite_file_uri
from app.schemas.event import Event

from .trace_cursor import TraceStreamRecord, encode_trace_cursor

_OWNER_TYPES_SQL = "event_type IN ('job_created', 'job_started', 'message_created')"
_SCHEMA_VERSION = 3


def _migrations() -> tuple[str, ...]:
    return tuple(load_sql(__package__, f"migrations/{name}") for name in (
        "001_trace.sql", "002_thread_index.sql", "003_job_lifecycle_index.sql",
    ))


class TraceDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        # 写连接由 TraceEventStore 的分片锁串行使用，读取始终独立打开快照。
        self._writer: sqlite3.Connection | None = None

    def _connect(self, *, write: bool) -> sqlite3.Connection | None:
        if not self.path.exists():
            # 开发中间态不是迁移契约；明确拒绝旧格式，不把旧历史伪装为空库。
            for name in (
                "events.jsonl",
                "messages.jsonl",
                "turn-events.index.json",
                "turn-events.index.jsonl",
            ):
                if (self.path.parent / name).exists():
                    raise RuntimeError(
                        f"Trace 使用旧 JSONL 格式，须显式处理后再打开: {self.path.parent / name}"
                    )
            if not write:
                return None
            # 先在同目录完成 schema，再发布主库；读者不会见到建库中的空表。
            native_io_path(self.path.parent).mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(
                prefix=".trace-", dir=native_io_path(self.path.parent)
            ) as staging:
                candidate = Path(staging) / self.path.name
                with SQLiteStateDatabase(
                    path=candidate,
                    schema_version=_SCHEMA_VERSION,
                    migrations=_migrations(),
                    allow_shared_processes=True,
                ):
                    pass
                fsync_file(candidate)
                try:
                    os.link(native_io_path(candidate), native_io_path(self.path))
                except FileExistsError:
                    # 并发初始化者已发布完整库，不能覆盖其后续提交。
                    pass
                fsync_directory(self.path.parent)
        mode = "rw" if write else "ro"
        connection = sqlite3.connect(
            sqlite_file_uri(self.path) + f"?mode={mode}",
            uri=True,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=not write,
        )
        try:
            connection.row_factory = sqlite3.Row
            version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            if version != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"Trace SQLite schema 不受支持: path={self.path}, version={version}; "
                    "请停写后显式执行 uv run python -m "
                    "app.services.infrastructure.session_trace.trace_database <trace.sqlite>"
                )
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection | None]:
        connection = self._connect(write=write)
        if connection is None:
            yield None
            return
        with closing(connection):
            # 所有多条读取共用一致快照；写入由 SQLite 原子提交，读取不占写锁。
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            with connection:
                yield connection

    def append(self, event: Event) -> TraceStreamRecord:
        # 提交与实时交付共用独立事件，后续调用方修改模型不会改变已提交事实。
        event = event.model_copy(deep=True)
        raw = event.__pydantic_serializer__.to_json(event) + b"\n"
        if self._writer is None:
            self._writer = self._connect(write=True)
        connection = self._writer
        assert connection is not None
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            tail = connection.execute(
                "SELECT end FROM trace_events ORDER BY start DESC LIMIT 1"
            ).fetchone()
            start = tail[0] if tail else 0
            connection.execute(
                "INSERT INTO trace_events(start, raw) VALUES (?, ?)",
                (start, raw.decode("utf-8")),
            )
            thread_id = connection.execute(
                "SELECT thread_id FROM trace_events WHERE start=?", (start,),
            ).fetchone()[0]
        return TraceStreamRecord(
            event=event,
            cursor=encode_trace_cursor(event_id=event.event_id, start=start, end=start + len(raw), line=raw),
            start=start, end=start + len(raw), thread_id=thread_id,
        )

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None


def job_threads(
    connection: sqlite3.Connection | None, session_id: str, jobs: set[str]
) -> dict[str, str]:
    owners: dict[str, str] = {}
    if connection is not None:
        # 每次只读目标 Job 的少量 owner 事实；生成列及索引由正文自动维护。
        for job in jobs:
            for row in connection.execute(
                f"SELECT owner_session, owner_thread FROM trace_events WHERE job_id=? AND {_OWNER_TYPES_SQL}",
                (job,),
            ):
                if row[0] != session_id or not isinstance(row[1], str) or not row[1]:
                    raise RuntimeError(f"Trace Job 归属与 Session 不一致: job_id={job}")
                previous = owners.setdefault(job, row[1])
                if previous != row[1]:
                    raise RuntimeError(f"Trace Job 的 Thread 归属漂移: job_id={job}")
    missing = jobs - owners.keys()
    if missing:
        raise RuntimeError(
            f"Trace Job 缺少持久 Thread 归属: session_id={session_id}, job_ids={sorted(missing)}"
        )
    return owners


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="停写后显式升级单个 Session Trace SQLite 索引")
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error(f"Trace SQLite 不存在: {args.database}")
    with SQLiteStateDatabase(
        path=args.database, schema_version=_SCHEMA_VERSION, migrations=_migrations(),
    ):
        pass
    print(f"Trace SQLite 已升级: {args.database}")
