"""开发目录沿同一安装链发布不可变版本，再显式切换原运行绑定。"""

from __future__ import annotations

import json

import commentjson
import httpx
import psutil

from app.schemas.gateway.service_plugin import PluginSlotStatusDTO
from tests.integration.gateway.test_plugin_install_operations import (
    source_gateway as source_gateway,  # noqa: PLC0414 - 复用隔离 Gateway，不启动无关 Backend。
)
from tests.integration.gateway.test_service_plugins import NODE_PLUGIN
from tests.support.plugin_package import (
    expect_plugin_install_failure,
    install_plugin,
    native_plugin_target,
)


def test_development_directory_update_requires_immutable_publication_and_explicit_binding(source_gateway) -> None:
    client, context, _ = source_gateway
    evidence: list[dict[str, object]] = []
    gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    target = {"gateway_id": gateway_id}
    source = context.artifacts_dir / "development-source"
    entry = source / "arbitrary/engine/main.mjs"
    entry.parent.mkdir(parents=True)
    entry.write_text("import { marker } from './value.mjs';\n" + NODE_PLUGIN.replace(
        "version:identity.package_version,", "marker,version:identity.package_version,"), encoding="utf-8")
    value = entry.with_name("value.mjs")
    value.write_text("export const marker = 'original';\n", encoding="utf-8")
    manifest = {"manifest_version": 1, "package_id": "development.probe", "version": "1.0.0",
        "name": "开发目录更新探针", "extension_contract_version": 1, "target": native_plugin_target("node"),
        "plugins": [{"id": "node", "name": "目录运行探针", "instance_scope": "gateway", "runtime": "node",
            "entrypoint": ["arbitrary/engine/main.mjs"], "management_protocol_version": 1,
            "requires": [{"extension_point": "services", "version": 1}],
            "contributes": {"services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}]}}]}
    manifest_path = source / "plugin.jsonc"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def post(path: str, body: dict[str, object]) -> httpx.Response:
        request_id = f"development-plugin-{len(evidence)}"
        response = client.post(path, json=body, headers={"X-Request-ID": request_id})
        evidence.append({"path": path, "status": response.status_code, "body": response.json()})
        assert response.headers["X-Request-ID"] == request_id
        return response

    def status() -> PluginSlotStatusDTO:
        items = post("/api/gateway/service-plugins/status", target).raise_for_status().json()["data"]["plugins"]
        item, = [item for item in items if item["binding"]["package_id"] == "development.probe"]
        return PluginSlotStatusDTO.model_validate(item)

    def bind(version: str | None, *, enabled: bool = True) -> None:
        path = "/api/gateway/config/sources/gateway_mutable_override"
        document = client.get(path).raise_for_status().json()["data"]
        configuration = commentjson.loads(document["jsonc"])
        configuration.setdefault("plugins", {})["runtime"] = {"entries": [] if version is None else [{
            "package_id": "development.probe", "package_version": version, "plugin_id": "node", "enabled": enabled}]}
        response = client.put(path, json={**target, "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(configuration), "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": f"development-binding-{len(evidence)}"})
        evidence.append({"path": path, "status": response.status_code, "body": response.json()})
        assert response.raise_for_status().json()["data"]["edit"]["state"] == "active"

    def call(slot: PluginSlotStatusDTO) -> httpx.Response:
        assert slot.instance is not None
        return client.get(f"/api/gateway/service-plugins/development.probe/node/instances/"
            f"{slot.instance.instance_id}/services/echo/value", params={"package_version": slot.binding.package_version})

    try:
        original = install_plugin(post, gateway_id=gateway_id, path=str(source), development_directory=True)
        assert original.artifact.source_kind == "development_directory"
        assert post("/api/gateway/service-plugins/status", target).json()["data"]["configured_bindings"] == []
        bind("1.0.0")
        first = status()
        assert first.state == "ready" and first.instance is not None
        assert call(first).raise_for_status().json()["marker"] == "original"

        value.write_text("export const marker = 'edited';\n", encoding="utf-8")
        assert status().instance == first.instance
        restarted = PluginSlotStatusDTO.model_validate(post("/api/gateway/service-plugins/restart", {
            **target, "package_id": "development.probe", "package_version": "1.0.0", "plugin_id": "node",
            "revision": first.revision}).raise_for_status().json()["data"])
        assert restarted.instance is not None and restarted.instance != first.instance
        assert not psutil.pid_exists(first.instance.process_id)
        assert call(restarted).raise_for_status().json()["marker"] == "original", "重启只消费原已发布快照"
        conflict = expect_plugin_install_failure(post, gateway_id=gateway_id, path=str(source), development_directory=True)
        assert conflict.error and "不可变" in conflict.error
        assert status() == restarted, "同版本内容冲突不能触碰原绑定或实例"

        manifest["version"] = "2.0.0"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        updated = install_plugin(post, gateway_id=gateway_id, path=str(source), development_directory=True)
        assert updated.artifact.package_version == "2.0.0" and updated.artifact.digest != original.artifact.digest
        assert status() == restarted, "显式发布开发目录也不能自动切换运行版本"
        bind("2.0.0")
        current = status()
        assert current.state == "ready" and current.instance is not None
        result = call(current).raise_for_status().json()
        assert result["version"] == "2.0.0" and result["marker"] == "edited"
        assert not psutil.pid_exists(restarted.instance.process_id)
        assert call(restarted).status_code == 409

        bind("2.0.0", enabled=False)
        assert status().state == "stopped" and not psutil.pid_exists(current.instance.process_id)
        blocked = post("/api/gateway/service-plugins/uninstall", {**target,
            "package_id": "development.probe", "package_version": "2.0.0"})
        assert blocked.status_code == 409 and "引用" in blocked.text
        bind(None)
        for version in ("1.0.0", "2.0.0"):
            post("/api/gateway/service-plugins/uninstall", {**target,
                "package_id": "development.probe", "package_version": version}).raise_for_status()
        assert post("/api/gateway/service-plugins/inventory", target).json()["data"]["packages"] == []
        assert value.read_text(encoding="utf-8") == "export const marker = 'edited';\n", "卸载不删除开发原件"
    finally:
        (context.artifacts_dir / "development-flow.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
