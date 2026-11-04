"""经真实 Relay 编辑原生 Gateway 来源，目标和提交身份始终保持。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import commentjson
import httpx

from app.gateway.federation.ports import principal_ref_for_gateway
from tests.integration.gateway.test_target_gateway_routing import (
    TargetGatewayNetwork,
    _observations,
    target_gateway_network,
)

__all__ = ["target_gateway_network"]


def test_gateway_source_relay_keeps_native_target_and_original_submission(
    target_gateway_network: TargetGatewayNetwork,
) -> None:
    network = target_gateway_network
    relay, native = network.clients
    gateway_a, gateway_b = network.gateway_ids
    source_key = "gateway_mutable_override"
    source_path = f"/api/gateway/config/sources/{source_key}"
    originals = tuple(path.read_bytes() for path in network.gateway_config_paths)
    exchanges: list[dict[str, object]] = []

    def exchange(
        client: httpx.Client, method: str, path: str, request_id: str, *,
        body: dict[str, object] | None = None, target: str | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = {"X-Request-ID": request_id}
        if target is not None:
            headers["X-BoxTeam-Target-Gateway-Id"] = target
        response = client.request(method, path, json=body, headers=headers, params=params)
        payload = response.json()
        assert response.headers["X-Request-ID"] == payload["request_id"] == request_id, response.text
        exchanges.append({"method": method, "path": path, "target": target,
                          "request_id": request_id, "status": response.status_code, "body": payload})
        return response

    def edit_request(document: dict[str, object], text: str, key: str) -> dict[str, object]:
        return {"gateway_id": gateway_b, "source_key": source_key, "jsonc": text,
                "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
                "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
                "expected_active_digest": document["active_digest"], "idempotency_key": key}

    def grant(capabilities: tuple[str, ...], key: str) -> None:
        # 策略经 B 的原生 writer 生效，不替产品准备 SQLite 或运行态事实。
        current = exchange(native, "GET", source_path, f"{key}-read")
        assert current.status_code == 200, current.text
        document = current.json()["data"]
        config = commentjson.loads(document["jsonc"])
        config.setdefault("permissions", {})["federation"] = {
            "default_effect": "allow",
            "rules": [{"capability": capability, "effect": "allow",
                       "principal_ref": principal_ref_for_gateway(gateway_a)} for capability in capabilities],
        }
        text = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        saved = exchange(native, "PUT", source_path, f"{key}-save", body=edit_request(document, text, key))
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["edit"]["state"] == "active", saved.text

    try:
        topology = json.loads((network.context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
        paired = exchange(relay, "POST", "/api/gateway/remote-gateways", "source-relay-pair", body={
            "name": "来源目标 B", "host": "127.0.0.1", "port": 22, "username": "source-relay",
            "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3],
        })
        assert paired.status_code == 200, paired.text
        peer, = [item for item in paired.json()["data"]["items"] if item["gateway_id"] == gateway_b]
        assert peer["workspace_id"] == network.workspace_id
        connection, = peer["remote_gateways"]
        assert connection["gateway_id"] == gateway_b
        connection_id = connection["gateway_connection_id"]
        denied = exchange(relay, "GET", source_path, "source-relay-default-denied", target=gateway_b)
        assert denied.status_code == 403, denied.text
        assert tuple(path.read_bytes() for path in network.gateway_config_paths) == originals
        unsupported = exchange(relay, "GET", "/api/gateway/config/reload-status",
                               "source-relay-unsupported-status", target=gateway_b)
        assert unsupported.status_code == 409, unsupported.text
        assert _observations(network.observation_paths["gateway-b"], "source-relay-unsupported-status") == []

        grant(("configuration_read",), "source-relay-read-only")
        readable = exchange(relay, "GET", source_path, "source-relay-readable", target=gateway_b)
        assert readable.status_code == 200 and readable.json()["data"]["gateway_id"] == gateway_b, readable.text
        document = readable.json()["data"]
        rejected_body = edit_request(document, document["jsonc"], "source-relay-denied-write")
        before = tuple(path.read_bytes() for path in network.gateway_config_paths)
        for method, suffix in (("POST", "/preflight"), ("PUT", "")):
            denied = exchange(relay, method, source_path + suffix, f"source-relay-denied-{method}",
                              target=gateway_b, body=rejected_body)
            assert denied.status_code == 403, denied.text
        assert tuple(path.read_bytes() for path in network.gateway_config_paths) == before

        grant(("configuration_read", "configuration_write"), "source-relay-read-write")
        listing = exchange(relay, "GET", "/api/gateway/config/sources", "source-relay-list", target=gateway_b)
        assert listing.status_code == 200, listing.text
        assert source_key in {item["source_key"] for item in listing.json()["data"]["sources"]}
        baseline = exchange(relay, "GET", source_path, "source-relay-baseline", target=gateway_b)
        assert baseline.status_code == 200, baseline.text
        document = baseline.json()["data"]
        listed_source, = [item for item in listing.json()["data"]["sources"] if item["source_key"] == source_key]
        assert listed_source["layer_digest"] == document["layer_digest"]
        assert listed_source["layer_revision"] == document["layer_revision"]
        config = commentjson.loads(document["jsonc"])
        config["permissions"]["federation"]["default_effect"] = "deny"
        text = "// 通过 A 保存 B 的来源\n" + json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        request = edit_request(document, text, "source-relay-concurrent-save")
        before = tuple(path.read_bytes() for path in network.gateway_config_paths)
        preview = exchange(relay, "POST", source_path + "/preflight", "source-relay-preview",
                           target=gateway_b, body=request)
        assert preview.status_code == 200, preview.text
        assert preview.json()["data"]["document"]["gateway_id"] == gateway_b
        assert preview.json()["data"]["changed_paths"] == ["/permissions/federation/default_effect"]
        assert tuple(path.read_bytes() for path in network.gateway_config_paths) == before
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(exchange, relay, "PUT", source_path, request_id,
                                       target=gateway_b, body=request)
                       for request_id in ("source-relay-save", "source-relay-concurrent")]
            responses = [future.result() for future in futures]
        assert all(response.status_code == 200 for response in responses), [response.text for response in responses]
        query = {"idempotency_key": str(request["idempotency_key"]), "gateway_connection_id": connection_id}
        queried = exchange(relay, "GET", source_path, "source-relay-query", target=gateway_b, params=query)
        assert queried.status_code == 200, queried.text
        result = queried.json()["data"]
        assert result["gateway_id"] == gateway_b and result["edit"]["state"] == "active"
        assert all(response.json()["data"]["edit"]["candidate_id"] == result["edit"]["candidate_id"]
                   for response in responses)
        assert network.gateway_config_paths[0].read_bytes() == originals[0]
        assert network.gateway_config_paths[1].read_text(encoding="utf-8") == text
        retried = exchange(relay, "PUT", source_path, "source-relay-retry", target=gateway_b, body=request)
        assert retried.status_code == 200 and retried.json()["data"]["edit"] == result["edit"], retried.text
        stale = exchange(relay, "PUT", source_path, "source-relay-stale", target=gateway_b,
                         body={**request, "idempotency_key": "source-relay-stale"})
        assert stale.status_code == 409, stale.text
        misbound = exchange(relay, "PUT", source_path, "source-relay-misbound", target=gateway_b,
                            body={**edit_request(result, text, "source-relay-misbound"), "gateway_id": gateway_a})
        assert misbound.status_code == 409, misbound.text
        events = exchange(native, "GET", "/api/gateway/config/events", "source-relay-native-events")
        assert events.status_code == 200, events.text
        accepted = [item for item in events.json()["data"]["events"]
                    if item["idempotency_key"] == request["idempotency_key"]]
        assert len(accepted) == 1 and accepted[0]["result"] == "applied"
        assert accepted[0]["candidate_id"] == result["edit"]["candidate_id"]
        for request_id in ("source-relay-list", "source-relay-preview", "source-relay-save", "source-relay-stale"):
            local_observation, = _observations(network.observation_paths["gateway-a"], request_id)
            remote_observation, = _observations(network.observation_paths["gateway-b"], request_id)
            assert local_observation["sender_gateway_id"] is None
            assert local_observation["target_gateway_id"] == gateway_b
            assert remote_observation["sender_gateway_id"] == gateway_a
            assert remote_observation["target_gateway_id"] == gateway_b
            assert _observations(network.observation_paths["backend-a"], request_id) == []
            assert _observations(network.observation_paths["backend-b"], request_id) == []
        grant((), "source-relay-revoke")
        revoked = exchange(relay, "GET", source_path, "source-relay-revoked", target=gateway_b)
        assert revoked.status_code == 403, revoked.text
        assert network.gateway_config_paths[0].read_bytes() == originals[0]
    finally:
        (network.context.artifacts_dir / "gateway-config-source-relay-http.json").write_text(json.dumps({
            "boundary": "真实配对与双 Gateway socket Relay；SSH 隧道沿原 loopback 替身，不含 UI 或真实 SSH",
            "exchanges": exchanges,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
