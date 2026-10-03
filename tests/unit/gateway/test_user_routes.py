"""Gateway 用户访问与具名删除路由的请求级回归测试。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import verify_gateway_token
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.user_access import (
    USER_ACCESS_COOKIE_NAME,
    UserAccessService,
)
from app.gateway.control.user_profile import UserProfileStore
from app.gateway.routes import users


@pytest.fixture
def gateway_user_route_context(
    tmp_path: Path,
) -> Iterator[
    tuple[FastAPI, UserAccessService, UserProfileStore, GatewayStateStore]
]:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    service = UserAccessService(state=state)
    profiles = UserProfileStore(gateway_root=tmp_path / "profiles")
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(users.router)
    application.state.user_access_service = service
    application.state.user_profile_store = profiles
    application.dependency_overrides[verify_gateway_token] = lambda: "test-token"
    try:
        yield application, service, profiles, state
    finally:
        application.dependency_overrides.clear()
        state.close()


def test_create_user_rejects_current_without_side_effects_and_accepts_other_ids(
    gateway_user_route_context: tuple[
        FastAPI,
        UserAccessService,
        UserProfileStore,
        GatewayStateStore,
    ],
) -> None:
    application, service, profiles, state = gateway_user_route_context
    headers = {"X-Local-Token": "test-token"}

    with TestClient(application) as client:
        rejected = client.post(
            "/api/gateway/users",
            json={"display_name": "保留 ID", "user_id": "current"},
            headers=headers,
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"] == "用户 ID current 是当前访问路由的保留标识"
        assert service.list_users() == ()
        assert not profiles.user_path("current").exists()

        connection = state.connection()
        try:
            assert connection.execute("SELECT COUNT(*) FROM user_account").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM user_access_lease").fetchone()[0] == 0
        finally:
            connection.close()

        created = client.post(
            "/api/gateway/users",
            json={"display_name": "正常用户", "user_id": "current-user"},
            headers=headers,
        )
        assert created.status_code == 200
        assert created.json()["data"]["user_id"] == "current-user"

    assert profiles.user_path("current-user").is_dir()
    assert service.list_users()[0].lease.occupied is False


def test_delete_current_releases_lease_and_clears_cookie(
    gateway_user_route_context: tuple[
        FastAPI,
        UserAccessService,
        UserProfileStore,
        GatewayStateStore,
    ],
) -> None:
    application, service, _, _ = gateway_user_route_context
    user = service.create_user(display_name="当前用户", user_id="current-user")
    access = service.acquire_user(
        user_id=user.user_id,
        client_label="当前客户端",
    )

    with TestClient(application) as client:
        client.cookies.set(USER_ACCESS_COOKIE_NAME, access.access_session_id)
        response = client.delete(
            "/api/gateway/users/current",
            headers={"X-Local-Token": "test-token"},
        )

        assert response.status_code == 200
        assert response.json()["data"] == {"released": True}
        assert "boxteam-user-access=" in response.headers["set-cookie"]
        assert "max-age=0" in response.headers["set-cookie"].lower()

    assert service.resolve_cookie(access.access_session_id) is None
    assert service.list_users()[0].lease.occupied is False


def test_named_user_delete_keeps_lease_conflict_and_not_found_behavior(
    gateway_user_route_context: tuple[
        FastAPI,
        UserAccessService,
        UserProfileStore,
        GatewayStateStore,
    ],
) -> None:
    application, service, profiles, _ = gateway_user_route_context
    user = service.create_user(display_name="具名用户", user_id="named-user")
    profiles.ensure_user(user_id=user.user_id, display_name=user.display_name)
    access = service.acquire_user(
        user_id=user.user_id,
        client_label="占用客户端",
    )

    with TestClient(application) as client:
        occupied = client.delete(
            f"/api/gateway/users/{user.user_id}",
            headers={"X-Local-Token": "test-token"},
        )
        assert occupied.status_code == 409
        assert occupied.json()["detail"]["code"] == "user_lease_occupied"
        assert service.resolve_cookie(access.access_session_id) is not None
        assert profiles.user_path(user.user_id).is_dir()

        service.release(access)
        deleted = client.delete(
            f"/api/gateway/users/{user.user_id}",
            headers={"X-Local-Token": "test-token"},
        )
        assert deleted.status_code == 200
        assert deleted.json()["data"] == {"user_id": user.user_id}
        assert not profiles.user_path(user.user_id).exists()
        assert service.list_users() == ()

        missing = client.delete(
            "/api/gateway/users/unknown-user",
            headers={"X-Local-Token": "test-token"},
        )
        assert missing.status_code == 404
        assert "未知用户 ID: unknown-user" in missing.json()["detail"]
