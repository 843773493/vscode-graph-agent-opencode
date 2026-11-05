"""真实双 Gateway／Backend 的候选计划；SSH 执行及隧道复用受控本机替身。"""

from __future__ import annotations

import json
import time

import commentjson
import httpx

from app.core.backend_identity import read_backend_id
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from tests.integration.gateway.test_target_gateway_routing import (
    TargetGatewayNetwork,
    _observations,
)

pytest_plugins = ["tests.integration.gateway.test_target_gateway_routing"]


def test_native_gateway_authorizes_exact_backend_candidate_without_installing(
    target_gateway_network: TargetGatewayNetwork,
) -> None:
    network = target_gateway_network
    local, native = network.clients
    gateway_a, gateway_b = network.gateway_ids
    backend_a, backend_b = (read_backend_id(home) for home in network.gateway_homes)
    assert backend_a != backend_b and network.native_routes == (network.workspace_id, network.workspace_id)
    target = {"kind": "backend", "gateway_id": gateway_b, "backend_id": backend_b}
    source = {"kind": "manual", "name": "same-http", "version": "1", "definition": {"remotes": [
        {"type": "streamable-http", "url": "https://service.test/mcp"}]}}
    intent: dict[str, object] = {"target": target, "source": source}
    paths = ("/api/v1/mcp", f"/api/v1/workspaces/{network.workspace_id}/mcp")
    workspace_sources = tuple(root / ".boxteam/workspace.jsonc" for root in network.workspace_roots)
    originals = tuple(path.read_bytes() for path in workspace_sources)
    evidence: list[dict[str, object]] = []

    def exchange(
        client: httpx.Client, method: str, path: str, request_id: str, *,
        body: dict[str, object] | None = None, remote: bool = False,
    ) -> httpx.Response:
        response = client.request(method, path, json=body, headers={
            "X-Request-ID": request_id,
            **({TARGET_GATEWAY_ID_HEADER: gateway_b, "X-BoxTeam-Workspace-Id": network.workspace_id} if remote else {}),
        })
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        evidence.append({"request_id": request_id, "method": method, "path": path,
                         "status": response.status_code, "response": response.json()})
        return response

    def publish_policy(rules: list[dict[str, str]], label: str) -> None:
        # 只编辑隔离目标的正式用户配置，由生产 watcher 发布授权，不替换内存策略。
        config_path = network.gateway_config_paths[1]
        previous = exchange(native, "GET", "/api/gateway/config/reload-status", label + "-before").json()["data"]
        config = commentjson.loads(config_path.read_text(encoding="utf-8"))
        config.setdefault("permissions", {})["federation"] = {"default_effect": "allow", "rules": rules}
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        deadline = time.monotonic() + 15
        for attempt in range(76):
            current = exchange(native, "GET", "/api/gateway/config/reload-status", f"{label}-poll-{attempt}").json()["data"]
            if current["revision"] != previous["revision"] and current["healthy"] is True:
                assert current["state"] == "active" and current["restart_required"] is False
                return
            assert time.monotonic() < deadline, current
            time.sleep(0.2)
        raise AssertionError("正式配置 watcher 未在期限内发布 MCP 管理授权")

    def assert_native_only(request_id: str, *, reaches_backend: bool) -> None:
        forwarded, = _observations(network.observation_paths["gateway-b"], request_id)
        assert (forwarded["sender_gateway_id"], forwarded["target_gateway_id"], forwarded["response_request_id"]) == (
            gateway_a, gateway_b, request_id,
        )
        assert _observations(network.observation_paths["backend-a"], request_id) == []
        downstream = _observations(network.observation_paths["backend-b"], request_id)
        if reaches_backend:
            backend, = downstream
            assert (backend["sender_gateway_id"], backend["workspace_id"], backend["response_request_id"]) == (
                gateway_b, network.workspace_id, request_id,
            )
        else:
            assert downstream == []

    try:
        topology = json.loads((network.context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
        registered = exchange(local, "POST", "/api/gateway/remote-gateways", "mcp-register-peer", body={
            "name": "MCP 目标 B", "host": "127.0.0.1", "port": 22, "username": "mcp-integration",
            "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3],
        })
        assert registered.status_code == 200, registered.text
        projected, = [item for item in registered.json()["data"]["items"] if item["gateway_id"] == gateway_b]
        assert projected["workspace_id"] == network.workspace_id
        principal = principal_ref_for_gateway(gateway_a)
        other_capabilities = [{"capability": capability, "effect": "allow", "principal_ref": principal}
                              for capability in ("discovery", "read", "managed_workspace_admin", "configuration_read", "configuration_write")]
        for label, rules in (
            ("default", None),
            ("other-capabilities", other_capabilities),
            ("wildcard", [{"capability": "mcp_installation_manage", "effect": "allow"}]),
            ("wrong-principal", [{"capability": "mcp_installation_manage", "effect": "allow",
                                   "principal_ref": principal_ref_for_gateway(gateway_b)}]),
        ):
            if rules is not None:
                publish_policy(rules, "mcp-" + label)
            for index, path in enumerate(paths):
                request_id = f"mcp-denied-{label}-{index}"
                denied = exchange(local, "POST", path + "/candidate-manifests", request_id, body=intent, remote=True)
                assert denied.status_code == 403 and "mcp_installation_manage" in denied.text
                assert_native_only(request_id, reaches_backend=False)

        publish_policy([{"capability": "mcp_installation_manage", "effect": "allow", "principal_ref": principal}], "mcp-explicit")
        for index, path in enumerate(paths):
            inspected = exchange(local, "POST", path + "/candidate-manifests", f"mcp-inspect-{index}", body=intent, remote=True)
            assert inspected.status_code == 200, inspected.text
            manifest = inspected.json()["data"]
            assert inspected.json()["target"] == target
            candidate = {**intent, "manifest_digest": manifest["manifest_digest"], "variant": manifest["variants"][0]["ref"]}
            prepared = exchange(local, "POST", path + "/candidate-plans", f"mcp-prepare-{index}", body=candidate, remote=True)
            assert prepared.status_code == 200, prepared.text
            plan = prepared.json()["data"]
            assert plan["disposition"] == "configuration_only" and plan["download_required"] is False
            assert prepared.headers["Cache-Control"] == "no-store" and "command" not in plan["parameters"]
            validated = exchange(local, "POST", path + "/candidate-plans/validate", f"mcp-validate-{index}",
                                 body={"candidate": candidate, "plan_digest": plan["plan_digest"]}, remote=True)
            assert validated.status_code == 200 and validated.json()["data"] == plan
            wrong_target = {**candidate, "target": {**target, "backend_id": backend_a}}
            rejected = exchange(local, "POST", path + "/candidate-plans", f"mcp-wrong-backend-{index}", body=wrong_target, remote=True)
            assert rejected.status_code == 409, rejected.text
            for stage in ("inspect", "prepare", "validate", "wrong-backend"):
                assert_native_only(f"mcp-{stage}-{index}", reaches_backend=True)

        publish_policy([], "mcp-revoke")
        for index, path in enumerate(paths):
            request_id = f"mcp-revoked-{index}"
            denied = exchange(local, "POST", path + "/candidate-plans", request_id, body=intent, remote=True)
            assert denied.status_code == 403, denied.text
            assert_native_only(request_id, reaches_backend=False)
        assert tuple(path.read_bytes() for path in workspace_sources) == originals
        for index, client in enumerate(network.clients):
            servers = exchange(client, "GET", "/api/v1/mcp/servers", f"mcp-final-catalog-{index}")
            assert servers.status_code == 200 and servers.json()["data"] == []
    finally:
        (network.context.artifacts_dir / "mcp-candidate-target-http.json").write_text(
            json.dumps({"target": target, "backend_a": backend_a, "http": evidence}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
