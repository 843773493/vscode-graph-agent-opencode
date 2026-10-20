from __future__ import annotations

import json
from pathlib import Path

import httpx
import jsonschema
import pytest
from fastapi import FastAPI

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import verify_gateway_token
from app.gateway.config import GatewayConfigReloadService, load_gateway_config
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.routes.health_config import router


@pytest.fixture
def validation_owner(tmp_path: Path):
    config_path = tmp_path / "gateway.jsonc"
    schema_path = tmp_path / "gateway_schema.jsonc"
    original = json.dumps({"config_version": 2, "workspaces": []})
    config_path.write_text(original, encoding="utf-8")
    schema_path.write_bytes(Path("configs/gateway_schema.jsonc").read_bytes())
    with GatewayStateStore(path=tmp_path / "gateway.sqlite") as store:
        config = load_gateway_config(
            config_path=config_path, schema_path=schema_path, state_store=store,
        )
        service = GatewayConfigReloadService(
            state_store=store, config=config, config_path=config_path,
            local_config_path=tmp_path / "gateway_local.jsonc", schema_path=schema_path,
        )
        service.initialize_active_snapshot()
        application = FastAPI()
        application.add_middleware(TraceMiddleware)
        application.include_router(router)
        application.state.gateway_config_reload = service
        application.dependency_overrides[verify_gateway_token] = lambda: "validation-test"
        yield application, service, store, config_path, schema_path, original


@pytest.mark.parametrize(
    ("override", "location"),
    [
        ({"workspaces": "invalid"}, "workspaces"),
        ({"ui": {"theme": {"default_theme_id": 3}}}, "ui.theme.default_theme_id"),
    ],
)
async def test_invalid_reload_exposes_schema_reason_without_native_source_paths(
    validation_owner, override: dict[str, object], location: str,
) -> None:
    application, service, store, config_path, schema_path, original = validation_owner
    initial_config = service.config
    active = store.get_active_config_snapshot("gateway")
    payload = json.loads(original) | override
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="配置验证失败") as failure:
        await service.reload()
    cause = failure.value.__cause__
    assert isinstance(cause, jsonschema.ValidationError)
    expected = f"配置验证失败: location={location}: {cause.message}"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/gateway/config/reload-status", headers={"X-Request-ID": "invalid-config"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "invalid-config"
        status = response.json()["data"]
        assert status["healthy"] is False
        assert status["reason"] == "invalid_config"
        assert status["last_error"] == f"ValueError: {expected}"
        assert str(config_path) not in response.text
        assert str(schema_path) not in response.text
        assert str(config_path.parent) not in response.text
        assert service.config is initial_config
        assert store.get_active_config_snapshot("gateway") == active
        assert store.get_pending_config_candidate(config_domain="gateway") is None
        assert service.list_events() == ()

        # 修正源文件后走同一 reload，错误必须清除，原 active 不被坏候选覆盖。
        config_path.write_text(original, encoding="utf-8")
        await service.reload()
        recovered = await client.get("/api/gateway/config/reload-status")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["data"]["healthy"] is True
        assert recovered.json()["data"]["last_error"] is None
        assert recovered.json()["data"]["reason"] is None
        assert store.get_active_config_snapshot("gateway") == active


@pytest.mark.parametrize(("damage", "exception", "reason"), [
    ("config_root", TypeError, "配置文件根节点必须是对象"),
    ("schema_root", TypeError, "配置文件根节点必须是对象"),
    ("schema_missing", FileNotFoundError, "配置文件不存在"),
    ("schema_directory", IsADirectoryError, "配置文件读取失败"),
])
async def test_source_read_failure_has_no_native_path_and_preserves_active_state(
    validation_owner, damage: str, exception: type[Exception], reason: str,
) -> None:
    application, service, store, config_path, schema_path, original = validation_owner
    initial_config = service.config
    active = store.get_active_config_snapshot("gateway")
    schema_bytes = schema_path.read_bytes()
    if damage == "config_root":
        config_path.write_text("[]", encoding="utf-8")
    elif damage == "schema_root":
        schema_path.write_text("[]", encoding="utf-8")
    else:
        schema_path.unlink()
        if damage == "schema_directory":
            schema_path.mkdir()

    with pytest.raises(exception, match=reason):
        await service.reload()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        response = await client.get("/api/gateway/config/reload-status")
        assert response.status_code == 200, response.text
        status = response.json()["data"]
        assert status["healthy"] is False
        assert status["reason"] == "invalid_config"
        assert reason in status["last_error"]
        assert str(config_path.parent) not in response.text
        assert service.config is initial_config
        assert store.get_active_config_snapshot("gateway") == active
        assert store.get_pending_config_candidate(config_domain="gateway") is None
        assert service.list_events() == ()
        if damage == "schema_directory":
            schema_path.rmdir()
        schema_path.write_bytes(schema_bytes)
        config_path.write_text(original, encoding="utf-8")
        await service.reload()
        recovered = (await client.get("/api/gateway/config/reload-status")).json()["data"]
        assert recovered["healthy"] is True
        assert recovered["last_error"] is None
        assert store.get_active_config_snapshot("gateway") == active
