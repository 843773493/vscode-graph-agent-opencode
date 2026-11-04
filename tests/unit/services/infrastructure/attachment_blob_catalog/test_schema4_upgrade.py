"""固定旧 schema 4 转换，只迁结构，原件与结果仍由原 owner 核实。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.resource_uri import parse_resource_uri
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog.directories import (
    remove_directory,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_directory,
    resolve_file_version,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.unit.services.infrastructure.test_attachment_blob_catalog import (
    SESSION_A,
    _attach,
    _store,
)

BODY = b"schema4-original-body"


@dataclass(frozen=True)
class Schema4Case:
    root: Path
    database: Path
    attachment_id: str
    reference: AttachmentRef
    directory: Path
    created_at: str
    association: tuple[object, ...]


@pytest.fixture
async def schema4_case(tmp_path: Path, session_bundle_factory) -> Schema4Case:
    with closing(_store(tmp_path, session_bundle_factory, SESSION_A)) as store:
        reference, = await store.persist_inline(SESSION_A, [_attach("schema4.txt", "text/plain", BODY)],
            thread_id=store._path_resolver.main_thread_id(SESSION_A), message_id="msg_schema4")
        attachment_id = parse_resource_uri(reference.file_id).attachment_id
        assert attachment_id is not None
        store.release_session_references(SESSION_A)
        original = store._catalog.get_directory(attachment_id)
        assert original is not None
        directory = resolve_attachment_directory(store._attachments_root, original.relative_locator)
        source = store._catalog.connection
        tables = [row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        rows = {name: [tuple(row) for row in source.execute(f"SELECT * FROM {name}")]
                for name in tables if name not in {"attachment_directories", "attachment_directory_metadata"}}
        association, = rows["attachment_directory_associations"]
        workspace_id = store.workspace_id
        database = store._catalog.database_path
    # 字节和关联来自正式上传；固定旧 DDL 只构造存量格式，不另实现旧 owner。
    remove_directory(tmp_path / ".boxteam/attachments/.versions")
    target = database.with_suffix(".schema4.sqlite")
    with closing(sqlite3.connect(target)) as connection:
        connection.executescript((Path.cwd() / "tests/unit/services/infrastructure/attachment_blob_catalog/schema4.sql").read_text())
        connection.execute("INSERT INTO attachment_directory_metadata VALUES (1,?,4)", (workspace_id,))
        connection.execute("INSERT INTO attachment_directories VALUES (?,?,?,?,?,?,?,?,?)", (
            attachment_id, original.relative_locator, "available", original.protection, original.created_at, original.updated_at,
            original.updated_at, (datetime.fromisoformat(original.updated_at) + timedelta(days=30)).isoformat(), None,
        ))
        for name, values in rows.items():
            for row in values:
                connection.execute(f"INSERT INTO {name} VALUES ({','.join('?' for _ in row)})", row)
        connection.commit()
    target.replace(database)
    return Schema4Case(tmp_path, database, attachment_id, reference, directory, original.created_at, association)


@pytest.fixture
def upgraded_store(schema4_case: Schema4Case):
    case = schema4_case
    with closing(AttachmentBlobStore(case.root,
        leases=ExternalResourceLeaseLedger(state_path=case.root / ".boxteam/resources.json"))) as store:
        yield store


@pytest.mark.parametrize("old_state", ["available", "deleting"])
async def test_upgrade_saves_original_before_release_and_rebuilds_same_uri(
    schema4_case: Schema4Case, upgraded_store: AttachmentBlobStore, old_state: str,
) -> None:
    case, store = schema4_case, upgraded_store
    with closing(sqlite3.connect(case.database)) as connection:
        connection.execute("UPDATE attachment_directories SET availability=?", (old_state,))
        connection.commit()
    assert store._catalog.connection.execute("PRAGMA user_version").fetchone()[0] == 5
    assert tuple(store._catalog.connection.execute("SELECT * FROM attachment_directory_associations").fetchone()) == case.association
    names = {row[1] for row in store._catalog.connection.execute("PRAGMA table_info(attachment_directories)")}
    assert "unattached_since" not in names and "gc_after" not in names
    assert await store.process_pending_directories() == (case.attachment_id,)
    assert not case.directory.exists()
    version = store._catalog.file_version(case.attachment_id, "original/content")
    assert version is not None
    assert resolve_file_version(store._attachments_root, version.digest).read_bytes() == BODY
    assert store.read(SESSION_A, case.reference.file_id).data == BODY
    final = store._catalog.get_directory(case.attachment_id)
    assert final is not None and final.created_at == case.created_at
    assert resolve_attachment_directory(store._attachments_root, final.relative_locator) == case.directory
    assert store.reference(case.attachment_id) == case.reference.file_id


async def test_upgrade_preserves_modified_results_on_repeated_processing(
    schema4_case: Schema4Case, upgraded_store: AttachmentBlobStore,
) -> None:
    case = schema4_case
    output = case.directory / "derived/result.txt"
    output.write_bytes(b"keep-native-processing-result")
    for _ in range(2):
        assert await upgraded_store.process_pending_directories() == ()
        record = upgraded_store._catalog.get_directory(case.attachment_id)
        assert record is not None and record.availability == "available" and record.release_pending == 0
        assert record.release_reason == "目录相对上传初始基线有修改，保留原位置与处理结果"
        assert output.read_bytes() == b"keep-native-processing-result"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_upgrade_missing_or_invalid_source_retains_directory_and_reason(
    schema4_case: Schema4Case, upgraded_store: AttachmentBlobStore, damage: str,
) -> None:
    case = schema4_case
    original = case.directory / "original/content"
    original.parent.chmod(0o700)
    original.chmod(0o600)
    if damage == "missing":
        original.unlink()
    else:
        original.write_bytes(b"changed-original")
    output = case.directory / "derived/result.txt"
    output.write_bytes(b"must-retain-result")
    with pytest.raises(FileNotFoundError if damage == "missing" else RuntimeError):
        await upgraded_store.process_pending_directories()
    record = upgraded_store._catalog.get_directory(case.attachment_id)
    assert record is not None and record.availability == "available" and record.release_pending == 1
    assert record.release_reason is not None and "工作副本处理未完成" in record.release_reason
    assert output.read_bytes() == b"must-retain-result"


async def test_upgrade_keeps_only_explicit_retention_promise(
    schema4_case: Schema4Case, upgraded_store: AttachmentBlobStore,
) -> None:
    case, store = schema4_case, upgraded_store
    until = datetime.fromisoformat(case.created_at) + timedelta(days=90)
    with closing(sqlite3.connect(case.database)) as connection:
        connection.execute("UPDATE attachment_directories SET retention_until=?", (until.isoformat(),))
        connection.commit()
    assert await store.process_pending_directories() == ()
    record = store._catalog.get_directory(case.attachment_id)
    assert record is not None and record.retention_until == until.isoformat()
    assert record.release_reason == "附件目录仍有明确保留承诺" and case.directory.is_dir()
    store._clock = lambda: until
    assert await store.process_pending_directories() == (case.attachment_id,)


async def test_upgrade_never_revives_legacy_tombstone(
    schema4_case: Schema4Case, upgraded_store: AttachmentBlobStore,
) -> None:
    case = schema4_case
    remove_directory(case.directory)
    with closing(sqlite3.connect(case.database)) as connection:
        connection.execute("UPDATE attachment_directories SET availability='tombstoned'")
        connection.commit()
    assert await upgraded_store.process_pending_directories() == ()
    record = upgraded_store._catalog.get_directory(case.attachment_id)
    assert record is not None and record.availability == "tombstoned"
    with pytest.raises(FileNotFoundError):
        upgraded_store.read(SESSION_A, case.reference.file_id)
    assert not case.directory.exists()


async def test_unknown_schema4_shape_is_rejected_without_changing_original(
    schema4_case: Schema4Case, upgraded_store: AttachmentBlobStore,
) -> None:
    case = schema4_case
    with closing(sqlite3.connect(case.database)) as connection:
        connection.execute("CREATE INDEX unknown_index ON attachment_directories(created_at)")
        connection.commit()
    with pytest.raises(RuntimeError, match="schema 4 结构不完整或被改写"):
        _ = upgraded_store._catalog.connection
    with closing(sqlite3.connect(case.database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT schema_version FROM attachment_directory_metadata").fetchone()[0] == 4
    assert (case.directory / "original/content").read_bytes() == BODY
    assert not (case.root / ".boxteam/attachments/.versions").exists()
