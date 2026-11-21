"""端口规则独立于工作区；覆盖传输、持久启停及多 Host 路由。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import verify_gateway_token
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes._shared import get_registry
from app.gateway.runtime.port_forwarding import PortForwardManager
from app.gateway.runtime.port_forwarding.hosts import SshHost
from app.gateway.runtime.process_ports import allocate_local_port
from app.gateway.server.port_forwarding import get_port_forward_manager, router
from app.schemas.gateway.port_forwarding import (
    CreatePortForwardRequest,
    PortForwardEndpoint,
)


@pytest.fixture
def registry(tmp_path: Path):
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json")
    yield registry
    registry.close()


@pytest.fixture
async def manager(tmp_path: Path, registry: GatewayWorkspaceRegistry) -> AsyncIterator[PortForwardManager]:
    manager = PortForwardManager(registry=registry, storage_path=tmp_path / "forwards.json", log_dir=tmp_path / "logs")
    yield manager
    await manager.close()


@pytest.fixture
async def echo_service():
    async def echo(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()


def rule(destination_port: int, *, listener_host: str = "local", destination_host: str = "local", enabled: bool = True):
    return CreatePortForwardRequest(
        listener=PortForwardEndpoint(host_id=listener_host, port=allocate_local_port()),
        destination=PortForwardEndpoint(host_id=destination_host, port=destination_port), enabled=enabled,
    )


async def exchange(port: int) -> None:
    async with asyncio.timeout(3):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        message = b"actual forwarded bytes\x00" * 2000
        writer.write(message)
        await writer.drain()
        assert await reader.readexactly(len(message)) == message
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_tcp_rules_start_stop_restart_restore_and_share_destination(manager, registry, tmp_path, echo_service):
    first = await manager.create(rule(echo_service))
    second = await manager.create(rule(echo_service))
    assert first.status == second.status == "active"
    assert first.gateway_id == registry.gateway_id
    await exchange(first.listener.port)
    await exchange(second.listener.port)
    stopped = await manager.set_running(first.forward_id, False)
    assert stopped.status == "stopped" and not stopped.enabled
    assert len(await manager.list()) == 2
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", first.listener.port)
    restarted = await manager.set_running(first.forward_id, True)
    await exchange(restarted.listener.port)
    await manager.close()
    restored = PortForwardManager(registry=registry, storage_path=tmp_path / "forwards.json", log_dir=tmp_path / "logs")
    try:
        await restored.restore()
        assert [item.status for item in await restored.list()] == ["active", "active"]
        await exchange(first.listener.port)
        await restored.delete(first.forward_id)
        assert [item.forward_id for item in await restored.list()] == [second.forward_id]
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_conflict_edit_and_failed_bind_keep_authoritative_rule(manager, echo_service):
    first = await manager.create(rule(echo_service))
    duplicate = CreatePortForwardRequest(listener=first.listener, destination=first.destination)
    with pytest.raises(ValueError, match="监听端口"):
        await manager.create(duplicate)
    occupied = rule(echo_service)
    occupied.listener.port = echo_service
    # 不同逻辑入口的业务服务占用真实监听端口，启动失败仍保留可编辑的规则。
    occupied.destination.port = echo_service + 1
    failed = await manager.create(occupied)
    assert failed.status == "error" and failed.error
    edited = await manager.update(failed.forward_id, rule(echo_service))
    assert edited.forward_id == failed.forward_id and edited.status == "active"
    await exchange(edited.listener.port)


@pytest.fixture
def ssh_processes(tmp_path, monkeypatch):
    calls = []
    processes = []
    def resolve(_registry, host_id):
        return None if host_id == "local" else SshHost(host_id, 22, "tester", None, host_id)
    monkeypatch.setattr("app.gateway.runtime.port_forwarding.manager.resolve_host", resolve)
    def starter(**kwargs):
        calls.append(kwargs)
        process = SimpleNamespace(code=None, closed=False)
        process.process = SimpleNamespace(poll=lambda: process.code)
        def close(**_):
            process.closed = True
        process.close = close
        path = tmp_path / f"{kwargs['forward_id']}.log"
        marker = "remote forward success for:" if kwargs['forward'].direction == 'R' else "Local forwarding listening on"
        path.write_text(marker)
        processes.append(process)
        return process, path, 0
    return starter, calls, processes


@pytest.mark.asyncio
@pytest.mark.parametrize(("listener", "destination", "directions"), [
    ("local", "host-b", ["L"]), ("host-a", "local", ["R"]), ("host-a", "host-b", ["L", "R"]),
])
async def test_ssh_direction_and_cross_host_relay(tmp_path, registry, ssh_processes, listener, destination, directions):
    starter, calls, processes = ssh_processes
    manager = PortForwardManager(registry=registry, storage_path=tmp_path / "ssh.json", log_dir=tmp_path / "logs", process_starter=starter)
    try:
        item = await manager.create(rule(3000, listener_host=listener, destination_host=destination))
        assert item.status == "active"
        assert [call['forward'].direction for call in calls] == directions
        assert calls[-1]['forward'].listen_port == item.listener.port
        if len(calls) == 2:
            assert calls[0]['host'] == "host-b" and calls[1]['host'] == "host-a"
            assert calls[1]['forward'].destination_port == calls[0]['forward'].listen_port
            # 一段退出立即关闭整条规则；不等用户刷新或依赖工作区生命周期。
            processes[0].code = 255
            async with asyncio.timeout(2):
                while not all(process.closed for process in processes):
                    await asyncio.sleep(.02)
            assert (await manager.list())[0].status == "error"
    finally:
        await manager.close()
    assert all(process.closed for process in processes)


@pytest.mark.asyncio
async def test_partial_ssh_failure_closes_first_leg_and_retains_rule(tmp_path, registry, ssh_processes):
    starter, calls, processes = ssh_processes
    def fail_second(**kwargs):
        if calls:
            raise OSError("second SSH failed")
        return starter(**kwargs)
    manager = PortForwardManager(registry=registry, storage_path=tmp_path / "ssh.json", log_dir=tmp_path / "logs", process_starter=fail_second)
    try:
        failed = await manager.create(rule(3000, listener_host="host-a", destination_host="host-b"))
        assert failed.status == "error" and processes[0].closed
        assert (await manager.list())[0].enabled
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_gateway_api_has_no_workspace_dependency(manager, registry, echo_service, monkeypatch):
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router)
    app.dependency_overrides[verify_gateway_token] = lambda: "test"
    app.dependency_overrides[get_port_forward_manager] = lambda: manager
    app.dependency_overrides[get_registry] = lambda: registry
    monkeypatch.setattr("app.gateway.runtime.port_forwarding.hosts.list_ssh_connection_options", lambda _: [])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
        hosts = await client.get("/api/gateway/port-forwards/hosts")
        assert hosts.json()["data"]["items"][0]["host_id"] == "local"
        created = await client.post("/api/gateway/port-forwards", json=rule(echo_service).model_dump(), headers={"X-Request-ID": "port-rule-request"})
        assert created.status_code == 200, created.text
        assert created.headers['X-Request-ID'] == created.json()['request_id'] == "port-rule-request"
        item = created.json()['data']['items'][0]
        assert 'workspace_id' not in item
        await exchange(item['listener']['port'])
        stopped = await client.post(f"/api/gateway/port-forwards/{item['forward_id']}/stop")
        assert stopped.json()['data']['items'][0]['status'] == "stopped"
        assert (await client.post(f"/api/gateway/port-forwards/{item['forward_id']}/start")).status_code == 200
        assert (await client.delete(f"/api/gateway/port-forwards/{item['forward_id']}")).json()['data']['items'] == []
        assert (await client.get('/api/gateway/workspaces/old/port-forwards')).status_code == 404


@pytest.mark.parametrize("version", [1, 2])
def test_old_definition_is_not_silently_rewritten(tmp_path, registry, version):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"schema_version": version, "items": []}))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="版本不匹配"):
        PortForwardManager(registry=registry, storage_path=path, log_dir=tmp_path / "logs")
    assert path.read_bytes() == before
