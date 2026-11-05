"""连接指纹保留受管绑定身份，并沿原准入和重启对账核实。"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.domain.itemized.hashing import sha256_jcs
from app.schemas.internal_v2.mcp import (
    McpConnectionOperationDTO,
    McpManagedLaunchDTO,
    McpServerDTO,
)
from app.services.infrastructure.mcp import connection_service
from app.services.infrastructure.mcp.catalog_owner import McpCatalogOwner
from app.services.infrastructure.mcp.config import McpOAuthConfig, McpServerConfig
from app.services.infrastructure.mcp.connection_service import McpConnectionService
from app.services.infrastructure.mcp.operation_store import McpConnectionOperationStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


@dataclass
class ConnectionCase:
    service: McpConnectionService
    owner: Mock
    operations: Mock
    server: McpServerConfig
    operation: McpConnectionOperationDTO


@pytest.fixture
def connection_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConnectionCase:
    binding = {"backend_id": "00000000-0000-4000-8000-000000000001", "installation_id": "a" * 32,
        "generation": "b" * 32, "artifact_digest": "sha256:jcs:v1:" + "c" * 64}
    server = McpServerConfig(server_id="managed", enabled=True, transport="stdio",
        connection={"transport": "stdio", "command": "/managed/server", "args": ["workspace"], "env": {}, "cwd": str(tmp_path)},
        managed_launch=McpManagedLaunchDTO.model_validate(binding))
    now = time.time()
    operation = McpConnectionOperationDTO(operation_id="original", connection_id="mcp-server:v1:workspace:managed",
        server_id="managed", action="connect", status="pending", stage="queued", created_at=now, deadline=now + 60, updated_at=now)
    owner = Mock(spec=McpCatalogOwner)
    owner.server_configuration.return_value = server
    owner.list_servers.return_value = [McpServerDTO(server_id="managed", transport="stdio", enabled=True, status="ready")]
    operations = Mock(spec=McpConnectionOperationStore)
    operations.get.return_value = operation
    operations.active.return_value = (operation,)
    operations.configuration_ref.return_value = sha256_jcs({"server_id": "managed", "enabled": True, "transport": "stdio",
        "connection": server.connection, "oauth": None, "managed_launch": binding})
    monkeypatch.setattr(connection_service, "McpConnectionOperationStore", lambda _store: operations)
    state = Mock(spec=WorkspaceStateStore)
    state.workspace_root = tmp_path
    service = McpConnectionService(owner=owner, store=state, workspace_id="workspace")
    return ConnectionCase(service, owner, operations, server, operation)


@pytest.mark.parametrize("transport", ["stdio", "streamable_http"])
def test_unmanaged_configuration_keeps_original_fingerprint(connection_case: ConnectionCase, transport: str) -> None:
    server = (McpServerConfig(server_id="plain", enabled=True, transport="stdio",
        connection={"transport": "stdio", "command": "server", "args": [], "env": {}, "cwd": "/workspace"})
        if transport == "stdio" else McpServerConfig(server_id="plain", enabled=True, transport="streamable_http",
            connection={"transport": "streamable_http", "url": "http://localhost/mcp", "headers": {}},
            oauth=McpOAuthConfig(redirect_uri="http://localhost/callback")))
    connection_case.owner.server_configuration.return_value = server
    assert connection_case.service._configuration_ref("plain") == sha256_jcs(asdict(server))


@pytest.mark.parametrize("field,value", [
    ("backend_id", "00000000-0000-4000-8000-000000000002"),
    ("installation_id", "d" * 32),
    ("generation", "e" * 32),
    ("artifact_digest", "sha256:jcs:v1:" + "f" * 64),
])
def test_managed_binding_admits_original_and_fences_changed_identity(connection_case: ConnectionCase, field: str, value: str) -> None:
    case = connection_case
    assert case.service._configuration_ref("managed") == case.operations.configuration_ref.return_value
    case.service._admit(case.operation.operation_id)
    binding = case.server.managed_launch
    assert binding is not None
    # 保持解析后的命令与工作区参数不变，仍须核对原受管绑定的每一项身份。
    case.owner.server_configuration.return_value = replace(case.server, managed_launch=binding.model_copy(update={field: value}))
    with pytest.raises(PermissionError, match="配置已改变"):
        case.service._admit(case.operation.operation_id)


@pytest.mark.parametrize("changed", [False, True])
def test_restart_reconciles_managed_operation_only_for_original_binding(connection_case: ConnectionCase, changed: bool) -> None:
    case = connection_case
    if changed:
        binding = case.server.managed_launch
        assert binding is not None
        case.owner.server_configuration.return_value = replace(case.server, managed_launch=binding.model_copy(update={"generation": "d" * 32}))
    case.service.start()
    case.operations.advance.assert_called_once_with(case.operation.operation_id,
        status="failed" if changed else "succeeded", stage="reconcile", error_code="process_restarted_unconfirmed" if changed else None)
