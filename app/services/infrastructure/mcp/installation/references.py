"""制品保留只读取已登记工作区的原配置与原资源账本，不保存引用镜像。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from app.core.workspace_identity import read_workspace_id
from app.domain.mcp_candidates import McpCandidateConflict
from app.schemas.internal_v2.mcp import (
    McpArtifactReferenceDTO,
    McpArtifactReferencesDTO,
    McpManagedLaunchDTO,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)


class McpArtifactInUse(McpCandidateConflict):
    def __init__(self, references: McpArtifactReferencesDTO) -> None:
        super().__init__("MCP 制品仍有实际引用或无法核实的占用，拒绝物理卸载")
        self.references = references


def managed_launches(payload: dict[str, object] | None) -> Iterator[tuple[str, McpManagedLaunchDTO]]:
    if payload is None:
        return
    mcp = payload.get("mcp", {})
    if not isinstance(mcp, dict):
        raise TypeError("MCP 引用检查要求 mcp 为对象")
    servers = mcp.get("servers", {})
    if not isinstance(servers, dict):
        raise TypeError("MCP 引用检查要求 servers 为对象")
    for server_id, server in servers.items():
        if not isinstance(server_id, str) or not isinstance(server, dict):
            raise TypeError("MCP 引用检查要求 Server 配置为具名对象")
        if "managed_launch" in server:
            yield server_id, McpManagedLaunchDTO.model_validate(server["managed_launch"])


def workspace_references(root: Path, workspace_id: str, binding: McpManagedLaunchDTO) -> list[McpArtifactReferenceDTO]:
    from app.services.infrastructure.workspace_state_store import WorkspaceStateStore

    references: list[McpArtifactReferenceDTO] = []
    try:
        if read_workspace_id(root) != workspace_id:
            raise ValueError("已登记工作区的稳定身份改变")
        payloads = WorkspaceStateStore.mcp_reference_payloads(root)
        for kind, key, state, payload in payloads:
            for server_id, current in managed_launches(payload):
                if current.installation_id == binding.installation_id and current != binding:
                    raise ValueError("原配置中的 installation 身份与已发布 generation／digest 冲突")
                if current == binding:
                    references.append(McpArtifactReferenceDTO(
                        workspace_id=workspace_id, kind=kind, status=state, server_id=server_id,
                        source_key=key if kind == "source_layer" else None,
                        candidate_id=key if kind == "pending" else None,
                        reason="工作区配置仍保存该精确制品引用；禁用或被遮蔽配置同样保留制品",
                    ))
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error) as error:
        references.append(McpArtifactReferenceDTO(workspace_id=workspace_id, kind="unavailable", status="unknown",
            reason=f"无法核实已登记工作区的原配置事实（{type(error).__name__}）"))
    try:
        state_path = root / ".boxteam" / "resources.json"
        if not state_path.is_file():
            raise FileNotFoundError("已登记工作区的原资源账本缺失")
        ledger = ExternalResourceLeaseLedger(state_path=state_path)
        records = ledger.records_by_kind("mcp_connection")
        leases = ledger.leases_for_resources([record.resource_id for record in records])
        for record in records:
            if (record.managed_launch is not None and record.managed_launch.installation_id == binding.installation_id
                    and record.managed_launch != binding):
                raise ValueError("原连接中的 installation 身份与已发布 generation／digest 冲突")
            unsettled = leases[record.resource_id]
            legacy_unknown = record.mcp_server_id is None and (record.status != "stopped" or bool(unsettled))
            if record.managed_launch != binding and not legacy_unknown:
                continue
            if (legacy_unknown or record.status != "stopped" or record.cleanup_failed
                    or record.process is None or not record.process.exit_verified):
                references.append(McpArtifactReferenceDTO(
                    workspace_id=workspace_id, kind="connection", status="cleanup_failed" if record.cleanup_failed else record.status,
                    server_id=record.mcp_server_id, connection_ref=record.resource_id,
                    reason="旧连接缺少制品归属证据" if legacy_unknown else "原连接尚未核实清理与真实子进程退出",
                ))
            for lease in unsettled:
                references.append(McpArtifactReferenceDTO(
                    workspace_id=workspace_id, kind="lease", status=lease.status,
                    server_id=record.mcp_server_id, connection_ref=record.resource_id,
                    lease_id=lease.lease_id, operation_id=lease.operation_id,
                    reason="原调用或封存资格尚未结清，关闭连接不代替结果核实",
                ))
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        references.append(McpArtifactReferenceDTO(workspace_id=workspace_id, kind="unavailable", status="unknown",
            reason=f"无法核实已登记工作区的原资源账本（{type(error).__name__}）"))
    return references
