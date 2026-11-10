"""经真实 Gateway 来源 writer 热切换采集并观察实际代理，不构建工作台。"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from collections.abc import Iterator
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.gateway.test_service_plugins import PYTHON_PLUGIN
from tests.support.plugin_package import install_plugin, native_plugin_target
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)


@pytest.fixture
def observation_gateway(request: pytest.FixtureRequest, integration_workspace_root_path: str
                        ) -> Iterator[tuple[httpx.Client, TestRunContext]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    gateway = start_gateway_process(
        workspace_root=Path(integration_workspace_root_path), default_backend_url=None,
        port=integration_port_block_for_file(Path(request.node.path)).port(0),
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir,
        extra_env={name: os.environ.get(name, "unused-observation-test-key") for name in (
            "OPENCODE_ZEN_API_KEY", "KILO_API_KEY", "OPENROUTER_API_KEY", "CCTQ_API_KEY",
        )},
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
                          timeout=20, trust_env=False) as client:
            yield client, context
    finally:
        close_gateway_process(gateway)


def test_gateway_collection_hot_switches_through_original_source_writer(
    observation_gateway: tuple[httpx.Client, TestRunContext],
) -> None:
    client, context = observation_gateway
    exchanges = []
    binding = {"package_id": "observation.probe", "plugin_id": "echo", "package_version": "1.0.0", "enabled": True}

    def request(method: str, path: str, **kwargs) -> httpx.Response:
        request_id = f"observation-{len(exchanges)}"
        response = client.request(method, path, headers={"X-Request-ID": request_id}, **kwargs)
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        exchanges.append({"path": path, "status": response.status_code, "body": response.json()})
        return response

    def exchange(method: str, path: str, **kwargs) -> dict:
        response = request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()["data"]

    def diagnostics() -> dict:
        return exchange("GET", "/api/gateway/diagnostics")["observation"]

    def save(enabled: bool) -> str:
        source_key = "gateway_mutable_override"
        path = f"/api/gateway/config/sources/{source_key}"
        document = exchange("GET", path)
        config = commentjson.loads(document["jsonc"])
        config["telemetry"] = {"runtime": {"enabled": enabled, "sample_ratio": 1, "sample_buffer_limit": 2}}
        config["statistics"] = {"runtime": {"enabled": enabled, "collection_interval_ms": 1000}}
        config.setdefault("plugins", {})["runtime"] = {"entries": [binding]}
        result = exchange("PUT", path, json={
            "gateway_id": document["gateway_id"], "source_key": source_key,
            "jsonc": json.dumps(config), "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": f"observation-save-{len(exchanges)}",
        })
        assert result["edit"]["state"] == "active" and not result["status"]["restart_required"]
        return result["status"]["revision"]

    try:
        gateway_id = exchange("GET", "/api/gateway/health")["gateway_id"]
        package = context.artifacts_dir / "observation-probe.zip"
        manifest = {"manifest_version": 1, "package_id": binding["package_id"], "version": "1.0.0",
                    "name": "采集验收探针", "extension_contract_version": 1, "target": native_plugin_target("python"),
                    "plugins": [{"id": "echo", "name": "echo", "instance_scope": "gateway", "runtime": "python",
                                 "entrypoint": ["main.py"], "management_protocol_version": 1,
                                 "requires": [{"extension_point": "services", "version": 1}],
                                 "contributes": {"services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}]}}]}
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("plugin.jsonc", json.dumps(manifest))
            archive.writestr("main.py", PYTHON_PLUGIN)
            archive.writestr("local_module.py", "VALUE = 1\n")
        install_plugin(
            lambda path, payload: request("POST", path, json=payload),
            gateway_id=gateway_id,
            path=str(package),
            expected_digest=hashlib.sha256(package.read_bytes()).hexdigest(),
        )
        initial = diagnostics()
        assert not initial["telemetry"]["enabled"] and not initial["statistics"]["enabled"]
        assert initial["requests"] is None and initial["samples"] == [] and initial["processes"] == []
        revision = save(True)
        enabled = diagnostics()
        assert enabled["instance_id"] == initial["instance_id"] and enabled["applied_revision"] == revision
        assert enabled["telemetry"]["enabled"] and enabled["statistics"]["enabled"]
        assert any(item["service"] == "gateway" and item["rss_bytes"] > 0 for item in enabled["processes"])
        statuses = exchange("POST", "/api/gateway/service-plugins/status", json={"gateway_id": gateway_id})
        slot, = statuses["plugins"]
        assert slot["state"] == "ready"
        path = (f"/api/gateway/service-plugins/observation.probe/echo/instances/{slot['instance']['instance_id']}"
                "/services/echo/")
        def proxy_get(resource: str) -> httpx.Response:
            return client.get(path + resource, params={"package_version": "1.0.0", "target_gateway_id": gateway_id},
                              headers={"X-Request-ID": "observation-proxy"})

        success = proxy_get("value")
        assert success.status_code == 200 and success.json()["request_id"] == "observation-proxy"
        failed = proxy_get("failure")
        assert failed.status_code == 500
        observed = diagnostics()
        assert observed["requests"]["proxy_count"] == enabled["requests"]["proxy_count"] + 2
        assert observed["requests"]["proxy_errors"] == enabled["requests"]["proxy_errors"] + 1
        assert observed["requests"]["proxy_duration_ms"] > enabled["requests"]["proxy_duration_ms"]
        assert len(observed["samples"]) == 2
        assert observed["unsupported_metrics"] == ["plugin_internal_stages"]
        revision = save(False)
        for _ in range(3):
            assert proxy_get("value").status_code == 200
        disabled = diagnostics()
        assert disabled["instance_id"] == initial["instance_id"] and disabled["applied_revision"] == revision
        assert not disabled["telemetry"]["enabled"] and not disabled["statistics"]["enabled"]
        assert disabled["requests"] is None and disabled["samples"] == [] and disabled["processes"] == []
    finally:
        (context.artifacts_dir / "gateway-observation-http.json").write_text(
            json.dumps(exchanges, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
