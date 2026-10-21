from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from tests.support.session_navigation_http import submit_navigation_operation


@pytest.fixture(autouse=True)
def navigation_headers(client: httpx.AsyncClient) -> None:
    client.headers.update(
        {
            "X-BoxTeam-Gateway-Id": "gateway_0123456789abcdef0123456789abcdef",
            "X-BoxTeam-Navigation-Actor": "guest",
        }
    )


async def _create_session(client: httpx.AsyncClient, title: str) -> str:
    response = await client.post("/api/v1/sessions", json={"title": title})
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["session_id"])


async def _catalog_nodes(client: httpx.AsyncClient) -> dict[str, dict[str, object]]:
    response = await client.get("/api/v1/session-catalog/export")
    assert response.status_code == 200, response.text
    assert "storage_relative_path" not in response.text
    return {str(item["node_id"]): item for item in response.json()["data"]["items"]}


async def _create_folder(
    client: httpx.AsyncClient,
    name: str,
    parent_node_id: str | None = None,
) -> str:
    receipt = await submit_navigation_operation(
        client,
        "create_folder",
        name=name,
        parent_node_id=parent_node_id,
    )
    assert receipt["state"] == "committed", receipt
    return str(receipt["created_node_id"])


async def _move(client: httpx.AsyncClient, node_id: str, parent_id: str | None) -> None:
    receipt = await submit_navigation_operation(
        client,
        "move_node",
        target_node_id=node_id,
        parent_node_id=parent_id,
    )
    assert receipt["state"] == "committed", receipt


def _write_bundle_sentinels(session_dir: Path) -> dict[str, bytes]:
    values = {
        f"{directory}/e2e-sentinel.bin": directory.encode()
        for directory in (
            "checkpoints",
            "logs/traces",
            "resources",
            "attachments",
            "tool-results",
            "context",
            "changes",
        )
    }
    for relative_path, value in values.items():
        target = session_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    values["session.json"] = (session_dir / "session.json").read_bytes()
    return values


def _assert_bundle(session_dir: Path, values: dict[str, bytes], inode: int) -> None:
    assert session_dir.stat().st_ino == inode
    for relative_path, expected in values.items():
        assert (session_dir / relative_path).read_bytes() == expected


