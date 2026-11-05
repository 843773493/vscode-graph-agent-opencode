"""真实 HTTP 适配只投影唯一 URI grammar，不访问资源或创建能力。"""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.identifier import create_prefixed_id
from app.core.resource_uri import resource_display_uri, thread_resource_uri
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import get_gateway_local_token
from app.gateway.routes.resource_addresses import router

GATEWAY = "gateway_" + "a" * 32
REMOTE = "gateway_" + "b" * 32
WORKSPACE = "ws-test"
SESSION = create_prefixed_id("ses")
THREAD = create_prefixed_id("thr")


@pytest.fixture
def address_client() -> Iterator[TestClient]:
    app = FastAPI()
    app.state.gateway_id = GATEWAY
    app.add_middleware(TraceMiddleware)
    # 故意不装配 registry 或文件 owner：解析不能访问工作区状态。
    app.include_router(router)
    with TestClient(app, headers={"X-Local-Token": get_gateway_local_token()}) as client:
        yield client


@pytest.mark.parametrize("authority", [None, GATEWAY, REMOTE])
@pytest.mark.parametrize("child", [False, True])
def test_session_and_child_keep_explicit_owner_without_reading_resources(
    address_client: TestClient, authority: str | None, child: bool,
) -> None:
    uri = thread_resource_uri(WORKSPACE, SESSION, THREAD, gateway_authority=authority) if child else resource_display_uri(
        scope="workspace", scope_id=WORKSPACE, kind="session", tail_segments=(SESSION,), gateway_authority=authority,
    )
    response = address_client.post("/api/gateway/resource-addresses/parse", json={"uri": uri, "local_gateway_id": GATEWAY},
                                   headers={"X-Request-ID": "address-owner"})
    assert response.status_code == 200
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == "address-owner"
    data = response.json()["data"]
    assert data["gateway_id"] == (authority or GATEWAY)
    assert (data["scope"], data["scope_id"], data["kind"]) == ("workspace", WORKSPACE, "session")
    assert data["session_id"] == SESSION
    assert data["thread_id"] == (THREAD if child else None)
    assert data["file_root_uri"] is None
    expected = uri.replace(f"boxteam://{GATEWAY}/", "boxteam://")
    assert data["uri"] == expected
    assert data["reference_uri"] == resource_display_uri(
        scope="workspace", scope_id=WORKSPACE, kind="session",
        tail_segments=(SESSION, "threads", THREAD) if child else (SESSION,),
        gateway_authority=authority or GATEWAY,
    )


def test_file_root_comes_from_same_uri_owner(address_client: TestClient) -> None:
    root = resource_display_uri(scope="workspace", scope_id=WORKSPACE, kind="files", tail_segments=("root",))
    uri = root + "/%E6%96%87%E4%BB%B6%40%3A7.txt"
    response = address_client.post("/api/gateway/resource-addresses/parse", json={"uri": uri, "local_gateway_id": GATEWAY})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["uri"] == uri
    assert data["reference_uri"] == uri.replace("boxteam://", f"boxteam://{GATEWAY}/", 1)
    assert data["file_root_uri"] == root
    assert data["session_id"] is data["thread_id"] is None


@pytest.mark.parametrize("suffix", ["?version=1", "#L2", "/..", "/%2F", "/%FF"])
def test_invalid_uri_is_not_rewritten_as_a_different_target(address_client: TestClient, suffix: str) -> None:
    uri = thread_resource_uri(WORKSPACE, SESSION, THREAD) + suffix
    response = address_client.post("/api/gateway/resource-addresses/parse", json={"uri": uri, "local_gateway_id": GATEWAY})
    assert response.status_code == 400
    assert response.json()["detail"]["owner"] == "resource_uri_grammar"


def test_changed_source_gateway_is_rejected_and_credentials_are_required(address_client: TestClient) -> None:
    uri = thread_resource_uri(WORKSPACE, SESSION, THREAD)
    response = address_client.post("/api/gateway/resource-addresses/parse", json={"uri": uri, "local_gateway_id": REMOTE})
    assert response.status_code == 409
    assert "Gateway 身份已改变" in response.json()["detail"]
    response = address_client.post("/api/gateway/resource-addresses/parse", json={"uri": uri, "local_gateway_id": GATEWAY},
                                   headers={"X-Local-Token": "invalid"})
    assert response.status_code == 401
