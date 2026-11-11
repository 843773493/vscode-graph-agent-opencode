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
from typing import Literal
from urllib.parse import urlsplit

import commentjson
import httpx
import psutil

from app.core.resource_uri import parse_resource_uri
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from app.schemas.gateway.config import GatewayConfigSourceDocumentDTO
from app.schemas.gateway.plugin_commands import PluginCommandSnapshotDTO
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


def _plugin_archive(version: str, *, target: dict[str, object] | None = None) -> bytes:
    manifest = {
        "manifest_version": 1,
        "package_id": "relay.management.probe",
        "version": version,
        "name": "Relay management probe",
        "target": native_plugin_target("python") if target is None else target,
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
                {"extension_point": "skills", "version": 1},
                {"extension_point": "commands", "version": 1},
            ],
            "contributes": {
                "services": [
                    {"service_id": "events", "service_type": "terminal", "protocol": "http"},
                ],
                "configuration": {
                    "type": "object",
                    "properties": {"marker": {"type": "string"}},
                },
                "skills": [{"id": "guide", "path": "instructions/relay-guide"}],
                "commands": [{"id": "probe", "handler": "probe", "title": f"Relay command {version}",
                    "arguments_schema": {"type": "object", "additionalProperties": False,
                        "properties": {"wait": {"type": "boolean"}}},
                    "result_schema": {"type": "object", "required": ["version", "pid", "count"],
                        "properties": {"version": {"type": "string"}, "pid": {"type": "integer"}, "count": {"type": "integer"}}},
                    "required_capabilities": ["read"]}],
            },
        }],
    }
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("server.py", PLUGIN.replace(
            "app.include_router(plugin_command_router({}, shutdown=shutdown))",
            """from app.core.trace_middleware import TraceMiddleware
app.add_middleware(TraceMiddleware)
command_calls = 0
async def probe(arguments, context):
    global command_calls
    command_calls += 1
    await context.progress(1, 2, 'relay-command-started')
    if arguments.get('wait'): await asyncio.Event().wait()
    return {'version': identity['package_version'], 'pid': os.getpid(), 'count': command_calls}
app.include_router(plugin_command_router({'probe': probe}, shutdown=shutdown))""",
        ))
        archive.writestr("instructions/relay-guide/SKILL.md",
            f"---\nname: relay-management-guide\ndescription: Relay source proof\n---\nTARGET_B_SKILL_{version} Ω\n")
        archive.writestr("instructions/relay-guide/reference.txt", f"TARGET_B_REFERENCE_{version}\n")
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

    contents = {version: _plugin_archive(version) for version in ("1.0.0", "1.1.0")}
    wrong_platform = dict(native_plugin_target("python"))
    wrong_platform["os"] = "windows" if wrong_platform["os"] != "windows" else "linux"
    wrong_abi = {**native_plugin_target("python"), "python": {"version": "0.0.0", "abi": "cp000"}}
    contents["2.0.0"] = _plugin_archive("2.0.0", target=wrong_platform)
    contents["2.1.0"] = _plugin_archive("2.1.0", target=wrong_abi)
    source_server = _source_server(contents)
    source_thread = threading.Thread(target=source_server.serve_forever, daemon=True)
    source_thread.start()
    evidence: list[dict[str, object]] = []
    skill_timings: list[dict[str, object]] = []

    def skill_request(*, relay: bool, uri: str | None = None,
                      operation: str = "read", expected_status: int = 200) -> dict:
        client = client_a if relay else client_b
        prefix = "skill-source-connections" if relay else "skill-sources"
        path = f"/api/gateway/{prefix}/{gateway_b}/workspaces/{network.workspace_id}"
        request_id = f"plugin-skill-{'relay' if relay else 'native'}-{len(evidence)}"
        started = time.monotonic()
        if uri is None:
            response = client.get(path, headers={"X-Request-ID": request_id})
        else:
            response = client.post(path + "/navigation", headers={"X-Request-ID": request_id}, json={
                "uri": uri, "operation": operation,
                **({"query": "TARGET_B_REFERENCE", "search_mode": "content"} if operation == "search" else {}),
            })
        skill_timings.append({"relay": relay, "operation": "snapshot" if uri is None else operation,
                              "elapsed_ms": (time.monotonic() - started) * 1000, "request_id": request_id})
        payload = response.json()
        evidence.append({"path": path, "request_id": request_id, "status": response.status_code, "body": payload})
        assert response.status_code == expected_status, response.text
        assert response.headers["X-Request-ID"] == payload["request_id"] == request_id
        return payload["data"] if expected_status == 200 else payload

    def skill_snapshot(version: str | None) -> dict:
        native = skill_request(relay=False)
        assert skill_request(relay=True) == native
        assert native["gateway_id"] == gateway_b and native["workspace_id"] == network.workspace_id
        entries = [entry for entry in native["entries"] if entry.get("plugin")]
        assert [entry["plugin"]["package_version"] for entry in entries] == ([] if version is None else [version])
        return native

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

    def install(artifact: PluginSourceArtifact, catalog_revision: str, *,
                expected_status: Literal["succeeded", "failed"] = "succeeded") -> PluginInstallOperationDTO:
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
        assert installed.status == expected_status, installed
        return installed

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

    def command_publication(*, relay: bool) -> dict:
        snapshot = PluginCommandSnapshotDTO.model_validate(exchange(client_a if relay else client_b,
            "/api/gateway/service-plugins/commands/publications", {"gateway_id": gateway_b},
            f"command-publication-{len(evidence)}", remote=relay))
        assert snapshot.gateway_id == gateway_b and snapshot.workspace_id is None
        publication, = snapshot.publications
        assert publication.binding.package_id == "relay.management.probe"
        return publication.model_dump(mode="json")

    def command_payload(publication: dict, call_id: str) -> dict:
        registration, = publication["registrations"]
        return {"gateway_id": gateway_b, **{key: publication["binding"][key]
            for key in ("package_id", "plugin_id", "package_version")},
            "instance_id": publication["instance_id"], "registration_id": registration["registration_id"],
            "command_id": "probe", "call_id": call_id, "arguments": {}, "remaining_ms": 10_000}

    def call_command(publication: dict, call_id: str, *, relay: bool = True) -> dict:
        client = client_a if relay else client_b
        response = client.post("/api/gateway/service-plugins/commands/call", json=command_payload(publication, call_id),
            headers={**(headers if relay else {}), "X-Request-ID": call_id})
        response.raise_for_status()
        assert response.headers["X-Request-ID"] == call_id
        events = [json.loads(line) for line in response.iter_lines()]
        assert [event["kind"] for event in events] == ["started", "progress", "result"], events
        assert [event["sequence"] for event in events] == [1, 2, 3]
        for event in events:
            assert event["request_id"] == event["call_id"] == call_id
            assert event["instance_id"] == publication["instance_id"]
            assert event["registration_id"] == publication["registrations"][0]["registration_id"]
        evidence.append({"command": call_id, "relay": relay, "events": events})
        return events[-1]["result"]

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
        skill_snapshot(None)
        assert all(package.manifest.package_id != "relay.management.probe" for package in inventory_b.packages)

        artifacts = {item.package_version: item for item in source.items}
        install(artifacts["1.0.0"], source.catalog_revision)
        inventory_b = PluginInventoryResultDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/inventory", target,
                               "relay-inventory-b-installed"))
        inventory_a = PluginInventoryResultDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/inventory",
                               {"gateway_id": gateway_a}, "relay-inventory-a-still-empty", remote=False))
        assert len([package for package in inventory_b.packages if package.manifest.package_id == "relay.management.probe"]) == 1
        assert all(package.manifest.package_id != "relay.management.probe" for package in inventory_a.packages)
        skill_snapshot(None)

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
        original_command = command_publication(relay=True)
        assert command_publication(relay=False) == original_command
        assert call_command(original_command, "relay-command-v1") == {"version": "1.0.0", "pid": original_pid, "count": 1}
        assert call_command(original_command, "native-command-v1", relay=False) == {"version": "1.0.0", "pid": original_pid, "count": 2}
        first_skills = skill_snapshot("1.0.0")
        first_entry, = [entry for entry in first_skills["entries"] if entry.get("plugin")]
        original_uri = first_entry["display_uri"]
        assert parse_resource_uri(original_uri).scope_id == gateway_b
        assert first_entry["plugin"]["package_id"] == "relay.management.probe"
        assert first_entry["plugin"]["workspace_id"] is None
        root_uri = original_uri.removesuffix("/SKILL.md")
        for operation, uri in (("read", original_uri), ("list", root_uri), ("search", root_uri)):
            native_result = skill_request(relay=False, uri=uri, operation=operation)
            assert skill_request(relay=True, uri=uri, operation=operation) == native_result
            if operation == "read":
                assert native_result["content"] == first_entry["content"]
            elif operation == "list":
                assert {item["uri"].rsplit("/", 1)[1] for item in native_result["entries"]} == {"SKILL.md", "reference.txt"}
            else:
                assert native_result["matches"][0]["text"] == "TARGET_B_REFERENCE_1.0.0"
        local_sources = client_a.get(
            f"/api/gateway/skill-sources/{gateway_a}/workspaces/{network.workspace_id}",
        ).raise_for_status().json()["data"]
        assert not [entry for entry in local_sources["entries"] if entry.get("plugin")]
        wrong_owner = client_a.post(
            f"/api/gateway/skill-sources/{gateway_a}/workspaces/{network.workspace_id}/navigation",
            json={"uri": original_uri, "operation": "read"},
        )
        assert wrong_owner.status_code == 404, wrong_owner.text
        assert wrong_owner.json()["detail"]["code"] == "unknown_resource"

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

        # 目录提供的完整制品仍须由 B 判兼容；拒绝新版不能撤旧绑定或改投 A。
        for version, reason in (("2.0.0", "插件目标操作系统不匹配"), ("2.1.0", "插件 Python 运行时不匹配")):
            failed = install(artifacts[version], source.catalog_revision, expected_status="failed")
            assert failed.gateway_id == gateway_b and failed.artifact.package_id == "relay.management.probe"
            assert failed.artifact.package_version == version and failed.artifact.target == artifacts[version].target
            assert failed.artifact.digest == artifacts[version].digest
            assert failed.error and reason in failed.error
            assert "package=" in failed.error and "gateway=" in failed.error and "完整插件 ZIP" in failed.error
            for reader, remote in ((client_a, True), (client_b, False)):
                unchanged = PluginRuntimeStatusDTO.model_validate(exchange(reader,
                    "/api/gateway/service-plugins/status", target,
                    f"incompatible-{version}-unchanged-{remote}", remote=remote))
                retained, = [item for item in unchanged.plugins if item.binding.package_id == "relay.management.probe"]
                assert retained == slot, "不兼容来源制品不得改变原绑定、revision或运行实例"
            available = PluginInventoryResultDTO.model_validate(exchange(client_a,
                "/api/gateway/service-plugins/inventory", target, f"incompatible-{version}-inventory"))
            assert {item.manifest.version for item in available.packages
                    if item.manifest.package_id == "relay.management.probe"} == {"1.0.0"}
            called = client_a.post(service, headers={**headers, "Content-Encoding": "gzip",
                "X-Request-ID": f"incompatible-{version}-old-call"}, content=gzip.compress(payload))
            assert called.status_code == 200 and called.content == payload
            assert called.headers["X-Request-ID"] == f"incompatible-{version}-old-call"

        # 业务调用不需要安装／启停权限；同一条贡献流在业务撤权时结束。
        publish_policy(["read", "send"])
        business_command = command_publication(relay=True)
        # revision 跟随当前配置；权限变更不改变绑定、实例或实际 handler 资格。
        assert business_command == {**original_command, "revision": business_command["revision"]}
        assert business_command["revision"] != original_command["revision"]
        assert call_command(original_command, "relay-command-business-only")["count"] == 3
        exchange(client_a, "/api/gateway/service-plugins/inventory", target,
                 "command-business-no-install", expected_status=403)
        with client_a.stream("POST", "/api/gateway/service-plugins/commands/events", json=target,
                            headers={**headers, "X-Request-ID": "relay-command-events"}, timeout=15) as stream:
            stream.raise_for_status()
            assert stream.headers["X-Request-ID"] == "relay-command-events"
            lines = stream.iter_lines()
            assert next(lines) == "event: snapshot"
            assert json.loads(next(lines).removeprefix("data: "))["publications"] == [business_command]
            assert next(lines) == ""
            active_call = {**command_payload(original_command, "relay-command-in-flight"), "arguments": {"wait": True}}
            with client_a.stream("POST", "/api/gateway/service-plugins/commands/call", json=active_call,
                                 headers={**headers, "X-Request-ID": "relay-command-in-flight"}, timeout=15) as executing:
                executing.raise_for_status()
                assert executing.headers["X-Request-ID"] == "relay-command-in-flight"
                command_lines = executing.iter_lines()
                partial = [json.loads(next(command_lines)) for _ in range(2)]
                assert [event["kind"] for event in partial] == ["started", "progress"]
                assert all(event["call_id"] == event["request_id"] == "relay-command-in-flight" for event in partial)
                publish_policy(["service_plugin_installation_manage", "configuration_read",
                                "service_plugin_runtime_manage", "configuration_write"])
                received, transport_error = [], None
                try:
                    received = list(command_lines)
                except httpx.RemoteProtocolError as error:
                    # 已发响应头的业务通道可中断分块传输；消费者按缺终态报告结果未知。
                    transport_error = str(error)
                assert received == [], "执行通道撤权后不能伪造成功或重放"
                evidence.append({"command": "relay-command-in-flight", "events": partial,
                                 "transport_error": transport_error, "result_unknown": True})
            assert list(lines) == [], "撤权后原命令贡献订阅必须结清"
        denied_command = client_a.post("/api/gateway/service-plugins/commands/call",
            json=command_payload(original_command, "relay-command-revoked"),
            headers={**headers, "X-Request-ID": "relay-command-revoked"})
        assert denied_command.status_code == 403, denied_command.text
        assert denied_command.headers["X-Request-ID"] == denied_command.json()["request_id"] == "relay-command-revoked"
        assert call_command(original_command, "native-command-after-revoke", relay=False)["count"] == 5
        revoked = client_a.post(service, headers={
            **headers, "X-Request-ID": "relay-plugin-call-revoked",
        }, content=gzip.compress(payload))
        assert revoked.status_code == 403, revoked.text
        assert revoked.headers["X-Request-ID"] == revoked.json()["request_id"] == "relay-plugin-call-revoked"
        assert "send" in revoked.text
        denied_skill = skill_request(relay=True, uri=original_uri, expected_status=409)
        assert denied_skill["detail"]["code"] == "authorization-revoked", denied_skill
        assert denied_skill["detail"]["detail"]["capability"] == "read"
        assert skill_request(relay=False, uri=original_uri)["content"] == first_entry["content"]
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
        assert command_publication(relay=True) == {**original_command, "revision": current.revision}
        assert skill_snapshot("1.0.0") == first_skills, "安装新版不能改换正在使用的 Skill 来源"

        edit_gateway_b(lambda document: bind(document, "1.1.0"), remote=True)
        switched = PluginRuntimeStatusDTO.model_validate(exchange(client_a, "/api/gateway/service-plugins/status", target, "relay-explicit-version-switch"))
        replacement, = [item for item in switched.plugins if item.binding.package_id == "relay.management.probe"]
        assert replacement.state == "ready" and replacement.binding.package_version == "1.1.0" and replacement.instance is not None
        replacement_id, replacement_pid = replacement.instance.instance_id, replacement.instance.process_id
        assert replacement_id != instance_id and not psutil.pid_exists(original_pid)
        replacement_command = command_publication(relay=True)
        assert replacement_command["instance_id"] == replacement_id
        assert replacement_command["commands"][0]["title"] == "Relay command 1.1.0"
        assert call_command(replacement_command, "relay-command-v2") == {"version": "1.1.0", "pid": replacement_pid, "count": 1}
        stale = client_a.post("/api/gateway/service-plugins/commands/call",
            json=command_payload(original_command, "relay-command-stale"), headers=headers)
        assert stale.status_code == 409, stale.text
        second_skills = skill_snapshot("1.1.0")
        second_entry, = [entry for entry in second_skills["entries"] if entry.get("plugin")]
        assert second_skills["revision"] != first_skills["revision"]
        assert second_entry["entry_identity"] != first_entry["entry_identity"]
        assert second_entry["display_uri"] != original_uri
        assert "TARGET_B_SKILL_1.1.0" in skill_request(relay=True, uri=second_entry["display_uri"])["content"]
        skill_request(relay=True, uri=original_uri, expected_status=404)
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
        skill_snapshot(None)
        skill_request(relay=True, uri=second_entry["display_uri"], expected_status=404)
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
                        "revoked_status": revoked.status_code, "skill_timings": skill_timings,
                        "skill_versions": [first_entry, second_entry], "evidence": evidence},
                       ensure_ascii=False, indent=2), encoding="utf-8",
        )
    finally:
        source_server.shutdown()
        source_server.server_close()
        source_thread.join(timeout=5)


def _source_server(contents: dict[str, bytes]) -> ThreadingHTTPServer:
    targets = {}
    for version, content in contents.items():
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            targets[version] = json.loads(archive.read("plugin.jsonc"))["target"]

    class SourceHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/catalog":
                body = json.dumps({
                    "protocol_version": 1, "source_id": "relay-proof",
                    "catalog_revision": "catalog-relay-v1", "items": [{
                        "package_id": "relay.management.probe", "package_version": version,
                        "target": targets[version],
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
