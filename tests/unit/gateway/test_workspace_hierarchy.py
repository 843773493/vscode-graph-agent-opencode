from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.gateway.auth import get_gateway_local_token
from app.gateway.main import app, get_registry
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget


@pytest.fixture(autouse=True)
def _hermetic_proxy_env(monkeypatch: pytest.MonkeyPatch):
    """R19 测试内 hermetic 修复：清掉本机代理环境变量。

    本文件用例不经外部网络；而环境的 NO_PROXY 含 IPv6 字面量（``::1``/
    ``[::1]``）会让用例内（或被测 gateway 代码内）构造的 httpx 客户端在
    代理解析时直接抛 ``InvalidURL: Invalid port: ':1]'``——属外部环境噪
    声混入测试。与 R18 对 gateway 会话上下文客户端的同类修复一致（任务
    书许可的 monkeypatch 清 ``*_proxy`` 做法）；清掉后客户端不取代理，
    与用例意图一致。
    """
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


def _target(workspace_id: str) -> WorkspaceTarget:
    return WorkspaceTarget(
        workspace_id=workspace_id,
        name=workspace_id,
        root_path=f"/workspace/{workspace_id}",
        backend_url="http://127.0.0.1:18010",
        connection_kind="local",
    )


def test_workspace_parent_persists_and_rejects_cycles(tmp_path: Path) -> None:
    storage_path = tmp_path / "workspaces.json"
    registry = GatewayWorkspaceRegistry(storage_path=storage_path)
    registry.upsert(_target("gw_parent"))
    registry.upsert(_target("gw_child"))
    registry.upsert(_target("gw_grandchild"))

    registry.set_parent("gw_child", "gw_parent")
    registry.set_parent("gw_grandchild", "gw_child")

    with pytest.raises(ValueError, match="不能形成循环"):
        registry.set_parent("gw_parent", "gw_grandchild")
    with pytest.raises(ValueError, match="不能成为自己的父工作区"):
        registry.set_parent("gw_parent", "gw_parent")

    restored = GatewayWorkspaceRegistry(storage_path=storage_path)
    assert restored.resolve("gw_child").parent_workspace_id == "gw_parent"
    assert (
        restored.resolve("gw_grandchild").parent_workspace_id
        == "gw_child"
    )


def test_remove_workspace_promotes_direct_children_to_roots(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json"
    )
    registry.upsert(_target("gw_parent"))
    registry.upsert(_target("gw_child"))
    registry.set_parent("gw_child", "gw_parent")

    registry.remove("gw_parent")

    assert registry.resolve("gw_child").parent_workspace_id is None


def test_workspace_update_is_atomic_when_parent_is_invalid(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json"
    )
    registry.upsert(_target("gw_child"))

    with pytest.raises(KeyError, match="未知 Gateway 父工作区"):
        registry.update(
            "gw_child",
            name="Renamed",
            parent_workspace_id="gw_unknown",
        )

    assert registry.resolve("gw_child").name == "gw_child"
    assert registry.resolve("gw_child").parent_workspace_id is None


@pytest.mark.asyncio
async def test_workspace_update_endpoint_sets_and_clears_parent(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json"
    )
    registry.upsert(_target("gw_parent"))
    registry.upsert(_target("gw_child"))
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway.test",
        ) as client:
            bind_response = await client.patch(
                "/api/gateway/workspaces/gw_child",
                    headers={"X-Local-Token": get_gateway_local_token()},
                json={"parent_workspace_id": "gw_parent"},
            )
            unbind_response = await client.patch(
                "/api/gateway/workspaces/gw_child",
                    headers={"X-Local-Token": get_gateway_local_token()},
                json={"parent_workspace_id": None},
            )
    finally:
        app.dependency_overrides.clear()

    assert bind_response.status_code == 200
    bound = next(
        item
        for item in bind_response.json()["data"]["items"]
        if item["workspace_id"] == "gw_child"
    )
    assert bound["parent_workspace_id"] == "gw_parent"
    assert unbind_response.status_code == 200
    unbound = next(
        item
        for item in unbind_response.json()["data"]["items"]
        if item["workspace_id"] == "gw_child"
    )
    assert unbound["parent_workspace_id"] is None


@pytest.mark.asyncio
async def test_workspace_update_endpoint_rejects_unknown_parent(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json"
    )
    registry.upsert(_target("gw_child"))
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway.test",
        ) as client:
            response = await client.patch(
                "/api/gateway/workspaces/gw_child",
                    headers={"X-Local-Token": get_gateway_local_token()},
                json={"parent_workspace_id": "gw_unknown"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert "未知 Gateway 父工作区" in response.json()["detail"]
