from __future__ import annotations

from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


def test_workspace_state_uses_workspace_boundary_and_activity_cursor(tmp_path):
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        assert store.path == workspace_root / ".boxteam" / "state" / "workspace.sqlite"
        store.set_config(
            config_key="workspace",
            config_version=1,
            payload={"jobs": {"max_concurrency": 2}},
        )
        first = store.append_activity(
            event_id="event-1",
            session_id="session-1",
            status="completed",
            summary="任务完成",
        )
        second = store.append_activity(
            event_id="event-2",
            session_id="session-2",
            status="failed",
            summary="任务失败",
        )
        duplicate = store.append_activity(
            event_id="event-1",
            session_id="session-1",
            status="completed",
            summary="任务完成",
        )
        assert first.event_seq == 1
        assert duplicate.event_seq == first.event_seq
        assert [item.event_id for item in store.list_activity(after=first.event_seq)] == [
            second.event_id
        ]
        assert store.diagnostics().schema_version == 1
    finally:
        store.close()