@pytest.mark.asyncio
async def test_catalog_node_move_supports_session_and_folder_children(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    sessions_root = Path(e2e_workspace_root_path) / ".boxteam/sessions"
    resolver = get_session_path_resolver(sessions_root)
    parent_id = await _create_session(client, "拖放父会话")
    child_id = await _create_session(client, "拖放子会话")
    paths = {
        session_id: resolver.resolve_session_node(session_id)
        for session_id in (parent_id, child_id)
    }
    bundles = {
        session_id: _write_bundle_sentinels(path) for session_id, path in paths.items()
    }
    inodes = {session_id: path.stat().st_ino for session_id, path in paths.items()}
    response = await client.patch(
        f"/api/v1/sessions/{child_id}", json={"parent_session_id": parent_id}
    )
    assert response.status_code == 422, response.text
    folder_id = await _create_folder(client, "拖放文件夹")
    await _move(client, child_id, folder_id)
    await _move(client, folder_id, parent_id)
    nested = await _catalog_nodes(client)
    assert nested[folder_id]["parent_node_id"] == parent_id
    assert nested[child_id]["parent_node_id"] == folder_id
    response = await client.get(f"/api/v1/sessions/{child_id}")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["parent_session_id"] == parent_id
    await _move(client, folder_id, None)
    response = await client.get(f"/api/v1/sessions/{child_id}")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["parent_session_id"] is None
    await _move(client, child_id, parent_id)
    before = await _catalog_nodes(client)
    rejected = await submit_navigation_operation(
        client, "move_node", target_node_id=parent_id, parent_node_id=child_id
    )
    assert rejected["state"] == "rejected"
    assert "循环" in str(rejected["error_detail"])
    assert await _catalog_nodes(client) == before
    for session_id, path in paths.items():
        assert resolver.resolve_session_node(session_id) == path
        _assert_bundle(path, bundles[session_id], inodes[session_id])
    folder = resolver.catalog_store.get_node(folder_id)
    assert folder.kind == "folder"
    assert folder.storage_relative_locator is None and folder.main_thread_id is None
    assert not list(sessions_root.rglob(folder_id))
    assert not list(sessions_root.rglob(".boxteam-folder.json"))


@pytest.mark.asyncio
async def test_session_catalog_uses_authoritative_index_for_crud_and_validation(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    sessions_root = Path(e2e_workspace_root_path) / ".boxteam/sessions"
    resolver = get_session_path_resolver(sessions_root)
    session_ids = [
        await _create_session(client, title)
        for title in (
            "Catalog Alpha",
            "Catalog Needle",
            "Catalog Root One",
            "Catalog Root Two",
            "Catalog Root Three",
        )
    ]
    session_id = session_ids[1]
    path = resolver.resolve_session_node(session_id)
    inode = path.stat().st_ino
    bundle = _write_bundle_sentinels(path)
    stored = resolver.catalog_store.get_node(session_id)
    assert path.name == session_id and stored.main_thread_id
    assert (
        stored.storage_relative_locator
        == f"sessions/{path.relative_to(sessions_root).as_posix()}"
    )
    parent_id = await _create_folder(client, "项目资料")
    child_id = await _create_folder(client, "阶段一", parent_id)
    sibling_id = await _create_folder(client, "待归档")
    await _move(client, session_ids[0], parent_id)
    await _move(client, session_id, child_id)
    for node_id, name in (
        (child_id, "阶段一已重命名"),
        (session_id, "Catalog Needle Renamed"),
    ):
        receipt = await submit_navigation_operation(
            client, "rename_node", target_node_id=node_id, name=name
        )
        assert receipt["state"] == "committed", receipt
    await _move(client, child_id, sibling_id)
    assert resolver.resolve_session_node(session_id) == path
    assert (
        resolver.catalog_store.get_node(session_id).main_thread_id
        == stored.main_thread_id
    )
    _assert_bundle(path, bundle, inode)
    for folder_id in (parent_id, child_id, sibling_id):
        folder = resolver.catalog_store.get_node(folder_id)
        assert folder.kind == "folder"
        assert folder.storage_relative_locator is None and folder.main_thread_id is None
        assert not list(sessions_root.rglob(folder_id))
    assert not list(sessions_root.rglob(".boxteam-folder.json"))

    nodes = await _catalog_nodes(client)
    root_ids = {
        node_id for node_id, node in nodes.items() if node["parent_node_id"] is None
    }
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, str | int] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/api/v1/session-catalog/roots", params=params)
        assert response.status_code == 200, response.text
        assert "storage_relative_path" not in response.text
        page = response.json()["data"]
        assert page["total"] == len(root_ids)
        seen.extend(item["node_id"] for item in page["items"])
        cursor = page["cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) and set(seen) == root_ids
    response = await client.get(f"/api/v1/session-catalog/breadcrumb/{session_id}")
    assert response.status_code == 200, response.text
    assert [item["name"] for item in response.json()["data"]["items"]] == [
        "待归档",
        "阶段一已重命名",
        "Catalog Needle Renamed",
    ]
    assert "storage_relative_path" not in response.text
    search_alias = path.relative_to(sessions_root).as_posix()
    for query in ("Catalog Needle Renamed", session_id, search_alias):
        response = await client.get(
            "/api/v1/session-catalog/search", params={"query": query, "limit": 10}
        )
        assert response.status_code == 200, response.text
        result = response.json()["data"]["items"]
        assert [item["node"]["node_id"] for item in result] == [session_id]
        assert (
            result[0]["relative_path"] == "待归档/阶段一已重命名/Catalog Needle Renamed"
        )
        assert "storage_relative_path" not in response.text
        assert search_alias not in response.text

    # 故障注入只挪走产品创建的真实 Session，不替产品准备业务状态。
    moved = path.with_name(f"manual-{session_id}")
    path.replace(moved)
    try:
        response = await client.get(f"/api/v1/sessions/{session_id}")
        assert response.status_code == 409, response.text
        assert "会话物理目录缺失" in response.text
        assert str(path) not in response.text
        assert session_id in response.text
        assert not path.exists()
    finally:
        moved.replace(path)
    response = await client.get(f"/api/v1/sessions/{session_id}")
    assert response.status_code == 200, response.text
    refreshed = await client.post("/api/v1/session-catalog/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert "storage_relative_path" not in refreshed.text
    _assert_bundle(path, bundle, inode)
    assert (await _catalog_nodes(client))[session_id] == nodes[session_id]
    assert (
        resolver.catalog_store.get_node(session_id).storage_relative_locator
        == stored.storage_relative_locator
    )


@pytest.mark.asyncio
async def test_session_folder_rejects_cycle_without_moving_physical_nodes(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    parent_id = await _create_folder(client, "循环父目录")
    child_id = await _create_folder(client, "循环子目录", parent_id)
    before = await _catalog_nodes(client)
    receipt = await submit_navigation_operation(
        client, "move_node", target_node_id=parent_id, parent_node_id=child_id
    )
    assert receipt["state"] == "rejected"
    assert "循环" in str(receipt["error_detail"])
    assert await _catalog_nodes(client) == before
    resolver = get_session_path_resolver(
        Path(e2e_workspace_root_path) / ".boxteam/sessions"
    )
    for folder_id in (parent_id, child_id):
        folder = resolver.catalog_store.get_node(folder_id)
        assert folder.storage_relative_locator is None and folder.main_thread_id is None


@pytest.mark.asyncio
async def test_session_folder_recursive_delete_removes_complete_session_bundles(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    resolver = get_session_path_resolver(
        Path(e2e_workspace_root_path) / ".boxteam/sessions"
    )
    parent_id = await _create_folder(client, "递归删除年月日")
    child_id = await _create_folder(client, "时分秒", parent_id)
    session_id = await _create_session(client, "递归删除生成会话")
    await _move(client, session_id, child_id)
    path = resolver.resolve_session_node(session_id)
    _write_bundle_sentinels(path)
    before = await _catalog_nodes(client)
    rejected = await submit_navigation_operation(
        client, "delete_folder", target_node_id=parent_id
    )
    assert (
        rejected["state"] == "rejected"
        and rejected["error_code"] == "invalid_operation"
    )
    assert await _catalog_nodes(client) == before
    assert path.is_dir()
    receipt = await submit_navigation_operation(
        client,
        "delete_folder",
        target_node_id=parent_id,
        recursive=True,
        wait_for_settlement=True,
    )
    assert receipt["state"] == "committed" and receipt["settlement"] == "settled", (
        receipt
    )
    assert not path.exists()
    nodes = await _catalog_nodes(client)
    for node_id in (parent_id, child_id, session_id):
        assert node_id not in nodes
        with pytest.raises(KeyError):
            resolver.catalog_store.get_node(node_id)
    response = await client.get(f"/api/v1/sessions/{session_id}")
    assert response.status_code == 404, response.text
