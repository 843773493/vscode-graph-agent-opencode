"""按权威 catalog 只读核对离线附件迁移输入，不扫描日期桶。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_catalog_store.fork_source_capture import ForkSourceCaptureMixin
from app.core.session_catalog_store.queries import _register_session_list_functions
from app.core.session_catalog_store.schema_validation import require_catalog_schema
from app.core.session_catalog_store.validators import validate_session_storage_identity
from app.core.session_control_store import SessionControlStore
from app.core.workspace_identity import read_workspace_id
from app.domain.itemized.enums import ControlOutcome
from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    digest_for_blob_id,
    validate_blob_relative_locator,
)
from app.services.infrastructure.attachment_blob_catalog.schema import (
    _SCHEMA_STATEMENTS,
)
from app.services.infrastructure.rollout_context.fork.source_capture import (
    read_captured_snapshot,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    ROLLOUT_SCHEMA_VERSION,
)

SOURCE_ATTACHMENT_SCHEMA = 3


@dataclass(frozen=True, slots=True)
class BackupSource:
    path: Path
    locator: str
    kind: Literal["file", "sqlite"]
    digest: str
    wal_digest: str | None = None


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wal_digest(path: Path) -> str | None:
    wal = path.with_name(path.name + "-wal")
    # SQLite 只读打开也可能创建空 WAL；空文件与缺失均没有已提交页，非空正文仍逐字核对。
    return file_digest(wal) if wal.exists() and wal.stat().st_size else None


def checked_path(root: Path, locator: str) -> Path:
    if root.is_symlink():
        raise RuntimeError("离线维护根目录不能是符号链接")
    parts = locator.split("/")
    if not parts or any(part in {"", ".", ".."} or "\\" in part or ":" in part for part in parts):
        raise ValueError("备份 locator 不是受检相对路径")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise RuntimeError(f"离线维护拒绝符号链接: locator={locator}")
    return path


@contextmanager
def read_database(path: Path, *, frozen: bool = False) -> Iterator[sqlite3.Connection]:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise RuntimeError("离线维护数据库不是普通文件")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and not stat.S_ISREG(sidecar.lstat().st_mode)):
            raise RuntimeError("离线维护数据库 sidecar 不是普通文件")
    # backup API 生成的独立快照以 immutable 核验，不在备份中产生 WAL/SHM。
    uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if frozen else "")
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        _register_session_list_functions(connection)
        connection.execute("BEGIN")
        if tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
            raise RuntimeError("离线维护数据库完整性检查失败")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("离线维护数据库外键检查失败")
        yield connection


def require_terminal(connection: sqlite3.Connection, table: str, key: str, field: str, states: tuple[str, ...]) -> None:
    placeholders = ",".join("?" for _ in states)
    row = connection.execute(
        f"SELECT {key}, {field} FROM {table} WHERE {field} NOT IN ({placeholders}) LIMIT 1", states,
    ).fetchone()
    if row is not None:
        raise RuntimeError(f"离线迁移须先由原 owner 收敛操作: table={table}, identity={row[0]}, state={row[1]}")


def _version(connection: sqlite3.Connection, expected: int, label: str) -> None:
    actual = connection.execute("PRAGMA user_version").fetchone()[0]
    if actual != expected:
        raise RuntimeError(f"离线迁移不支持该库版本: owner={label}, version={actual}, supported={expected}")


def _source(root: Path, locator: str, *, database: bool = False) -> BackupSource:
    path = checked_path(root, locator)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise RuntimeError(f"离线迁移输入不是普通文件: locator={locator}")
    return BackupSource(path, locator, "sqlite" if database else "file", file_digest(path),
                        wal_digest(path) if database else None)


def _tree(root: Path, directory: Path, *, frozen: bool = False) -> list[BackupSource]:
    sources = []
    for path in sorted(directory.rglob("*")):
        locator = path.relative_to(root).as_posix()
        checked_path(root, locator)
        if path.is_dir():
            continue
        # 只有线上 owner 的 SQLite 使用 backup API；冻结 capture 内的数据库必须逐字保全。
        database = not frozen and (path.name == "session-control.sqlite" or (path.name == "index.sqlite" and path.parent.name == "rollout"))
        if not frozen and path.name.endswith(("-wal", "-shm", "-journal")):
            base = path.with_name(path.name.rsplit("-", 1)[0])
            if base.name == "session-control.sqlite" or (base.name == "index.sqlite" and base.parent.name == "rollout"):
                continue
        sources.append(_source(root, locator, database=database))
    return sources


def _verify_control(path: Path) -> None:
    with read_database(path) as connection:
        _version(connection, SessionControlStore.SCHEMA_VERSION, "session_control")
        require_terminal(connection, "session_operation_leases", "lease_id", "state", ("completed", "cancelled", "failed"))
        require_terminal(connection, "thread_creation_records", "child_thread_id", "state", ("published", "aborted"))


def _verify_rollout(path: Path, session_id: str) -> None:
    with read_database(path) as connection:
        owner = connection.execute("SELECT session_id, schema_version FROM database_meta WHERE singleton_id=1").fetchone()
        if owner is None or tuple(owner) != (session_id, ROLLOUT_SCHEMA_VERSION):
            raise RuntimeError("离线迁移 rollout owner 或 schema 不匹配")
        require_terminal(connection, "execution_queue", "job_id", "phase", ("queued", "terminal"))
        require_terminal(connection, "executions", "execution_id", "outcome", tuple(ControlOutcome))
        row = connection.execute(
            "SELECT execution_id FROM executions e WHERE outcome='unknown' AND NOT EXISTS "
            "(SELECT 1 FROM execution_queue q WHERE q.execution_id=e.execution_id AND q.phase='queued') LIMIT 1"
        ).fetchone()
        if row is not None:
            raise RuntimeError(f"离线迁移存在尚未核实的执行: execution_id={row[0]}")


def _verify_attachment_schema(connection: sqlite3.Connection) -> None:
    # 与线上初始化共用唯一 SQL 顺序，只在内存生成参照，绝不修复迁移源。
    with closing(sqlite3.connect(":memory:")) as canonical:
        for statement in _SCHEMA_STATEMENTS:
            canonical.execute(statement)
        expected = {tuple(row) for row in canonical.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )}
    actual = {tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    )}
    if not expected <= actual:
        missing = sorted(row[1] for row in expected - actual)
        raise RuntimeError(f"离线迁移附件 schema 3 不完整，拒绝自动修复: objects={missing}")
    if connection.execute("SELECT revision FROM attachment_catalog_revision WHERE id=1").fetchone() is None:
        raise RuntimeError("离线迁移附件 revision 单例缺失")


def plan_backup(workspace: Path) -> tuple[str, int, list[BackupSource]]:
    """调用方持独占维护锁；排队输入保留，未知操作和外部任务不猜测终态。"""
    workspace_id = read_workspace_id(workspace)
    root = workspace / ".boxteam"
    sources: dict[str, BackupSource] = {}

    def add(source: BackupSource) -> None:
        existing = sources.setdefault(source.locator, source)
        if existing != source:
            raise RuntimeError(f"备份期间输入发生变化: locator={source.locator}")

    catalog_path = checked_path(root, "navigation/session-catalog.sqlite")
    with read_database(catalog_path) as catalog:
        _version(catalog, SessionCatalogStore.SCHEMA_VERSION, "session_catalog")
        require_catalog_schema(catalog, database_path=catalog_path, current=SessionCatalogStore.SCHEMA_VERSION, database_label="附件迁移来源")
        for table, key, field, states in (
            ("nodes", "node_id", "state", ("active",)),
            ("session_creation_records", "session_id", "state", ("published", "aborted")),
            ("subtree_delete_records", "root_node_id", "state", ("completed", "aborted")),
            ("fork_source_captures", "copy_operation_id", "state", ("materialized", "aborted")),
        ):
            require_terminal(catalog, table, key, field, states)
        if catalog.execute("SELECT 1 FROM fork_source_captures WHERE export_release_state='pending' LIMIT 1").fetchone():
            raise RuntimeError("离线迁移存在未核实的 fork export release")
        generation = int(catalog.execute("SELECT generation FROM catalog_metadata WHERE singleton_id=1").fetchone()[0])
        for row in catalog.execute("SELECT * FROM nodes WHERE kind='session'"):
            if row["workspace_id"] != workspace_id:
                raise RuntimeError("离线迁移 Session workspace owner 不匹配")
            locator = row["storage_relative_locator"]
            validate_session_storage_identity(row["node_id"], datetime.fromisoformat(row["created_at"]), locator)
            directory = checked_path(root, locator)
            if not directory.is_dir():
                raise RuntimeError(f"离线迁移已登记 Session 目录缺失: session_id={row['node_id']}")
            _verify_control(directory / "session-control.sqlite")
            for source in _tree(root, directory):
                if source.kind == "sqlite" and source.path.name == "index.sqlite":
                    _verify_rollout(source.path, row["node_id"])
                add(source)
        for row in catalog.execute("SELECT * FROM fork_source_captures"):
            record = ForkSourceCaptureMixin._fork_capture_from_row(row)
            snapshot_id = row["snapshot_id"]
            if not isinstance(snapshot_id, str) or len(snapshot_id) != 32 or any(c not in "0123456789abcdef" for c in snapshot_id):
                raise RuntimeError("离线迁移 fork snapshot 身份非法")
            capture = checked_path(root, f"navigation/fork-source-captures/{snapshot_id}")
            if capture.exists():
                read_captured_snapshot(root / "sessions", record)
                for source in _tree(root, capture, frozen=True):
                    add(source)
            elif row["state"] != "aborted":
                raise RuntimeError(f"离线迁移冻结 fork capture 缺失: snapshot_id={snapshot_id}")
    add(_source(root, "navigation/session-catalog.sqlite", database=True))
    attachment_path = checked_path(root, "attachments/catalog.sqlite")
    with read_database(attachment_path) as attachment:
        _version(attachment, SOURCE_ATTACHMENT_SCHEMA, "attachments")
        _verify_attachment_schema(attachment)
        require_terminal(attachment, "attachment_ingest_records", "ingest_id", "state", ("published", "aborted"))
        require_terminal(attachment, "attachment_blob_commit_claims", "blob_id", "state", ("published", "aborted"))
        require_terminal(attachment, "fork_attachment_claims", "claim_id", "state", ("committed", "aborted", "target_deleted"))
        for row in attachment.execute("SELECT * FROM attachment_blobs"):
            validate_blob_relative_locator(row["relative_locator"])
            if row["digest"] != digest_for_blob_id(row["blob_id"]):
                raise RuntimeError("离线迁移附件 blob/digest 身份冲突")
            if row["availability"] == "tombstoned":
                continue
            source = _source(root, "attachments/" + row["relative_locator"])
            if row["digest"] != "sha256:" + source.digest or source.path.stat().st_size != row["length"]:
                raise RuntimeError(f"离线迁移附件原件 hash/length 不一致: blob_id={row['blob_id']}")
            add(source)
        for row in attachment.execute("SELECT attachment_id FROM attachment_owner_refs"):
            identity = row[0]
            if not isinstance(identity, str) or len(identity) != 36 or not identity.startswith("att_") or any(c not in "0123456789abcdef" for c in identity[4:]):
                raise RuntimeError("离线迁移 attachment 身份非法")
            preview = checked_path(root, f"attachments/.derived/{identity}-512.webp")
            if preview.exists():
                add(_source(root, preview.relative_to(root).as_posix()))
    add(_source(root, "attachments/catalog.sqlite", database=True))
    add(_source(root, "workspace-identity.json"))
    ledger = checked_path(root, "resources.json")
    if ledger.exists():
        payload = json.loads(ledger.read_text())
        for name, terminal in (("records", {"stopped"}), ("leases", {"released", "settled"})):
            rows = payload[name]
            if not isinstance(rows, list) or any(not isinstance(row, dict) or row.get("status") not in terminal for row in rows):
                raise RuntimeError(f"离线迁移存在未核实的外部资源或占用: ledger={name}")
        resource_ids = {row["resource_id"] for row in payload["records"]}
        if any(row["resource_id"] not in resource_ids for row in payload["leases"]):
            raise RuntimeError("离线迁移存在缺少资源记录的外部占用")
        add(_source(root, "resources.json"))
    return workspace_id, generation, sorted(sources.values(), key=lambda source: source.locator)
