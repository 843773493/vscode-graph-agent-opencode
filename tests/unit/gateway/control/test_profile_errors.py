"""用户 profile 的原生故障在 owner 边界保留诊断，不进入普通响应。"""

from __future__ import annotations

import errno
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import verify_gateway_token
from app.gateway.config import GatewayConfig
from app.gateway.control import user_profile as profile_module
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.user_access import UserAccessService
from app.gateway.control.user_profile import UserProfileStore
from app.gateway.routes import ui_assets, users


@pytest.fixture
def profile_http_context(tmp_path: Path) -> Iterator[tuple[TestClient, UserProfileStore, UserAccessService]]:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    profiles = UserProfileStore(gateway_root=tmp_path / "private-profile-root")
    service = UserAccessService(state=state)
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(users.router)
    application.include_router(ui_assets.router)
    application.state.user_access_service = service
    application.state.user_profile_store = profiles
    application.state.gateway_config = GatewayConfig()
    application.dependency_overrides[verify_gateway_token] = lambda: "test-token"
    try:
        with TestClient(application, headers={"X-Request-ID": "profile-error-request"}) as client:
            created = client.post("/api/gateway/users", json={"user_id": "profile-user", "display_name": "配置用户"})
            assert created.status_code == 200, created.text
            acquired = client.post("/api/gateway/users/profile-user/access", json={"client_label": "profile-test"})
            assert acquired.status_code == 200, acquired.text
            yield client, profiles, service
    finally:
        state.close()


@pytest.fixture(params=["/private/profile-secret", r"C:\private\profile-secret", r"\\server\private\profile-secret"])
def native_failure(request: pytest.FixtureRequest) -> PermissionError:
    return PermissionError(errno.EACCES, f"原生存储拒绝: {request.param}", request.param)


@pytest.mark.parametrize("operation", ["initialize", "read", "write", "theme-assets", "delete"])
def test_profile_io_errors_preserve_cause_and_route_behavior(
    profile_http_context, native_failure, operation, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    client, profiles, service = profile_http_context
    path = profiles.user_path("profile-user") / "profile.jsonc"
    original = path.read_bytes()
    raw_error = native_failure
    real_mkdir, real_read, real_replace, real_rmtree = Path.mkdir, Path.read_text, profile_module.os.replace, profile_module.shutil.rmtree

    def mkdir(target: Path, *args: object, **kwargs: object) -> None:
        if (operation == "initialize" and target == profiles.user_path("another-user")) or (
            operation == "theme-assets" and target == path.parent / "themes/assets"
        ):
            raise raw_error
        real_mkdir(target, *args, **kwargs)

    def read(target: Path, *args: object, **kwargs: object) -> str:
        if operation == "read" and target == path:
            raise raw_error
        return real_read(target, *args, **kwargs)

    def replace(source: Path, destination: Path) -> None:
        if operation == "write" and destination == path:
            raise raw_error
        real_replace(source, destination)

    def rmtree(target: Path, *args: object, **kwargs: object) -> None:
        if operation == "delete" and target == path.parent:
            raise raw_error
        real_rmtree(target, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(profile_module.os, "replace", replace)
    monkeypatch.setattr(profile_module.shutil, "rmtree", rmtree)
    with caplog.at_level(logging.ERROR, logger=profile_module.__name__):
        if operation == "initialize":
            response = client.post("/api/gateway/users", json={"user_id": "another-user", "display_name": "新用户"})
            assert response.status_code == 500, response.text
            assert [item.user.user_id for item in service.list_users()] == ["profile-user"], "创建失败必须回滚账户"
            assert not profiles.user_path("another-user").exists()
        elif operation == "delete":
            assert client.delete("/api/gateway/users/current").status_code == 200
            response = client.delete("/api/gateway/users/profile-user")
            assert response.status_code == 500, response.text
            assert service.list_users() == (), "保持原先账户删除在 profile 删除之前的顺序"
            assert path.is_file()
        elif operation == "write":
            response = client.put("/api/gateway/ui-settings", json={"layout": {"workbench_view": "gateway"}})
            assert response.status_code == 409, response.text
        else:
            response = client.get("/api/gateway/ui-settings")
            assert response.status_code == 409, response.text
    assert response.headers["X-Request-ID"] == "profile-error-request"
    detail = response.json()["detail"]
    assert f"operation={operation}" in detail
    assert "error_type=PermissionError" in detail and f"errno={errno.EACCES}" in detail
    assert raw_error.filename not in response.text
    assert "profile-secret" not in response.text and str(path.parent) not in response.text
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".profile.jsonc.*")), "写失败应回收临时文件"
    record, = [record for record in caplog.records if record.name == profile_module.__name__]
    assert record.exc_info is not None and record.exc_info[1] is raw_error
    assert raw_error.filename in caplog.text

    # 移除同一故障后继续走原产品入口，不能把失败伪装成成功或改变用户身份。
    monkeypatch.undo()
    if operation == "initialize":
        recovered = client.post("/api/gateway/users", json={"user_id": "another-user", "display_name": "新用户"})
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["data"]["user_id"] == "another-user"
    elif operation == "delete":
        profiles.delete_user(user_id="profile-user")
        assert not path.parent.exists()
    else:
        recovered = client.put("/api/gateway/ui-settings", json={"layout": {"workbench_view": "gateway"}})
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["data"]["layout"]["workbench_view"] == "gateway"
        assert profiles.read_profile(user_id="profile-user")["display_name"] == "配置用户"


