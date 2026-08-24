from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from app.core.path_utils import get_gateway_root
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.schemas.gateway import (
    GatewayDiagnosticLogDTO,
    GatewayDiagnosticsDTO,
    GatewayDiagnosticStatus,
    GatewayDiagnosticWorkspaceDTO,
)

_MAX_READ_BYTES = 256 * 1024
_MAX_GATEWAY_LOG_ENTRIES = 120
_SERVICE_LABELS = {
    "workspace_api": "Workspace API",
    "terminal_manager": "Terminal Manager",
    "browser_manager": "Browser Manager",
}


@dataclass(frozen=True, slots=True)
class _LogCandidate:
    log_id: str
    source: str
    service: str
    label: str
    path: Path | None
    workspace_id: str | None = None
    workspace_name: str | None = None


def _gateway_log_dir() -> Path:
    return get_gateway_root() / "logs"


def _launcher_log_path() -> Path:
    return get_gateway_root().parent / "launcher" / "logs" / "services.log"


def _port_from_url(url: str | None) -> int | None:
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.port


def _process_log_path(service: str, port: int | None) -> Path | None:
    if port is None:
        return None
    prefix = {
        "workspace_api": "local-backend",
        "terminal_manager": "local-terminal",
        "browser_manager": "local-browser",
    }.get(service)
    if prefix is None:
        raise ValueError(f"不支持的诊断服务: {service}")
    return _gateway_log_dir() / f"{prefix}-{port}.log"


def _candidate_gateway_logs(known_workspace_files: set[str]) -> list[_LogCandidate]:
    candidates = [
        _LogCandidate(
            log_id="gateway:launcher",
            source="gateway",
            service="launcher",
            label="Launcher 汇总日志",
            path=_launcher_log_path(),
        )
    ]
    log_dir = _gateway_log_dir()
    if not log_dir.exists():
        return candidates
    paths = [
        item
        for item in log_dir.iterdir()
        if item.is_file()
        and not item.is_symlink()
        and item.name.endswith(".log")
        and item.name not in known_workspace_files
    ]
    paths.sort(key=lambda item: (item.stat().st_mtime, item.name), reverse=True)
    for path in paths[:_MAX_GATEWAY_LOG_ENTRIES]:
        candidates.append(
            _LogCandidate(
                log_id=f"gateway:file:{path.name}",
                source="gateway",
                service="gateway_runtime",
                label=f"Gateway 运行日志 · {path.name}",
                path=path,
            )
        )
    return candidates


def _workspace_candidates(
    target: WorkspaceTarget,
    registry: GatewayWorkspaceRegistry,
) -> list[_LogCandidate]:
    runtime_urls = registry.runtime_service_urls(target.workspace_id)
    if target.managed:
        runtime_urls.setdefault("workspace_api", target.backend_url)
    candidates: list[_LogCandidate] = []
    for service in ("workspace_api", "terminal_manager", "browser_manager"):
        service_url = runtime_urls.get(service)
        path = _process_log_path(service, _port_from_url(service_url))
        if service == "workspace_api" and path is None and target.managed:
            path = _process_log_path(service, _port_from_url(target.backend_url))
        candidates.append(
            _LogCandidate(
                log_id=f"workspace:{target.workspace_id}:{service}",
                source="workspace",
                workspace_id=target.workspace_id,
                workspace_name=target.name,
                service=service,
                label=f"{target.name} · {_SERVICE_LABELS[service]}",
                path=path,
            )
        )
    return candidates


def _read_log(candidate: _LogCandidate, tail_lines: int) -> GatewayDiagnosticLogDTO:
    if candidate.path is None:
        return GatewayDiagnosticLogDTO(
            log_id=candidate.log_id,
            source=candidate.source,
            workspace_id=candidate.workspace_id,
            workspace_name=candidate.workspace_name,
            service=candidate.service,
            label=candidate.label,
            status="unavailable",
            error="该工作区不是当前 Gateway 托管进程，日志不在 Gateway 控制面中。",
        )
    path = candidate.path
    if path.is_symlink():
        raise RuntimeError(f"拒绝读取符号链接日志: {path}")
    if not path.exists():
        return GatewayDiagnosticLogDTO(
            log_id=candidate.log_id,
            source=candidate.source,
            workspace_id=candidate.workspace_id,
            workspace_name=candidate.workspace_name,
            service=candidate.service,
            label=candidate.label,
            status="unavailable",
            error=f"日志文件不存在: {path.name}",
        )
    if not path.is_file():
        raise RuntimeError(f"诊断日志路径不是普通文件: {path}")
    file_stat = path.stat()
    updated_at = datetime.fromtimestamp(
        file_stat.st_mtime,
        UTC,
    ).isoformat().replace("+00:00", "Z")
    if file_stat.st_size == 0:
        return GatewayDiagnosticLogDTO(
            log_id=candidate.log_id,
            source=candidate.source,
            workspace_id=candidate.workspace_id,
            workspace_name=candidate.workspace_name,
            service=candidate.service,
            label=candidate.label,
            status="empty",
            size_bytes=0,
            updated_at=updated_at,
        )

    with path.open("rb") as file:
        file.seek(max(0, file_stat.st_size - _MAX_READ_BYTES))
        raw = file.read(_MAX_READ_BYTES)
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    truncated = len(raw) < file_stat.st_size
    if len(lines) > tail_lines:
        lines = lines[-tail_lines:]
        truncated = True
    return GatewayDiagnosticLogDTO(
        log_id=candidate.log_id,
        source=candidate.source,
        workspace_id=candidate.workspace_id,
        workspace_name=candidate.workspace_name,
        service=candidate.service,
        label=candidate.label,
        status="available",
        tail="\n".join(lines),
        truncated=truncated,
        line_count=len(lines),
        size_bytes=file_stat.st_size,
        updated_at=updated_at,
    )


