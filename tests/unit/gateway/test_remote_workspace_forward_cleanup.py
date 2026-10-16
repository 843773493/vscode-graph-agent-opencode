from __future__ import annotations

import json
import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import get_gateway_local_token, verify_gateway_token
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import (
    FEDERATION_PROTOCOL_VERSION,
    RemoteGatewayConnection,
    build_projected_workspace_id,
    build_remote_gateway_connection_id,
)
from app.gateway.main import app
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes import workspaces_lifecycle
from app.gateway.routes._shared import get_workspace_runtime_controller
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.port_forwarding import get_port_forward_manager
from app.schemas.gateway.port_forwarding import CreatePortForwardRequest


class _FakeManagedProcess:
    def __init__(self, listener: socket.socket) -> None:
        self.process = SimpleNamespace(poll=lambda: None)
        self.listener = listener
        self.fail_next_close = True
        self.closed = False

    def close(self, *, timeout_seconds: float = 8) -> None:
        del timeout_seconds
        if self.fail_next_close:
            self.fail_next_close = False
            raise OSError("injected SSH forward process close failure")
        self.listener.close()
        self.closed = True


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.mark.asyncio
async def test_remote_workspace_removal_keeps_registry_when_forward_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert_remote_gateway(
        RemoteGatewayConnection(
            connection_id="rgw_cleanup",
            name="Cleanup test",
            host="server.example.com",
            port=22,
            username="developer",
            private_key_path=None,
            ssh_config_host=None,
            remote_gateway_port=8014,
            remote_gateway_id="remote-gateway",
            protocol_version=1,
            source_owner="manual",
        )
    )
    for workspace_id in ("workspace_remove", "workspace_sibling"):
        registry.upsert(
            WorkspaceTarget(
                workspace_id=workspace_id,
                name=workspace_id,
                root_path=f"/remote/{workspace_id}",
                backend_url="http://127.0.0.1:41000",
                connection_kind="remote_gateway",
                backend_workspace_id="00000000-0000-4000-8000-000000000002",
                remote_gateway_connection_id="rgw_cleanup",
                remote_workspace_id=workspace_id,
            ),
            activate=False,
        )

    processes: list[_FakeManagedProcess] = []

    def start_forward(**kwargs: object) -> tuple[_FakeManagedProcess, Path]:
        forward = kwargs["forward"]
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", forward.local_port))
        listener.listen()
        process = _FakeManagedProcess(listener)
        processes.append(process)
        return process, tmp_path / "logs" / "forward.log"

    manager = SshPortForwardManager(
        registry=registry,
        storage_path=tmp_path / "port-forwards.json",
        log_dir=tmp_path / "logs",
        process_starter=start_forward,
        port_allocator=_available_port,
    )
    forward = await manager.create(
        "workspace_remove",
        CreatePortForwardRequest(remote_port=5173, protocol="http"),
    )
    process = processes[0]

    monkeypatch.setattr(app.state, "registry", registry, raising=False)
    monkeypatch.setattr(app.state, "port_forward_manager", manager, raising=False)
    monkeypatch.setitem(
        app.dependency_overrides,
        get_workspace_runtime_controller,
        lambda: None,
    )
    headers = {
        "X-Local-Token": get_gateway_local_token(),
        "X-Request-ID": "remote-forward-cleanup-failure",
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway.test",
        ) as client:
            failed = await client.delete(
                "/api/gateway/workspaces/workspace_remove",
                headers=headers,
            )
            assert failed.status_code == 502
            assert failed.headers["X-Request-ID"] == "remote-forward-cleanup-failure"
            assert failed.json() == {
                "detail": "injected SSH forward process close failure",
                "request_id": "remote-forward-cleanup-failure",
            }
            assert registry.resolve("workspace_remove").workspace_id == "workspace_remove"
            assert registry.resolve("workspace_sibling").workspace_id == "workspace_sibling"
            assert (await manager.list("workspace_remove"))[0].forward_id == forward.forward_id
            assert process.closed is False
            restored_registry = GatewayWorkspaceRegistry(
                storage_path=tmp_path / "workspaces.json"
            )
            assert restored_registry.resolve("workspace_remove").workspace_id == "workspace_remove"
            assert json.loads(
                (tmp_path / "port-forwards.json").read_text()
            )["items"][0]["forward_id"] == forward.forward_id

            retried = await client.delete(
                "/api/gateway/workspaces/workspace_remove",
                headers={
                    "X-Local-Token": get_gateway_local_token(),
                    "X-Request-ID": "remote-forward-cleanup-retry",
                },
            )

        assert retried.status_code == 200
        assert retried.headers["X-Request-ID"] == "remote-forward-cleanup-retry"
        with pytest.raises(LookupError):
            registry.resolve("workspace_remove")
        with pytest.raises(LookupError):
            GatewayWorkspaceRegistry(
                storage_path=tmp_path / "workspaces.json"
            ).resolve("workspace_remove")
        assert registry.resolve("workspace_sibling").workspace_id == "workspace_sibling"
        assert process.closed is True
        assert json.loads((tmp_path / "port-forwards.json").read_text())["items"] == []
    finally:
        app.dependency_overrides.pop(get_workspace_runtime_controller, None)
        for process in processes:
            if not process.closed:
                process.listener.close()