@pytest.mark.parametrize("content", [b"[]", b"{}", b'{"config_version":2}', b'{"private":"/native/profile-secret",}', b'\xff/private/profile-secret'])
@pytest.mark.parametrize("method,url,payload", [
    ("GET", "/api/gateway/ui-settings", None),
    ("PUT", "/api/gateway/ui-settings", {"layout": {"workbench_view": "gateway"}}),
    ("GET", "/api/gateway/themes", None),
    ("GET", "/api/gateway/ui-assets", None),
])
def test_profile_content_failure_keeps_bytes_and_recovers_via_same_route(profile_http_context, content, method, url, payload) -> None:
    client, profiles, _ = profile_http_context
    path = profiles.user_path("profile-user") / "profile.jsonc"
    original = path.read_bytes()
    path.write_bytes(content)
    response = client.request(method, url, json=payload)
    assert response.status_code == 409, response.text
    assert "用户 profile" in response.json()["detail"]
    assert str(path.parent) not in response.text and "profile-secret" not in response.text
    assert response.headers["X-Request-ID"] == "profile-error-request"
    assert path.read_bytes() == content
    path.write_bytes(original)
    recovered = client.request(method, url, json=payload)
    assert recovered.status_code == 200, recovered.text


def test_missing_profile_and_invalid_delete_target_do_not_expose_paths(profile_http_context) -> None:
    client, profiles, service = profile_http_context
    path = profiles.user_path("profile-user") / "profile.jsonc"
    path.unlink()
    response = client.get("/api/gateway/ui-settings")
    assert response.status_code == 409 and "FileNotFoundError" in response.json()["detail"]
    assert str(path) not in response.text
    assert client.delete("/api/gateway/users/current").status_code == 200
    other = client.post("/api/gateway/users", json={"user_id": "delete-user", "display_name": "待删除"})
    assert other.status_code == 200, other.text
    root = profiles.user_path("delete-user")
    profile_module.shutil.rmtree(root)
    root.write_text("/private/profile-secret")
    response = client.delete("/api/gateway/users/delete-user")
    assert response.status_code == 500 and "NotADirectoryError" in response.json()["detail"]
    assert str(root) not in response.text and "profile-secret" not in response.text
    assert root.read_text() == "/private/profile-secret"
    assert [item.user.user_id for item in service.list_users()] == ["profile-user"]


def test_owner_io_projection_keeps_exception_category_and_raw_cause(profile_http_context, native_failure, monkeypatch) -> None:
    _, profiles, _ = profile_http_context
    path = profiles.user_path("profile-user") / "profile.jsonc"
    real_read = Path.read_text

    def read(target: Path, *args: object, **kwargs: object) -> str:
        if target == path:
            raise native_failure
        return real_read(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(PermissionError) as caught:
        profiles.read_profile(user_id="profile-user")
    assert caught.value.__cause__ is native_failure
    assert caught.value.filename is None
    assert native_failure.filename not in str(caught.value)
    assert json.loads(path.read_bytes())["display_name"] == "配置用户"
