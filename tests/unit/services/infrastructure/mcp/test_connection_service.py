"""连接操作指纹包含当前 MCP 配置及精确私有 header 引用。"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.domain.itemized.hashing import sha256_jcs
from app.schemas.internal_v2.mcp import (
    McpConnectionOperationDTO,
    McpPrivateHeaderRefDTO,
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
    server = McpServerConfig(
        server_id="remote",
        enabled=True,
        transport="streamable_http",
        connection={"transport": "streamable_http", "url": "http://localhost/mcp", "headers": {}},
    )
    now = time.time()
    operation = McpConnectionOperationDTO(
        operation_id="original",
        connection_id="mcp-server:v1:workspace:remote",
        server_id="remote",
        action="connect",
        status="pending",
        stage="queued",
        created_at=now,
        deadline=now + 60,
        updated_at=now,
    )
    owner = Mock(spec=McpCatalogOwner)
    owner.server_configuration.return_value = server
    owner.list_servers.return_value = [
        McpServerDTO(server_id="remote", transport="streamable_http", enabled=True, status="ready")
    ]
    operations = Mock(spec=McpConnectionOperationStore)
    operations.get.return_value = operation
    operations.active.return_value = (operation,)
    server_ref = asdict(server)
    server_ref.pop("private_headers")
    operations.configuration_ref.return_value = sha256_jcs(server_ref)
    monkeypatch.setattr(connection_service, "McpConnectionOperationStore", lambda _store: operations)
    state = Mock(spec=WorkspaceStateStore)
    state.workspace_root = tmp_path
    service = McpConnectionService(owner=owner, store=state, workspace_id="workspace")
    return ConnectionCase(service, owner, operations, server, operation)


def test_connection_configuration_fingerprint_covers_parsed_http_configuration(connection_case: ConnectionCase) -> None:
    server = replace(
        connection_case.server,
        oauth=McpOAuthConfig(redirect_uri="http://localhost/callback"),
    )
    connection_case.owner.server_configuration.return_value = server
    expected = asdict(server)
    expected.pop("private_headers")
    assert connection_case.service._configuration_ref("remote") == sha256_jcs(expected)


def test_private_header_exact_version_participates_in_original_operation_admission(
    connection_case: ConnectionCase,
) -> None:
    case = connection_case
    reference = McpPrivateHeaderRefDTO(
        workspace_id="00000000-0000-4000-8000-000000000001",
        server_id="remote",
        header_name="X-Secret",
        endpoint="https://service.test/mcp",
        version="a" * 32,
    )
    server = replace(case.server, private_headers=(reference,))
    case.owner.server_configuration.return_value = server
    case.operations.configuration_ref.return_value = case.service._configuration_ref("remote")
    case.service._admit(case.operation.operation_id)
    case.owner.server_configuration.return_value = replace(
        server,
        private_headers=(reference.model_copy(update={"version": "b" * 32}),),
    )
    with pytest.raises(PermissionError, match="配置已改变"):
        case.service._admit(case.operation.operation_id)


def test_restart_reconciles_operation_against_current_server_configuration(
    connection_case: ConnectionCase,
) -> None:
    case = connection_case
    case.service.start()
    case.operations.advance.assert_called_once_with(
        case.operation.operation_id,
        status="succeeded",
        stage="reconcile",
        error_code=None,
    )
