"""真实 Backend 配置 CAS 与 Gateway 插件卸载的并发边界。"""

from __future__ import annotations

import concurrent.futures
import hashlib
import itertools
import json
import zipfile
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.plugin_package import install_plugin, native_plugin_target
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


@pytest.fixture
def plugin_uninstall_race_case(request: pytest.FixtureRequest, unused_tcp_port: int):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    template = Path.cwd() / "tests/fixtures/workspaces/default_test_workspace"
    config = Path.cwd() / "configs/tests/workspace/default.jsonc"
    schema = Path.cwd() / "configs/workspace_schema.jsonc"
    gateway_workspace = prepare_test_workspace(
        workspace_root=context.workspace_root / "gateway-default",
        template_root=template,
        template_items=(),
    )
    install_test_workspace_config(workspace_root=gateway_workspace, config_path=config, schema_path=schema)
    workspace = prepare_test_workspace(
        workspace_root=context.workspace_root / "workspace",
        template_root=template,
        template_items=(),
    )
    install_test_workspace_config(workspace_root=workspace, config_path=config, schema_path=schema)
    gateway_url = f"http://127.0.0.1:{unused_tcp_port}"
    for root in (gateway_workspace, workspace):
        config_path = root / ".boxteam/workspace.jsonc"
        payload = commentjson.loads(config_path.read_text(encoding="utf-8"))
        payload.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = gateway_url
        config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    gateway = start_gateway_process(
        workspace_root=gateway_workspace,
        default_backend_url=None,
        port=unused_tcp_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
        artifacts_dir=context.artifacts_dir,
        asgi_app="tests.integration.gateway.service_plugin_uninstall_race_app:create_gateway_app",
    )
    evidence: list[dict[str, object]] = []
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
            trust_env=False,
        ) as client:
            yield client, context, workspace, evidence
    finally:
        (context.artifacts_dir / "plugin-uninstall-race-http.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        close_gateway_process(gateway)


def _package(path: Path) -> Path:
    manifest = {
        "manifest_version": 1,
        "package_id": "race.probe",
        "version": "1.0.0",
        "name": "卸载并发探针",
        "target": native_plugin_target("python"),
        "extension_contract_version": 1,
        "plugins": [{
            "id": "probe",
            "name": "并发探针",
            "instance_scope": "workspace",
            "requires": [{"extension_point": "services", "version": 1}],
            "contributes": {"services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}]},
            "runtime": "python",
            "entrypoint": ["entry.py"],
            "management_protocol_version": 1,
        }],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("entry.py", "raise SystemExit('配置竞态测试不启动插件')\n")
    return path


def test_uninstall_serializes_after_pending_binding_publication(plugin_uninstall_race_case) -> None:
    client, context, workspace, evidence = plugin_uninstall_race_case
    request_counter = itertools.count(1)

    def request(
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> httpx.Response:
        request_id = f"plugin-uninstall-race-{next(request_counter)}"
        response = client.request(
            method,
            path,
            json=body,
            headers={"X-Request-ID": request_id, **(headers or {})},
            timeout=timeout,
        )
        assert response.headers.get("X-Request-ID") == request_id
        if response.headers.get("content-type", "").startswith("application/json"):
            payload = response.json()
            if "request_id" in payload:
                assert payload["request_id"] == request_id
        evidence.append({
            "method": method,
            "path": path,
            "request_id": request_id,
            "status": response.status_code,
            "request_body": body,
            "response": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text,
        })
        return response

    gateway_id = request("GET", "/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    created = request("POST", "/api/gateway/managed-workspaces", {
        "root_path": str(workspace), "name": "插件卸载竞态工作区", "create_directory": False,
    }, timeout=75).raise_for_status().json()["data"]
    workspace_id = next(
        item["workspace_id"] for item in created["items"]
        if item["gateway_id"] == gateway_id and item["name"] == "插件卸载竞态工作区"
    )

    package_archive = _package(context.artifacts_dir / "race.probe.zip")
    install_plugin(
        lambda path, body: request("POST", path, body),
        gateway_id=gateway_id,
        path=str(package_archive),
        expected_digest=hashlib.sha256(package_archive.read_bytes()).hexdigest(),
    )

    source_path = f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override"
    workspace_headers = {"X-BoxTeam-Workspace-Id": workspace_id}
    source = request("GET", source_path, headers={"X-BoxTeam-Workspace-Id": workspace_id}).raise_for_status().json()["data"]
    config = commentjson.loads(source["jsonc"])
    config.setdefault("plugins", {})["runtime"] = {"entries": []}
    empty_config = json.dumps(config, ensure_ascii=False, indent=2)
    empty_cas = {
        "gateway_id": gateway_id,
        "workspace_id": workspace_id,
        "source_key": "workspace_root_mutable_override",
        "jsonc": empty_config,
        "base_file_digest": source["file_digest"],
        "base_layer_revision": source["layer_revision"],
        "base_layer_digest": source["layer_digest"],
        "expected_active_revision": source["active_revision"],
        "expected_active_digest": source["active_digest"],
        "idempotency_key": "race-probe-empty-source",
    }
    request("PUT", source_path, empty_cas, headers={"X-BoxTeam-Workspace-Id": workspace_id}).raise_for_status()
    baseline = request("GET", source_path, headers=workspace_headers).raise_for_status().json()["data"]
    baseline_active_revision = baseline["active_revision"]

    request("POST", "/__test/service-plugin-race/arm").raise_for_status()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        uninstall = executor.submit(request, "POST", "/api/gateway/service-plugins/uninstall", {
            "gateway_id": gateway_id,
            "package_id": "race.probe",
            "package_version": "1.0.0",
        }, timeout=75)
        request("POST", "/__test/service-plugin-race/wait-references", timeout=75).raise_for_status()

        source = request("GET", source_path, headers={"X-BoxTeam-Workspace-Id": workspace_id}).raise_for_status().json()["data"]
        config = commentjson.loads(source["jsonc"])
        config.setdefault("plugins", {})["runtime"] = {"entries": [{
            "package_id": "race.probe",
            "package_version": "1.0.0",
            "plugin_id": "probe",
            "enabled": True,
        }]}
        candidate = {
            "gateway_id": gateway_id,
            "workspace_id": workspace_id,
            "source_key": "workspace_root_mutable_override",
            "jsonc": json.dumps(config, ensure_ascii=False, indent=2),
            "base_file_digest": source["file_digest"],
            "base_layer_revision": source["layer_revision"],
            "base_layer_digest": source["layer_digest"],
            "expected_active_revision": source["active_revision"],
            "expected_active_digest": source["active_digest"],
            "idempotency_key": "race-probe-enable-after-uninstall-read",
        }
        config_update = executor.submit(
            request,
            "PUT",
            source_path,
            candidate,
            headers={"X-BoxTeam-Workspace-Id": workspace_id},
            timeout=75,
        )
        try:
            request("POST", "/__test/service-plugin-race/wait-validate", timeout=75).raise_for_status()
        finally:
            request("POST", "/__test/service-plugin-race/release").raise_for_status()
        uninstall_response = uninstall.result(timeout=90)
        update_response = config_update.result(timeout=90)

    assert uninstall_response.status_code == 200, uninstall_response.text
    assert update_response.status_code == 409, update_response.text
    active = request("GET", source_path, headers=workspace_headers,
        timeout=30).raise_for_status().json()["data"]
    candidate_document = request("GET", source_path + "?idempotency_key=race-probe-enable-after-uninstall-read",
        headers=workspace_headers).raise_for_status().json()["data"]
    assert active["active_revision"] == baseline_active_revision
    assert candidate_document["edit"]["state"] == "conflict"
    rejected_source = commentjson.loads(candidate_document["jsonc"])
    assert rejected_source["plugins"]["runtime"]["entries"][0]["package_id"] == "race.probe"
    inventory = request("POST", "/api/gateway/service-plugins/inventory", {"gateway_id": gateway_id})
    inventory.raise_for_status()
    assert not any(
        package["manifest"]["package_id"] == "race.probe"
        and package["manifest"]["version"] == "1.0.0"
        for package in inventory.json()["data"]["packages"]
    )


def test_uninstall_waits_for_real_skill_read_after_version_switch(
    plugin_uninstall_race_case, request: pytest.FixtureRequest,
) -> None:
    """安装和绑定由原入口生成，真实文件读取结清前仅旧版本受阻。"""
    client, context, _, evidence = plugin_uninstall_race_case
    package_id = "race.skill"
    request_counter = itertools.count(1)

    def exchange(method: str, path: str, body: dict[str, object] | None = None) -> httpx.Response:
        request_id = f"plugin-skill-read-{next(request_counter)}"
        response = client.request(method, path, json=body, headers={"X-Request-ID": request_id})
        assert response.headers["X-Request-ID"] == request_id
        payload = response.json()
        if "request_id" in payload:
            assert payload["request_id"] == request_id
        evidence.append({"method": method, "path": path, "request_id": request_id,
                         "status": response.status_code, "body": payload})
        return response

    gateway_id = exchange("GET", "/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    package_root = context.boxteam_home_for_node(request.node.nodeid) / "plugins/packages" / package_id
    workspace_id = exchange("GET", "/api/gateway/workspaces").raise_for_status().json()["data"]["items"][0]["workspace_id"]
    skill_path = f"/api/gateway/skill-sources/{gateway_id}/workspaces/{workspace_id}"
    for version in ("1.0.0", "2.0.0"):
        path = context.artifacts_dir / f"{package_id}-{version}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("plugin.jsonc", json.dumps({
                "package_id": package_id, "version": version, "name": "读取引用探针",
                "plugins": [{"id": "guide", "name": "说明", "instance_scope": "gateway",
                    "requires": [{"extension_point": "skills", "version": 1}],
                    "contributes": {"skills": [{"id": "guide", "path": "guide"}]}}],
            }))
            archive.writestr("guide/SKILL.md", f"---\nname: uninstall-read-probe\ndescription: Read lease\n---\nBODY_{version}\n")
        install_plugin(lambda endpoint, body: exchange("POST", endpoint, body),
                       gateway_id=gateway_id, path=str(path))

    def bind(version: str | None) -> None:
        path = "/api/gateway/config/sources/gateway_mutable_override"
        document = exchange("GET", path).raise_for_status().json()["data"]
        source = commentjson.loads(document["jsonc"])
        source.setdefault("plugins", {})["runtime"] = {"entries": [] if version is None else [{
            "package_id": package_id, "package_version": version, "plugin_id": "guide", "enabled": True,
        }]}
        saved = exchange("PUT", path, {"gateway_id": gateway_id, "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(source), "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": f"read-bind-{version}"}).raise_for_status().json()["data"]
        assert saved["edit"]["state"] == "active", saved

    def published(version: str) -> dict:
        data = exchange("GET", skill_path).raise_for_status().json()["data"]
        entry, = [item for item in data["entries"] if (item.get("plugin") or {}).get("package_id") == package_id]
        assert entry["plugin"]["package_version"] == version, entry
        return entry

    def uninstall(version: str) -> httpx.Response:
        return exchange("POST", "/api/gateway/service-plugins/uninstall", {
            "gateway_id": gateway_id, "package_id": package_id, "package_version": version,
        })

    def read(uri: str) -> httpx.Response:
        return exchange("POST", skill_path + "/navigation", {"uri": uri, "operation": "read", "timeout_ms": 30000})

    bind("1.0.0")
    old = published("1.0.0")
    exchange("POST", "/__test/service-plugin-race/arm-read", {"uri": old["display_uri"]}).raise_for_status()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        reading = executor.submit(read, old["display_uri"])
        try:
            exchange("POST", "/__test/service-plugin-race/wait-read").raise_for_status()
            bind("2.0.0")
            current = published("2.0.0")
            assert current["display_uri"] != old["display_uri"]
            assert "BODY_2.0.0" in read(current["display_uri"]).raise_for_status().json()["data"]["content"]
            assert read(old["display_uri"]).status_code == 404
            blocked = uninstall("1.0.0")
            assert blocked.status_code == 409 and f"skill-read:{package_id}:1.0.0" in blocked.text, blocked.text
            assert "configuration:" not in blocked.text and "workspace-configuration:" not in blocked.text
            assert (package_root / "1.0.0/guide/SKILL.md").is_file()
            assert not reading.done(), "旧版本实际读取未结束，不能提前释放引用"
        finally:
            exchange("POST", "/__test/service-plugin-race/release-read").raise_for_status()
        revoked = reading.result(timeout=10)
    assert revoked.status_code == 404, revoked.text
    uninstall("1.0.0").raise_for_status()
    assert not (package_root / "1.0.0").exists()
    assert (package_root / "2.0.0/guide/SKILL.md").is_file()
    assert "BODY_2.0.0" in read(current["display_uri"]).raise_for_status().json()["data"]["content"]
    packages = exchange("POST", "/api/gateway/service-plugins/inventory", {"gateway_id": gateway_id}).raise_for_status().json()["data"]["packages"]
    assert [item["manifest"]["version"] for item in packages if item["manifest"]["package_id"] == package_id] == ["2.0.0"]
    bind(None)
    uninstall("2.0.0").raise_for_status()
    assert not package_root.exists()
