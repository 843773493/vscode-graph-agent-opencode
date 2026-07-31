from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest


async def _create_session(client: httpx.AsyncClient, title: str) -> str:
    response = await client.post("/api/v1/sessions", json={"title": title})
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["session_id"])


async def _catalog_nodes(client: httpx.AsyncClient) -> dict[str, dict[str, object]]:
    response = await client.get("/api/v1/session-catalog/export")
    assert response.status_code == 200, response.text
    return {
        str(item["node_id"]): item for item in response.json()["data"]["items"]
    }


def _node_path(sessions_root: Path, node: dict[str, object]) -> Path:
    relative_path = node["storage_relative_path"]
    assert isinstance(relative_path, str) and relative_path
    candidate = sessions_root / relative_path
    assert candidate.resolve().is_relative_to(sessions_root.resolve())
    return candidate


def _assert_readable_stable_segment(path: Path, name: str, stable_id: str) -> None:
    del name
    assert path.name == stable_id


def _write_bundle_sentinels(session_dir: Path) -> dict[str, bytes]:
    values = {
        "checkpoints/e2e-checkpoint.bin": b"checkpoint-e2e",
        "logs/traces/e2e-trace.bin": b"trace-e2e",
        "resources/e2e-resource.bin": b"resource-e2e",
        "attachments/e2e-attachment.bin": b"attachment-e2e",
        "tool-results/e2e-tool-result.bin": b"tool-result-e2e",
        "context/e2e-context.bin": b"context-e2e",
        "changes/e2e-change.bin": b"change-e2e",
    }
    for relative_path, value in values.items():
        target = session_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    return values


def _assert_bundle_sentinels(session_dir: Path, values: dict[str, bytes]) -> None:
    assert (session_dir / "session.json").is_file()
    for relative_path, expected in values.items():
        assert (session_dir / relative_path).read_bytes() == expected


