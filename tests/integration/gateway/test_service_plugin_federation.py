"""真实双 Gateway 的原实例转发及撤权；SSH 执行和隧道使用显式 loopback 替身。"""

from __future__ import annotations

import concurrent.futures
import gzip
import json
from collections.abc import Callable

import commentjson
import httpx
import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from tests.integration.gateway.test_service_plugin_streams import install_stream_plugin
from tests.integration.gateway.test_target_gateway_routing import TargetGatewayNetwork

pytest_plugins = ["tests.integration.gateway.test_target_gateway_routing"]


@pytest.fixture
def target_gateway_port(unused_tcp_port_factory: Callable[[], int]) -> Callable[[int], int]:
    # 复用双 Gateway 装具；独立端口由 pytest 分配并交原 E2EProcessLease 核验。
    ports = {offset: unused_tcp_port_factory() for offset in (0, 1, 20, 21, 22, 2)}
    return ports.__getitem__


def test_federated_services_use_native_instance_and_revoke_idle_streams(
    target_gateway_network: TargetGatewayNetwork,
) -> None:
    network = target_gateway_network
    local, native = network.clients
    gateway_a, gateway_b = network.gateway_ids
    slot = install_stream_plugin(native, network.context)
    assert slot.instance is not None
    instance_id = slot.instance.instance_id
    topology = json.loads((network.context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    registration = local.post("/api/gateway/remote-gateways", json={
        "name": "插件原生目标 B", "host": "127.0.0.1", "port": 22, "username": "plugin-integration",
        "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3],
    }).raise_for_status().json()["data"]
    projected, = [item for item in registration["items"] if item["gateway_id"] == gateway_b]
    connection_id = projected["remote_gateways"][0]["gateway_connection_id"]
    headers = {TARGET_GATEWAY_ID_HEADER: gateway_b, "X-BoxTeam-Gateway-Connection-Id": connection_id}
    base = f"/api/gateway/service-plugins/stream.probe/streams/instances/{instance_id}/services"
    path = base + "/events/compressed?package_version=1.0.0"
    payload = '联邦压缩正文🙂'.encode()
    for client, label, target_headers in ((native, "native", {}), (local, "relay", headers)):
        request_id = "plugin-compressed-" + label
        response = client.post(path, content=gzip.compress(payload),
            headers={**target_headers, "Content-Encoding": "gzip", "X-Request-ID": request_id}).raise_for_status()
        assert response.content == payload
        assert response.headers["X-Request-ID"] == request_id
        assert not {"x-local-token", "x-boxteam-federation-token", "x-boxteam-target-gateway-id",
            "x-boxteam-gateway-connection-id", "set-cookie", "content-encoding"} & set(response.headers)
    # 普通业务只核 send/read；安装和运行管理权限未授权，不能借服务访问获得管理权。
    for operation in ("inventory", "status"):
        denied = local.post("/api/gateway/service-plugins/" + operation, json={"gateway_id": gateway_b})
        assert denied.status_code == 403, denied.text
    assert local.post("/api/gateway/service-plugins/inventory", json={"gateway_id": gateway_a}).raise_for_status().json()["data"]["packages"] == []
    # 复用原配对产生的凭据模拟联邦入站；它不能把 B 再当成到 A 的中继。
    credential = FederationCredentialStore(storage_path=network.gateway_homes[0] /
        "state/gateway/credentials/federation.json").get(connection_id)
    nested_headers = {"X-BoxTeam-Federation-Token": credential.token, TARGET_GATEWAY_ID_HEADER: gateway_a}
    with httpx.Client(base_url=native.base_url, timeout=10, trust_env=False) as inbound:
        nested = inbound.post(path, headers=nested_headers, content=b"nested-relay")
        assert nested.status_code == 403 and "Relay" in nested.text
    nested_url = str(native.base_url).replace("http:", "ws:").rstrip("/") + base + "/socket/echo?package_version=1.0.0"
    with pytest.raises(InvalidStatus) as nested_handshake:
        connect(nested_url, additional_headers=nested_headers, open_timeout=5)
    assert nested_handshake.value.response.status_code == 403

    def revoke() -> None:
        source_path = "/api/gateway/config/sources/gateway_mutable_override"
        source = native.get(source_path).raise_for_status().json()["data"]
        document = commentjson.loads(source["jsonc"])
        document.setdefault("permissions", {})["federation"] = {"default_effect": "allow", "rules": [{
            "capability": "read", "effect": "deny", "principal_ref": principal_ref_for_gateway(gateway_a)}]}
        updated = native.put(source_path, json={"gateway_id": gateway_b, "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(document), "base_file_digest": source["file_digest"],
            "base_layer_revision": source["layer_revision"], "base_layer_digest": source["layer_digest"],
            "expected_active_revision": source["active_revision"], "expected_active_digest": source["active_digest"],
            "idempotency_key": "revoke-federated-plugin-read"}).raise_for_status()
        assert updated.json()["data"]["edit"]["state"] == "active"

    socket_url = str(local.base_url).replace("http:", "ws:").rstrip("/") + base + "/socket/echo?package_version=1.0.0&marker=relay"
    socket_headers = {**headers, "X-Local-Token": local.headers["X-Local-Token"], "X-Request-ID": "plugin-relay-stream"}
    with connect(socket_url, additional_headers=socket_headers, open_timeout=10, close_timeout=3) as socket:
        receipt = json.loads(socket.recv(timeout=5))
        assert receipt == {"instance_id": instance_id, "request_id": "plugin-relay-stream", "query": "marker=relay"}
        for message in ("远端文本🙂", b"\x00\xff\x01"):
            socket.send(message)
            assert socket.recv(timeout=5) == message
        with local.stream("GET", base + "/events/stream?package_version=1.0.0",
            headers={**headers, "X-Request-ID": "plugin-relay-sse"}, timeout=15) as response:
            response.raise_for_status()
            lines = response.iter_lines()
            assert json.loads(next(lines).removeprefix("data: ")) == {
                "instance_id": instance_id, "request_id": "plugin-relay-sse"}
            assert next(lines) == ""
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                publication = pool.submit(revoke)
                with pytest.raises(ConnectionClosed):
                    socket.recv(timeout=10)
                with pytest.raises(StopIteration):
                    next(lines)
                publication.result(timeout=20)
    denied = local.post(path, headers=headers, content=b"revoked")
    assert denied.status_code == 403 and "read" in denied.text
    with pytest.raises(InvalidStatus) as handshake:
        connect(socket_url, additional_headers=socket_headers, open_timeout=5)
    assert handshake.value.response.status_code == 403
    assert "read" in json.loads(handshake.value.response.body)["detail"]
    current, = native.post("/api/gateway/service-plugins/status", json={"gateway_id": gateway_b}).raise_for_status().json()["data"]["plugins"]
    assert current["state"] == "ready" and current["instance"]["instance_id"] == instance_id
    (network.context.artifacts_dir / "federated-plugin-streams.json").write_text(json.dumps({
        "gateway_ids": [gateway_a, gateway_b], "instance_id": instance_id,
        "websocket": receipt, "revoked_http_status": denied.status_code,
        "revoked_handshake_status": handshake.value.response.status_code,
        "native_instance_unchanged": True, "local_installation_empty": True,
        "transport_boundary": topology["transport_boundary"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
