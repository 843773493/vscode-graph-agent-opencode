from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from app.gateway.auth import get_gateway_local_token
from app.gateway.main import app
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes._shared import get_registry

GATEWAY = "gateway_" + "a" * 32
PARENT = "00000000-0000-4000-8000-000000000001"
CHILD = "00000000-0000-4000-8000-000000000002"
GRANDCHILD = "00000000-0000-4000-8000-000000000003"
UNKNOWN = "00000000-0000-4000-8000-000000000099"


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
        gateway_id=GATEWAY,
        workspace_id=workspace_id,
        name=workspace_id,
        root_path=f"/workspace/{workspace_id}",
        backend_url="",  # 只核对离线登记层级，不让单元测试探测外部服务。
        connection_kind="local",
    )


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[GatewayWorkspaceRegistry]:
    with closing(GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json", gateway_id=GATEWAY)) as result:
        yield result


def test_workspace_parent_persists_and_rejects_cycles(tmp_path: Path, registry: GatewayWorkspaceRegistry) -> None:
    registry.upsert(_target(PARENT))
    registry.upsert(_target(CHILD))
    registry.upsert(_target(GRANDCHILD))

    registry.set_parent((GATEWAY, CHILD), (GATEWAY, PARENT))
    registry.set_parent((GATEWAY, GRANDCHILD), (GATEWAY, CHILD))

    with pytest.raises(ValueError, match="不能形成循环"):
        registry.set_parent((GATEWAY, PARENT), (GATEWAY, GRANDCHILD))
    with pytest.raises(ValueError, match="不能成为自己的父工作区"):
        registry.set_parent((GATEWAY, PARENT), (GATEWAY, PARENT))

    with closing(GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json", gateway_id=GATEWAY)) as restored:
        assert restored.resolve((GATEWAY, CHILD)).parent_workspace == (GATEWAY, PARENT)
        assert restored.resolve((GATEWAY, GRANDCHILD)).parent_workspace == (GATEWAY, CHILD)


def test_remove_workspace_promotes_direct_children_to_roots(
    registry: GatewayWorkspaceRegistry,
) -> None:
    registry.upsert(_target(PARENT))
    registry.upsert(_target(CHILD))
    registry.set_parent((GATEWAY, CHILD), (GATEWAY, PARENT))

    registry.remove((GATEWAY, PARENT))

    assert registry.resolve((GATEWAY, CHILD)).parent_workspace is None


def test_workspace_update_is_atomic_when_parent_is_invalid(
    registry: GatewayWorkspaceRegistry,
) -> None:
    registry.upsert(_target(CHILD))

    with pytest.raises(KeyError, match="未知 Gateway 父工作区"):
        registry.update(
            (GATEWAY, CHILD),
            name="Renamed",
            parent_workspace=(GATEWAY, UNKNOWN),
        )

    assert registry.resolve((GATEWAY, CHILD)).name == CHILD
    assert registry.resolve((GATEWAY, CHILD)).parent_workspace is None


@pytest.mark.asyncio
async def test_workspace_update_endpoint_sets_and_clears_parent(
    registry: GatewayWorkspaceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry.upsert(_target(PARENT))
    registry.upsert(_target(CHILD))
    monkeypatch.setitem(app.dependency_overrides, get_registry, lambda: registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    ) as client:
        bind_response = await client.patch(
            f"/api/gateway/workspaces/{CHILD}",
            headers={"X-Local-Token": get_gateway_local_token()},
            json={"parent_workspace": {"gateway_id": GATEWAY, "workspace_id": PARENT}},
        )
        unbind_response = await client.patch(
            f"/api/gateway/workspaces/{CHILD}",
            headers={"X-Local-Token": get_gateway_local_token()},
            json={"parent_workspace": None},
        )

    assert bind_response.status_code == 200
    bound = next(
        item
        for item in bind_response.json()["data"]["items"]
        if (item["gateway_id"], item["workspace_id"]) == (GATEWAY, CHILD)
    )
    assert bound["parent_workspace"] == {"gateway_id": GATEWAY, "workspace_id": PARENT}
    assert unbind_response.status_code == 200
    unbound = next(
        item
        for item in unbind_response.json()["data"]["items"]
        if (item["gateway_id"], item["workspace_id"]) == (GATEWAY, CHILD)
    )
    assert unbound["parent_workspace"] is None


@pytest.mark.asyncio
async def test_workspace_update_endpoint_rejects_unknown_parent(
    registry: GatewayWorkspaceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry.upsert(_target(CHILD))
    monkeypatch.setitem(app.dependency_overrides, get_registry, lambda: registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    ) as client:
        response = await client.patch(
            f"/api/gateway/workspaces/{CHILD}",
            headers={"X-Local-Token": get_gateway_local_token()},
            json={"parent_workspace": {"gateway_id": GATEWAY, "workspace_id": UNKNOWN}},
        )

    assert response.status_code == 404
    assert "未知 Gateway 父工作区" in response.json()["detail"]
