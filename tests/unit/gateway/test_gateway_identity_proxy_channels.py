"""验证生命周期控制面与联邦 catalog 注入原生 Gateway 身份。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import reset_current_request_id, set_current_request_id
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.federation.workspace_port import WorkspaceCatalogPort
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController


def _gateway_id(tmp_path: Path) -> str:
    """读取/生成本机 identity.json 的权威 gateway_id，作为断言基准。"""

    identity = get_gateway_root() / "identity.json"
    return json.loads(identity.read_text(encoding="utf-8"))["gateway_id"]


@pytest.mark.parametrize("value", [
    "local", "8014", "127.0.0.1:8014", "gateway_short",
    "gateway_" + "A" * 32, "gateway_" + "g" * 32, None,
])
def test_invalid_persisted_gateway_identity_is_not_replaced(tmp_path: Path, value: object) -> None:
    path = tmp_path / "identity.json"
    original = json.dumps({"gateway_id": value}).encode()
    path.write_bytes(original)
    with pytest.raises(ValueError, match="Gateway 身份文件非法"):
        load_or_create_gateway_id(path)
    assert path.read_bytes() == original


def test_backend_headers_inject_authoritative_gateway_id(tmp_path: Path) -> None:
    """生命周期/配置 control-plane 的 ``_backend_headers`` 注入真实 gateway_id。"""

    controller = GatewayWorkspaceRuntimeController(
        registry=GatewayWorkspaceRegistry(storage_path=get_gateway_root() / "workspaces.json"),
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
    )

    headers = controller._backend_headers(
        "req_control_plane",
        "00000000-0000-4000-8000-000000000001",
    )

    assert headers["X-BoxTeam-Gateway-Id"] == _gateway_id(tmp_path)
    assert headers["X-BoxTeam-Gateway-Id"].startswith("gateway_")
    # 既有头部语义不被改动：仍是同一 request_id，绝不补造第二个请求 ID。
    assert headers["X-Request-ID"] == "req_control_plane"
    assert headers["X-Local-Token"]
    assert headers["X-BoxTeam-Workspace-Id"] == "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def catalog_request_id():
    request_id = "req_catalog_gateway_identity"
    token = set_current_request_id(request_id)
    yield request_id
    reset_current_request_id(token)


@pytest.mark.asyncio
async def test_workspace_catalog_port_injects_authoritative_gateway_id(
    tmp_path: Path,
    catalog_request_id: str,
) -> None:
    """联邦冷 catalog 端口对工作区后端的请求带真实 gateway_id。"""

    captured: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "request_id": request.headers["X-Request-ID"],
                "data": {
                    "items": [{"session_id": "session_catalog_identity"}],
                    "catalog_revision": "rev_34c",
                },
            },
            headers={"X-Request-ID": request.headers["X-Request-ID"]},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as client:
        port = WorkspaceCatalogPort(http_client=client)
        port.project_workspaces(
            (("00000000-0000-4000-8000-000000000001", "http://127.0.0.1:41100"),)
        )
        workspaces = await port.local_session_workspaces("session_catalog_identity")

    assert workspaces == ("00000000-0000-4000-8000-000000000001",)
    assert len(captured) == 1
    # httpx 头部名统一小写；断言线上可见的真实权威值。
    assert captured[0]["x-boxteam-gateway-id"] == _gateway_id(tmp_path)
    assert captured[0]["x-boxteam-workspace-id"] == "00000000-0000-4000-8000-000000000001"
    assert captured[0]["x-request-id"] == catalog_request_id
