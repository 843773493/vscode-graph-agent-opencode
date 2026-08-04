from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tests.support.processes import close_backend_process, start_backend_process


LOCAL_TOKEN_HEADERS = {"X-Local-Token": "local-dev-token"}


def _legacy_session_payload(session_id: str, title: str) -> dict[str, object]:
    timestamp = datetime(2026, 7, 1, 8, 30, tzinfo=UTC).isoformat()
    return {
        "session_id": session_id,
        "workspace_id": "ws_local",
        "title": title,
        "title_source": "user",
        "current_agent_id": "default",
        "current_provider_id": "primary",
        "parent_session_id": None,
        "kind": "normal",
        "delegation": None,
        "generation_origin": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _write_legacy_session_bundle(
    sessions_root: Path,
    *,
    session_id: str,
    title: str,
) -> dict[str, bytes]:
    session_dir = sessions_root / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(
        json.dumps(
            _legacy_session_payload(session_id, title),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sentinels = {
        "checkpoints/e2e-checkpoint.bin": b"legacy-checkpoint",
        "logs/traces/e2e-trace.bin": b"legacy-trace",
        "logs/llm_requests/e2e-request.bin": b"legacy-request",
        "resources/e2e-resource.bin": b"legacy-resource",
        "attachments/legacy.bin": b"legacy-attachment",
        "tool-results/legacy.txt": b"legacy-tool-result",
        "context/history.md": b"legacy context history",
        "changes/legacy.diff": b"legacy change",
        "e2e-pending-request.bin": b"legacy-pending-request",
    }
    for relative_path, value in sentinels.items():
        target = session_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    return sentinels


def _assert_migrated_bundle(
    session_dir: Path,
    *,
    session_id: str,
    sentinels: dict[str, bytes],
) -> None:
    manifest = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    assert manifest["session_id"] == session_id
    for relative_path, expected in sentinels.items():
        assert (session_dir / relative_path).read_bytes() == expected


def _catalog_nodes(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    data = payload["data"]
    assert isinstance(data, dict)
    items = data["items"]
    assert isinstance(items, list)
    return {str(item["node_id"]): item for item in items}


@pytest.mark.asyncio
async def test_legacy_flat_sessions_and_folder_navigation_migrate_as_whole_bundles(
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
    e2e_backend_port: int,
) -> None:
    workspace_root = Path(e2e_workspace_root_path).resolve()
    assert Path(e2e_workspace_config_path).is_file()
    sessions_root = workspace_root / ".boxteam" / "sessions"
    navigation_root = workspace_root / ".boxteam" / "navigation"
    sessions_root.mkdir(parents=True, exist_ok=True)
    navigation_root.mkdir(parents=True, exist_ok=True)

    project_folder_id = "fld_legacy_project_00000001"
    day_folder_id = "fld_legacy_day_00000002"
    project_session_id = "ses_legacy_project_00000001"
    day_session_id = "ses_legacy_day_00000002"
    root_session_id = "ses_legacy_root_00000003"
    session_titles = {
        project_session_id: "旧项目会话",
        day_session_id: "旧日期会话",
        root_session_id: "旧根会话",
    }
    sentinels_by_session = {
        session_id: _write_legacy_session_bundle(
            sessions_root,
            session_id=session_id,
            title=title,
        )
        for session_id, title in session_titles.items()
    }
    timestamp = datetime(2026, 7, 1, 8, 30, tzinfo=UTC).isoformat()
    legacy_navigation = {
        "schema_version": 1,
        "folders": [
            {
                "folder_id": project_folder_id,
                "name": "旧项目",
                "parent_folder_id": None,
                "session_ids": [project_session_id],
                "created_at": timestamp,
                "updated_at": timestamp,
            },
            {
                "folder_id": day_folder_id,
                "name": "2026-07-01",
                "parent_folder_id": project_folder_id,
                "session_ids": [day_session_id],
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        ],
        "session_parents": {},
    }
    legacy_navigation_path = navigation_root / "session-folders.json"
    legacy_navigation_path.write_text(
        json.dumps(legacy_navigation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    legacy_paths = {
        session_id: sessions_root / session_id for session_id in session_titles
    }

    backend = start_backend_process(
        workspace_root=str(workspace_root),
        port=e2e_backend_port,
        log_name="session-storage-migration-backend",
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            sessions_response = await client.get("/api/v1/sessions", params={"limit": 20})
            assert sessions_response.status_code == 200, sessions_response.text
            assert {
                item["session_id"]
                for item in sessions_response.json()["data"]["items"]
            } == set(session_titles)
            export_response = await client.get("/api/v1/session-catalog/export")
            assert export_response.status_code == 200, export_response.text
            nodes = _catalog_nodes(export_response.json())
    finally:
        close_backend_process(backend)

    for legacy_path in legacy_paths.values():
        assert not legacy_path.exists()
    assert nodes[project_folder_id]["parent_node_id"] is None
    assert nodes[day_folder_id]["parent_node_id"] == project_folder_id
    assert nodes[project_session_id]["parent_node_id"] == project_folder_id
    assert nodes[day_session_id]["parent_node_id"] == day_folder_id
    assert nodes[root_session_id]["parent_node_id"] is None

    migrated_paths: dict[str, str] = {}
    for session_id, title in session_titles.items():
        relative_path = nodes[session_id]["storage_relative_path"]
        assert isinstance(relative_path, str)
        migrated_paths[session_id] = relative_path
        session_dir = sessions_root / relative_path
        assert session_dir.name.startswith(title)
        assert session_dir.name.endswith(f"--{session_id[-8:]}")
        _assert_migrated_bundle(
            session_dir,
            session_id=session_id,
            sentinels=sentinels_by_session[session_id],
        )

    project_folder_path = sessions_root / str(
        nodes[project_folder_id]["storage_relative_path"]
    )
    day_folder_path = sessions_root / str(nodes[day_folder_id]["storage_relative_path"])
    assert project_folder_path.name == f"旧项目--{project_folder_id[-8:]}"
    assert day_folder_path.parent == project_folder_path
    assert day_folder_path.name == f"2026-07-01--{day_folder_id[-8:]}"
    assert (project_folder_path / ".boxteam-folder.json").is_file()
    assert (day_folder_path / ".boxteam-folder.json").is_file()

    migration_path = (
        workspace_root
        / ".boxteam"
        / "migrations"
        / "session-physical-layout-v1.json"
    )
    migration_before_restart = migration_path.read_text(encoding="utf-8")
    migration_record = json.loads(migration_before_restart)
    assert migration_record["status"] == "completed"
    assert {
        operation["operation"] for operation in migration_record["operations"]
    } == {"create_folder", "move_session"}
    archived_navigation = (
        workspace_root / ".boxteam" / "migrations" / "session-folders-v1.json"
    )
    assert json.loads(archived_navigation.read_text(encoding="utf-8")) == (
        legacy_navigation
    )
    assert not legacy_navigation_path.exists()

    restarted = start_backend_process(
        workspace_root=str(workspace_root),
        port=e2e_backend_port,
        log_name="session-storage-migration-restarted-backend",
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{restarted.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            export_response = await client.get("/api/v1/session-catalog/export")
            assert export_response.status_code == 200, export_response.text
            restarted_nodes = _catalog_nodes(export_response.json())
    finally:
        close_backend_process(restarted)

    assert migration_path.read_text(encoding="utf-8") == migration_before_restart
    assert {
        session_id: restarted_nodes[session_id]["storage_relative_path"]
        for session_id in session_titles
    } == migrated_paths
    for session_id, relative_path in migrated_paths.items():
        _assert_migrated_bundle(
            sessions_root / relative_path,
            session_id=session_id,
            sentinels=sentinels_by_session[session_id],
        )


def test_legacy_session_parented_to_session_is_rejected_before_backend_startup(
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
    e2e_backend_port: int,
) -> None:
    source_workspace = Path(e2e_workspace_root_path).resolve()
    rejected_workspace = source_workspace / "rejected-session-parent-workspace"
    rejected_workspace.mkdir(parents=True)
    (rejected_workspace / "README.md").write_text(
        "# rejected legacy session parent migration\n",
        encoding="utf-8",
    )
    rejected_boxteam = rejected_workspace / ".boxteam"
    rejected_boxteam.mkdir()
    source_config = Path(e2e_workspace_config_path)
    shutil.copy2(source_config, rejected_boxteam / "workspace.jsonc")
    shutil.copy2(
        source_config.parent / "workspace_config.jsonc",
        rejected_boxteam / "workspace_config.jsonc",
    )
    rejected_sessions_root = rejected_boxteam / "sessions"
    parent_session_id = "ses_legacy_parent_00000001"
    child_session_id = "ses_legacy_child_00000002"
    _write_legacy_session_bundle(
        rejected_sessions_root,
        session_id=parent_session_id,
        title="旧父会话",
    )
    _write_legacy_session_bundle(
        rejected_sessions_root,
        session_id=child_session_id,
        title="旧子会话",
    )
    navigation_root = rejected_boxteam / "navigation"
    navigation_root.mkdir()
    (navigation_root / "session-folders.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "folders": [],
                "session_parents": {child_session_id: parent_session_id},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="后端进程提前退出"):
        start_backend_process(
            workspace_root=str(rejected_workspace),
            port=e2e_backend_port + 1,
            log_name="session-parent-migration-rejected",
        )

    stderr_path = (
        rejected_boxteam / "logs" / "session-parent-migration-rejected.stderr.log"
    )
    stderr = stderr_path.read_text(encoding="utf-8")
    assert "旧会话只能挂在文件夹下" in stderr
    assert parent_session_id in stderr
