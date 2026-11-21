"""资源账本的增量事务及冷恢复读取；不在普通写入中遍历旧历史。"""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from pydantic_core import to_json

from app.core.atomic_fs import atomic_write_bytes, fsync_directory, native_io_path
from app.core.failures import failure_detail
from app.core.sqlite_state import (
    SQLiteProcessOwnership,
    SQLiteStateDatabase,
    load_sql,
    sqlite_file_uri,
)

from .models import RESOURCE_LEASES, RESOURCE_RECORD, ResourceLease, ResourceRecord


def retire_empty_legacy_ledger(workspace_root: Path, *, preview: bool = False) -> bool:
    """仅退休用户批准的空开发账本；非空或未知形状没有自动迁移合同。"""
    root = workspace_root / ".boxteam"
    path = native_io_path(root / "resources.json")
    if not path.exists():
        return False
    with ExitStack() as access:
        if not preview:
            ownership = SQLiteProcessOwnership(root / "state/workspace.sqlite")
            ownership.acquire()
            access.callback(ownership.release)
        original = path.read_bytes()
        if json.loads(original) != {"records": [], "leases": []}:
            raise ValueError(f"旧资源账本非空或形状未知，需要用户决定；保留原件: {path}")
        backup = native_io_path(root / "resources.json.retired-empty.bak")
        if backup.exists() and backup.read_bytes() != original:
            raise ValueError(f"空资源账本的原始备份内容不同，拒绝覆盖: {backup}")
        if preview:
            return True
        if not backup.exists():
            atomic_write_bytes(backup, original)
        path.unlink()
        fsync_directory(root)
        return True


class LeaseDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection | None]:
        if self.path.with_suffix(".json").exists():
            raise RuntimeError("资源账本仍为旧 JSON 格式，须显式处理后再打开: artifact=external_resource_leases")
        if not self.path.exists():
            if not write:
                yield None
                return
            with SQLiteStateDatabase(
                path=self.path, schema_version=1,
                migrations=(load_sql(__package__, "migrations/001.sql"),),
                allow_shared_processes=True,
            ):
                pass
        mode = "rw" if write else "ro"
        with closing(sqlite3.connect(
            sqlite_file_uri(self.path) + f"?mode={mode}", uri=True, timeout=5,
        )) as connection:
            versions = tuple(row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version"))
            if versions != (1,):
                raise RuntimeError(f"资源账本 schema 不受支持: versions={versions}")
            connection.execute("PRAGMA foreign_keys=ON")
            # 派发前的占用必须 durable，不能把断电后可能消失的记录当作已保护。
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            with connection:
                yield connection

    def read(self) -> tuple[dict[str, ResourceRecord], dict[str, ResourceLease]]:
        with self.connection() as connection:
            if connection is None:
                return {}, {}
            records = {}
            for key, payload in connection.execute("SELECT resource_id,payload FROM resource_records ORDER BY rowid"):
                record = RESOURCE_RECORD.validate_json(payload)
                if record.resource_id != key:
                    self._invalid("资源账本 resource_id 与原身份不一致")
                records[key] = record
            rows = connection.execute("SELECT lease_id,payload FROM resource_leases ORDER BY rowid").fetchall()
            values = RESOURCE_LEASES.validate_python([json.loads(row[1]) for row in rows])
            leases = {}
            for (key, _), lease in zip(rows, values, strict=True):
                if lease.lease_id != key:
                    self._invalid("资源账本 lease_id 与原身份不一致")
                if lease.resource_id not in records:
                    self._invalid("资源账本 lease 的 resource_id 未登记")
                leases[key] = lease
            return records, leases

    def _invalid(self, reason: str) -> None:
        error = TypeError(reason)
        error.add_note(f"资源账本位置: {self.path}")
        raise TypeError(failure_detail(error, stage="validate", label=f"{reason}: artifact=external_resource_leases")) from error

    def commit(self, *, records: Sequence[ResourceRecord], leases: Sequence[ResourceLease]) -> None:
        with self.connection(write=True) as connection:
            assert connection is not None
            connection.executemany(
                "INSERT INTO resource_records(resource_id,payload) VALUES (?,?) "
                "ON CONFLICT(resource_id) DO UPDATE SET payload=excluded.payload",
                [(record.resource_id, to_json(record, inf_nan_mode="constants").decode()) for record in records],
            )
            connection.executemany(
                "INSERT INTO resource_leases(lease_id,payload) VALUES (?,?) "
                "ON CONFLICT(lease_id) DO UPDATE SET payload=excluded.payload",
                [(lease.lease_id, to_json(lease).decode()) for lease in leases],
            )
