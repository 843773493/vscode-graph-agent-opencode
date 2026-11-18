"""工作区不拥有端口规则；移除 Host 连接只关闭隧道、保留定义。"""
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import verify_gateway_token
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes._shared import get_registry, get_workspace_runtime_controller
from app.gateway.routes.workspaces_lifecycle import router
from app.gateway.runtime.port_forwarding import PortForwardManager
from app.gateway.runtime.process_ports import allocate_local_port
from app.schemas.gateway.port_forwarding import CreatePortForwardRequest, PortForwardEndpoint


@pytest.fixture
async def state(tmp_path: Path):
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / 'registry.json')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    target = WorkspaceTarget(
        gateway_id=registry.gateway_id, workspace_id='00000000-0000-4000-8000-000000000002',
        name='Independent workspace', root_path=str(workspace), backend_url='http://127.0.0.1:9',
        connection_kind='local', managed=False, owner='manual',
    )
    registry.upsert(target)
    manager = PortForwardManager(registry=registry, storage_path=tmp_path / 'forwards.json', log_dir=tmp_path / 'logs')
    item = await manager.create(CreatePortForwardRequest(
        listener=PortForwardEndpoint(host_id='local', port=allocate_local_port()),
        destination=PortForwardEndpoint(host_id='local', port=allocate_local_port()),
    ))
    try:
        yield registry, manager, item, target
    finally:
        await manager.close()
        registry.close()


@pytest.mark.asyncio
async def test_workspace_deletion_does_not_stop_or_remove_host_forward(state):
    registry, manager, item, target = state
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router)
    app.dependency_overrides[verify_gateway_token] = lambda: 'test'
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_workspace_runtime_controller] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway.test') as client:
        response = await client.delete(f'/api/gateway/workspaces/{target.workspace_id}')
    assert response.status_code == 200, response.text
    remaining, = await manager.list()
    assert remaining.forward_id == item.forward_id and remaining.status == 'active'
    with pytest.raises(LookupError):
        registry.resolve(target.workspace_id)


@pytest.mark.asyncio
async def test_disconnecting_host_stops_forward_and_preserves_editable_rule(state):
    _, manager, item, _ = state
    await manager.disconnect_host('local')
    remaining, = await manager.list()
    assert remaining.forward_id == item.forward_id and remaining.enabled
    assert remaining.status == 'error' and remaining.error
    with pytest.raises(OSError):
        import asyncio
        await asyncio.open_connection('127.0.0.1', remaining.listener.port)
