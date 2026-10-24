"""产品上传生成旧附件，离线转换独立目录且不改写历史。"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.domain.itemized.execution_admission import UserExecutionIdentity
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.schemas.internal_v2.message import (
    AttachmentRef,
    MessageCreateRequest,
    MessageDTO,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceBusyError,
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.attachment_blob_catalog.migration import (
    backup,
    directories,
)
from app.services.infrastructure.attachment_blob_catalog.migration.preflight import (
    BackupSource,
    file_digest,
)
from tests.integration.backend.sessions.test_attachment_offline_backup import (
    BODY,
    BackupCase,
    cli,
)
from tests.integration.backend.sessions.test_attachment_offline_backup import (
    backup_case as backup_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_attachment_offline_backup import (
    online_backup_case as online_backup_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_attachment_offline_backup import (
    startup_container as startup_container,  # noqa: PLC0414
)


def accept_message(container: AppContainer, message: MessageDTO) -> None:
    identity = UserExecutionIdentity(
        job_id=create_prefixed_id("job"), turn_id=create_prefixed_id("turn"),
        admission_idempotency_key=create_prefixed_id("adm"),
    )
    container.message_service.accept_user_message(message, execution_identity=identity, queue_input=ExecutionQueueInput(
        execution_binding=identity, message_id=message.message_id, agent_id="default", provider_id=None,
        delivery_policy="after_turn", gateway_id=None, request_id=None, created_at=message.created_at.isoformat(),
    ))


@pytest.fixture
async def shared_blob_case(online_backup_case: BackupCase, startup_container: AppContainer) -> BackupCase:
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="相同原件的独立 owner"))
    message = await container.message_service.create(session.session_id, MessageCreateRequest(
        content="另一份上传", attachments=[AttachmentRef(
            file_id="inline:renamed.txt", name="另一份显示名.txt", content_type="text/plain",
            data_url="data:text/plain;base64," + base64.b64encode(BODY).decode(),
        )],
    ))
    accept_message(container, message)
    container.attachment_blob_store.close()
    container.session_catalog.close()
    return online_backup_case


def source_rows(case: BackupCase) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    path = case.workspace / ".boxteam/attachments/catalog.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return ([dict(row) for row in connection.execute("SELECT * FROM attachment_owner_refs ORDER BY attachment_id")],
                [dict(row) for row in connection.execute("SELECT * FROM attachment_blobs ORDER BY blob_id")])


def manifest(case: BackupCase) -> dict[str, object]:
    return json.loads((case.output / "converted/manifest.json").read_bytes())


async def test_cli_materializes_shared_blob_as_independent_directories_without_touching_history(shared_blob_case: BackupCase) -> None:
    case = shared_blob_case
    owners, blobs = source_rows(case)
    assert len(owners) == 2 and len(blobs) == 1
    backup.prepare_backup(case.workspace, case.output)
    before = {row["locator"]: file_digest(case.output / "data" / row["locator"])
              for row in json.loads((case.output / "manifest.json").read_bytes())["files"]}
    result = cli("convert", "--backup", str(case.output))
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["state"] == "directories_prepared" and response["directories"] == 2
    assert response["source_attachment_schema"] == 3
    assert str(case.workspace) not in result.stdout and str(case.output) not in result.stdout
    verified = cli("verify-directories", "--backup", str(case.output))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout) == response
    converted = manifest(case)
    resources = converted["resources"]
    assert [row["source_owner"] for row in resources] == owners
    assert all(row["source_blob"] == blobs[0] for row in resources)
    paths = []
    for row, owner in zip(resources, owners, strict=True):
        stamp = datetime.fromisoformat(owner["created_at"]).astimezone(UTC)
        assert row["relative_locator"] == f"{stamp:%Y/%m/%d}/{owner['attachment_id']}"
        root = case.output / "converted/attachments" / row["relative_locator"]
        assert (root / "original/content").read_bytes() == BODY
        assert (root / "preview").is_dir() and (root / "derived").is_dir()
        assert row["unattached_since"] is None and row["gc_after"] is None
        paths.append(root / "original/content")
    assert paths[0].stat().st_ino != paths[1].stat().st_ino
    assert all(file_digest(case.output / "data" / name) == digest for name, digest in before.items())
    assert case.original.read_bytes() == BODY
    with closing(sqlite3.connect(case.index_path)) as connection:
        assert tuple(connection.execute("SELECT item_id, content_hash FROM item_catalog ORDER BY item_sequence")) == case.canonical_items
        assert connection.execute("SELECT phase FROM execution_queue").fetchone() == ("queued",)
    with closing(sqlite3.connect(case.workspace / ".boxteam/attachments/catalog.sqlite")) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
    initial = {str(path.relative_to(case.output / "converted")): (file_digest(path), path.stat().st_mtime_ns)
               for path in (case.output / "converted").rglob("*") if path.is_file()}
    assert directories.convert_backup(case.output) == response
    assert initial == {str(path.relative_to(case.output / "converted")): (file_digest(path), path.stat().st_mtime_ns)
                       for path in (case.output / "converted").rglob("*") if path.is_file()}


async def test_product_image_preview_is_copied_without_regeneration(online_backup_case: BackupCase, startup_container: AppContainer) -> None:
    case = online_backup_case
    content = BytesIO()
    Image.new("RGB", (900, 600), (32, 64, 96)).save(content, format="PNG")
    message = await startup_container.message_service.create(case.session_id, MessageCreateRequest(
        content="图片预览", attachments=[AttachmentRef(
            file_id="inline:image.png", name="图像.png", content_type="image/png",
            data_url="data:image/png;base64," + base64.b64encode(content.getvalue()).decode(),
        )],
    ))
    attachment = message.attachments[0]
    preview = startup_container.attachment_blob_store.read_thumbnail(case.session_id, attachment.file_id).data
    accept_message(startup_container, message)
    startup_container.attachment_blob_store.close()
    startup_container.session_catalog.close()
    backup.prepare_backup(case.workspace, case.output)
    directories.convert_backup(case.output)
    resource = next(row for row in manifest(case)["resources"] if row["source_owner"]["mime_type"] == "image/png")
    root = case.output / "converted/attachments" / resource["relative_locator"]
    assert (root / "original/content").read_bytes() == content.getvalue()
    assert (root / "preview/512.webp").read_bytes() == preview
    with Image.open(root / "preview/512.webp") as image:
        assert max(image.size) == 512


@pytest.mark.parametrize("retention_days", [None, 10, 90])
async def test_released_owner_uses_recorded_release_time_and_preserves_explicit_protection(backup_case: BackupCase, retention_days: int | None) -> None:
    case = backup_case
    released = datetime(2020, 1, 2, 23, 45, tzinfo=UTC)
    until = None if retention_days is None else released + timedelta(days=retention_days)
    with closing(sqlite3.connect(case.workspace / ".boxteam/attachments/catalog.sqlite")) as connection:
        connection.execute("UPDATE attachment_owner_refs SET state='released', updated_at=?, retention_until=?",
                           (released.isoformat(), None if until is None else until.isoformat()))
        connection.commit()
    # 局部迁移输入只调整释放事实；上传和排队历史仍由产品生成，不充当真实 detach 验收。
    backup.prepare_backup(case.workspace, case.output)
    directories.convert_backup(case.output)
    resource, = manifest(case)["resources"]
    assert resource["unattached_since"] == released.isoformat()
    assert resource["gc_after"] == (released + timedelta(days=max(30, retention_days or 0))).isoformat()
    assert resource["source_owner"]["retention_until"] == (None if until is None else until.isoformat())


async def test_tombstone_does_not_create_an_empty_directory(backup_case: BackupCase) -> None:
    case = backup_case
    with closing(sqlite3.connect(case.workspace / ".boxteam/attachments/catalog.sqlite")) as connection:
        connection.execute("UPDATE attachment_owner_refs SET state='released'")
        connection.execute("UPDATE attachment_blobs SET availability='tombstoned'")
        connection.commit()
    case.original.unlink()
    backup.prepare_backup(case.workspace, case.output)
    response = directories.convert_backup(case.output)
    assert response["resources"] == 1 and response["directories"] == 0 and response["files"] == 0
    resource, = manifest(case)["resources"]
    assert resource["relative_locator"] is None and resource["source_blob"]["availability"] == "tombstoned"
    assert list((case.output / "converted/attachments").iterdir()) == []


async def test_conversion_uses_owner_utc_date_instead_of_blob_or_migration_date(backup_case: BackupCase) -> None:
    case = backup_case
    with closing(sqlite3.connect(case.workspace / ".boxteam/attachments/catalog.sqlite")) as connection:
        connection.execute("UPDATE attachment_owner_refs SET created_at='2024-03-01T00:30:00+14:00'")
        connection.commit()
    backup.prepare_backup(case.workspace, case.output)
    directories.convert_backup(case.output)
    resource, = manifest(case)["resources"]
    assert resource["relative_locator"] == "2024/02/29/" + resource["attachment_id"]


@pytest.mark.parametrize("damage", ["missing_thread", "missing_blob", "unknown_release_time"])
async def test_conversion_rejects_ambiguous_old_ownership_or_time(backup_case: BackupCase, damage: str) -> None:
    case = backup_case
    with closing(sqlite3.connect(case.workspace / ".boxteam/attachments/catalog.sqlite")) as connection:
        statement = {
            "missing_thread": "UPDATE attachment_owner_refs SET owner_thread_id='unknown'",
            "missing_blob": "DELETE FROM attachment_blobs",
            "unknown_release_time": "UPDATE attachment_owner_refs SET state='released', updated_at='2020-01-01T00:00:00'",
        }[damage]
        connection.execute(statement)
        connection.commit()
    backup.prepare_backup(case.workspace, case.output)
    with pytest.raises((ValueError, RuntimeError), match="Thread owner|正文版本|包含时区"):
        directories.convert_backup(case.output)
    assert not (case.output / "converted").exists()


async def test_interrupted_copy_can_resume_without_rewriting_completed_files(shared_blob_case: BackupCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = shared_blob_case
    backup.prepare_backup(case.workspace, case.output)
    copy = directories._copy
    count = 0

    def interrupt_second(source: BackupSource, target: Path) -> dict[str, object]:
        nonlocal count
        count += 1
        if count == 2:
            target.write_bytes(b"partial copy")
            raise OSError("复制中断")
        return copy(source, target)

    monkeypatch.setattr(directories, "_copy", interrupt_second)
    with pytest.raises(OSError, match="复制中断"):
        directories.convert_backup(case.output)
    assert json.loads((case.output / "converted/journal.json").read_bytes())["state"] == "preparing"
    completed, = (case.output / "converted/attachments").rglob("content")
    original_stat = completed.stat()
    with pytest.raises(RuntimeError, match="尚未完成"):
        directories.verify_directories(case.output)
    monkeypatch.setattr(directories, "_copy", copy)
    assert directories.convert_backup(case.output)["directories"] == 2
    assert completed.stat().st_ino == original_stat.st_ino and completed.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert not list((case.output / "converted").rglob("*.partial"))


@pytest.mark.parametrize("window", [
    "before_manifest", "manifest_partial", "manifest_published", "journal_partial",
    "journal_published", "before_root_rename", "after_root_rename",
])
async def test_initialization_process_exit_resumes_without_exposing_incomplete_root(
    backup_case: BackupCase, window: str,
) -> None:
    case = backup_case
    backup.prepare_backup(case.workspace, case.output)
    original_files = {row["locator"]: file_digest(case.output / "data" / row["locator"])
                      for row in json.loads((case.output / "manifest.json").read_bytes())["files"]}
    unrelated = case.output / ".converted-unrelated.preparing"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_bytes(b"unrelated preparation")
    probe = r'''
import os
import sys
from pathlib import Path
from app.services.infrastructure.attachment_blob_catalog.migration import directories

directory, window = Path(sys.argv[1]), sys.argv[2]
write, replace = directories.atomic_write_bytes, directories.os.replace

def interrupted_write(path, payload):
    if path.name == "manifest.json" and window == "before_manifest":
        os._exit(86)
    if (path.name, window) in {("manifest.json", "manifest_partial"), ("journal.json", "journal_partial")}:
        # 模拟原子写尚未 rename 时进程退出，不能执行 finally 清理。
        path.with_name("." + path.name + ".crash").write_bytes(payload[:7])
        os._exit(86)
    write(path, payload)
    if (path.name, window) in {("manifest.json", "manifest_published"), ("journal.json", "journal_published")}:
        os._exit(86)

def interrupted_replace(source, target):
    publishing = Path(target) == directory / "converted"
    if publishing and window == "before_root_rename":
        os._exit(86)
    result = replace(source, target)
    if publishing and window == "after_root_rename":
        os._exit(86)
    return result

directories.atomic_write_bytes = interrupted_write
directories.os.replace = interrupted_replace
directories.convert_backup(directory)
'''
    crashed = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-c", probe, str(case.output), window],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert crashed.returncode == 86, (crashed.stdout, crashed.stderr)
    root = case.output / "converted"
    if root.exists():
        assert (root / "manifest.json").is_file() and (root / "journal.json").is_file()
    prepared = cli("convert", "--backup", str(case.output))
    assert prepared.returncode == 0, prepared.stderr
    response = json.loads(prepared.stdout)
    assert response["state"] == "directories_prepared"
    assert directories.verify_directories(case.output) == response
    assert list(case.output.glob(".converted-*.preparing")) == [unrelated]
    assert (unrelated / "keep.txt").read_bytes() == b"unrelated preparation"
    assert {name: file_digest(case.output / "data" / name) for name in original_files} == original_files
    assert case.original.read_bytes() == BODY


@pytest.mark.parametrize("damage", ["body", "extra_directory", "manifest", "source", "symlink"])
async def test_prepared_conversion_damage_is_not_overwritten_or_accepted(backup_case: BackupCase, damage: str) -> None:
    case = backup_case
    backup.prepare_backup(case.workspace, case.output)
    directories.convert_backup(case.output)
    root = case.output / "converted"
    content, = (root / "attachments").rglob("content")
    if damage == "body":
        content.write_bytes(b"corrupt")
    elif damage == "extra_directory":
        (root / "attachments/unregistered").mkdir()
    elif damage == "manifest":
        (root / "manifest.json").write_bytes(b"{}")
    elif damage == "source":
        (case.output / "data" / case.original.relative_to(case.workspace / ".boxteam")).write_bytes(b"corrupt source")
    else:
        content.unlink()
        content.symlink_to(case.original)
    with pytest.raises(RuntimeError):
        directories.convert_backup(case.output)
    with pytest.raises(RuntimeError):
        directories.verify_directories(case.output)
    assert case.original.read_bytes() == BODY


async def test_conversion_lock_rejects_concurrent_prepare_and_verification(backup_case: BackupCase) -> None:
    case = backup_case
    backup.prepare_backup(case.workspace, case.output)
    with AttachmentMaintenanceLock(case.output / "converted", exclusive=True):
        for operation in (directories.convert_backup, directories.verify_directories):
            with pytest.raises(AttachmentMaintenanceBusyError):
                operation(case.output)
    assert not (case.output / "converted").exists()
