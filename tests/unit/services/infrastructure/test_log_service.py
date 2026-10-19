"""快照由 catalog 定位，移动不搬数据，删除后不能重建会话目录。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.path_utils import get_session_catalog_components
from app.core.session_subtree_delete import SessionSubtreeDeleteService
from app.services.infrastructure.log_service import LogService, LogSnapshotRecord

SESSION_ID = "ses_019c257bff2c7c6989d1c38a6ae7fbf2"


@pytest.fixture
def snapshot_storage(tmp_path: Path, session_bundle_factory):
    sessions_root = tmp_path / ".boxteam" / "sessions"
    session_root = session_bundle_factory(sessions_root, SESSION_ID)
    components = get_session_catalog_components(sessions_root)
    service = LogService(workspace_root=tmp_path, path_resolver=components.resolver)
    deletion = SessionSubtreeDeleteService(
        store=components.resolver.catalog_store, sessions_root=sessions_root, workspace_id=components.resolver.workspace_id,
    )
    return service, components, deletion, session_root, tmp_path


@pytest.mark.asyncio
async def test_catalog_rename_keeps_snapshot_reference_and_delete_isolates_whole_node(snapshot_storage) -> None:
    service, components, deletion, session_root, _ = snapshot_storage
    result = await service.write_html_snapshot(LogSnapshotRecord(session_id=SESSION_ID, html="<p>历史</p>"))
    store = components.resolver.catalog_store
    store.rename_node(SESSION_ID, "重命名后")
    folder_id = "ses_019c257bff2c7c6989d1c38a6ae7fbf3"
    store.create_folder(folder_id, components.resolver.workspace_id, None, "快照分组")
    store.move_node(SESSION_ID, folder_id)
    assert await service.read_html_snapshot(result.snapshot_id, session_id=SESSION_ID, metadata=False) == "<p>历史</p>"
    deleted = await deletion.delete(idempotency_key="snapshot-delete", root_node_id=SESSION_ID)
    assert deleted.record_state == "completed" and not session_root.exists()
    isolated = components.resolver.sessions_root / ".deleting" / "snapshot-delete" / SESSION_ID
    assert (isolated / "logs" / "webview" / f"{result.snapshot_id}.html").read_text() == "<p>历史</p>"
    with pytest.raises(KeyError):
        await service.read_html_snapshot(result.snapshot_id, session_id=SESSION_ID, metadata=False)
    with pytest.raises(KeyError):
        await service.write_html_snapshot(LogSnapshotRecord(session_id=SESSION_ID, html="<p>禁止复活</p>"))
    assert not session_root.exists()


@pytest.mark.asyncio
async def test_snapshot_reopen_uses_catalog_and_metadata_identity(snapshot_storage) -> None:
    service, components, _, session_root, workspace_root = snapshot_storage
    result = await service.write_html_snapshot(LogSnapshotRecord(session_id=SESSION_ID, html="<p>重启读取</p>"))
    reopened = LogService(workspace_root=workspace_root, path_resolver=components.resolver)
    assert await reopened.read_html_snapshot(result.snapshot_id, session_id=SESSION_ID, metadata=False) == "<p>重启读取</p>"
    metadata = session_root / "logs" / "webview" / f"{result.snapshot_id}.json"
    metadata.write_text('{"snapshot_id":"other"}')
    with pytest.raises(RuntimeError, match="身份不一致"):
        await reopened.read_html_snapshot(result.snapshot_id, session_id=SESSION_ID, metadata=True)
