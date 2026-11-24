"""真实 HTTP 出口和跨配置代次的响应排空，内部控制客户端仍直连。"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.gateway.runtime.consumer_protocol import GatewayRuntimeConsumerTransaction
from app.gateway.runtime.external_http import GatewayExternalHTTPTransport
from app.gateway.secrets.vault import SecretVault

SECRET_ID = "sec_" + "1" * 32


@pytest.fixture
async def endpoint():
    import asyncio

    requests: list[tuple[str, str | None]] = []
    writers: set[asyncio.StreamWriter] = set()

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.add(writer)
        try:
            data = await reader.readuntil(b"\r\n\r\n")
            lines = data.decode().split("\r\n")
            auth = next((line.partition(":")[2].strip() for line in lines if line.lower().startswith("proxy-authorization:")), None)
            requests.append((lines[0], auth))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            writers.remove(writer)

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", requests
    finally:
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
            await writer.wait_closed()


@pytest.fixture
def vault(tmp_path: Path) -> SecretVault:
    return SecretVault(tmp_path / "secrets")


@pytest.fixture
async def owner(vault: SecretVault) -> AsyncIterator[GatewayExternalHTTPTransport]:
    transport = GatewayExternalHTTPTransport(vault)
    try:
        yield transport
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_explicit_proxy_and_internal_control_use_different_routes(owner, endpoint, monkeypatch):
    url, requests = endpoint
    monkeypatch.setenv("ALL_PROXY", "http://unreachable.invalid:1")
    monkeypatch.setenv("NO_PROXY", "*")
    await owner.start({"proxy": {"mode": "explicit", "url": url}})
    async with httpx.AsyncClient(transport=owner, trust_env=False) as external:
        assert (await external.get("http://external.invalid/catalog", timeout=2)).text == "ok"
        async with httpx.AsyncClient(trust_env=False) as internal:
            assert (await internal.get(url + "/control", timeout=2)).text == "ok"
    assert requests == [("GET http://external.invalid/catalog HTTP/1.1", None), ("GET /control HTTP/1.1", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [False, True])
async def test_switch_keeps_old_response_and_transaction_pool_until_settlement(owner, endpoint, rollback):
    url, requests = endpoint
    await owner.start({"proxy": {"mode": "direct"}})
    previous = owner._active
    async with httpx.AsyncClient(transport=owner, trust_env=False) as client:
        response = await client.send(client.build_request("GET", url + "/old", timeout=2), stream=True)
        result = await GatewayRuntimeConsumerTransaction((owner.stage(
            payload={"proxy": {"mode": "explicit", "url": url}}, generation="test-switch",
            fencing_token_digest=None, fence_check=None,
        ),)).apply()
        candidate = owner._active
        assert candidate is not previous
        assert (await client.get("http://external.invalid/new", timeout=2)).text == "ok"
        await response.aclose()
        assert not previous.client.is_closed
        if rollback:
            await result.rollback()
        else:
            await result.promote()
        await result.finalize()
        assert (owner._active is previous) == rollback
        assert candidate.client.is_closed == rollback
        assert previous.client.is_closed != rollback
    assert [line for line, _ in requests] == ["GET /old HTTP/1.1", "GET http://external.invalid/new HTTP/1.1"]


@pytest.mark.asyncio
async def test_same_effective_route_reuses_pool(owner, endpoint):
    url, _ = endpoint
    payload = {"proxy": {"mode": "explicit", "url": url}}
    await owner.start(payload)
    previous = owner._active
    result = await GatewayRuntimeConsumerTransaction((owner.stage(
        payload=payload, generation="unchanged", fencing_token_digest=None, fence_check=None,
    ),)).apply()
    await result.promote()
    await result.finalize()
    assert owner._active is previous
    assert len(owner._pools) == 1
    assert previous.readers == 0
    assert not previous.client.is_closed


@pytest.mark.asyncio
async def test_secret_rotation_changes_next_call_and_deletion_never_reuses_old_password(owner, vault, endpoint):
    url, requests = endpoint
    vault.create(secret_id=SECRET_ID, name="proxy", value="first")
    await owner.start({"proxy": {"mode": "explicit", "url": url,
        "auth": {"username": "user", "password": "secret:" + SECRET_ID}}})
    previous = owner._active
    async with httpx.AsyncClient(transport=owner, trust_env=False) as client:
        held = await client.send(client.build_request("GET", "http://external.invalid/first", timeout=2), stream=True)
        vault.update(SECRET_ID, expected_revision=1, name="proxy", value="second")
        owner.invalidate_secret(SECRET_ID)
        assert (await client.get("http://external.invalid/second", timeout=2)).text == "ok"
        assert owner._active.secret_revision == 2
        assert not previous.client.is_closed
        await held.aclose()
        assert previous.client.is_closed
        vault.delete(SECRET_ID, expected_revision=2)
        owner.invalidate_secret(SECRET_ID)
        with pytest.raises(KeyError):
            await client.get("http://external.invalid/deleted", timeout=2)
    assert [auth for _, auth in requests] == ["Basic " + base64.b64encode(value).decode() for value in (b"user:first", b"user:second")]


@pytest.mark.asyncio
async def test_failed_proxy_never_falls_back_to_direct(owner, endpoint):
    import socket

    url, requests = endpoint
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        await owner.start({"proxy": {"mode": "explicit", "url": f"http://127.0.0.1:{unavailable.getsockname()[1]}"}})
        async with httpx.AsyncClient(transport=owner, trust_env=False) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(url + "/reachable", timeout=0.5)
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [False, True])
async def test_secret_rotation_of_prepared_candidate_affects_first_call(owner, vault, endpoint, rollback):
    import asyncio

    url, requests = endpoint
    vault.create(secret_id=SECRET_ID, name="proxy", value="first")
    await owner.start({"proxy": {"mode": "direct"}})
    stage = owner.stage(payload={"proxy": {"mode": "explicit", "url": url,
        "auth": {"username": "user", "password": "secret:" + SECRET_ID}}},
        generation="prepared", fencing_token_digest=None, fence_check=None)
    await stage.prepare()
    vault.update(SECRET_ID, expected_revision=1, name="proxy", value="rotated")
    owner.invalidate_secret(SECRET_ID)
    stage.apply()
    async with httpx.AsyncClient(transport=owner, trust_env=False) as client:
        pending = asyncio.create_task(client.get(url + "/rotated", timeout=2))
        await asyncio.sleep(0)
        assert not pending.done()
        assert requests == []
        assert stage.health().details["secret_revision"] == 1
        if rollback:
            stage.rollback()
        await stage.finalize()
        assert (await pending).text == "ok"
        assert len(owner._pools) == 1
        assert owner._active.settings.proxy.mode == ("direct" if rollback else "explicit")
    expected = [("GET /rotated HTTP/1.1", None)] if rollback else [
        (f"GET {url}/rotated HTTP/1.1", "Basic dXNlcjpyb3RhdGVk")]
    assert requests == expected


@pytest.mark.asyncio
async def test_failed_candidate_preparation_releases_previous_pool_and_switch_lock(owner, endpoint):
    from app.gateway.runtime.consumer_protocol import GatewayRuntimeConsumerApplyError

    url, requests = endpoint
    await owner.start({"proxy": {"mode": "direct"}})
    previous = owner._active
    with pytest.raises(GatewayRuntimeConsumerApplyError) as failure:
        await GatewayRuntimeConsumerTransaction((owner.stage(
            payload={"proxy": {"mode": "explicit", "url": url,
                "auth": {"username": "user", "password": "secret:" + SECRET_ID}}},
            generation="missing-secret", fencing_token_digest=None, fence_check=None,
        ),)).apply()
    assert isinstance(failure.value.original_error, KeyError)
    assert not failure.value.recovery_required
    assert owner._active is previous
    assert previous.readers == 0
    assert not owner._switch_lock.locked()
    async with httpx.AsyncClient(transport=owner, trust_env=False) as client:
        assert (await client.get(url + "/unchanged", timeout=2)).text == "ok"
    assert requests == [("GET /unchanged HTTP/1.1", None)]


@pytest.mark.asyncio
async def test_pool_close_failure_still_releases_all_transaction_references(owner, endpoint, monkeypatch):
    url, _ = endpoint
    await owner.start({"proxy": {"mode": "direct"}})
    previous = owner._active
    result = await GatewayRuntimeConsumerTransaction((owner.stage(
        payload={"proxy": {"mode": "explicit", "url": url}},
        generation="close-failure", fencing_token_digest=None, fence_check=None,
    ),)).apply()
    candidate = owner._active
    await result.promote()

    async def failed_close():
        raise OSError("pool close failed")

    with monkeypatch.context() as patch:
        patch.setattr(previous.client, "aclose", failed_close)
        with pytest.raises(ExceptionGroup):
            await result.finalize()
    assert previous.readers == candidate.readers == 0
    assert not owner._switch_lock.locked()
    assert owner._active is candidate
    assert not candidate.client.is_closed
