"""真实双 Gateway Relay 插件安装、schema 读取、原实例调用与撤权。"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import threading
import time
import zipfile
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import commentjson
import httpx

from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from tests.integration.gateway.test_service_plugin_streams import PLUGIN
from tests.integration.gateway.test_target_gateway_routing import (
    TargetGatewayNetwork,
    _observations,
)
from tests.support.plugin_package import native_plugin_target

pytest_plugins = ["tests.integration.gateway.test_target_gateway_routing"]


def _plugin_archive() -> bytes:
    manifest = {
        "manifest_version": 1,
        "package_id": "relay.management.probe",
        "version": "1.0.0",
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

    content = _plugin_archive()
    source_server = _source_server(content)
    source_thread = threading.Thread(target=source_server.serve_forever, daemon=True)
    source_thread.start()
    evidence: list[dict[str, object]] = []

    def exchange(client: httpx.Client, path: str, body: dict[str, object], request_id: str,
                 *, remote: bool = True, expected_status: int = 200) -> dict[str, object]:
        response = client.post(path, json=body, headers={
            **(headers if remote else {}), "X-Request-ID": request_id,
        })
        evidence.append({"request_id": request_id, "path": path,
                         "status": response.status_code, "body": response.json()})
        assert response.status_code == expected_status, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        return response.json().get("data", {})

    def edit_gateway_b(update: Callable[[dict[str, object]], None]) -> dict[str, object]:
        path = "/api/gateway/config/sources/gateway_mutable_override"
        source = client_b.get(path).raise_for_status().json()["data"]
        document = commentjson.loads(source["jsonc"])
        update(document)
        return client_b.put(path, json={
            "gateway_id": gateway_b,
            "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(document, ensure_ascii=False),
            "base_file_digest": source["file_digest"],
            "base_layer_revision": source["layer_revision"],
            "base_layer_digest": source["layer_digest"],
            "expected_active_revision": source["active_revision"],
            "expected_active_digest": source["active_digest"],
            "idempotency_key": f"relay-management-{len(evidence)}-{time.monotonic_ns()}",
        }).raise_for_status().json()["data"]

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
        assert edit["edit"]["state"] == "active", edit
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
        source = exchange(client_a, "/api/gateway/service-plugins/source-catalog",
            {**target, "source_id": "relay-proof"}, "relay-source-catalog")
        assert source["source_id"] == "relay-proof"

        inventory_a = exchange(client_a, "/api/gateway/service-plugins/inventory",
                               {"gateway_id": gateway_a}, "relay-inventory-a", remote=False)
        inventory_b = exchange(client_a, "/api/gateway/service-plugins/inventory", target,
                               "relay-inventory-b")
        assert all(package["manifest"]["package_id"] != "relay.management.probe"
                   for package in inventory_a["packages"])
        assert all(package["manifest"]["package_id"] != "relay.management.probe"
                   for package in inventory_b["packages"])

        package_artifact = source["items"][0]
        install = exchange(client_a, "/api/gateway/service-plugins/install", {
            **target, "source_id": "relay-proof", "catalog_revision": source["catalog_revision"],
            "artifact": package_artifact, "operation_id": "relay-plugin-install-1",
        }, "relay-install")
        assert install["status"] in {"running", "pending"}
        deadline = time.monotonic() + 25
        while True:
            installed = exchange(client_a, "/api/gateway/service-plugins/install-operation", {
                **target, "operation_id": "relay-plugin-install-1",
            }, f"relay-install-poll-{len(evidence)}")
            if installed["status"] not in {"running", "pending"}:
                break
            assert time.monotonic() < deadline, installed
            time.sleep(0.05)
        assert installed["status"] == "succeeded", installed
        inventory_b = exchange(client_a, "/api/gateway/service-plugins/inventory", target,
                               "relay-inventory-b-installed")
        inventory_a = exchange(client_a, "/api/gateway/service-plugins/inventory",
                               {"gateway_id": gateway_a}, "relay-inventory-a-still-empty", remote=False)
        assert len([package for package in inventory_b["packages"]
                    if package["manifest"]["package_id"] == "relay.management.probe"]) == 1
        assert all(package["manifest"]["package_id"] != "relay.management.probe"
                   for package in inventory_a["packages"])

        schemas = exchange(client_a, "/api/gateway/service-plugins/configuration-schemas",
                           target, "relay-configuration-schema")
        assert schemas["gateway_id"] == gateway_b and schemas["workspace_id"] is None
        assert any(plugin["package_id"] == "relay.management.probe"
                   and plugin["configuration_schema"]["properties"]["marker"]["type"] == "string"
                   for plugin in schemas["plugins"])

        def enable(document: dict[str, object]) -> None:
            plugins = document.setdefault("plugins", {})
            assert isinstance(plugins, dict)
            plugins["runtime"] = {"entries": [{
                "package_id": "relay.management.probe", "plugin_id": "streams",
                "package_version": "1.0.0", "enabled": True,
            }]}

        edit_gateway_b(enable)
        status = exchange(client_a, "/api/gateway/service-plugins/status", target,
                          "relay-status-ready")
        slot, = [plugin for plugin in status["plugins"]
                 if plugin["binding"]["package_id"] == "relay.management.probe"]
        assert slot["state"] == "ready" and slot["instance"] is not None
        instance_id = slot["instance"]["instance_id"]

        service = f"/api/gateway/service-plugins/relay.management.probe/streams/instances/{instance_id}/services/events/compressed?package_version=1.0.0"
        payload = "relay call reaches Gateway B native instance 🙂".encode()
        response = client_a.post(service, headers={
            **headers, "X-Request-ID": "relay-plugin-instance-call",
            "Content-Encoding": "gzip",
        }, content=gzip.compress(payload))
        assert response.status_code == 200, response.text
        assert response.content == payload
        assert response.headers["X-Request-ID"] == "relay-plugin-instance-call"

        publish_policy(["service_plugin_installation_manage", "configuration_read",
                        "service_plugin_runtime_manage", "configuration_write"])
        revoked = client_a.post(service, headers={
            **headers, "X-Request-ID": "relay-plugin-call-revoked",
        }, content=gzip.compress(payload))
        assert revoked.status_code == 403, revoked.text
        assert revoked.headers["X-Request-ID"] == revoked.json()["request_id"] == "relay-plugin-call-revoked"
        assert "send" in revoked.text
        native_status = client_b.post("/api/gateway/service-plugins/status", json=target).raise_for_status().json()["data"]
        retained, = [plugin for plugin in native_status["plugins"]
                     if plugin["binding"]["package_id"] == "relay.management.probe"]
        assert retained["state"] == "ready"
        assert retained["instance"]["instance_id"] == instance_id
        assert all(package["manifest"]["package_id"] != "relay.management.probe"
                   for package in exchange(client_a, "/api/gateway/service-plugins/inventory",
                       {"gateway_id": gateway_a}, "relay-inventory-a-after-revoke", remote=False)["packages"])
        (network.context.artifacts_dir / "plugin-management-relay.json").write_text(
            json.dumps({"gateway_ids": [gateway_a, gateway_b], "instance_id": instance_id,
                        "revoked_status": revoked.status_code, "evidence": evidence},
                       ensure_ascii=False, indent=2), encoding="utf-8",
        )
    finally:
        source_server.shutdown()
        source_server.server_close()
        source_thread.join(timeout=5)


def _source_server(content: bytes) -> ThreadingHTTPServer:
    class SourceHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/catalog":
                body = json.dumps({
                    "protocol_version": 1, "source_id": "relay-proof",
                    "catalog_revision": "catalog-relay-v1", "items": [{
                        "package_id": "relay.management.probe", "package_version": "1.0.0",
                        "target": native_plugin_target("python"),
                        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                        "url": f"http://127.0.0.1:{self.server.server_port}/plugin.zip",
                        "filename": "relay-plugin.zip",
                    }], "next_cursor": None,
                }).encode()
                content_type = "application/json"
            elif path == "/plugin.zip":
                body, content_type = content, "application/zip"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
