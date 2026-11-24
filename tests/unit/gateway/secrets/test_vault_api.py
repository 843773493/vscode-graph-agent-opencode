from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import get_gateway_local_token
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.main import gateway_http_exception_handler
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes.secrets import router
from app.gateway.runtime.external_http import GatewayExternalHTTPTransport
from app.gateway.secrets.vault import SecretConflictError, SecretVault
from app.protocol.codecs.http_validation import request_validation_error_response

GATEWAY_ID = "gateway_00000000000040008000000000000001"
SECRET_ID = "sec_00000000000040008000000000000001"
OTHER_SECRET_ID = "sec_00000000000040008000000000000002"


@pytest.fixture
def vault(tmp_path: Path) -> SecretVault:
    return SecretVault(tmp_path / "credentials" / "secrets")


@pytest.fixture
def gateway_app(vault: SecretVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)
    app = FastAPI()
    app.add_middleware(TraceMiddleware, component="gateway")
    app.include_router(router)
    app.add_exception_handler(HTTPException, gateway_http_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_response)
    app.state.secret_vault = vault
    app.state.external_http_transport = GatewayExternalHTTPTransport(vault)
    app.state.gateway_id = GATEWAY_ID
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", gateway_id=GATEWAY_ID)
    app.state.registry = registry
    try:
        yield app
    finally:
        registry.close()


@pytest.fixture
async def client(gateway_app: FastAPI):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(gateway_app, client=("127.0.0.1", 50000)), base_url="http://gateway",
        headers={"X-Local-Token": get_gateway_local_token(), "X-Request-ID": "req_named_secrets"}) as client:
        yield client


def test_encrypted_roundtrip_and_revision_conflict(vault: SecretVault) -> None:
    created = vault.create(secret_id=SECRET_ID, name="共享 Key", value="test-secret-value")
    assert created.id == SECRET_ID
    raw = (vault.directory / "vault.json").read_text()
    assert "test-secret-value" not in raw
    assert "test-secret-value" not in repr(vault.resolve([created.id]))
    restarted = SecretVault(vault.directory)
    assert restarted.resolve([created.id])[created.id].value.get_secret_value() == "test-secret-value"
    renamed = restarted.update(created.id, expected_revision=1, name="重命名", value=None)
    assert renamed[0].id == created.id and renamed[0].revision == 2
    assert restarted.resolve([created.id])[created.id].value.get_secret_value() == "test-secret-value"
    with pytest.raises(SecretConflictError):
        restarted.update(created.id, expected_revision=1, name="陈旧修改", value="wrong")
    assert restarted.list() == renamed
    updated = restarted.update(created.id, expected_revision=2, name="重命名", value="rotated")
    assert updated[0].revision == 3
    assert restarted.resolve([created.id])[created.id].value.get_secret_value() == "rotated"
    assert restarted.delete(created.id, expected_revision=3) == []
    with pytest.raises(KeyError):
        restarted.resolve([created.id])


def test_empty_list_has_no_storage_side_effect(vault: SecretVault) -> None:
    assert vault.list() == []
    assert not vault.directory.exists()


def test_missing_master_key_never_rebuilds_existing_vault(vault: SecretVault) -> None:
    item = vault.create(secret_id=SECRET_ID, name="key", value="before")
    raw = (vault.directory / "vault.json").read_bytes()
    (vault.directory / "master.key").unlink()
    with pytest.raises(RuntimeError, match="主密钥缺失"):
        vault.resolve([item.id])
    with pytest.raises(RuntimeError, match="主密钥缺失"):
        vault.create(secret_id=OTHER_SECRET_ID, name="other", value="after")
    assert (vault.directory / "vault.json").read_bytes() == raw
    assert not (vault.directory / "master.key").exists()


