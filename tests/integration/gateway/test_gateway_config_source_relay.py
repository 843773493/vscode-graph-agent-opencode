"""经真实 Relay 编辑原生 Gateway 来源，目标和提交身份始终保持。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import commentjson
import httpx
from httpx_sse import ServerSentEvent, connect_sse

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
    stream_events: list[dict[str, object]] = []

    def exchange(
        client: httpx.Client, method: str, path: str, request_id: str, *,
        body: dict[str, object] | None = None, target: str | None = None,
        params: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = {**(extra_headers or {}), "X-Request-ID": request_id}
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
        denied_status = exchange(relay, "GET", "/api/gateway/config/reload-status",
                                 "source-relay-denied-status", target=gateway_b)
        assert denied_status.status_code == 403, denied_status.text

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
        denied_retry = exchange(relay, "POST", "/api/gateway/config/retry-restart",
                                "source-relay-denied-retry", target=gateway_b,
                                params={"candidate_ref": "not-issued"})
        assert denied_retry.status_code == 403, denied_retry.text
        for suffix in ("reload-status", "events"):
            allowed = exchange(relay, "GET", "/api/gateway/config/" + suffix,
                               "source-relay-read-only-" + suffix, target=gateway_b)
            assert allowed.status_code == 200, allowed.text
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
        remote_events = exchange(relay, "GET", "/api/gateway/config/events",
                                 "source-relay-events", target=gateway_b)
        assert remote_events.status_code == 200, remote_events.text
        assert remote_events.json()["data"] == events.json()["data"]
        remote_status = exchange(relay, "GET", "/api/gateway/config/reload-status",
                                 "source-relay-status", target=gateway_b)
        assert remote_status.status_code == 200, remote_status.text
        assert remote_status.json()["data"]["active_revision"] == result["active_revision"]
        last_event = remote_events.json()["data"]["events"][-1]
        cursor = str(last_event["event_seq"] - 1)
        for value, params, expected in (("not-a-cursor", None, 400), (cursor, {"after": str(int(cursor) + 1)}, 409)):
            invalid = exchange(relay, "GET", "/api/gateway/config/events/stream",
                               "source-relay-stream-invalid-" + str(expected), target=gateway_b,
                               params=params, extra_headers={"Last-Event-ID": value})
            assert invalid.status_code == expected, invalid.text
        with connect_sse(relay, "GET", "/api/gateway/config/events/stream", headers={
            "X-Request-ID": "source-relay-stream",
            "X-BoxTeam-Target-Gateway-Id": gateway_b,
            "Last-Event-ID": cursor,
        }) as stream:
            assert stream.response.status_code == 200
            assert stream.response.headers["X-Request-ID"] == "source-relay-stream"
            records = stream.iter_sse()
            replayed = next(records)
            assert replayed.event == "config" and replayed.id == str(last_event["event_seq"])
            assert replayed.json() == last_event
            stream_events.append({"event": replayed.event, "id": replayed.id, "data": replayed.json()})
            grant((), "source-relay-revoke")

            def next_revocation_event() -> ServerSentEvent:
                for event in records:
                    # 官方解码器会把带 sticky id 的心跳空帧派发成空 message。
                    if event.data == "" and event.event == "message" and event.id == replayed.id:
                        continue
                    return event
                raise AssertionError("撤权后未收到原生配置错误事件")

            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(next_revocation_event)
            try:
                revoked_event = future.result(timeout=10)
            finally:
                if not future.done():
                    stream.response.close()
                executor.shutdown(wait=False, cancel_futures=True)
            assert revoked_event.event == "error" and "configuration_read" in revoked_event.json()["message"]
            stream_events.append({"event": revoked_event.event, "id": revoked_event.id, "data": revoked_event.json()})
            assert next(records, None) is None
        for suffix in ("sources/" + source_key, "reload-status", "events"):
            revoked = exchange(relay, "GET", "/api/gateway/config/" + suffix,
                               "source-relay-revoked-" + suffix.replace("/", "-"), target=gateway_b)
            assert revoked.status_code == 403, revoked.text

        grant(("configuration_read", "configuration_write"), "source-relay-pending-access")
        before_pending = exchange(relay, "GET", source_path, "source-relay-pending-baseline", target=gateway_b)
        assert before_pending.status_code == 200, before_pending.text
        document = before_pending.json()["data"]
        local_status = exchange(relay, "GET", "/api/gateway/config/reload-status", "source-relay-local-baseline")
        pending_config = commentjson.loads(document["jsonc"])
        pending_config.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("process", {}).setdefault("health", {})[
            "request_timeout_seconds"
        ] = 3
        pending_text = json.dumps(pending_config, ensure_ascii=False, indent=2) + "\n"
        pending = exchange(relay, "PUT", source_path, "source-relay-pending-save", target=gateway_b,
                           body=edit_request(document, pending_text, "source-relay-pending-save"))
        assert pending.status_code == 200, pending.text
        pending_document = pending.json()["data"]
        assert pending_document["edit"]["state"] == "pending_restart"
        assert pending_document["active_revision"] == document["active_revision"]
        reference = {"candidate_ref": pending_document["status"]["candidate_ref"]}
        # 新 pending 尚未失败或到期，retry 仍按原生状态机拒绝，不能借 Relay 伪造重启。
        premature = exchange(relay, "POST", "/api/gateway/config/retry-restart",
                             "source-relay-premature-retry", target=gateway_b, params=reference)
        assert premature.status_code == 409 and "retry" in premature.text, premature.text
        false_proof = {
            "candidate_ref": reference["candidate_ref"], "candidate_id": pending_document["edit"]["candidate_id"],
            "generation": "not-started", "loaded_source": "pending", "gateway_id": gateway_b,
            "active_revision": document["active_revision"], "pending_revision": pending_document["status"]["pending_revision"],
            **{key: "unverified" for key in ("candidate_digest", "effective_digest", "secret_binding_digest",
                                            "fencing_token_digest", "health_digest")},
        }
        unresolved = exchange(relay, "POST", "/api/gateway/config/resolve-restart",
                              "source-relay-unverified-proof", target=gateway_b, params=reference, body=false_proof)
        assert unresolved.status_code == 409, unresolved.text
        discard_body = {"expected_active_revision": document["active_revision"],
                        "expected_active_digest": document["active_digest"]}
        stale_discard = exchange(relay, "POST", "/api/gateway/config/discard-restart",
                                 "source-relay-stale-discard", target=gateway_b, params=reference,
                                 body={**discard_body, "expected_active_revision": document["active_revision"] + 1})
        assert stale_discard.status_code == 409, stale_discard.text
        discarded = exchange(relay, "POST", "/api/gateway/config/discard-restart",
                             "source-relay-discard", target=gateway_b, params=reference, body=discard_body)
        assert discarded.status_code == 200, discarded.text
        assert discarded.json()["data"]["state"] == "discarded"
        assert discarded.json()["data"]["candidate_ref"] is None
        assert discarded.json()["data"]["restart_required"] is False
        assert discarded.json()["data"]["pending_revision"] == pending_document["status"]["pending_revision"]
        assert discarded.json()["data"]["active_revision"] == document["active_revision"]
        local_after = exchange(relay, "GET", "/api/gateway/config/reload-status", "source-relay-local-after")
        assert local_after.json()["data"] == local_status.json()["data"]
        for request_id in ("source-relay-list", "source-relay-preview", "source-relay-save", "source-relay-stale",
                           "source-relay-status", "source-relay-events", "source-relay-stream",
                           "source-relay-premature-retry", "source-relay-unverified-proof", "source-relay-discard"):
            local_observation, = _observations(network.observation_paths["gateway-a"], request_id)
            remote_observation, = _observations(network.observation_paths["gateway-b"], request_id)
            assert local_observation["sender_gateway_id"] is None
            assert local_observation["target_gateway_id"] == gateway_b
            assert remote_observation["sender_gateway_id"] == gateway_a
            assert remote_observation["target_gateway_id"] == gateway_b
            assert _observations(network.observation_paths["backend-a"], request_id) == []
            assert _observations(network.observation_paths["backend-b"], request_id) == []
        assert network.gateway_config_paths[0].read_bytes() == originals[0]
    finally:
        (network.context.artifacts_dir / "gateway-config-source-relay-http.json").write_text(json.dumps({
            "boundary": "真实配对与双 Gateway socket Relay；SSH 隧道沿原 loopback 替身，不含 UI 或真实 SSH",
            "exchanges": exchanges,
            "stream_events": stream_events,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
