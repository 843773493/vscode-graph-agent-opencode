"""真实 Gateway 来源提交、双语言插件及命令验证诊断部分失败与重启继承。"""

from __future__ import annotations

import json
from pathlib import Path

import commentjson
import httpx

from tests.harness.python.run_context import TestRunContext
from tests.integration.gateway.test_plugin_commands import (
    PYTHON,
    _archive,
    plugin_gateway,  # noqa: F401 — 复用真实 Gateway fixture，产物仍按当前测试路径隔离。
)
from tests.support.plugin_package import install_plugin

FAULTY_PYTHON = PYTHON.replace(
    "async def execute(arguments, context):",
    "reject_disable = False\nasync def execute(arguments, context):\n"
    "    global reject_disable\n"
    "    if arguments['value'] == 'refuse-disable': reject_disable = True\n"
    "    if arguments['value'] == 'accept-disable': reject_disable = False",
).replace(
    "app = FastAPI()",
    '''app = FastAPI()
from fastapi.responses import JSONResponse
@app.middleware('http')
async def reject_observation(request, call_next):
    if request.url.path == '/internal/plugin/observation' and request.method == 'POST':
        payload = await request.json()
        if reject_disable and not payload['statistics']['enabled']:
            return JSONResponse({'detail': 'probe refuses disable'}, status_code=503)
    return await call_next(request)''',
)


def test_plugin_actual_diagnostics_partial_failure_and_restart_inheritance(
    plugin_gateway: tuple[httpx.Client, TestRunContext],  # noqa: F811 — pytest 按名称注入导入的 fixture。
    prepared_developer_tools_package: Path,
) -> None:
    client, context = plugin_gateway
    evidence = []

    def request(method: str, path: str, **kwargs) -> httpx.Response:
        request_id = f"plugin-observation-{len(evidence)}"
        response = client.request(method, path, headers={"X-Request-ID": request_id}, **kwargs)
        data = response.json()
        evidence.append({"path": path, "status": response.status_code, "body": data})
        assert response.headers["X-Request-ID"] == data["request_id"] == request_id
        return response

    def exchange(method: str, path: str, **kwargs) -> dict:
        response = request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()["data"]

    gateway_id = exchange("GET", "/api/gateway/health")["gateway_id"]
    archive = context.artifacts_dir / "observation.zip"
    _archive(archive, prepared_developer_tools_package, python_source=FAULTY_PYTHON)
    install_plugin(
        lambda path, payload: request("POST", path, json=payload),
        gateway_id=gateway_id,
        path=str(archive),
    )
    bindings = [{"package_id": "proof.commands", "package_version": "1.0.0", "plugin_id": name, "enabled": True}
                for name in ("python", "node")]

    def save(enabled: bool, limit: int = 2) -> str:
        path = "/api/gateway/config/sources/gateway_mutable_override"
        document = exchange("GET", path)
        config = commentjson.loads(document["jsonc"])
        config.setdefault("plugins", {})["runtime"] = {"entries": bindings}
        config["telemetry"] = {"runtime": {"enabled": enabled, "sample_ratio": 1, "sample_buffer_limit": limit}}
        config["statistics"] = {"runtime": {"enabled": enabled, "collection_interval_ms": 1000}}
        result = exchange("PUT", path, json={"gateway_id": gateway_id, "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(config), "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": f"observation-save-{len(evidence)}"})
        assert result["edit"]["state"] == "active" and not result["status"]["restart_required"]
        return result["status"]["revision"]

    def diagnostics() -> dict[str, dict]:
        data = exchange("GET", "/api/gateway/diagnostics")
        return {item["instance"]["plugin_id"]: item for item in data["plugin_observations"]}

    def call(name: str, value: str) -> None:
        current = diagnostics()[name]["instance"]
        identity = {key: current[key] for key in ("gateway_id", "package_id", "package_version", "plugin_id", "instance_id")}
        registration, = exchange("POST", "/api/gateway/service-plugins/commands/registrations", json=identity)["commands"]
        request_id = f"observation-command-{len(evidence)}"
        response = client.post("/api/gateway/service-plugins/commands/call", headers={"X-Request-ID": request_id}, json={
            **identity, "registration_id": registration["registration_id"], "command_id": "execute",
            "call_id": request_id, "remaining_ms": 5000, "arguments": {"mode": "return", "value": value}})
        response.raise_for_status()
        events = [json.loads(line) for line in response.iter_lines()]
        assert events[-1]["kind"] == "result" and events[-1]["result"]["value"] == value
        assert all(event["request_id"] == request_id and event["instance_id"] == current["instance_id"] for event in events)
        evidence.append({"command": name, "events": events})

    try:
        revision = save(False)
        initial = diagnostics()
        assert set(initial) == {"python", "node"}
        for item in initial.values():
            assert item["error"] is None
            assert item["observation"]["applied_revision"] == revision
            assert item["observation"]["processes"] == [] and item["observation"]["samples"] == []
        revision = save(True)
        enabled = diagnostics()
        for name, item in enabled.items():
            assert item["instance"]["instance_id"] == initial[name]["instance"]["instance_id"]
            assert item["observation"]["applied_revision"] == revision and item["error"] is None
            assert item["observation"]["processes"][0]["pid"] == item["instance"]["process_id"]
            assert item["observation"]["processes"][0]["rss_bytes"] > 0
        call("node", "/native/path/user-body-must-not-be-sampled")
        node_observed = diagnostics()["node"]["observation"]
        assert node_observed["requests"]["count"] > 0 and node_observed["requests"]["duration_ms"] > 0
        assert len(node_observed["samples"]) == 2
        assert "user-body-must-not-be-sampled" not in json.dumps(node_observed)
        assert "http_requests" in enabled["python"]["observation"]["unsupported_metrics"]
        call("python", "refuse-disable")
        failed_revision = save(False)
        partial = diagnostics()
        assert "503" in partial["python"]["error"]
        assert partial["python"]["observation"]["applied_revision"] == revision
        assert partial["python"]["observation"]["statistics"]["enabled"]
        assert partial["node"]["error"] is None
        assert partial["node"]["observation"]["applied_revision"] == failed_revision
        assert partial["node"]["observation"]["requests"] is None and partial["node"]["observation"]["samples"] == []
        call("node", "disabled-but-still-callable")
        call("python", "accept-disable")
        closed_revision = save(False, limit=3)
        closed = diagnostics()
        for item in closed.values():
            assert item["error"] is None and item["observation"]["applied_revision"] == closed_revision
            assert item["observation"]["requests"] is None and item["observation"]["samples"] == []
            assert item["observation"]["processes"] == []
        statuses = exchange("POST", "/api/gateway/service-plugins/status", json={"gateway_id": gateway_id})
        for slot in statuses["plugins"]:
            exchange("POST", "/api/gateway/service-plugins/restart", json={"gateway_id": gateway_id,
                **{key: value for key, value in slot["binding"].items() if key != "enabled"}, "revision": slot["revision"]})
        restarted = diagnostics()
        for name, item in restarted.items():
            assert item["instance"]["instance_id"] != initial[name]["instance"]["instance_id"]
            assert item["observation"]["applied_revision"] == closed_revision
            assert item["observation"]["processes"] == [] and item["observation"]["samples"] == []
            call(name, "after-restart")
    finally:
        (context.artifacts_dir / "plugin-observation-http.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
