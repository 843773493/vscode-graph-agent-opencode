"""真实双 Gateway 验证 Workspace Skill metadata 的原生与 Relay 路由。"""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from uuid import uuid4

import commentjson
import httpx
import pytest

from app.core.resource_uri import parse_resource_uri
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from tests.integration.gateway.test_target_gateway_routing import (
    TargetGatewayNetwork,
    _observations,
)
from tests.integration.gateway.test_target_gateway_routing import (
    target_gateway_network as target_gateway_network,  # noqa: PLC0414 - pytest 显式重导出网络 fixture。
)
from tests.support.plugin_package import install_plugin
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS

pytest_plugins = ["tests.integration.gateway.test_target_gateway_routing"]


@pytest.fixture
def plugin_capable_target_gateway_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", LOCAL_TOKEN_HEADERS["X-Local-Token"])
    return request.getfixturevalue("target_gateway_network")


def test_gateway_skill_metadata_native_and_relay_use_exact_owner(
    plugin_capable_target_gateway_network: TargetGatewayNetwork,
) -> None:
    network = plugin_capable_target_gateway_network
    client_a, client_b = network.clients
    gateway_a, gateway_b = network.gateway_ids
    workspace_id = network.workspace_id
    topology = json.loads((network.context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    connection = client_a.post("/api/gateway/remote-gateways", json={
        "name": "Skill metadata owner B", "host": "127.0.0.1", "port": 22,
        "username": "skill-metadata-relay", "private_key_path": topology["private_key"],
        "remote_gateway_port": topology["ports"][3],
    }).raise_for_status().json()["data"]
    projected, = [item for item in connection["items"] if item["gateway_id"] == gateway_b]
    connection_id = projected["remote_gateways"][0]["gateway_connection_id"]
    relay_headers = {
        TARGET_GATEWAY_ID_HEADER: gateway_b,
        "X-BoxTeam-Workspace-Id": workspace_id,
        "X-BoxTeam-Gateway-Connection-Id": connection_id,
    }

    def publish_read_policy(*, enabled: bool) -> None:
        source_path = "/api/gateway/config/sources/gateway_mutable_override"
        initial = client_b.get(source_path).raise_for_status().json()["data"]
        document = commentjson.loads(initial["jsonc"])
        rules = ([{"capability": "read", "effect": "allow",
                   "principal_ref": principal_ref_for_gateway(gateway_a)}] if enabled else [])
        document.setdefault("permissions", {})["federation"] = {"default_effect": "deny", "rules": rules}
        edited = client_b.put(source_path, json={
            "gateway_id": gateway_b, "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(document, ensure_ascii=False),
            "base_file_digest": initial["file_digest"],
            "base_layer_revision": initial["layer_revision"],
            "base_layer_digest": initial["layer_digest"],
            "expected_active_revision": initial["active_revision"],
            "expected_active_digest": initial["active_digest"],
            "idempotency_key": f"skill-metadata-read-{enabled}",
        }).raise_for_status().json()["data"]
        assert edited["edit"]["state"] == "active", edited
        previous_revision = initial["active_revision"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = client_b.get("/api/gateway/config/reload-status").raise_for_status().json()["data"]
            if current["revision"] != previous_revision and current["healthy"] is True:
                return
            time.sleep(0.05)
        raise AssertionError("Gateway B 未发布 federation read policy")

    publish_read_policy(enabled=True)

    skill_root = network.gateway_homes[1] / "skills" / "metadata-proof"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: metadata-proof\ndescription: Metadata only\n---\nPRIVATE_BODY_MUST_NOT_APPEAR\n",
        encoding="utf-8",
    )
    metadata_path = "/api/v1/resources/gateway-skills"
    bad_workspace_id = str(uuid4())

    # 明确的测试准备动作发布新写入的本机 Skill；被测 metadata 读不应触发它。
    refreshed = client_b.post("/api/gateway/skill-sources/refresh", headers={
        "X-Request-ID": "skill-metadata-setup-refresh",
    })
    assert refreshed.status_code == 200, refreshed.text

    def metadata(client: httpx.Client, request_id: str, *, relay: bool) -> dict:
        response = client.get(metadata_path, headers={
            "X-Request-ID": request_id,
            **(relay_headers if relay else {}),
        })
        assert response.status_code == 200, response.text
        payload = response.json()
        assert response.headers["X-Request-ID"] == payload["request_id"] == request_id
        data = payload["data"]
        assert data["gateway_id"] == gateway_b and data["workspace_id"] == workspace_id
        assert "PRIVATE_BODY_MUST_NOT_APPEAR" not in response.text
        assert str(network.gateway_homes[1]) not in response.text
        return data

    # metadata 请求只返回来源投影，不刷新或读取来源正文。
    native = metadata(client_b, "skill-metadata-native", relay=False)
    assert any(entry["name"] == "metadata-proof" for entry in native["entries"])

    relayed = metadata(client_a, "skill-metadata-relay", relay=True)
    assert relayed == native
    connected_path = (
        f"/api/gateway/skill-source-connections/{gateway_b}/workspaces/{workspace_id}/metadata"
    )
    connected_response = client_a.get(connected_path, headers={
        "X-Request-ID": "skill-metadata-connected-relay",
    })
    assert connected_response.status_code == 200, connected_response.text
    assert connected_response.headers["X-Request-ID"] == connected_response.json()["request_id"] == "skill-metadata-connected-relay"
    assert connected_response.json()["data"] == native
    entry, = [item for item in native["entries"] if item["name"] == "metadata-proof"]
    assert entry["display_uri"].startswith(f"boxteam://gateway/{gateway_b}/resources/skills/")
    assert entry["layer"] == "gateway" and entry["available"] is True
    assert "content" not in entry and "private_uri" not in entry

    # 通过产品安装与 Workspace 配置入口生成插件 Skill 来源，不手写发布账本。
    archive_path = network.context.artifacts_dir / "workspace-skill-proof.zip"
    manifest = {
        "manifest_version": 1,
        "package_id": "workspace.skill.proof",
        "version": "1.0.0",
        "name": "Workspace Skill audience proof",
        "extension_contract_version": 1,
        "plugins": [{
            "id": "guide",
            "name": "Workspace Skill guide",
            "instance_scope": "workspace",
            "requires": [{"extension_point": "skills", "version": 1}],
            "contributes": {"skills": [{"id": "guide", "path": "skills/guide"}]},
        }],
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("skills/guide/SKILL.md", (
            "---\nname: workspace-skill-proof\ndescription: Workspace audience proof\n---\n"
            "WORKSPACE_SKILL_BODY_B"
        ))
    workspace_source = f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override"
    source = client_b.get(workspace_source, headers={"X-BoxTeam-Workspace-Id": workspace_id}).raise_for_status().json()["data"]
    document = commentjson.loads(source["jsonc"])
    document.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = (
        f"http://127.0.0.1:{topology['ports'][3]}"
    )
    connection_config = client_b.put(workspace_source, headers={
        "X-Request-ID": "skill-proof-gateway-url", "X-BoxTeam-Workspace-Id": workspace_id,
    }, json={
        "gateway_id": gateway_b, "workspace_id": workspace_id,
        "source_key": "workspace_root_mutable_override",
        "jsonc": json.dumps(document, ensure_ascii=False),
        "base_file_digest": source["file_digest"], "base_layer_revision": source["layer_revision"],
        "base_layer_digest": source["layer_digest"], "expected_active_revision": source["active_revision"],
        "expected_active_digest": source["active_digest"], "idempotency_key": "skill-proof-gateway-url",
    }).raise_for_status().json()["data"]
    assert connection_config["edit"]["state"] == "active", connection_config
    deadline = time.monotonic() + 15
    health: dict[str, object] = {}
    while time.monotonic() < deadline:
        health = client_b.get("/api/v1/health").raise_for_status().json()
        if health.get("backend_id") and health.get("backend_generation"):
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f"Backend identity 未在期限内就绪: {health}")
    install_plugin(
        lambda path, body: client_b.post(path, json=body, headers={"X-Request-ID": "skill-proof-install"}),
        gateway_id=gateway_b,
        path=str(archive_path),
        expected_digest=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    )
    source = client_b.get(workspace_source, headers={"X-BoxTeam-Workspace-Id": workspace_id}).raise_for_status().json()["data"]
    document = commentjson.loads(source["jsonc"])
    document.setdefault("plugins", {})["runtime"] = {"entries": [{
        "package_id": "workspace.skill.proof", "plugin_id": "guide",
        "package_version": "1.0.0", "enabled": True,
    }]}
    configured = client_b.put(workspace_source, headers={
        "X-Request-ID": "skill-proof-bind", "X-BoxTeam-Workspace-Id": workspace_id,
    }, json={
        "gateway_id": gateway_b, "workspace_id": workspace_id,
        "source_key": "workspace_root_mutable_override",
        "jsonc": json.dumps(document, ensure_ascii=False),
        "base_file_digest": source["file_digest"], "base_layer_revision": source["layer_revision"],
        "base_layer_digest": source["layer_digest"], "expected_active_revision": source["active_revision"],
        "expected_active_digest": source["active_digest"], "idempotency_key": "skill-proof-bind",
    }).raise_for_status().json()["data"]
    assert configured["edit"]["state"] == "active", configured

    deadline = time.monotonic() + 20
    plugin_entry = None
    plugin_metadata = {}
    plugin_status = {}
    while time.monotonic() < deadline:
        plugin_metadata = metadata(client_b, f"skill-proof-metadata-{int(deadline - time.monotonic())}", relay=False)
        plugin_status = client_b.post("/api/gateway/service-plugins/status", json={
            "gateway_id": gateway_b,
        }).raise_for_status().json()["data"]
        plugin_entry = next((item for item in plugin_metadata["entries"]
                             if item.get("plugin") is not None
                             and item["plugin"]["package_id"] == "workspace.skill.proof"), None)
        if plugin_entry is not None:
            break
        time.sleep(0.1)
    assert plugin_entry is not None, {"metadata": plugin_metadata, "plugin_status": plugin_status}
    skill_uri = plugin_entry["display_uri"]
    assert plugin_entry["plugin"]["workspace_id"] == workspace_id
    assert skill_uri.startswith(f"boxteam://gateway/{gateway_b}/resources/skills/")

    def read_skill(client: httpx.Client, request_id: str, *, relay: bool, audience: str = workspace_id) -> dict:
        response = client.post("/api/v1/resources/navigation", headers={
            "X-Request-ID": request_id,
            TARGET_GATEWAY_ID_HEADER: gateway_b,
            "X-BoxTeam-Workspace-Id": audience,
            **(relay_headers if relay else {}),
        }, json={"uri": skill_uri, "operation": "read"})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert response.headers["X-Request-ID"] == payload["request_id"] == request_id
        assert payload["data"]["content"].endswith("WORKSPACE_SKILL_BODY_B")
        requested = parse_resource_uri(skill_uri)
        resolved = parse_resource_uri(payload["data"]["node"]["uri"])
        assert (resolved.scope, resolved.scope_id, resolved.kind, resolved.tail_segments) == (
            requested.scope, requested.scope_id, requested.kind, requested.tail_segments,
        )
        assert resolved.gateway_authority in {None, gateway_b}
        (network.context.artifacts_dir / f"{request_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        return payload["data"]

    native_skill = read_skill(client_b, "skill-proof-read-native", relay=False)
    relayed_skill = read_skill(client_a, "skill-proof-read-relay", relay=True)
    assert relayed_skill["content"] == native_skill["content"]
    assert relayed_skill["node"]["uri"] != native_skill["node"]["uri"]
    (network.context.artifacts_dir / "skill-uri-contract.json").write_text(json.dumps({
        "metadata_display_uri": skill_uri,
        "native_result_uri": native_skill["node"]["uri"],
        "relay_result_uri": relayed_skill["node"]["uri"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sibling_skill = client_b.post("/api/v1/resources/navigation", headers={
        "X-Request-ID": "skill-proof-sibling-audience",
        TARGET_GATEWAY_ID_HEADER: gateway_b,
        "X-BoxTeam-Workspace-Id": str(uuid4()),
    }, json={"uri": skill_uri, "operation": "read"})
    assert sibling_skill.status_code in {403, 404}, sibling_skill.text
    assert sibling_skill.headers["X-Request-ID"] == sibling_skill.json()["request_id"] == "skill-proof-sibling-audience"

    # 原本机 audience 校验仍由 Gateway owner 执行，不接受另一个 workspace UUID。
    wrong_audience_id = "skill-metadata-wrong-audience"
    wrong_audience = client_b.get(
        f"/api/gateway/skill-source-connections/{gateway_b}/workspaces/{bad_workspace_id}/metadata",
        headers={"X-Request-ID": wrong_audience_id},
    )
    assert wrong_audience.status_code == 403, wrong_audience.text
    assert wrong_audience.headers["X-Request-ID"] == wrong_audience.json()["request_id"] == wrong_audience_id
    relayed_wrong_audience = client_a.get(
        connected_path.replace(workspace_id, bad_workspace_id),
        headers={"X-Request-ID": "skill-metadata-relay-wrong-audience"},
    )
    assert relayed_wrong_audience.status_code == 403, relayed_wrong_audience.text
    assert relayed_wrong_audience.headers["X-Request-ID"] == relayed_wrong_audience.json()["request_id"] == "skill-metadata-relay-wrong-audience"

    # 入口及 Gateway B 均只处理 metadata/read，不得触发模型请求或来源刷新。
    for request_id in ("skill-metadata-native", "skill-metadata-relay"):
        backend = _observations(network.observation_paths["backend-b"], request_id)
        assert backend and all(item["path"].endswith(metadata_path) for item in backend)
        assert all(item["response_request_id"] == request_id for item in backend)
        gateway_observations = _observations(network.observation_paths["gateway-b"], request_id)
        assert any(item["path"].endswith("/metadata") for item in gateway_observations)
        assert all("refresh" not in item["path"] and "publication" not in item["path"]
                   for item in gateway_observations)
        assert _observations(network.observation_paths["backend-a"], request_id) == []
    refresh_observation, = _observations(network.observation_paths["gateway-b"], "skill-metadata-setup-refresh")
    assert refresh_observation["path"] == "/api/gateway/skill-sources/refresh"

    # 撤销 A 对 B 的 read 后，Relay 失败并保留原授权错误；B 原生访问仍可读。
    publish_read_policy(enabled=False)

    connected_revoked = client_a.get(connected_path, headers={
        "X-Request-ID": "skill-metadata-connected-revoked",
    })
    assert connected_revoked.status_code == 409, connected_revoked.text
    assert connected_revoked.headers["X-Request-ID"] == connected_revoked.json()["request_id"] == "skill-metadata-connected-revoked"
    assert connected_revoked.json()["detail"]["code"] == "authorization-revoked"
    assert connected_revoked.json()["detail"]["detail"]["capability"] == "read"

    revoked = client_a.get(metadata_path, headers={
        **relay_headers, "X-Request-ID": "skill-metadata-revoked",
    })
    assert revoked.status_code == 200, revoked.text
    assert revoked.headers["X-Request-ID"] == revoked.json()["request_id"] == "skill-metadata-revoked"
    assert revoked.json()["data"] == plugin_metadata
    assert metadata(client_b, "skill-metadata-native-after-revoke", relay=False) == plugin_metadata

