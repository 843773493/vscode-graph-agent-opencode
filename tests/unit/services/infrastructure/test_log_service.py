"""快照由 catalog 定位，移动不搬数据，删除后不能重建会话目录。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.atomic_fs import native_io_path
from app.core.path_utils import (
    SessionCatalogComponents,
    get_session_catalog_components,
)
from app.core.session_subtree_delete import SessionSubtreeDeleteService
from app.services.infrastructure.log_service import LogService, LogSnapshotRecord

SESSION_ID = "ses_019c257bff2c7c6989d1c38a6ae7fbf2"


@dataclass(frozen=True, slots=True)
class LogSnapshotFixture:
    log_service: LogService
    catalog_components: SessionCatalogComponents
    subtree_deletion: SessionSubtreeDeleteService
    session_directory: Path
    workspace_root: Path


@dataclass(frozen=True, slots=True)
class WorkspacePathResolverProbe:
    sessions_root: Path
    workspace_id: str


@pytest.fixture
def long_path_workspace(tmp_path: Path) -> tuple[Path, WorkspacePathResolverProbe]:
    workspace_root = tmp_path
    for index in range(14):
        workspace_root /= f"long-segment-{index:02d}-abcdefghijklmnop"
    native_io_path(workspace_root).mkdir(parents=True)
    path_resolver = WorkspacePathResolverProbe(
        sessions_root=workspace_root / ".boxteam" / "sessions",
        workspace_id="workspace_probe",
    )
    return workspace_root, path_resolver


@pytest.fixture
def snapshot_storage(tmp_path: Path, session_bundle_factory) -> LogSnapshotFixture:
    sessions_root = tmp_path / ".boxteam" / "sessions"
    session_directory = session_bundle_factory(sessions_root, SESSION_ID)
    catalog_components = get_session_catalog_components(sessions_root)
    log_service = LogService(
        workspace_root=tmp_path,
        path_resolver=catalog_components.resolver,
    )
    subtree_deletion = SessionSubtreeDeleteService(
        store=catalog_components.resolver.catalog_store,
        sessions_root=sessions_root,
        workspace_id=catalog_components.resolver.workspace_id,
    )
    return LogSnapshotFixture(
        log_service=log_service,
        catalog_components=catalog_components,
        subtree_deletion=subtree_deletion,
        session_directory=session_directory,
        workspace_root=tmp_path,
    )


@pytest.mark.asyncio
async def test_catalog_rename_keeps_snapshot_reference_and_delete_isolates_whole_node(
    snapshot_storage,
) -> None:
    service = snapshot_storage.log_service
    catalog_components = snapshot_storage.catalog_components
    result = await service.write_html_snapshot(
        LogSnapshotRecord(session_id=SESSION_ID, html="<p>历史</p>")
    )
    store = catalog_components.resolver.catalog_store
    store.rename_node(SESSION_ID, "重命名后")
    folder_id = "ses_019c257bff2c7c6989d1c38a6ae7fbf3"
    store.create_folder(
        folder_id,
        catalog_components.resolver.workspace_id,
        None,
        "快照分组",
    )
    store.move_node(SESSION_ID, folder_id)
    assert (
        await service.read_html_snapshot(
            result.snapshot_id,
            session_id=SESSION_ID,
            metadata=False,
        )
        == "<p>历史</p>"
    )
    deleted = await snapshot_storage.subtree_deletion.delete(
        idempotency_key="snapshot-delete",
        root_node_id=SESSION_ID,
    )
    assert deleted.record_state == "completed"
    assert not snapshot_storage.session_directory.exists()
    isolated = (
        catalog_components.resolver.sessions_root
        / ".deleting"
        / "snapshot-delete"
        / SESSION_ID
    )
    assert (
        isolated / "logs" / "webview" / f"{result.snapshot_id}.html"
    ).read_text() == "<p>历史</p>"
    with pytest.raises(KeyError):
        await service.read_html_snapshot(
            result.snapshot_id,
            session_id=SESSION_ID,
            metadata=False,
        )
    with pytest.raises(KeyError):
        await service.write_html_snapshot(
            LogSnapshotRecord(session_id=SESSION_ID, html="<p>禁止复活</p>")
        )
    assert not snapshot_storage.session_directory.exists()


@pytest.mark.asyncio
async def test_snapshot_reopen_uses_catalog_and_metadata_identity(
    snapshot_storage,
) -> None:
    result = await snapshot_storage.log_service.write_html_snapshot(
        LogSnapshotRecord(session_id=SESSION_ID, html="<p>重启读取</p>")
    )
    reopened = LogService(
        workspace_root=snapshot_storage.workspace_root,
        path_resolver=snapshot_storage.catalog_components.resolver,
    )
    assert (
        await reopened.read_html_snapshot(
            result.snapshot_id,
            session_id=SESSION_ID,
            metadata=False,
        )
        == "<p>重启读取</p>"
    )
    metadata = (
        snapshot_storage.session_directory
        / "logs"
        / "webview"
        / f"{result.snapshot_id}.json"
    )
    metadata.write_text('{"snapshot_id":"other"}')
    with pytest.raises(RuntimeError, match="身份不一致"):
        await reopened.read_html_snapshot(
            result.snapshot_id, session_id=SESSION_ID, metadata=True
        )


@pytest.mark.asyncio
async def test_workspace_snapshot_round_trips_with_windows_long_path(
    long_path_workspace: tuple[Path, WorkspacePathResolverProbe],
) -> None:
    workspace_root, path_resolver = long_path_workspace
    snapshots_directory = workspace_root / ".boxteam" / "logs" / "webview"
    assert len(str(snapshots_directory)) > 260

    service = LogService(
        workspace_root=workspace_root,
        path_resolver=path_resolver,
    )

    result = await service.write_html_snapshot(
        LogSnapshotRecord(session_id=None, html="<p>long path</p>")
    )

    assert (
        await service.read_html_snapshot(
            result.snapshot_id,
            session_id=None,
            metadata=False,
        )
        == "<p>long path</p>"
    )
    metadata = await service.read_html_snapshot(
        result.snapshot_id,
        session_id=None,
        metadata=True,
    )
    assert json.loads(metadata)["snapshot_id"] == result.snapshot_id
