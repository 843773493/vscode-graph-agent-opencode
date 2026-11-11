"""参数保留与包版本解耦，真实重绑始终消费所选版本的 schema。"""

from __future__ import annotations

import json
import time
import zipfile

import commentjson
import httpx
import psutil

from tests.integration.gateway.test_plugin_install_operations import (
    source_gateway as source_gateway,  # noqa: PLC0414 - 复用原独立 Gateway，零业务 Backend
)
from tests.integration.gateway.test_service_plugins import PYTHON_PLUGIN
from tests.support.plugin_package import install_plugin, native_plugin_target


def test_reinstalled_version_validates_retained_parameters_before_binding(source_gateway) -> None:
    client, context, _ = source_gateway
    gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    target = {"gateway_id": gateway_id}
    package_id, plugin_id = "proof.parameter-rebinding", "probe"
    source_path = "/api/gateway/config/sources/gateway_mutable_override"
    evidence: list[dict] = []

    def exchange(method: str, path: str, body: dict | None = None) -> httpx.Response:
        request_id = f"parameter-rebinding-{len(evidence)}"
        started = time.monotonic()
        response = client.request(method, path, json=body, headers={"X-Request-ID": request_id})
        assert response.headers["X-Request-ID"] == request_id
        evidence.append({"method": method, "path": path, "request_id": request_id,
            "elapsed_ms": (time.monotonic() - started) * 1000,
            "status": response.status_code, "body": response.json()})
        return response

    def status() -> dict[str, dict]:
        data = exchange("POST", "/api/gateway/service-plugins/status", target).raise_for_status().json()["data"]
        return {item["binding"]["plugin_id"]: item for item in data["plugins"]}

    def save(version: str | None, value: str | int, *, enabled: bool = True, expected: int = 200) -> None:
        document = exchange("GET", source_path).raise_for_status().json()["data"]
        source = commentjson.loads(document["jsonc"])
        source["plugins"] = {"runtime": {"entries": [] if version is None else [{
            "package_id": package_id, "plugin_id": plugin_id, "package_version": version, "enabled": enabled}]},
            "configuration": {"values": {package_id: {plugin_id: {"value": value}}}}}
        response = exchange("PUT", source_path, {**target, "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(source), "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": f"parameter-save-{len(evidence)}"})
        assert response.status_code == expected, response.text
        current = exchange("GET", source_path).raise_for_status().json()["data"]
        if expected == 200:
            assert response.json()["data"]["edit"]["state"] == "active"
            assert current["source_effective"]["plugins"]["configuration"]["values"][package_id][plugin_id]["value"] == value
            if version is None:
                assert "configuration" not in current["effective"]["plugins"]
        else:
            for field in ("jsonc", "file_digest", "active_revision", "active_digest"):
                assert current[field] == document[field], field

    def call(slot: dict) -> httpx.Response:
        return exchange("GET", f"/api/gateway/service-plugins/{package_id}/{plugin_id}/instances/"
            f"{slot['instance']['instance_id']}/services/echo/value?package_version={slot['binding']['package_version']}")

    def uninstall(version: str, expected: int = 200) -> None:
        response = exchange("POST", "/api/gateway/service-plugins/uninstall", {**target,
            "package_id": package_id, "package_version": version})
        assert response.status_code == expected, response.text

    def install(version: str, value_type: str) -> None:
        archive_path = context.artifacts_dir / f"parameter-{version}.zip"
        manifest = {"package_id": package_id, "version": version, "name": "参数重绑定探针",
            "target": native_plugin_target("python"), "plugins": [{"id": plugin_id, "name": "参数探针",
                "instance_scope": "gateway", "runtime": "python", "entrypoint": ["entry.py"],
                "management_protocol_version": 1,
                "requires": [{"extension_point": "services", "version": 1}, {"extension_point": "configuration", "version": 1}],
                "contributes": {"services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}],
                    "configuration": {"type": "object", "required": ["value"], "additionalProperties": False,
                        "properties": {"value": {"type": value_type}}}}}]}
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("plugin.jsonc", json.dumps(manifest))
            archive.writestr("entry.py", PYTHON_PLUGIN.replace(
                "'pid': os.getpid(), 'instance_id': identity['instance_id'],",
                "'parameters': json.loads(os.environ['BOXTEAM_PLUGIN_PARAMETERS']), 'pid': os.getpid(), 'instance_id': identity['instance_id'],"))
            archive.writestr("local_module.py", "VALUE = 1\n")
        install_plugin(lambda path, payload: exchange("POST", path, payload),
            gateway_id=gateway_id, path=str(archive_path))

    try:
        install("1.0.0", "string")
        save("1.0.0", "retained")
        first = status()[plugin_id]
        assert call(first).raise_for_status().json()["parameters"] == {"value": "retained"}
        install("2.0.0", "integer")
        assert status()[plugin_id] == first, "安装新版不能改旧绑定或实例"
        save("2.0.0", "retained", expected=422)
        assert status()[plugin_id] == first
        call(first).raise_for_status()
        save(None, "retained")
        assert status() == {} and not psutil.pid_exists(first["instance"]["process_id"])
        uninstall("1.0.0")
        save("2.0.0", "retained", enabled=False, expected=422)
        assert status() == {}, "关闭态重绑也不能绕过新版 schema"
        save(None, 7)
        assert status() == {}, "未绑定参数编辑不能启动插件"
        save("2.0.0", 7, enabled=False)
        stopped = status()[plugin_id]
        assert stopped["state"] == "stopped" and stopped["instance"] is None
        uninstall("2.0.0", expected=409)
        save("2.0.0", 7)
        current = status()[plugin_id]
        assert call(current).raise_for_status().json()["parameters"] == {"value": 7}
        restarted = exchange("POST", "/api/gateway/service-plugins/restart", {**target,
            "package_id": package_id, "plugin_id": plugin_id, "package_version": "2.0.0",
            "revision": current["revision"]}).raise_for_status().json()["data"]
        assert restarted["instance"]["instance_id"] != current["instance"]["instance_id"]
        assert not psutil.pid_exists(current["instance"]["process_id"])
        assert call(restarted).raise_for_status().json()["parameters"] == {"value": 7}
        assert call(current).status_code == 409
        save(None, 7)
        assert not psutil.pid_exists(restarted["instance"]["process_id"])
        uninstall("2.0.0")
        assert status() == {}
        save(None, "editable-without-package")
    finally:
        (context.artifacts_dir / "parameter-rebinding.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
