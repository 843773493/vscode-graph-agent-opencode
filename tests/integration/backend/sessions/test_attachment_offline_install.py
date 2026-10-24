"""产品生成 schema3 历史后验证目录根安装、真实进程退出与原件保全。"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from app.services.infrastructure.attachment_blob_catalog.directory_schema import (
    require_directory_schema,
)
from app.services.infrastructure.attachment_blob_catalog.legacy_references import (
    verify_legacy_references,
)
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.attachment_blob_catalog.migration import (
    backup,
    catalog,
    directories,
    install,
)
from app.services.infrastructure.attachment_blob_catalog.migration.preflight import (
    file_digest,
)
from tests.integration.backend.sessions.test_attachment_offline_backup import (
    BODY,
    BackupCase,
    cli,
)
from tests.integration.backend.sessions.test_attachment_offline_catalog import (
    frozen_files,
)


@pytest.fixture
async def prepared_install(backup_case: BackupCase) -> BackupCase:
    backup.prepare_backup(backup_case.workspace, backup_case.output)
    directories.convert_backup(backup_case.output)
    catalog.prepare_converted_catalog(backup_case.output)
    return backup_case


def assert_installed(case: BackupCase) -> tuple[dict[str, object], Path]:
    root = case.workspace / ".boxteam"
    journal = json.loads((root / "attachment-migration.json").read_bytes())
    assert journal["state"] == "installed"
    live = root / "attachments"
    with closing(sqlite3.connect(live / "catalog.sqlite")) as connection:
        connection.row_factory = sqlite3.Row
        require_directory_schema(connection)
        verify_legacy_references(connection)
        rows = connection.execute("SELECT relative_locator FROM attachment_directories WHERE availability='available'").fetchall()
        assert rows
    for row in rows:
        original = live / row[0] / "original/content"
        assert original.read_bytes() == BODY
        if os.name != "nt":
            assert not original.stat().st_mode & 0o222
            assert not original.parent.stat().st_mode & 0o222
    with closing(sqlite3.connect(case.index_path)) as connection:
        assert tuple(connection.execute("SELECT item_id, content_hash FROM item_catalog ORDER BY item_sequence")) == case.canonical_items
        assert connection.execute("SELECT COUNT(*) FROM execution_queue WHERE phase='queued'").fetchone()[0] == 1
    retired = root / (".attachments-" + journal["backup_id"] + ".schema3")
    assert (retired / case.original.relative_to(live)).read_bytes() == BODY
    return journal, live


async def test_cli_install_retry_and_rollback_keep_history_and_both_directory_versions(prepared_install: BackupCase) -> None:
    case = prepared_install
    frozen = frozen_files(case.output)
    before = file_digest(case.original)
    response = cli("install", "--workspace", str(case.workspace), "--backup", str(case.output))
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout)["state"] == "installed"
    assert str(case.workspace) not in response.stdout and str(case.output) not in response.stdout
    journal, live = assert_installed(case)
    retried = cli("install", "--workspace", str(case.workspace), "--backup", str(case.output))
    assert retried.returncode == 0, retried.stderr
    assert json.loads(retried.stdout) == json.loads(response.stdout)
    rolled_back = cli("rollback", "--workspace", str(case.workspace), "--backup", str(case.output))
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert json.loads(rolled_back.stdout)["state"] == "aborted"
    assert file_digest(case.original) == before and case.original.read_bytes() == BODY
    with closing(sqlite3.connect(live / "catalog.sqlite")) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    preserved = case.workspace / ".boxteam" / (".attachment-install-" + journal["backup_id"]) / "discarded/catalog.sqlite"
    assert preserved.is_file()
    assert frozen_files(case.output) == frozen
    assert cli("rollback", "--workspace", str(case.workspace), "--backup", str(case.output)).returncode == 0
    with AttachmentMaintenanceLock(live, exclusive=False):
        pass


_EXIT_AFTER_RENAME = '''
import os, sys
from pathlib import Path
from app.services.infrastructure.attachment_blob_catalog.migration.install import install_converted_catalog
workspace, backup, window = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
original = os.rename
live = workspace / ".boxteam/attachments"
def interrupt(source, target):
    if window == "ready" and Path(source) == live:
        os._exit(73)
    original(source, target)
    if (window == "retire" and Path(source) == live) or (window == "publish" and Path(target) == live):
        os._exit(73)
os.rename = interrupt
install_converted_catalog(workspace, backup)
'''


@pytest.mark.parametrize("window", ["retire", "publish"])
@pytest.mark.parametrize("action", ["install", "rollback"])
async def test_process_exit_between_root_renames_blocks_startup_and_resumes_original_operation(
    prepared_install: BackupCase, window: str, action: str,
) -> None:
    case = prepared_install
    frozen = frozen_files(case.output)
    exited = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", _EXIT_AFTER_RENAME, str(case.workspace), str(case.output), window],
        timeout=30, capture_output=True, text=True, check=False)
    assert exited.returncode == 73, exited.stderr
    root = case.workspace / ".boxteam/attachments"
    with pytest.raises(RuntimeError, match="尚未完成"), AttachmentMaintenanceLock(root, exclusive=False):
        raise AssertionError("未完成安装不得进入业务")
    if action == "install":
        assert install.install_converted_catalog(case.workspace, case.output)["state"] == "installed"
        assert_installed(case)
    else:
        assert install.rollback_install(case.workspace, case.output)["state"] == "aborted"
        assert case.original.read_bytes() == BODY
        with closing(sqlite3.connect(root / "catalog.sqlite")) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    assert frozen_files(case.output) == frozen


async def test_copy_interruption_resumes_without_overwriting_source_or_partial_target(prepared_install: BackupCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = prepared_install
    original = install._copy
    def interrupt(source, target):
        original(source, target)
        raise OSError("catalog 副本 durable 后中断")
    with monkeypatch.context() as patch:
        patch.setattr(install, "_copy", interrupt)
        with pytest.raises(OSError, match="durable"):
            install.install_converted_catalog(case.workspace, case.output)
    assert case.original.read_bytes() == BODY
    assert install.install_converted_catalog(case.workspace, case.output)["state"] == "installed"
    assert_installed(case)


async def test_source_sqlite_row_change_since_backup_refuses_install_before_journal(prepared_install: BackupCase) -> None:
    case = prepared_install
    path = case.workspace / ".boxteam/attachments/catalog.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE attachment_owner_refs SET file_name='changed-after-backup.txt'")
        connection.commit()
    with pytest.raises(RuntimeError, match="内容不同"):
        install.install_converted_catalog(case.workspace, case.output)
    assert not (path.parent.parent / "attachment-migration.json").exists()
    assert case.original.read_bytes() == BODY


async def test_ready_resume_rechecks_new_source_files_before_retiring_original_root(prepared_install: BackupCase) -> None:
    case = prepared_install
    exited = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", _EXIT_AFTER_RENAME, str(case.workspace), str(case.output), "ready"],
        timeout=30, capture_output=True, text=True, check=False)
    assert exited.returncode == 73, exited.stderr
    late = case.index_path.parent / "late-output.json"
    late.write_text('{"result":"备份后新增的执行产物"}', encoding="utf-8")
    before = late.read_bytes()
    with pytest.raises(RuntimeError, match="来源文件集合已改变"):
        install.install_converted_catalog(case.workspace, case.output)
    assert late.read_bytes() == before and case.original.read_bytes() == BODY
    journal_path = case.workspace / ".boxteam/attachment-migration.json"
    journal = json.loads(journal_path.read_bytes())
    assert journal["state"] == "ready"
    assert not (journal_path.parent / (".attachments-" + journal["backup_id"] + ".schema3")).exists()
    assert install.rollback_install(case.workspace, case.output)["state"] == "aborted"
    assert late.read_bytes() == before and case.original.read_bytes() == BODY


async def test_post_install_file_write_refuses_rollback_and_install_retry_preserves_it(prepared_install: BackupCase) -> None:
    case = prepared_install
    install.install_converted_catalog(case.workspace, case.output)
    _, live = assert_installed(case)
    with closing(sqlite3.connect(live / "catalog.sqlite")) as connection:
        locator = connection.execute("SELECT relative_locator FROM attachment_directories").fetchone()[0]
    output = live / locator / "derived/new-result.txt"
    output.write_text("用户的新结果")
    with pytest.raises(RuntimeError, match="未登记"):
        install.rollback_install(case.workspace, case.output)
    assert install.install_converted_catalog(case.workspace, case.output)["state"] == "installed"
    assert output.read_text() == "用户的新结果"
    assert json.loads((live.parent / "attachment-migration.json").read_bytes())["state"] == "installed"


_EXIT_DURING_ROLLBACK = '''
import os, sys
from pathlib import Path
from app.services.infrastructure.attachment_blob_catalog.migration.install import rollback_install
workspace, backup, window = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
original = os.rename
live = workspace / ".boxteam/attachments"
def interrupt(source, target):
    original(source, target)
    if (window == "discard" and Path(source) == live) or (window == "restore" and Path(target) == live):
        os._exit(73)
os.rename = interrupt
rollback_install(workspace, backup)
'''


@pytest.mark.parametrize("window", ["discard", "restore"])
async def test_rollback_process_exit_preserves_both_versions_and_resumes(prepared_install: BackupCase, window: str) -> None:
    case = prepared_install
    frozen = frozen_files(case.output)
    install.install_converted_catalog(case.workspace, case.output)
    journal, _ = assert_installed(case)
    exited = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", _EXIT_DURING_ROLLBACK, str(case.workspace), str(case.output), window],
        timeout=30, capture_output=True, text=True, check=False)
    assert exited.returncode == 73, exited.stderr
    root = case.workspace / ".boxteam"
    with pytest.raises(RuntimeError, match="尚未完成"), AttachmentMaintenanceLock(root / "attachments", exclusive=False):
        raise AssertionError("未完成撤销不得进入业务")
    assert install.rollback_install(case.workspace, case.output)["state"] == "aborted"
    assert case.original.read_bytes() == BODY
    discarded = root / (".attachment-install-" + journal["backup_id"]) / "discarded"
    assert tuple(path.read_bytes() for path in discarded.glob("**/original/content")) == (BODY,)
    assert frozen_files(case.output) == frozen
    with AttachmentMaintenanceLock(root / "attachments", exclusive=False):
        pass
