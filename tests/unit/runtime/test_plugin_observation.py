"""Python 插件沿原应用 lifespan 与私有路由管理自身采集。"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.runtime_control import (
    verify_local_control_request,
    verify_runtime_control_token,
)
from app.runtime.plugin_observation import plugin_observation_router


@pytest.fixture
def plugin_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", "private-token")
    monkeypatch.setenv("BOXTEAM_RUNTIME_OBSERVATION", json.dumps({
        "instance_id": "original", "revision": "initial",
        "telemetry": {"enabled": False}, "statistics": {"enabled": False},
    }))
    app = FastAPI()
    app.include_router(plugin_observation_router(instance_id="original", service="probe",
        verify_local=verify_local_control_request, verify_token=verify_runtime_control_token))
    return app


def test_authenticated_apply_reports_actual_state_and_releases_on_disable(plugin_app: FastAPI) -> None:
    with TestClient(plugin_app, client=("127.0.0.1", 10000)) as client:
        url, headers = "/internal/plugin/observation", {"X-Local-Token": "private-token"}
        assert client.get(url).status_code == 401
        assert client.get(url, headers={**headers, "Origin": "http://localhost"}).status_code == 403
        initial = client.get(url, headers=headers).raise_for_status().json()
        assert initial["applied_revision"] == "initial"
        assert initial["requests"] is None and initial["samples"] == [] and initial["processes"] == []
        enabled = {"instance_id": "original", "revision": "enabled",
            "telemetry": {"enabled": True}, "statistics": {"enabled": True, "collection_interval_ms": 1000}}
        assert client.post(url, headers=headers, json={**enabled, "instance_id": "retired"}).status_code == 409
        assert client.get(url, headers=headers).json() == initial
        assert client.post(url, headers=headers, json={**enabled, "statistics": {"collection_interval_ms": 1}}).status_code == 422
        actual = client.post(url, headers=headers, json=enabled).raise_for_status().json()
        assert actual["applied_revision"] == "enabled" and actual["instance_id"] == "original"
        assert actual["processes"][0]["rss_bytes"] > 0
        assert actual["requests"] is None and actual["samples"] == []
        assert "http_requests" in actual["unsupported_metrics"]
        timer = plugin_app.state.runtime_observation._task
        assert timer is not None and not timer.done()
        disabled = client.post(url, headers=headers, json={"instance_id": "original", "revision": "disabled",
            "telemetry": {"enabled": False}, "statistics": {"enabled": False}}).raise_for_status().json()
        assert disabled["processes"] == [] and disabled["samples"] == [] and disabled["requests"] is None
        assert timer.done() and plugin_app.state.runtime_observation._task is None
    assert plugin_app.state.runtime_observation._task is None