def _status(workspaces: list[GatewayDiagnosticWorkspaceDTO]) -> GatewayDiagnosticStatus:
    if any(item.status == "offline" or item.connection_error for item in workspaces):
        return "degraded"
    return "ready"


def _scope_log_candidates(
    gateway_candidates: list[_LogCandidate],
    workspace_candidates: list[_LogCandidate],
    selected_workspace_id: str | None,
) -> list[_LogCandidate]:
    if selected_workspace_id is None:
        return [*gateway_candidates, *workspace_candidates]

    # Gateway 运行日志没有稳定的 workspace_id，不能在工作区筛选下伪装成目标工作区日志。
    # 保留 Launcher 汇总日志作为全局控制面入口，其他无归属日志只在“全部工作区”中展示。
    global_candidates = [
        candidate
        for candidate in gateway_candidates
        if candidate.log_id == "gateway:launcher"
    ]
    selected_workspace_candidates = [
        candidate
        for candidate in workspace_candidates
        if candidate.workspace_id == selected_workspace_id
    ]
    return [*global_candidates, *selected_workspace_candidates]


async def collect_gateway_diagnostics(
    registry: GatewayWorkspaceRegistry,
    *,
    gateway_id: str | None = None,
    gateway_name: str = "本机 Gateway",
    gateway_connection_id: str | None = None,
    connection_kind: str = "local",
    selected_workspace_id: str | None = None,
    selected_log_id: str | None = None,
    tail_lines: int = 300,
) -> GatewayDiagnosticsDTO:
    if not 20 <= tail_lines <= 1000:
        raise ValueError("tail_lines 必须在 20..1000 范围内")
    local_gateway_id = gateway_id or load_or_create_gateway_id(
        get_gateway_root() / "identity.json"
    )
    all_workspace_dtos = [
        item
        for item in await registry.list_dtos()
        if item.connection_kind == "local"
    ]
    if selected_workspace_id is not None and not any(
        item.workspace_id == selected_workspace_id for item in all_workspace_dtos
    ):
        raise LookupError(f"Gateway 工作区不存在: {selected_workspace_id}")

    workspace_dtos = [
        item
        for item in all_workspace_dtos
        if selected_workspace_id is None or item.workspace_id == selected_workspace_id
    ]
    workspace_items = [
        GatewayDiagnosticWorkspaceDTO(
            workspace_id=item.workspace_id,
            name=item.name,
            root_path=item.root_path,
            connection_kind=item.connection_kind,
            status=item.status,
            managed=item.managed,
            system_default=item.system_default,
            connection_error=item.connection_error,
        )
        for item in workspace_dtos
    ]
    targets = [
        target
        for target in registry.targets()
        if target.connection_kind == "local"
    ]
    workspace_candidates = [
        candidate
        for target in targets
        for candidate in _workspace_candidates(target, registry)
    ]
    known_workspace_files = {
        candidate.path.name
        for candidate in workspace_candidates
        if candidate.path is not None
    }
    gateway_candidates = _candidate_gateway_logs(known_workspace_files)
    candidates = _scope_log_candidates(
        gateway_candidates,
        workspace_candidates,
        selected_workspace_id,
    )
    if selected_log_id is not None and not any(
        candidate.log_id == selected_log_id for candidate in candidates
    ):
        raise LookupError(f"诊断日志不存在: {selected_log_id}")
    if selected_log_id is None:
        preferred = (
            [
                candidate
                for candidate in candidates
                if candidate.workspace_id == selected_workspace_id
            ]
            if selected_workspace_id is not None
            else [
                candidate
                for candidate in candidates
                if candidate.source == "gateway"
            ]
        )
        selected_log_id = next(
            (
                candidate.log_id
                for candidate in preferred
                if candidate.path is not None and candidate.path.exists()
            ),
            preferred[0].log_id if preferred else None,
        )
    logs = []
    for candidate in candidates:
        log = _read_log(
            candidate,
            tail_lines if candidate.log_id == selected_log_id else 20,
        )
        if candidate.log_id != selected_log_id:
            log.tail = ""
            log.truncated = False
            log.line_count = 0
        logs.append(log)
    return GatewayDiagnosticsDTO(
        gateway_id=local_gateway_id,
        gateway_name=gateway_name,
        gateway_connection_id=gateway_connection_id,
        connection_kind=connection_kind,
        status=_status(workspace_items),
        checked_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        selected_workspace_id=selected_workspace_id,
        selected_log_id=selected_log_id,
        workspaces=workspace_items,
        logs=logs,
    )
