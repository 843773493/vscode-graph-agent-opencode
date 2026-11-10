"""真实双 Gateway Relay 插件安装、schema 读取、原实例调用与撤权。"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import threading
import time
import zipfile
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import commentjson
import httpx
import psutil

from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from app.schemas.gateway.config import GatewayConfigSourceDocumentDTO
from app.schemas.gateway.plugin_installation import (
    PluginInstallOperationDTO,
    PluginInventoryResultDTO,
    PluginSourceArtifact,
    PluginSourceCatalogDTO,
)
from app.schemas.gateway.service_plugin import (
    PluginConfigurationSchemasDTO,
    PluginRuntimeStatusDTO,
)
from tests.integration.gateway.test_service_plugin_streams import PLUGIN
from tests.integration.gateway.test_target_gateway_routing import (
    TargetGatewayNetwork,
    _observations,
)
from tests.support.plugin_package import native_plugin_target

pytest_plugins = ["tests.integration.gateway.test_target_gateway_routing"]


def _plugin_archive(version: str) -> bytes:
    manifest = {
        "manifest_version": 1,
        "package_id": "relay.management.probe",
        "version": version,
        "name": "Relay management probe",
        "target": native_plugin_target("python"),
        "extension_contract_version": 1,
        "plugins": [{
            "id": "streams",
            "name": "Relay stream probe",
            "instance_scope": "gateway",
            "runtime": "python",
            "entrypoint": ["server.py"],
            "management_protocol_version": 1,
            "requires": [
                {"extension_point": "services", "version": 1},
                {"extension_point": "configuration", "version": 1},
            ],
            "contributes": {
                "services": [
                    {"service_id": "events", "service_type": "terminal", "protocol": "http"},
                ],
                "configuration": {
                    "type": "object",
                    "properties": {"marker": {"type": "string"}},
                },
            },
        }],
    }
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("server.py", PLUGIN)
    return content.getvalue()


def test_plugin_management_and_call_relay_to_original_gateway(
    target_gateway_network: TargetGatewayNetwork,
) -> None:
    network = target_gateway_network
    client_a, client_b = network.clients
    gateway_a, gateway_b = network.gateway_ids
    topology = json.loads((network.context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    connection = client_a.post("/api/gateway/remote-gateways", json={
        "name": "插件管理目标 B", "host": "127.0.0.1", "port": 22,
        "username": "plugin-management-relay", "private_key_path": topology["private_key"],
        "remote_gateway_port": topology["ports"][3],
    }).raise_for_status().json()["data"]
    projected, = [item for item in connection["items"] if item["gateway_id"] == gateway_b]
    connection_id = projected["remote_gateways"][0]["gateway_connection_id"]
    headers = {
        TARGET_GATEWAY_ID_HEADER: gateway_b,
        "X-BoxTeam-Gateway-Connection-Id": connection_id,
    }

    source_server = _source_server({version: _plugin_archive(version) for version in ("1.0.0", "1.1.0")})
    source_thread = threading.Thread(target=source_server.serve_forever, daemon=True)
    source_thread.start()
    evidence: list[dict[str, object]] = []

    def exchange(client: httpx.Client, path: str, body: Mapping[str, object], request_id: str,
                 *, remote: bool = True, expected_status: int = 200) -> dict[str, object]:
        response = client.post(path, json=body, headers={
            **(headers if remote else {}), "X-Request-ID": request_id,
        })
        evidence.append({"request_id": request_id, "path": path,
                         "status": response.status_code, "body": response.json()})
        assert response.status_code == expected_status, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        return response.json().get("data", {})

    def edit_gateway_b(update: Callable[[dict[str, object]], None], *, remote: bool = False) -> GatewayConfigSourceDocumentDTO:
        path = "/api/gateway/config/sources/gateway_mutable_override"
        client, routing = (client_a, headers) if remote else (client_b, {})
        read_id = f"relay-source-read-{len(evidence)}"
        response = client.get(path, headers={**routing, "X-Request-ID": read_id})
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == read_id
        source = response.raise_for_status().json()["data"]
        assert source["gateway_id"] == gateway_b
        document = commentjson.loads(source["jsonc"])
        update(document)
        write_id = f"relay-source-write-{len(evidence)}"
        response = client.put(path, headers={**routing, "X-Request-ID": write_id}, json={
            "gateway_id": gateway_b,
            "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(document, ensure_ascii=False),
            "base_file_digest": source["file_digest"],
            "base_layer_revision": source["layer_revision"],
            "base_layer_digest": source["layer_digest"],
            "expected_active_revision": source["active_revision"],
            "expected_active_digest": source["active_digest"],
            "idempotency_key": f"relay-management-{len(evidence)}-{time.monotonic_ns()}",
        })
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == write_id
        evidence.append({"request_id": write_id, "path": path, "remote": remote,
                         "status": response.status_code, "body": response.json()})
        result = GatewayConfigSourceDocumentDTO.model_validate(response.raise_for_status().json()["data"])
        assert result.edit is not None and result.edit.state == "active", result
        return result

    def install(artifact: PluginSourceArtifact, catalog_revision: str) -> None:
        operation_id = f"relay-plugin-install-{artifact.package_version}"
        accepted = PluginInstallOperationDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/install", {
            "gateway_id": gateway_b, "source_id": "relay-proof", "catalog_revision": catalog_revision,
            "artifact": artifact.model_dump(mode="json"), "operation_id": operation_id,
        }, f"relay-install-{artifact.package_version}"))
        assert accepted.status in {"running", "pending"}
        deadline = time.monotonic() + 25
        while True:
            installed = PluginInstallOperationDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/install-operation", {
                "gateway_id": gateway_b, "operation_id": operation_id,
            }, f"relay-install-poll-{len(evidence)}"))
            assert installed.gateway_id == gateway_b and installed.operation_id == operation_id
            if installed.status not in {"running", "pending"}:
                break
            assert time.monotonic() < deadline, installed
            time.sleep(0.05)
        assert installed.status == "succeeded", installed

    def publish_policy(capabilities: list[str] | None) -> None:
        principal = principal_ref_for_gateway(gateway_a)
        rules = [] if capabilities is None else [
            {"capability": capability, "effect": "allow", "principal_ref": principal}
            for capability in capabilities
        ]

        def update(document: dict[str, object]) -> None:
            permissions = document.setdefault("permissions", {})
            assert isinstance(permissions, dict)
            permissions["federation"] = {"default_effect": "deny", "rules": rules}

        before = client_b.get("/api/gateway/config/reload-status").raise_for_status().json()["data"]
        previous_revision = before["revision"]
        edit = edit_gateway_b(update)
        assert edit.edit is not None and edit.edit.state == "active", edit
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = client_b.get("/api/gateway/config/reload-status").raise_for_status().json()["data"]
            if current["revision"] != previous_revision and current["healthy"] is True:
                return
            time.sleep(0.1)
        raise AssertionError("Gateway B watcher 未发布 federation policy")

    try:
        target = {"gateway_id": gateway_b}
        source_path = "/api/gateway/config/sources/gateway_mutable_override"
        initial = client_b.get(source_path).raise_for_status().json()["data"]
        initial_document = commentjson.loads(initial["jsonc"])
        initial_document.setdefault("plugins", {})["sources"] = {"entries": [{
            "source_id": "relay-proof", "protocol_version": 1,
            "base_url": f"http://127.0.0.1:{source_server.server_port}", "enabled": True,
        }]}
        source_edit = client_b.put(source_path, json={
            "gateway_id": gateway_b, "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(initial_document, ensure_ascii=False),
            "base_file_digest": initial["file_digest"],
            "base_layer_revision": initial["layer_revision"],
            "base_layer_digest": initial["layer_digest"],
            "expected_active_revision": initial["active_revision"],
            "expected_active_digest": initial["active_digest"],
            "idempotency_key": "relay-management-source",
        }).raise_for_status().json()["data"]
        assert source_edit["edit"]["state"] == "active", source_edit

        # 安装资格默认拒绝；B 不应收到未经授权的 Relay 请求。
        publish_policy(None)
        exchange(client_a, "/api/gateway/service-plugins/inventory", target,
                 "relay-inventory-denied", expected_status=403)
        denied, = _observations(network.observation_paths["gateway-b"], "relay-inventory-denied")
        assert denied["status"] == 403

        publish_policy(["service_plugin_installation_manage", "configuration_read",
                        "service_plugin_runtime_manage", "configuration_write", "send", "read"])
        source = PluginSourceCatalogDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/source-catalog",
            {**target, "source_id": "relay-proof"}, "relay-source-catalog"))
        assert source.source_id == "relay-proof" and source.gateway_id == gateway_b

        inventory_a = PluginInventoryResultDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/inventory",
                               {"gateway_id": gateway_a}, "relay-inventory-a", remote=False))
        inventory_b = PluginInventoryResultDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/inventory", target,
                               "relay-inventory-b"))
        assert all(package.manifest.package_id != "relay.management.probe" for package in inventory_a.packages)
        assert all(package.manifest.package_id != "relay.management.probe" for package in inventory_b.packages)

        artifacts = {item.package_version: item for item in source.items}
        install(artifacts["1.0.0"], source.catalog_revision)
        inventory_b = PluginInventoryResultDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/inventory", target,
                               "relay-inventory-b-installed"))
        inventory_a = PluginInventoryResultDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/inventory",
                               {"gateway_id": gateway_a}, "relay-inventory-a-still-empty", remote=False))
        assert len([package for package in inventory_b.packages if package.manifest.package_id == "relay.management.probe"]) == 1
        assert all(package.manifest.package_id != "relay.management.probe" for package in inventory_a.packages)

        schemas = PluginConfigurationSchemasDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/configuration-schemas",
                           target, "relay-configuration-schema"))
        assert schemas.gateway_id == gateway_b and schemas.workspace_id is None
        schema, = [plugin for plugin in schemas.plugins if plugin.package_id == "relay.management.probe"]
        properties = schema.configuration_schema["properties"]
        assert isinstance(properties, dict)
        marker = properties["marker"]
        assert isinstance(marker, dict) and marker["type"] == "string"

        def bind(document: dict[str, object], version: str = "1.0.0", *, enabled: bool = True) -> None:
            plugins = document.setdefault("plugins", {})
            assert isinstance(plugins, dict)
            plugins["runtime"] = {"entries": [{
                "package_id": "relay.management.probe", "plugin_id": "streams",
                "package_version": version, "enabled": enabled,
            }]}

        edit_gateway_b(bind, remote=True)
        status = PluginRuntimeStatusDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/status", target,
                          "relay-status-ready"))
        slot, = [plugin for plugin in status.plugins if plugin.binding.package_id == "relay.management.probe"]
        assert slot.state == "ready" and slot.instance is not None
        instance_id, original_pid = slot.instance.instance_id, slot.instance.process_id

        service = f"/api/gateway/service-plugins/relay.management.probe/streams/instances/{instance_id}/services/events/compressed?package_version=1.0.0"
        payload = "relay call reaches Gateway B native instance 🙂".encode()
        response = client_a.post(service, headers={
            **headers, "X-Request-ID": "relay-plugin-instance-call",
            "Content-Encoding": "gzip",
        }, content=gzip.compress(payload))
        assert response.status_code == 200, response.text
        assert response.content == payload
        assert response.headers["X-Request-ID"] == "relay-plugin-instance-call"
        native = client_b.post(service, headers={"X-Request-ID": "native-plugin-instance-call", "Content-Encoding": "gzip"},
                               content=gzip.compress(payload))
        assert native.status_code == 200 and native.content == payload
        assert native.headers["X-Request-ID"] == "native-plugin-instance-call"

        publish_policy(["service_plugin_installation_manage", "configuration_read",
                        "service_plugin_runtime_manage", "configuration_write"])
        revoked = client_a.post(service, headers={
            **headers, "X-Request-ID": "relay-plugin-call-revoked",
        }, content=gzip.compress(payload))
        assert revoked.status_code == 403, revoked.text
        assert revoked.headers["X-Request-ID"] == revoked.json()["request_id"] == "relay-plugin-call-revoked"
        assert "send" in revoked.text
        native_status = PluginRuntimeStatusDTO.model_validate(client_b.post(
            "/api/gateway/service-plugins/status", json=target).raise_for_status().json()["data"])
        retained, = [plugin for plugin in native_status.plugins if plugin.binding.package_id == "relay.management.probe"]
        assert retained.state == "ready" and retained.instance is not None
        assert retained.instance.instance_id == instance_id
        inventory_a = PluginInventoryResultDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/inventory",
                       {"gateway_id": gateway_a}, "relay-inventory-a-after-revoke", remote=False))
        assert all(package.manifest.package_id != "relay.management.probe" for package in inventory_a.packages)

        publish_policy(["service_plugin_installation_manage", "configuration_read",
                        "service_plugin_runtime_manage", "configuration_write", "send", "read"])
        install(artifacts["1.1.0"], source.catalog_revision)
        after_install = PluginRuntimeStatusDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/status", target, "relay-update-keeps-instance"))
        current, = [item for item in after_install.plugins if item.binding.package_id == "relay.management.probe"]
        assert current.binding.package_version == "1.0.0" and current.instance is not None
        assert current.instance.instance_id == instance_id

        edit_gateway_b(lambda document: bind(document, "1.1.0"), remote=True)
        switched = PluginRuntimeStatusDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/status", target, "relay-explicit-version-switch"))
        replacement, = [item for item in switched.plugins if item.binding.package_id == "relay.management.probe"]
        assert replacement.state == "ready" and replacement.binding.package_version == "1.1.0" and replacement.instance is not None
        replacement_id, replacement_pid = replacement.instance.instance_id, replacement.instance.process_id
        assert replacement_id != instance_id and not psutil.pid_exists(original_pid)
        old = client_a.post(service, headers={**headers, "X-Request-ID": "relay-old-instance-rejected"},
                            content=gzip.compress(payload))
        assert old.status_code == 409, old.text
        assert old.headers["X-Request-ID"] == old.json()["request_id"] == "relay-old-instance-rejected"
        new_service = service.replace(instance_id, replacement_id).replace("package_version=1.0.0", "package_version=1.1.0")
        called = client_a.post(new_service, headers={**headers, "X-Request-ID": "relay-new-instance-call", "Content-Encoding": "gzip"},
                               content=gzip.compress(payload))
        assert called.status_code == 200 and called.content == payload
        assert called.headers["X-Request-ID"] == "relay-new-instance-call"
        exchange(client_a, "/api/gateway/service-plugins/uninstall", {
            **target, "package_id": "relay.management.probe", "package_version": "1.0.0",
        }, "relay-uninstall-retired-version")

        edit_gateway_b(lambda document: bind(document, "1.1.0", enabled=False), remote=True)
        stopped = PluginRuntimeStatusDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/status", target, "relay-disabled-binding"))
        closed, = [item for item in stopped.plugins if item.binding.package_id == "relay.management.probe"]
        assert closed.state == "stopped" and closed.instance is None and not closed.binding.enabled
        assert not psutil.pid_exists(replacement_pid)
        exchange(client_a, "/api/gateway/service-plugins/uninstall", {
            **target, "package_id": "relay.management.probe", "package_version": "1.1.0",
        }, "relay-disabled-still-referenced", expected_status=409)
        assert "configuration:streams" in json.dumps(evidence[-1]["body"])

        def unbind(document: dict[str, object]) -> None:
            plugins = document["plugins"]
            assert isinstance(plugins, dict)
            plugins["runtime"] = {"entries": []}

        edit_gateway_b(unbind, remote=True)
        exchange(client_a, "/api/gateway/service-plugins/uninstall", {
            **target, "package_id": "relay.management.probe", "package_version": "1.1.0",
        }, "relay-uninstall-unbound-version")
        for index, client in enumerate((client_a, client_b)):
            inventory = PluginInventoryResultDTO.model_validate(exchange(client, "/api/gateway/service-plugins/inventory", {"gateway_id": (gateway_a, gateway_b)[index]},
                                 f"native-final-inventory-{index}", remote=False))
            assert all(item.manifest.package_id != "relay.management.probe" for item in inventory.packages)
            assert not (network.gateway_homes[index] / "plugins/packages/relay.management.probe").exists()
        (network.context.artifacts_dir / "plugin-management-relay.json").write_text(
            json.dumps({"gateway_ids": [gateway_a, gateway_b], "instance_id": instance_id,
                        "replacement_id": replacement_id, "exited_pids": [original_pid, replacement_pid],
                        "revoked_status": revoked.status_code, "evidence": evidence},
                       ensure_ascii=False, indent=2), encoding="utf-8",
        )
    finally:
        source_server.shutdown()
        source_server.server_close()
        source_thread.join(timeout=5)


def _source_server(contents: dict[str, bytes]) -> ThreadingHTTPServer:
    class SourceHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/catalog":
                body = json.dumps({
                    "protocol_version": 1, "source_id": "relay-proof",
                    "catalog_revision": "catalog-relay-v1", "items": [{
                        "package_id": "relay.management.probe", "package_version": version,
                        "target": native_plugin_target("python"),
                        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                        "url": f"http://127.0.0.1:{server.server_port}/plugin-{version}.zip",
                        "filename": f"relay-plugin-{version}.zip",
                    } for version, content in contents.items()], "next_cursor": None,
                }).encode()
                content_type = "application/json"
            elif path.startswith("/plugin-") and path.removeprefix("/plugin-").removesuffix(".zip") in contents:
                body, content_type = contents[path.removeprefix("/plugin-").removesuffix(".zip")], "application/zip"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
    return server
