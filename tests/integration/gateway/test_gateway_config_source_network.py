"""真实配对主体经 socket 操作原生 Gateway 来源；不替代客户端 Relay 验收。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import commentjson
import httpx

from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.ports import principal_ref_for_gateway
from tests.integration.gateway.test_target_gateway_routing import (
    TargetGatewayNetwork,
    _observations,
    target_gateway_network,
)

__all__ = ["target_gateway_network"]


def test_paired_gateway_principal_edits_only_native_gateway_source(
    target_gateway_network: TargetGatewayNetwork,
) -> None:
    network = target_gateway_network
    local, native = network.clients
    gateway_a, gateway_b = network.gateway_ids
    source_key = "gateway_mutable_override"
    source_path = f"/api/gateway/config/sources/{source_key}"
    originals = tuple(path.read_bytes() for path in network.gateway_config_paths)
    evidence: list[dict[str, object]] = []

    def exchange(
        client: httpx.Client, method: str, path: str, request_id: str, *,
        body: dict[str, object] | None = None,
    ) -> httpx.Response:
        response = client.request(method, path, json=body, headers={"X-Request-ID": request_id})
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        evidence.append({"request_id": request_id, "path": path, "status": response.status_code, "body": response.json()})
        return response

    def edit_request(document: dict[str, object], text: str, key: str) -> dict[str, object]:
        return {"gateway_id": gateway_b, "source_key": source_key, "jsonc": text,
                "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
                "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
                "expected_active_digest": document["active_digest"], "idempotency_key": key}

    def publish_policy(rules: list[dict[str, str]], label: str) -> None:
        # 权限也通过本机真实来源 writer 发布，不预填数据库或内存策略。
        current = exchange(native, "GET", source_path, f"{label}-read")
        assert current.status_code == 200, current.text
        document = current.json()["data"]
        config = commentjson.loads(document["jsonc"])
        config.setdefault("permissions", {})["federation"] = {"default_effect": "allow", "rules": rules}
        text = "// 原生来源网络验收\n" + json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        saved = exchange(native, "PUT", source_path, f"{label}-save", body=edit_request(document, text, label))
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["edit"]["state"] == "active"
        assert saved.json()["data"]["status"]["restart_required"] is False

    try:
        topology = json.loads((network.context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
        paired = exchange(local, "POST", "/api/gateway/remote-gateways", "gateway-source-pair", body={
            "name": "来源目标 B", "host": "127.0.0.1", "port": 22, "username": "source-integration",
            "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3],
        })
        assert paired.status_code == 200, paired.text
        projected, = [item for item in paired.json()["data"]["items"] if item["gateway_id"] == gateway_b]
        assert projected["workspace_id"] == network.workspace_id
        # 读取产品配对生成的原凭据；不伪造认证主体，不把秘密写入证据。
        credential_path = network.gateway_config_paths[1].parent.parent / "state/gateway/credentials/federation.json"
        credential, = [item for item in FederationCredentialStore(storage_path=credential_path).list_valid()
                        if item.peer_gateway_id == gateway_a]
        with httpx.Client(base_url=str(native.base_url), headers={
            "X-BoxTeam-Federation-Token": credential.token, "X-BoxTeam-Gateway-Id": gateway_a,
        }, timeout=25, trust_env=False) as peer:
            denied = exchange(peer, "GET", source_path, "gateway-source-default-denied")
            assert denied.status_code == 403, denied.text
            assert tuple(path.read_bytes() for path in network.gateway_config_paths) == originals
            publish_policy([{"capability": "configuration_read", "effect": "allow"}], "gateway-source-wildcard")
            wildcard = exchange(peer, "GET", source_path, "gateway-source-wildcard-denied")
            assert wildcard.status_code == 403, wildcard.text
            principal = principal_ref_for_gateway(gateway_a)
            read_rule = {"capability": "configuration_read", "effect": "allow", "principal_ref": principal}
            publish_policy([read_rule], "gateway-source-read-only")
            readable = exchange(peer, "GET", source_path, "gateway-source-readable")
            assert readable.status_code == 200, readable.text
            document = readable.json()["data"]
            assert (document["gateway_id"], document["source_key"], document["writable"]) == (gateway_b, source_key, True)
            request = edit_request(document, document["jsonc"], "gateway-source-denied-write")
            before = tuple(path.read_bytes() for path in network.gateway_config_paths)
            for method, suffix in (("POST", "/preflight"), ("PUT", "")):
                rejected = exchange(peer, method, source_path + suffix, f"gateway-source-read-only-{method}", body=request)
                assert rejected.status_code == 403, rejected.text
            assert tuple(path.read_bytes() for path in network.gateway_config_paths) == before

            publish_policy([read_rule, {"capability": "configuration_write", "effect": "allow", "principal_ref": principal}],
                           "gateway-source-read-write")
            baseline = exchange(peer, "GET", source_path, "gateway-source-baseline")
            assert baseline.status_code == 200, baseline.text
            document = baseline.json()["data"]
            config = commentjson.loads(document["jsonc"])
            config["permissions"]["federation"]["default_effect"] = "deny"
            text = "// 原生来源网络验收\n" + json.dumps(config, ensure_ascii=False, indent=2) + "\n"
            request = edit_request(document, text, "gateway-source-concurrent-save")
            before = tuple(path.read_bytes() for path in network.gateway_config_paths)
            preview = exchange(peer, "POST", source_path + "/preflight", "gateway-source-preview", body=request)
            assert preview.status_code == 200, preview.text
            assert preview.json()["data"]["candidate"]["permissions"]["federation"]["default_effect"] == "deny"
            assert preview.json()["data"]["changed_paths"] == ["/permissions/federation/default_effect"]
            assert tuple(path.read_bytes() for path in network.gateway_config_paths) == before
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(exchange, peer, "PUT", source_path, request_id, body=request)
                           for request_id in ("gateway-source-save", "gateway-source-concurrent")]
                responses = [future.result() for future in futures]
            assert all(response.status_code == 200 for response in responses), [response.text for response in responses]
            results = [response.json()["data"] for response in responses]
            completed = [result for result in results if result["edit"]["state"] == "active"]
            assert completed, results
            result = completed[0]
            assert all(item["edit"]["candidate_id"] == result["edit"]["candidate_id"] for item in results)
            assert result["effective"]["permissions"]["federation"]["default_effect"] == "deny"
            assert network.gateway_config_paths[0].read_bytes() == originals[0]
            assert network.gateway_config_paths[1].read_text(encoding="utf-8") == text
            retried = exchange(peer, "PUT", source_path, "gateway-source-retry", body=request)
            assert retried.status_code == 200 and retried.json()["data"]["edit"] == result["edit"], retried.text
            queried = exchange(peer, "GET", source_path + "?idempotency_key=gateway-source-concurrent-save", "gateway-source-query")
            assert queried.status_code == 200 and queried.json()["data"]["edit"] == result["edit"], queried.text
            stale = exchange(peer, "PUT", source_path, "gateway-source-stale", body={**request, "idempotency_key": "gateway-source-stale"})
            assert stale.status_code == 409, stale.text
            misbound = exchange(peer, "PUT", source_path, "gateway-source-misbound", body={
                **edit_request(result, text, "gateway-source-misbound"), "gateway_id": gateway_a,
            })
            assert misbound.status_code == 409, misbound.text
            events = exchange(peer, "GET", "/api/gateway/config/events", "gateway-source-events")
            assert events.status_code == 200, events.text
            accepted = [item for item in events.json()["data"]["events"] if item["idempotency_key"] == request["idempotency_key"]]
            assert len(accepted) == 1 and accepted[0]["result"] == "applied"
            assert accepted[0]["candidate_id"] == result["edit"]["candidate_id"]
            for request_id in ("gateway-source-preview", "gateway-source-save", "gateway-source-misbound"):
                observed, = _observations(network.observation_paths["gateway-b"], request_id)
                assert observed["sender_gateway_id"] == gateway_a
                assert _observations(network.observation_paths["gateway-a"], request_id) == []
                assert _observations(network.observation_paths["backend-a"], request_id) == []
                assert _observations(network.observation_paths["backend-b"], request_id) == []
            publish_policy([], "gateway-source-revoke")
            revoked = exchange(peer, "GET", source_path, "gateway-source-revoked")
            assert revoked.status_code == 403, revoked.text
            assert network.gateway_config_paths[0].read_bytes() == originals[0]
    finally:
        (network.context.artifacts_dir / "gateway-configuration-source-http.json").write_text(json.dumps({
            "boundary": "真实产品配对主体A直接HTTP访问原生Gateway B；不含客户端Relay或真实SSH传输",
            "exchanges": evidence,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
