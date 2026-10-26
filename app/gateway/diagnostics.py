from __future__ import annotations

import asyncio
import heapq
import logging
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.core.logging_contract import get_runtime_log_root, load_runtime_log_contract
from app.core.path_utils import get_gateway_root
from app.gateway.auth import get_gateway_local_token
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.logging_control import (
    RuntimeLoggingProcess,
    RuntimeLoggingProcessControl,
    runtime_logging_processes,
)
from app.schemas.gateway.diagnostics import (
    GatewayDiagnosticLogDTO,
    GatewayDiagnosticLoggingStateDTO,
    GatewayDiagnosticLogSinkDTO,
    GatewayDiagnosticLogSource,
    GatewayDiagnosticProcessDTO,
    GatewayDiagnosticsDTO,
    GatewayDiagnosticStatus,
    GatewayDiagnosticWorkspaceDTO,
)
from app.schemas.gateway.workspaces import GatewayConnectionKind

_MAX_READ_BYTES = 256 * 1024
_MAX_GATEWAY_LOG_ENTRIES = 120
logger = logging.getLogger(__name__)
_SERVICE_LABELS = {
    "workspace_api": "Workspace API",
    "terminal_manager": "Terminal Manager",
    "browser_manager": "Browser Manager",
}


@dataclass(frozen=True, slots=True)
class _LogCandidate:
    log_id: str
    source: GatewayDiagnosticLogSource
    service: str
    label: str
    log_path: Path | None
    workspace_id: str | None = None
    workspace_name: str | None = None


def _launcher_log_path() -> Path:
    configured = os.environ.get("BOXTEAM_SERVICE_LOG_PATH", "").strip()
    if configured:
        return Path(configured).resolve()
    return load_runtime_log_contract().path_for(get_runtime_log_root(), "launcher")


def _launcher_log_candidate() -> _LogCandidate:
    return _LogCandidate(
        log_id="gateway:launcher",
        source="gateway",
        service="launcher",
        label="Launcher 日志",
        log_path=_launcher_log_path(),
    )


def _port_from_url(url: str | None) -> int | None:
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.port


def _process_log_path(service: str, port: int | None) -> Path | None:
    if port is None:
        return None
    role = {
        "workspace_api": "backend",
        "terminal_manager": "terminal",
        "browser_manager": "browser",
    }.get(service)
    if role is None:
        raise ValueError(f"不支持的诊断服务: {service}")
    return load_runtime_log_contract().path_for(get_runtime_log_root(), role, port)


def _candidate_gateway_logs(known_workspace_files: set[str]) -> list[_LogCandidate]:
    candidates = [_launcher_log_candidate()]
    log_dir = get_runtime_log_root()
    if not log_dir.exists():
        return candidates

    def gateway_log_entries() -> Iterator[tuple[float, str, Path]]:
        directories = {parts[0] for parts in load_runtime_log_contract().paths.values()}
        launcher_path = load_runtime_log_contract().path_for(log_dir, "launcher")
        for directory in sorted(directories):
            role_dir = log_dir / directory
            if role_dir.is_symlink() or not role_dir.is_dir():
                continue
            for log_path in role_dir.iterdir():
                if log_path == launcher_path or not log_path.name.endswith(".log") or log_path.name in known_workspace_files:
                    continue
                try:
                    metadata = log_path.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    yield metadata.st_mtime, log_path.name, log_path

    newest_log_entries = heapq.nlargest(
        _MAX_GATEWAY_LOG_ENTRIES,
        gateway_log_entries(),
        key=lambda log_entry: log_entry[:2],
    )
    for _, _, log_path in newest_log_entries:
        candidates.append(
            _LogCandidate(
                log_id=f"gateway:file:{log_path.name}",
                source="gateway",
                service="gateway_runtime",
                label=f"Gateway 运行日志 · {log_path.name}",
                log_path=log_path,
            )
        )
    return candidates


def _workspace_candidates(
    target: WorkspaceTarget,
    registry: GatewayWorkspaceRegistry,
) -> list[_LogCandidate]:
    workspace_runtime_urls = registry.runtime_service_urls(target.workspace_id)
    if target.managed:
        workspace_runtime_urls.setdefault("workspace_api", target.backend_url)
    candidates: list[_LogCandidate] = []
    for service in ("workspace_api", "terminal_manager", "browser_manager"):
        service_url = workspace_runtime_urls.get(service)
        log_path = _process_log_path(service, _port_from_url(service_url))
        if service == "workspace_api" and log_path is None and target.managed:
            log_path = _process_log_path(service, _port_from_url(target.backend_url))
        candidates.append(
            _LogCandidate(
                log_id=f"workspace:{target.workspace_id}:{service}",
                source="workspace",
                workspace_id=target.workspace_id,
                workspace_name=target.name,
                service=service,
                label=f"{target.name} · {_SERVICE_LABELS[service]}",
                log_path=log_path,
            )
        )
    return candidates


def _read_log(
    candidate: _LogCandidate,
    tail_lines: int,
    *,
    include_tail: bool,
) -> GatewayDiagnosticLogDTO:
    if candidate.log_path is None:
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
    log_path = candidate.log_path
    try:
        file_stat = log_path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return GatewayDiagnosticLogDTO(
            log_id=candidate.log_id,
            source=candidate.source,
            workspace_id=candidate.workspace_id,
            workspace_name=candidate.workspace_name,
            service=candidate.service,
            label=candidate.label,
            status="unavailable",
            error=f"日志文件不存在: {log_path.name}",
        )
    if stat.S_ISLNK(file_stat.st_mode):
        raise RuntimeError(f"拒绝读取符号链接日志: {log_path}")
    if not stat.S_ISREG(file_stat.st_mode):
        raise RuntimeError(f"诊断日志路径不是普通文件: {log_path}")
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
    if include_tail:
        with log_path.open("rb") as file:
            file.seek(max(0, file_stat.st_size - _MAX_READ_BYTES))
            raw = file.read(_MAX_READ_BYTES)
        lines = raw.decode("utf-8", errors="replace").splitlines()
        truncated = len(raw) < file_stat.st_size
        if len(lines) > tail_lines:
            lines = lines[-tail_lines:]
            truncated = True
    else:
        lines = []
        truncated = False
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


