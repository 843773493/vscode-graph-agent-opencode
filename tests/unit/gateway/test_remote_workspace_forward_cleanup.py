from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.gateway.auth import get_gateway_local_token
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.main import app
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes._shared import get_workspace_runtime_controller
from app.gateway.runtime.port_forwarding import SshPortForwardManager
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