def test_corrupt_vault_is_not_replaced(vault: SecretVault) -> None:
    vault.create(secret_id=SECRET_ID, name="key", value="before")
    payload = json.loads((vault.directory / "vault.json").read_text())
    payload["version"] = 999
    (vault.directory / "vault.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        vault.list()
    assert json.loads((vault.directory / "vault.json").read_text())["version"] == 999


async def test_http_management_never_returns_value_and_internal_resolution_is_private(client: httpx.AsyncClient) -> None:
    listed = await client.get("/api/gateway/secrets")
    assert listed.status_code == 200 and listed.json()["data"]["items"] == []
    created = await client.post("/api/gateway/secrets", json={"id": SECRET_ID, "name": "Shared", "value": "never-in-metadata"})
    assert created.status_code == 200
    assert "never-in-metadata" not in created.text and "ciphertext" not in created.text
    item = created.json()["data"]["item"]
    assert item["id"] == SECRET_ID
    assert created.json()["data"]["gateway_id"] == GATEWAY_ID
    assert created.headers["X-Request-ID"] == created.json()["request_id"]
    path = "/api/gateway/internal/secrets/resolve"
    resolved = await client.post(path, json={"ids": [item["id"], item["id"]]})
    assert resolved.status_code == 200 and resolved.headers["cache-control"] == "no-store"
    assert resolved.json()["data"]["items"][item["id"]] == {"value": "never-in-metadata", "revision": 1}
    for headers in ({"Origin": "http://gateway"}, {"Sec-Fetch-Site": "same-origin"}):
        rejected = await client.post(path, json={"ids": [item["id"]]}, headers=headers)
        assert rejected.status_code == 403 and "never-in-metadata" not in rejected.text
    assert (await client.get("/api/gateway/secrets/" + item["id"])).status_code == 405
    renamed = await client.put("/api/gateway/secrets/" + item["id"], json={"name": "Renamed", "expected_revision": 1})
    assert renamed.status_code == 200 and renamed.json()["data"]["items"][0]["revision"] == 2
    conflict = await client.delete("/api/gateway/secrets/" + item["id"], params={"expected_revision": 1})
    assert conflict.status_code == 409
    deleted = await client.delete("/api/gateway/secrets/" + item["id"], params={"expected_revision": 2})
    assert deleted.status_code == 200 and deleted.json()["data"]["items"] == []
    assert (await client.post(path, json={"ids": [item["id"]]})).status_code == 404


async def test_secret_request_validation_never_echoes_input(client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture) -> None:
    response = await client.post("/api/gateway/secrets", json={"id": SECRET_ID, "name": "", "value": "never-log-this-value"})
    assert response.status_code == 422
    assert "never-log-this-value" not in response.text + caplog.text
    rejected = await client.get("/api/gateway/secrets", headers={"X-Local-Token": "wrong"})
    assert rejected.status_code == 401
    rejected = await client.get("/api/gateway/secrets", params={"gateway_connection_id": "not-local"})
    assert rejected.status_code == 409


@pytest.mark.parametrize("fault", [None, "route_replaced", "invalid_receipt", "wrong_owner", "request_id"])
async def test_remote_management_routes_original_secret_without_writing_local_vault(
    client: httpx.AsyncClient, gateway_app: FastAPI, vault: SecretVault, fault: str | None,
) -> None:
    from app.gateway.registry import WorkspaceRuntime

    remote_id = "gateway_" + "2" * 32
    registry = gateway_app.state.registry
    connection = RemoteGatewayConnection(
        connection_id="rgw_secrets", name="远端", host="remote.test", port=22, username="unit",
        private_key_path=None, ssh_config_host=None, remote_gateway_port=8014, remote_gateway_id=remote_id,
        protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual", remote_config_event_cursor=1,
    )
    registry.apply_remote_projection_snapshot(connection=connection, projections=(),
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://remote.test"}))
    credential = FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").issue(
        connection_id="rgw_secrets", peer_gateway_id=GATEWAY_ID,
    )
    requests: list[httpx.Request] = []
    item = {"id": "sec_" + "3" * 32, "name": "远端共享", "revision": 1, "created_at": "now", "updated_at": "now"}

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if fault == "route_replaced":
            registry.apply_remote_projection_snapshot(connection=replace(connection, remote_config_event_cursor=2),
                projections=(), runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://replacement.test"}))
        if fault == "invalid_receipt":
            return httpx.Response(200, headers={"X-Request-ID": request.headers["X-Request-ID"]},
                json={"request_id": request.headers["X-Request-ID"], "data": {}})
        return httpx.Response(200, headers={"X-Request-ID": request.headers["X-Request-ID"]}, json={
            "request_id": "req_other" if fault == "request_id" else request.headers["X-Request-ID"],
            "data": {"gateway_id": GATEWAY_ID if fault == "wrong_owner" else remote_id, "item": item},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as outgoing:
        gateway_app.state.http_client = outgoing
        created = await client.post("/api/gateway/secrets?gateway_connection_id=rgw_secrets",
            headers={"X-BoxTeam-Target-Gateway-Id": remote_id}, json={"id": item["id"], "name": "远端共享", "value": "remote-value"})
    assert created.status_code == (502 if fault else 200), created.text
    if fault is None:
        assert created.json()["data"] == {"gateway_id": remote_id, "item": item}
    assert "remote-value" not in created.text
    request, = requests
    assert str(request.url) == "http://remote.test/api/gateway/secrets"
    assert json.loads(request.content) == {"id": item["id"], "name": "远端共享", "value": "remote-value"}
    assert request.headers["X-BoxTeam-Federation-Token"] == credential.token
    assert request.headers["X-BoxTeam-Target-Gateway-Id"] == remote_id
    assert request.headers["X-Request-ID"] == created.json()["request_id"]
    assert vault.list() == [] and not vault.directory.exists()


async def test_client_id_is_required_and_concurrent_duplicate_never_replaces_original(
    client: httpx.AsyncClient, vault: SecretVault,
) -> None:
    for invalid_id in (None, "invalid-id"):
        payload = {"name": "Key", "value": "request-only-secret"}
        if invalid_id is not None:
            payload["id"] = invalid_id
        response = await client.post("/api/gateway/secrets", json=payload)
        assert response.status_code == 422 and "request-only-secret" not in response.text
    assert not vault.directory.exists()
    responses = await asyncio.gather(*(client.post("/api/gateway/secrets", json={
        "id": SECRET_ID, "name": name, "value": value,
    }) for name, value in (("first", "first-secret"), ("second", "second-secret"))))
    assert sorted(response.status_code for response in responses) == [200, 409]
    created, = [response.json()["data"]["item"] for response in responses if response.status_code == 200]
    assert created["id"] == SECRET_ID and created["revision"] == 1
    assert len(vault.list()) == 1
    original = (vault.directory / "vault.json").read_bytes()
    rejected = await client.post("/api/gateway/secrets", json={"id": SECRET_ID, "name": "replacement", "value": "wrong"})
    assert rejected.status_code == 409
    assert (vault.directory / "vault.json").read_bytes() == original
    # 创建回执遗失时，重启后的现有元数据入口足以按预定 ID 找回资源。
    listed = await client.get("/api/gateway/secrets")
    assert listed.json()["data"]["items"] == [created]
    assert [item.model_dump() for item in SecretVault(vault.directory).list()] == [created]