def _status(
    workspace_dtos: list[GatewayDiagnosticWorkspaceDTO],
) -> GatewayDiagnosticStatus:
    if any(
        workspace_dto.status == "offline" or workspace_dto.connection_error
        for workspace_dto in workspace_dtos
    ):
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


async def _logging_process_diagnostics(
    registry: GatewayWorkspaceRegistry, selected_workspace_id: str | None, candidates: list[_LogCandidate],
) -> list[GatewayDiagnosticProcessDTO]:
    async def observe(process: RuntimeLoggingProcess) -> GatewayDiagnosticProcessDTO:
        try:
            state = await process.read()
        except Exception as error:  # 管理诊断逐进程显示失败，原始异常仍写入具名日志入口。
            logger.exception("读取产品进程日志状态失败: service=%s", process.service)
            return GatewayDiagnosticProcessDTO(
                service=process.service, workspace_ids=list(process.workspace_ids),
                output_mode="unknown", error=f"日志状态读取失败: service={process.service}, error={type(error).__name__}；完整诊断见原始服务日志",
            )
        sink = state.log_sink
        public_sink = None if sink is None else GatewayDiagnosticLogSinkDTO(
            **sink.model_dump(exclude={"path"}),
            log_id=next((candidate.log_id for candidate in candidates if candidate.log_path == Path(sink.path)), None),
        )
        return GatewayDiagnosticProcessDTO(
            service=process.service, workspace_ids=list(process.workspace_ids),
            state=GatewayDiagnosticLoggingStateDTO(**state.model_dump(exclude={"log_sink"}), log_sink=public_sink),
            output_mode="file" if state.log_sink is not None else "stdout-only",
        )

    async with registry.backend_topology_lock, httpx.AsyncClient(trust_env=False) as client:
        control = RuntimeLoggingProcessControl(client=client, token=get_gateway_local_token())
        processes = runtime_logging_processes(registry, control)
        return list(await asyncio.gather(*(
            observe(process) for process in processes
            if selected_workspace_id is None or not process.workspace_ids or selected_workspace_id in process.workspace_ids
        )))


async def collect_gateway_diagnostics(
    registry: GatewayWorkspaceRegistry,
    *,
    gateway_id: str | None = None,
    gateway_name: str = "本机 Gateway",
    gateway_connection_id: str | None = None,
    connection_kind: GatewayConnectionKind = "local",
    selected_workspace_id: str | None = None,
    selected_log_id: str | None = None,
    tail_lines: int = 300,
) -> GatewayDiagnosticsDTO:
    if not 20 <= tail_lines <= 1000:
        raise ValueError("tail_lines 必须在 20..1000 范围内")
    local_gateway_id = gateway_id or load_or_create_gateway_id(
        get_gateway_root() / "identity.json"
    )
    workspace_dtos = [
        workspace_dto
        for workspace_dto in await registry.list_dtos()
        if workspace_dto.connection_kind == "local"
        and (
            selected_workspace_id is None
            or workspace_dto.workspace_id == selected_workspace_id
        )
    ]
    if selected_workspace_id is not None and not workspace_dtos:
        raise LookupError(f"Gateway 工作区不存在: {selected_workspace_id}")

    workspace_items = [
        GatewayDiagnosticWorkspaceDTO(
            workspace_id=workspace_dto.workspace_id,
            name=workspace_dto.name,
            connection_kind=workspace_dto.connection_kind,
            status=workspace_dto.status,
            managed=workspace_dto.managed,
            system_default=workspace_dto.system_default,
            connection_error=workspace_dto.connection_error,
        )
        for workspace_dto in workspace_dtos
    ]
    local_workspace_targets = [
        target
        for target in registry.targets()
        if target.connection_kind == "local"
        and (
            selected_workspace_id is None
            or target.workspace_id == selected_workspace_id
        )
    ]
    workspace_candidates = [
        candidate
        for target in local_workspace_targets
        for candidate in _workspace_candidates(target, registry)
    ]
    if selected_workspace_id is None:
        known_workspace_files = {
            candidate.log_path.name
            for candidate in workspace_candidates
            if candidate.log_path is not None
        }
        gateway_candidates = _candidate_gateway_logs(known_workspace_files)
    else:
        gateway_candidates = [_launcher_log_candidate()]
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
        preferred_log_candidates = (
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
                for candidate in preferred_log_candidates
                if candidate.log_path is not None and candidate.log_path.exists()
            ),
            preferred_log_candidates[0].log_id if preferred_log_candidates else None,
        )
    log_results = []
    for candidate in candidates:
        is_selected = candidate.log_id == selected_log_id
        log_dto = _read_log(
            candidate,
            tail_lines,
            include_tail=is_selected,
        )
        log_results.append(log_dto)
    processes = await _logging_process_diagnostics(registry, selected_workspace_id, candidates)
    return GatewayDiagnosticsDTO(
        gateway_id=local_gateway_id,
        gateway_name=gateway_name,
        gateway_connection_id=gateway_connection_id,
        connection_kind=connection_kind,
        status="degraded" if any(process.error is not None for process in processes) else _status(workspace_items),
        checked_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        selected_workspace_id=selected_workspace_id,
        selected_log_id=selected_log_id,
        workspaces=workspace_items,
        logs=log_results,
        processes=processes,
    )