@pytest.mark.asyncio
async def test_catalog_node_move_supports_session_and_folder_children(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    sessions_root = (
        Path(e2e_workspace_root_path).resolve() / ".boxteam" / "sessions"
    )
    parent_session_id = await _create_session(client, "拖放父会话")
    child_session_id = await _create_session(client, "拖放子会话")
    implicit_move_response = await client.patch(
        f"/api/v1/sessions/{child_session_id}",
        json={"parent_session_id": parent_session_id},
    )
    assert implicit_move_response.status_code == 422, implicit_move_response.text

    folder_response = await client.post(
        "/api/v1/session-catalog/folders",
        json={"name": "拖放文件夹"},
    )
    assert folder_response.status_code == 200, folder_response.text
    folder_id = folder_response.json()["data"]["items"][-1]["folder_id"]
    assign_response = await client.put(
        f"/api/v1/session-catalog/sessions/{child_session_id}/folder",
        json={"folder_id": folder_id},
    )
    assert assign_response.status_code == 200, assign_response.text

    move_folder_response = await client.patch(
        f"/api/v1/session-catalog/nodes/{folder_id}/parent",
        json={"parent_node_id": parent_session_id},
    )
    assert move_folder_response.status_code == 200, move_folder_response.text
    nested_nodes = await _catalog_nodes(client)
    parent_path = _node_path(sessions_root, nested_nodes[parent_session_id])
    folder_path = _node_path(sessions_root, nested_nodes[folder_id])
    child_path = _node_path(sessions_root, nested_nodes[child_session_id])
    assert folder_path.parent == parent_path / "children"
    assert child_path.parent == folder_path
    child_response = await client.get(f"/api/v1/sessions/{child_session_id}")
    assert child_response.status_code == 200, child_response.text
    assert child_response.json()["data"]["parent_session_id"] == parent_session_id

    move_folder_root_response = await client.patch(
        f"/api/v1/session-catalog/nodes/{folder_id}/parent",
        json={"parent_node_id": None},
    )
    assert move_folder_root_response.status_code == 200, move_folder_root_response.text
    root_nodes = await _catalog_nodes(client)
    assert _node_path(sessions_root, root_nodes[folder_id]).parent == sessions_root
    root_child_response = await client.get(f"/api/v1/sessions/{child_session_id}")
    assert root_child_response.status_code == 200, root_child_response.text
    assert root_child_response.json()["data"]["parent_session_id"] is None

    bind_direct_response = await client.patch(
        f"/api/v1/session-catalog/nodes/{child_session_id}/parent",
        json={"parent_node_id": parent_session_id},
    )
    assert bind_direct_response.status_code == 200, bind_direct_response.text
    bound_nodes = await _catalog_nodes(client)
    assert (
        _node_path(sessions_root, bound_nodes[child_session_id]).parent
        == parent_path / "children"
    )

    cycle_response = await client.patch(
        f"/api/v1/session-catalog/nodes/{parent_session_id}/parent",
        json={"parent_node_id": child_session_id},
    )
    assert cycle_response.status_code == 409, cycle_response.text
    assert "循环" in cycle_response.text

    delete_parent_response = await client.delete(
        f"/api/v1/sessions/{parent_session_id}",
        params={"cascade": True},
    )
    assert delete_parent_response.status_code == 200, delete_parent_response.text
    delete_folder_response = await client.delete(
        f"/api/v1/session-catalog/folders/{folder_id}"
    )
    assert delete_folder_response.status_code == 204, delete_folder_response.text


@pytest.mark.asyncio
async def test_session_catalog_uses_authoritative_index_for_crud_and_validation(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    workspace_root = Path(e2e_workspace_root_path).resolve()
    sessions_root = workspace_root / ".boxteam" / "sessions"
    session_ids = [
        await _create_session(client, title)
        for title in [
            "Catalog Alpha",
            "Catalog Needle",
            "Catalog Root One",
            "Catalog Root Two",
            "Catalog Root Three",
        ]
    ]
    initial_nodes = await _catalog_nodes(client)
    initial_session_path = _node_path(sessions_root, initial_nodes[session_ids[1]])
    _assert_readable_stable_segment(
        initial_session_path,
        "Catalog Needle",
        session_ids[1],
    )
    sentinel_values = _write_bundle_sentinels(initial_session_path)

    parent_response = await client.post(
        "/api/v1/session-catalog/folders",
        json={"name": "项目资料"},
    )
    assert parent_response.status_code == 200, parent_response.text
    parent_folder_id = parent_response.json()["data"]["items"][-1]["folder_id"]
    child_response = await client.post(
        "/api/v1/session-catalog/folders",
        json={"name": "阶段一", "parent_folder_id": parent_folder_id},
    )
    assert child_response.status_code == 200, child_response.text
    child_folder_id = child_response.json()["data"]["items"][-1]["folder_id"]
    sibling_response = await client.post(
        "/api/v1/session-catalog/folders",
        json={"name": "待归档"},
    )
    assert sibling_response.status_code == 200, sibling_response.text
    sibling_folder_id = sibling_response.json()["data"]["items"][-1]["folder_id"]

    created_nodes = await _catalog_nodes(client)
    parent_path = _node_path(sessions_root, created_nodes[parent_folder_id])
    child_path = _node_path(sessions_root, created_nodes[child_folder_id])
    sibling_path = _node_path(sessions_root, created_nodes[sibling_folder_id])
    _assert_readable_stable_segment(parent_path, "项目资料", parent_folder_id)
    _assert_readable_stable_segment(child_path, "阶段一", child_folder_id)
    _assert_readable_stable_segment(sibling_path, "待归档", sibling_folder_id)
    assert child_path.parent == parent_path
    assert (parent_path / ".boxteam-folder.json").is_file()
    assert (child_path / ".boxteam-folder.json").is_file()

    assign_parent_response = await client.put(
        f"/api/v1/session-catalog/sessions/{session_ids[0]}/folder",
        json={"folder_id": parent_folder_id},
    )
    assert assign_parent_response.status_code == 200, assign_parent_response.text
    assign_child_response = await client.put(
        f"/api/v1/session-catalog/sessions/{session_ids[1]}/folder",
        json={"folder_id": child_folder_id},
    )
    assert assign_child_response.status_code == 200, assign_child_response.text

    assigned_nodes = await _catalog_nodes(client)
    assigned_session_path = _node_path(
        sessions_root,
        assigned_nodes[session_ids[1]],
    )
    assert not initial_session_path.exists()
    assert assigned_session_path.parent == child_path
    _assert_bundle_sentinels(assigned_session_path, sentinel_values)

    rename_folder_response = await client.patch(
        f"/api/v1/session-catalog/folders/{child_folder_id}",
        json={"name": "阶段一已重命名"},
    )
    assert rename_folder_response.status_code == 200, rename_folder_response.text
    renamed_nodes = await _catalog_nodes(client)
    renamed_child_path = _node_path(sessions_root, renamed_nodes[child_folder_id])
    renamed_session_path = _node_path(sessions_root, renamed_nodes[session_ids[1]])
    assert renamed_child_path == child_path
    assert renamed_child_path.parent == parent_path
    assert renamed_session_path.parent == renamed_child_path
    _assert_bundle_sentinels(renamed_session_path, sentinel_values)

    move_folder_response = await client.patch(
        f"/api/v1/session-catalog/folders/{child_folder_id}",
        json={"parent_folder_id": sibling_folder_id},
    )
    assert move_folder_response.status_code == 200, move_folder_response.text
    moved_nodes = await _catalog_nodes(client)
    moved_child_path = _node_path(sessions_root, moved_nodes[child_folder_id])
    moved_session_path = _node_path(sessions_root, moved_nodes[session_ids[1]])
    assert not renamed_child_path.exists()
    assert moved_child_path.parent == sibling_path
    assert moved_session_path.parent == moved_child_path
    _assert_bundle_sentinels(moved_session_path, sentinel_values)

    rename_session_response = await client.patch(
        f"/api/v1/sessions/{session_ids[1]}",
        json={"title": "Catalog Needle Renamed"},
    )
    assert rename_session_response.status_code == 200, rename_session_response.text
    title_nodes = await _catalog_nodes(client)
    title_session_path = _node_path(sessions_root, title_nodes[session_ids[1]])
    assert title_session_path == moved_session_path
    assert title_session_path.exists()
    _assert_bundle_sentinels(title_session_path, sentinel_values)

    roots_page_response = await client.get(
        "/api/v1/session-catalog/roots",
        params={"limit": 2},
    )
    assert roots_page_response.status_code == 200, roots_page_response.text
    roots_page = roots_page_response.json()["data"]
    assert roots_page["total"] == 5
    assert len(roots_page["items"]) == 2
    assert roots_page["cursor"]

    breadcrumb_response = await client.get(
        f"/api/v1/session-catalog/breadcrumb/{session_ids[1]}"
    )
    assert breadcrumb_response.status_code == 200, breadcrumb_response.text
    assert [
        item["name"] for item in breadcrumb_response.json()["data"]["items"]
    ] == ["待归档", "阶段一已重命名", "Catalog Needle Renamed"]
    search_response = await client.get(
        "/api/v1/session-catalog/search",
        params={"query": "Catalog Needle Renamed", "limit": 10},
    )
    assert search_response.status_code == 200, search_response.text
    search_result = search_response.json()["data"]["items"][0]
    assert search_result["relative_path"] == (
        "待归档/阶段一已重命名/Catalog Needle Renamed"
    )
    assert search_result["node"]["storage_relative_path"] == (
        title_nodes[session_ids[1]]["storage_relative_path"]
    )

    manual_name = "手工移动阶段"
    manual_child_path = parent_path / f"{manual_name}--{child_folder_id[-8:]}"
    os.replace(moved_child_path, manual_child_path)
    assert not moved_child_path.exists()
    assert manual_child_path.is_dir()
    refresh_response = await client.post("/api/v1/session-catalog/refresh")
    assert refresh_response.status_code == 409, refresh_response.text
    assert "绕过软件修改会话目录结构" in refresh_response.text
    export_response = await client.get("/api/v1/session-catalog/export")
    assert export_response.status_code == 409, export_response.text

    os.replace(manual_child_path, moved_child_path)
    refresh_response = await client.post("/api/v1/session-catalog/refresh")
    assert refresh_response.status_code == 200, refresh_response.text
    rebuilt_nodes = await _catalog_nodes(client)
    assert rebuilt_nodes[child_folder_id]["name"] == "阶段一已重命名"
    assert rebuilt_nodes[child_folder_id]["parent_node_id"] == sibling_folder_id
    rebuilt_child_path = _node_path(sessions_root, rebuilt_nodes[child_folder_id])
    rebuilt_session_path = _node_path(sessions_root, rebuilt_nodes[session_ids[1]])
    assert rebuilt_child_path == moved_child_path
    assert rebuilt_session_path.parent == moved_child_path
    _assert_bundle_sentinels(rebuilt_session_path, sentinel_values)
    rebuilt_breadcrumb_response = await client.get(
        f"/api/v1/session-catalog/breadcrumb/{session_ids[1]}"
    )
    assert rebuilt_breadcrumb_response.status_code == 200
    assert [
        item["name"]
        for item in rebuilt_breadcrumb_response.json()["data"]["items"]
    ] == ["待归档", "阶段一已重命名", "Catalog Needle Renamed"]

    index_path = (
        workspace_root / ".boxteam" / "navigation" / "session-catalog-index.json"
    )
    stored_index = json.loads(index_path.read_text(encoding="utf-8"))
    assert stored_index["schema_version"] == 3
    indexed_session = next(
        node for node in stored_index["nodes"] if node["node_id"] == session_ids[1]
    )
    assert indexed_session["parent_node_id"] == child_folder_id
    assert indexed_session["name"] == "Catalog Needle Renamed"

    nonempty_delete_response = await client.delete(
        f"/api/v1/session-catalog/folders/{parent_folder_id}"
    )
    assert nonempty_delete_response.status_code == 409
    assert parent_path.is_dir()

    delete_session_response = await client.delete(
        f"/api/v1/sessions/{session_ids[1]}"
    )
    assert delete_session_response.status_code == 200, delete_session_response.text
    assert not rebuilt_session_path.exists()
    delete_child_response = await client.delete(
        f"/api/v1/session-catalog/folders/{child_folder_id}"
    )
    assert delete_child_response.status_code == 204, delete_child_response.text
    assert not rebuilt_child_path.exists()

    move_parent_session_to_root = await client.put(
        f"/api/v1/session-catalog/sessions/{session_ids[0]}/folder",
        json={"folder_id": None},
    )
    assert move_parent_session_to_root.status_code == 200
    delete_parent_response = await client.delete(
        f"/api/v1/session-catalog/folders/{parent_folder_id}"
    )
    assert delete_parent_response.status_code == 204
    delete_sibling_response = await client.delete(
        f"/api/v1/session-catalog/folders/{sibling_folder_id}"
    )
    assert delete_sibling_response.status_code == 204
    assert not parent_path.exists()
    assert not sibling_path.exists()


@pytest.mark.asyncio
async def test_session_folder_rejects_cycle_without_moving_physical_nodes(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    sessions_root = Path(e2e_workspace_root_path).resolve() / ".boxteam" / "sessions"
    parent_response = await client.post(
        "/api/v1/session-catalog/folders",
        json={"name": "循环父目录"},
    )
    assert parent_response.status_code == 200, parent_response.text
    parent_id = parent_response.json()["data"]["items"][-1]["folder_id"]
    child_response = await client.post(
        "/api/v1/session-catalog/folders",
        json={"name": "循环子目录", "parent_folder_id": parent_id},
    )
    assert child_response.status_code == 200, child_response.text
    child_id = child_response.json()["data"]["items"][-1]["folder_id"]
    before_nodes = await _catalog_nodes(client)
    parent_path = _node_path(sessions_root, before_nodes[parent_id])
    child_path = _node_path(sessions_root, before_nodes[child_id])

    cycle_response = await client.patch(
        f"/api/v1/session-catalog/folders/{parent_id}",
        json={"parent_folder_id": child_id},
    )
    assert cycle_response.status_code == 400, cycle_response.text
    assert "循环" in cycle_response.text
    after_nodes = await _catalog_nodes(client)
    assert after_nodes[parent_id]["storage_relative_path"] == before_nodes[parent_id][
        "storage_relative_path"
    ]
    assert after_nodes[child_id]["storage_relative_path"] == before_nodes[child_id][
        "storage_relative_path"
    ]
    assert parent_path.is_dir()
    assert child_path.is_dir()


@pytest.mark.asyncio
async def test_session_folder_recursive_delete_removes_complete_session_bundles(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    sessions_root = Path(e2e_workspace_root_path).resolve() / ".boxteam" / "sessions"
    parent_response = await client.post(
        "/api/v1/session-catalog/folders",
        json={"name": "递归删除年月日"},
    )
    assert parent_response.status_code == 200, parent_response.text
    parent_id = parent_response.json()["data"]["items"][-1]["folder_id"]
    child_response = await client.post(
        "/api/v1/session-catalog/folders",
        json={"name": "时分秒", "parent_folder_id": parent_id},
    )
    assert child_response.status_code == 200, child_response.text
    child_id = child_response.json()["data"]["items"][-1]["folder_id"]
    session_id = await _create_session(client, "递归删除生成会话")
    assign_response = await client.put(
        f"/api/v1/session-catalog/sessions/{session_id}/folder",
        json={"folder_id": child_id},
    )
    assert assign_response.status_code == 200, assign_response.text
    nodes = await _catalog_nodes(client)
    parent_path = _node_path(sessions_root, nodes[parent_id])
    session_path = _node_path(sessions_root, nodes[session_id])
    _write_bundle_sentinels(session_path)

    guarded_response = await client.delete(
        f"/api/v1/session-catalog/folders/{parent_id}"
    )
    assert guarded_response.status_code == 409
    assert parent_path.is_dir()
    assert session_path.is_dir()

    recursive_response = await client.delete(
        f"/api/v1/session-catalog/folders/{parent_id}",
        params={"recursive": "true"},
    )
    assert recursive_response.status_code == 204, recursive_response.text
    assert not parent_path.exists()
    assert not session_path.exists()
    remaining_nodes = await _catalog_nodes(client)
    assert parent_id not in remaining_nodes
    assert child_id not in remaining_nodes
    assert session_id not in remaining_nodes
    session_response = await client.get(f"/api/v1/sessions/{session_id}")
    assert session_response.status_code == 404
