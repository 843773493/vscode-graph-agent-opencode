"""插件启动参数交付只暴露明确的 owner 输入错误。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import JsonValue

from app.api.deps import get_workspace_container
from app.api.runtime_control import verify_local_control_request
from app.api.service_plugins import router
from app.core.trace_middleware import TraceMiddleware
from app.main import workspace_http_exception_handler
from app.schemas.gateway.service_plugin import PluginBinding
from configs.plugin_parameters import PluginParameters

GATEWAY_ID = "gateway_" + "a" * 32
REQUEST_ID = "plugin-parameter-delivery"
_SCHEMA = {
    "type": "object",
    "required": ["credential"],
    "properties": {"credential": {"type": "string", "writeOnly": True}},
}


@pytest.fixture
def delivery_api(
    empty_workspace_scope: tuple[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, PluginParameters, str]]:
    workspace_id, workspace_root = empty_workspace_scope
    parameters: PluginParameters = {
        "probe.tools": {
            "probe": {"credential": {
                "kind": "plugin_private_file",
                "gateway_id": GATEWAY_ID,
                "workspace_id": workspace_id,
                "package_id": "probe.tools",
                "plugin_id": "probe",
                "parameter_pointer": "/credential",
                "version": "0" * 32,
            }},
        },
    }
    runtime = SimpleNamespace(
        revision="revision-1",
        to_dict=lambda: {"plugins": {"configuration": {"values": parameters}}},
    )
    service = SimpleNamespace(
        publication_lock=RLock(),
        get_snapshot=lambda: runtime,
        get_workspace_plugin_binding_snapshot=lambda: ([
            PluginBinding(package_id="probe.tools", plugin_id="probe", package_version="1.0.0",
                          workspace_id=workspace_id, enabled=True),
        ], []),
    )
    container = SimpleNamespace(
        config_service=service,
        workspace_root=workspace_root,
        workspace_id=workspace_id,
    )
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", "runtime-control-token")
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.exception_handler(HTTPException)(workspace_http_exception_handler)
    application.include_router(router)
    application.dependency_overrides[get_workspace_container] = lambda: container
    application.dependency_overrides[verify_local_control_request] = lambda: None
    with TestClient(application, raise_server_exceptions=False, headers={
        "X-Request-ID": REQUEST_ID,
        "X-BoxTeam-Gateway-Id": GATEWAY_ID,
        "X-BoxTeam-Workspace-Id": workspace_id,
        "X-Local-Token": "runtime-control-token",
    }) as client:
        try:
            yield client, parameters, workspace_id
        finally:
            application.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("reference_field", "reference_value", "detail", "secret"),
    [
        (None, None, "插件私有输入版本不存在", None),
        ("gateway_id", "gateway_" + "b" * 32, "插件私有引用不属于当前目标", None),
        ("version", "private-delivery-secret-sentinel", "插件私有引用结构无效",
         "private-delivery-secret-sentinel"),
    ],
)
def test_delivery_resolve_error_is_conflict_without_secret_echo(
    delivery_api: tuple[TestClient, PluginParameters, str], reference_field: str | None,
    reference_value: str | None, detail: str, secret: str | None,
) -> None:
    client, parameters, workspace_id = delivery_api
    reference: dict[str, JsonValue] = {
        "kind": "plugin_private_file",
        "gateway_id": GATEWAY_ID,
        "workspace_id": workspace_id,
        "package_id": "probe.tools",
        "plugin_id": "probe",
        "parameter_pointer": "/credential",
        "version": "0" * 32,
    }
    if reference_field is not None:
        reference[reference_field] = reference_value
    parameters["probe.tools"]["probe"]["credential"] = reference
    response = client.post("/api/internal/service-plugins/parameters", json={
        "revision": "revision-1",
        "binding": {
            "package_id": "probe.tools",
            "plugin_id": "probe",
            "package_version": "1.0.0",
            "workspace_id": workspace_id,
            "enabled": True,
        },
        "configuration_schema": _SCHEMA,
    })

    assert response.status_code == 409
    assert detail in response.json()["detail"]
    if secret is not None:
        assert secret not in response.text
    assert response.json()["request_id"] == REQUEST_ID
    assert response.headers["X-Request-ID"] == REQUEST_ID
