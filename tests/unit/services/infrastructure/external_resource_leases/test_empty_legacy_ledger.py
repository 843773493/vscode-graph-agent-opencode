"""空旧账本仅在停写后备份退休，不能把非空资源或未知结构当作空。"""

from pathlib import Path

import pytest

from app.core.sqlite_state import SQLiteProcessOwnership
from app.services.infrastructure.external_resource_leases.database import (
    retire_empty_legacy_ledger,
)


@pytest.fixture
def legacy_ledger(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    source = workspace / ".boxteam/resources.json"
    source.parent.mkdir(parents=True)
    return workspace, source


def test_empty_ledger_has_read_only_preview_exact_backup_and_idempotent_retirement(legacy_ledger) -> None:
    workspace, source = legacy_ledger
    original = b'{ "leases": [], "records": [] }\n'
    source.write_bytes(original)
    assert retire_empty_legacy_ledger(workspace, preview=True)
    assert list(source.parent.iterdir()) == [source]
    assert source.read_bytes() == original
    assert retire_empty_legacy_ledger(workspace)
    backup = source.with_name("resources.json.retired-empty.bak")
    assert backup.read_bytes() == original and not source.exists()
    assert not retire_empty_legacy_ledger(workspace)
    assert backup.read_bytes() == original


@pytest.mark.parametrize("content", [
    b'{"records":[{"resource_id":"must-keep"}],"leases":[]}',
    b'{"records":[],"leases":[{"lease_id":"must-keep"}]}',
    b'{"records":[],"leases":[],"unknown":1}', b'{}', b'[]',
])
def test_nonempty_or_unknown_ledger_is_preserved(legacy_ledger, content) -> None:
    workspace, source = legacy_ledger
    source.write_bytes(content)
    with pytest.raises(ValueError, match="非空或形状未知，需要用户决定"):
        retire_empty_legacy_ledger(workspace, preview=True)
    assert source.read_bytes() == content and list(source.parent.iterdir()) == [source]


def test_live_workspace_writer_blocks_empty_retirement(legacy_ledger) -> None:
    workspace, source = legacy_ledger
    original = b'{"records":[],"leases":[]}'
    source.write_bytes(original)
    ownership = SQLiteProcessOwnership(workspace / ".boxteam/state/workspace.sqlite")
    ownership.acquire()
    try:
        with pytest.raises(RuntimeError, match="已被另一个进程占用"):
            retire_empty_legacy_ledger(workspace)
    finally:
        ownership.release()
    assert source.read_bytes() == original
    assert not source.with_name("resources.json.retired-empty.bak").exists()


def test_different_backup_prevents_retirement(legacy_ledger) -> None:
    workspace, source = legacy_ledger
    source.write_bytes(b'{"records":[],"leases":[]}')
    backup = source.with_name("resources.json.retired-empty.bak")
    backup.write_bytes(b"previous original")
    with pytest.raises(ValueError, match="拒绝覆盖"):
        retire_empty_legacy_ledger(workspace, preview=True)
    assert source.exists() and backup.read_bytes() == b"previous original"
