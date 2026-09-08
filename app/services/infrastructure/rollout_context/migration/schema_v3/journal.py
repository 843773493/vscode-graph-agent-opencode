"""非覆盖式 artifact 发布与可重试审计；不提交 SQLite、不回滚 canonical。"""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.migration.artifacts import (
    read_regular,
    require_safe_path,
    sync_directory,
    write_private,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.binding import (
    digest,
    validate_bound_sql,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.model import (
    SchemaV3UpgradeError,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.sql import (
    fingerprint,
    validate_target,
)


def private_directory(path: Path) -> None:
    """逐层创建并同步目录项，避免只 fsync 最深父目录遗漏新祖先。"""
    require_safe_path(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        sync_directory(directory)
        sync_directory(directory.parent)


def immutable_file(path: Path, raw: bytes, *, retry: bool) -> None:
    require_safe_path(path)
    temporary = path.with_name(f".{path.name}.{digest(raw)}.next")
    require_safe_path(temporary)
    if path.exists() and temporary.exists():
        target_info, next_info = path.lstat(), temporary.lstat()
        if (target_info.st_dev, target_info.st_ino) == (next_info.st_dev, next_info.st_ino):
            # link 发布后、unlink 临时名字之前退出；只清除本次字节身份对应的
            # 第二个名字，不触碰其他 link，不接受源文件或不匹配内容。
            if not retry or not stat.S_ISREG(target_info.st_mode) or target_info.st_nlink != 2:
                raise SchemaV3UpgradeError("schema-upgrade-artifact-conflict: publication link")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                if stream.read() != raw:
                    raise SchemaV3UpgradeError("schema-upgrade-artifact-conflict: publication bytes")
            temporary.unlink()
            sync_directory(path.parent)
    if path.exists():
        if not retry or read_regular(path) != raw:
            raise SchemaV3UpgradeError(f"schema-upgrade-artifact-conflict: {path}")
        return
    private_directory(path.parent)
    if temporary.exists():
        if read_regular(temporary) != raw:
            # 仅隔离本 audit 的未发布半文件；保留字节供审计，不覆盖半成品。
            isolated = temporary.with_name(temporary.name + ".incomplete-" + uuid4().hex)
            temporary.rename(isolated)
        else:
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
            sync_directory(path.parent)
            return
    write_private(temporary, raw)
    require_safe_path(path)
    os.link(temporary, path, follow_symlinks=False)
    temporary.unlink()
    sync_directory(path.parent)


@dataclass(frozen=True)
class PreparedSchemaV3Upgrade:
    migration_sql: str
    audit_id: str
    rollout_root: Path
    checkpoint_ns: str
    source_fingerprint: str
    target_fingerprint: str
    original_files: dict[str, str]
    new_files: dict[str, str]
    source_connection: sqlite3.Connection

    @property
    def audit_root(self) -> Path:
        return self.rollout_root / "schema-upgrade-v3" / self.audit_id

    @property
    def migration_checksum(self) -> str:
        return digest(self.migration_sql.encode())

    def _verify_originals(self) -> None:
        for relative, expected in self.original_files.items():
            if digest(read_regular(self.rollout_root / relative)) != expected:
                raise SchemaV3UpgradeError(f"source-mismatch: 迁移原件变化: {relative}")

    def publish(self) -> None:
        """调用方保持同一 connection 到 publish 完成；后续可关闭并提交主线事务。"""
        self._verify_originals()
        if fingerprint(self.source_connection) != self.source_fingerprint:
            raise SchemaV3UpgradeError("source-mismatch: prepare 后 SQLite 发生变化")
        for relative, expected in self.new_files.items():
            staged = read_regular(self.audit_root / "staged" / relative)
            if digest(staged) != expected:
                raise SchemaV3UpgradeError("source-mismatch: 暂存 typed detail hash")
            immutable_file(self.rollout_root / relative, staged, retry=True)
        immutable_file(self.audit_root / "published.json", canonical_json_bytes({"audit_id": self.audit_id}), retry=True)
        sync_directory(self.rollout_root)

    def verify_migrated(self, connection: sqlite3.Connection) -> None:
        """主线在 COMMIT 前调用；任一跨文件/SQL 验证失败必须回滚整个版本事务。"""
        validate_bound_sql(self.migration_sql, {
            "source_fingerprint": self.source_fingerprint, "target_fingerprint": self.target_fingerprint,
            "original_files": self.original_files, "new_files": self.new_files, "checkpoint_ns": self.checkpoint_ns,
        })
        self._verify_originals()
        version = connection.execute("SELECT schema_version FROM database_meta WHERE singleton_id=1").fetchone()
        completed = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE from_version=2 AND to_version=3 AND migration_checksum=? AND status='completed'",
            (self.migration_checksum,),
        ).fetchone()
        if version is None or version[0] < 3 or completed is None:
            raise SchemaV3UpgradeError("schema-upgrade-not-committed: 缺少匹配的 completed journal")
        if fingerprint(connection) != self.target_fingerprint:
            raise SchemaV3UpgradeError("source-mismatch: target SQLite 与已验证升级计划不一致")
        for relative, expected in self.new_files.items():
            if digest(read_regular(self.rollout_root / relative)) != expected:
                raise SchemaV3UpgradeError("source-mismatch: target typed detail 文件不一致")
        validate_target(connection, checkpoint_ns=self.checkpoint_ns)

    def verify_committed(self, connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            raise SchemaV3UpgradeError("schema-upgrade-not-committed: SQLite 仍在事务中")
        self.verify_migrated(connection)
        immutable_file(self.audit_root / "completed.json", canonical_json_bytes({
            "audit_id": self.audit_id, "migration_checksum": self.migration_checksum,
        }), retry=True)

    def discard_uncommitted(self, connection: sqlite3.Connection) -> None:
        """只记录 abort，保留新 orphan/staging 供显式重试；不删除任何既有文件。"""
        version = connection.execute("SELECT schema_version FROM database_meta WHERE singleton_id=1").fetchone()
        if version is None or version[0] != 2:
            raise SchemaV3UpgradeError("schema-upgrade-already-committed: 禁止丢弃已发布 SQL identity")
        self._verify_originals()
        immutable_file(self.audit_root / "aborted.json", canonical_json_bytes({"audit_id": self.audit_id}), retry=True)


def persist_prepared(connection: sqlite3.Connection, prepared: PreparedSchemaV3Upgrade, files: dict[str, bytes]) -> None:
    root = prepared.audit_root
    require_safe_path(root)
    private_directory(root)
    backup = root / "index.schema2.backup"
    require_safe_path(backup)
    if not backup.exists():
        temporary = root / ("index.schema2.backup.next-" + uuid4().hex)
        require_safe_path(temporary)
        # 崩溃留下 .next 只保留供审计；重试使用新的排他名字，从源重新备份。
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(temporary)) as destination:
            connection.backup(destination)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.rename(temporary, backup)
        sync_directory(root)
    read_regular(backup)
    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro&immutable=1", uri=True)) as source:
        if fingerprint(source) != prepared.source_fingerprint:
            raise SchemaV3UpgradeError("source-mismatch: schema2 backup 不属于本次升级")
    for relative in prepared.original_files:
        immutable_file(root / "originals" / relative, read_regular(prepared.rollout_root / relative), retry=True)
    for relative, raw in files.items():
        immutable_file(root / "staged" / relative, raw, retry=True)
    immutable_file(root / "migration.sql", prepared.migration_sql.encode(), retry=True)
    immutable_file(root / "prepared.json", canonical_json_bytes({
        "audit_id": prepared.audit_id, "source_fingerprint": prepared.source_fingerprint,
        "target_fingerprint": prepared.target_fingerprint,
        "migration_checksum": prepared.migration_checksum,
        "original_files": prepared.original_files, "new_files": prepared.new_files,
        "checkpoint_ns": prepared.checkpoint_ns,
    }), retry=True)
    sync_directory(root.parent)