@pytest.mark.asyncio
async def test_add_remote_gateway_http_reconciles_published_projection_and_reports_both_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json",
        state_store=state,
    )
    host = "postcommit.example.com"
    port = 22
    username = "developer"
    remote_gateway_port = 8014
    connection_id = build_remote_gateway_connection_id(
        host=host,
        port=port,
        username=username,
        remote_gateway_port=remote_gateway_port,
    )

    class _TrackingRuntime(WorkspaceRuntime):
        def __init__(self, url: str) -> None:
            super().__init__(service_urls={"workspace_api": url})
            self.closed = False

        def close(self) -> None:
            self.closed = True

    old_connection = RemoteGatewayConnection(
        connection_id=connection_id,
        name="Old remote",
        host=host,
        port=port,
        username=username,
        private_key_path=None,
        ssh_config_host=None,
        remote_gateway_port=remote_gateway_port,
        remote_gateway_id="old-gateway",
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="manual",
    )
    old_runtime = _TrackingRuntime("http://127.0.0.1:41000")
    old_workspace_id = build_projected_workspace_id(
        connection_id, "old-remote-workspace"
    )
    old_target = WorkspaceTarget(
        workspace_id=old_workspace_id,
        name="Old remote workspace",
        root_path="/remote/old",
        backend_url=old_runtime.service_urls["workspace_api"],
        connection_kind="remote_gateway",
        backend_workspace_id="00000000-0000-4000-8000-000000000002",
        owner="remote_projection",
        connection_id=connection_id,
        remote_gateway_connection_id=connection_id,
        remote_workspace_id="old-remote-workspace",
        remote_service_names=("workspace_api",),
    )
    registry.apply_remote_projection_snapshot(
        connection=old_connection,
        projections=(old_target,),
        runtime=old_runtime,
    )
    previous_revision = registry.registry_revision

    processes: list[_FakeManagedProcess] = []

    def start_forward(**kwargs: object) -> tuple[_FakeManagedProcess, Path]:
        forward = kwargs["forward"]
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", forward.local_port))
        listener.listen()
        process = _FakeManagedProcess(listener)
        processes.append(process)
        return process, tmp_path / "logs" / "forward.log"

    manager = SshPortForwardManager(
        registry=registry,
        storage_path=tmp_path / "port-forwards.json",
        log_dir=tmp_path / "logs",
        process_starter=start_forward,
        port_allocator=_available_port,
    )
    forward = await manager.create(
        old_workspace_id,
        CreatePortForwardRequest(remote_port=5173, protocol="http"),
    )
    old_forward_process = processes[0]
    new_runtime = _TrackingRuntime("http://127.0.0.1:42000")
    new_connection = replace(
        old_connection,
        name="New remote",
        remote_gateway_id="new-gateway",
    )
    new_target = WorkspaceTarget(
        workspace_id=build_projected_workspace_id(
            connection_id, "new-remote-workspace"
        ),
        name="New remote workspace",
        root_path="/remote/new",
        backend_url=new_runtime.service_urls["workspace_api"],
        connection_kind="remote_gateway",
        backend_workspace_id="00000000-0000-4000-8000-000000000003",
        owner="remote_projection",
        connection_id=connection_id,
        remote_gateway_connection_id=connection_id,
        remote_workspace_id="new-remote-workspace",
        remote_service_names=("workspace_api",),
    )

    async def register_remote_gateway(**_: object) -> None:
        registry.apply_remote_projection_snapshot(
            connection=new_connection,
            projections=(new_target,),
            runtime=new_runtime,
        )

    primary_error = RuntimeError("remote registry observer failed after commit")

    def fail_commit_observer() -> None:
        raise primary_error

    reconcile_calls = 0
    original_reconcile = manager.reconcile_workspaces

    async def observe_failed_reconcile() -> None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        await original_reconcile()

    monkeypatch.setattr(
        workspaces_lifecycle,
        "register_remote_gateway",
        register_remote_gateway,
    )
    monkeypatch.setattr(
        manager,
        "reconcile_workspaces",
        observe_failed_reconcile,
    )
    monkeypatch.setattr(
        workspaces_lifecycle,
        "_gateway_root",
        lambda: tmp_path / "gateway-home",
    )
    registry.add_commit_observer(fail_commit_observer)

    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.state.gateway_config = SimpleNamespace(
        gateway_process_health_request_timeout_seconds=2,
        gateway_process_health_poll_interval_seconds=0.01,
    )
    application.dependency_overrides[verify_gateway_token] = lambda: "local-test-token"
    application.dependency_overrides[workspaces_lifecycle.get_registry] = lambda: registry
    application.dependency_overrides[get_port_forward_manager] = lambda: manager
    application.include_router(workspaces_lifecycle.router)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://gateway",
        ) as client:
            response = await client.post(
                "/api/gateway/remote-gateways",
                json={
                    "name": "New remote",
                    "host": host,
                    "port": port,
                    "username": username,
                    "private_key_path": "/tmp/unused-test-key",
                    "remote_gateway_port": remote_gateway_port,
                },
                headers={"X-Request-ID": "add-remote-postcommit-01"},
            )

        assert response.status_code == 500, response.text
        assert response.headers["X-Request-ID"] == "add-remote-postcommit-01"
        response_body = response.json()
        assert response_body["request_id"] == "add-remote-postcommit-01"
        assert "remote registry observer failed after commit" in response_body["message"]
        assert "SSH 端口转发对账失败" in response_body["message"]
        assert "injected SSH forward process close failure" in response_body["message"]
        assert primary_error.args == (
            "remote registry observer failed after commit",
        )
        assert reconcile_calls == 1
        assert registry.resolve(new_target.workspace_id).backend_url == "http://127.0.0.1:42000"
        assert registry.registry_revision == previous_revision + 1
        assert state.get_registry_revision() == registry.registry_revision
        saved = state.load_workspace_registry()
        assert saved is not None
        assert any(
            target["workspace_id"] == new_target.workspace_id
            and target["backend_url"] == "http://127.0.0.1:42000"
            for target in saved["targets"]
        )
        assert not any(
            target["workspace_id"] == old_workspace_id
            for target in saved["targets"]
        )
        assert not registry.has_target(old_workspace_id)
        assert registry._remote_gateway_runtimes[connection_id] is new_runtime
        assert new_runtime.closed is False
        assert old_runtime.closed is True
        assert forward.forward_id in manager._definitions
        assert old_forward_process.closed is False
    finally:
        registry.remove_commit_observer(fail_commit_observer)
        for process in processes:
            if not process.closed:
                process.listener.close()
        registry.close()
        state.close()
