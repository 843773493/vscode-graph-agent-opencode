from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.gateway.runtime.process import (
    AdoptedManagedProcess,
    allocate_local_port,
    start_local_backend_process,
    start_local_node_service_process,
    wait_for_http_ok,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.workspace_ids import build_managed_local_workspace_id


async def _adopt_browser_manager(
    *,
    service_url: str,
    workspace_root: Path,
) -> AdoptedManagedProcess | None:
    parsed = urlparse(service_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError(f"持久化 Browser Manager URL 必须是本机 HTTP 地址: {service_url}")
    if parsed.port is None:
        raise ValueError(f"持久化 Browser Manager URL 缺少端口: {service_url}")
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(f"{service_url.rstrip('/')}/health")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        raise RuntimeError(
            "持久化 Browser Manager 健康检查失败: "
            f"url={service_url}, status={response.status_code}, body={response.text[:300]}"
        )
    payload = response.json()
    expected_workspace = str(workspace_root.resolve())
    if payload.get("workspace_root") != expected_workspace:
        raise RuntimeError(
            "持久化 Browser Manager 工作区身份不匹配: "
            f"url={service_url}, expected={expected_workspace}, "
            f"actual={payload.get('workspace_root')!r}"
        )
    process_id = payload.get("process_id")
    if isinstance(process_id, bool) or not isinstance(process_id, int):
        raise TypeError(
            f"持久化 Browser Manager 健康响应缺少 process_id: url={service_url}"
        )
    return AdoptedManagedProcess(pid=process_id)


async def start_managed_local_workspace_runtime(
    *,
    project_root: Path,
    workspace_root: Path,
    log_dir: Path,
    backend_debug_port: int | None = None,
    reusable_service_urls: dict[str, str] | None = None,
    health_request_timeout_seconds: float = 2,
    health_poll_interval_seconds: float = 0.5,
    connection_drain_timeout_seconds: float = 2,
    default_skill_groups: Sequence[str] = (),
) -> WorkspaceRuntime:
    workspace_id = build_managed_local_workspace_id(str(workspace_root.resolve()))
    allocated_ports: set[int] = set()

    def next_port() -> int:
        port = allocate_local_port()
        while port in allocated_ports:
            port = allocate_local_port()
        allocated_ports.add(port)
        return port

    backend_port = next_port()
    terminal_port = next_port()
    reusable_browser_url = (reusable_service_urls or {}).get("browser_manager")
    adopted_browser = (
        await _adopt_browser_manager(
            service_url=reusable_browser_url,
            workspace_root=workspace_root,
        )
        if reusable_browser_url
        else None
    )
    browser_port = (
        urlparse(reusable_browser_url).port
        if reusable_browser_url and adopted_browser is not None
        else next_port()
    )
    if browser_port is None:
        raise RuntimeError(f"Browser Manager URL 缺少端口: {reusable_browser_url}")
    service_urls = {
        "workspace_api": f"http://127.0.0.1:{backend_port}",
        "terminal_manager": f"http://127.0.0.1:{terminal_port}",
        "browser_manager": f"http://127.0.0.1:{browser_port}",
    }
    runtime = WorkspaceRuntime(
        service_urls=service_urls,
        backend_debug_port=backend_debug_port,
    )
    try:
        terminal = start_local_node_service_process(
            project_root=project_root,
            workspace_root=workspace_root,
            workspace_id=workspace_id,
            service="terminal",
            port=terminal_port,
            log_dir=log_dir,
        )
        runtime.set_process("terminal_manager", terminal)
        if adopted_browser is None:
            browser = start_local_node_service_process(
                project_root=project_root,
                workspace_root=workspace_root,
                workspace_id=workspace_id,
                service="browser",
                port=browser_port,
                log_dir=log_dir,
            )
            browser_process = browser.process
        else:
            browser = adopted_browser
            browser_process = None
        runtime.set_process("browser_manager", browser)
        await wait_for_http_ok(
            f"{service_urls['terminal_manager']}/health",
            terminal.process,
            request_timeout_seconds=health_request_timeout_seconds,
            poll_interval_seconds=health_poll_interval_seconds,
        )
        await wait_for_http_ok(
            f"{service_urls['browser_manager']}/health",
            browser_process,
            request_timeout_seconds=health_request_timeout_seconds,
            poll_interval_seconds=health_poll_interval_seconds,
        )

        backend = start_local_backend_process(
            project_root=project_root,
            workspace_root=workspace_root,
            port=backend_port,
            log_dir=log_dir,
            extra_env={
                "BOXTEAM_TERMINAL_BACKEND_URL": service_urls["terminal_manager"],
                "BOXTEAM_BROWSER_BACKEND_URL": service_urls["browser_manager"],
                "BOXTEAM_DEFAULT_SKILL_GROUPS": json.dumps(
                    list(default_skill_groups),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
            debug_port=backend_debug_port,
            connection_drain_timeout_seconds=connection_drain_timeout_seconds,
        )
        runtime.set_process("workspace_api", backend)
        await wait_for_http_ok(
            f"{service_urls['workspace_api']}/api/v1/health",
            backend.process,
            request_timeout_seconds=health_request_timeout_seconds,
            poll_interval_seconds=health_poll_interval_seconds,
        )
        return runtime
    except Exception:
        runtime.close_for_gateway_restart()
        raise


async def restart_managed_workspace_backend(
    *,
    runtime: WorkspaceRuntime,
    project_root: Path,
    workspace_root: Path,
    log_dir: Path,
    health_request_timeout_seconds: float = 2,
    health_poll_interval_seconds: float = 0.5,
    connection_drain_timeout_seconds: float = 2,
    default_skill_groups: Sequence[str] = (),
) -> None:
    backend_url = runtime.service_urls["workspace_api"]
    backend_port = int(backend_url.rsplit(":", 1)[1])
    runtime.close_process("workspace_api")
    backend = start_local_backend_process(
        project_root=project_root,
        workspace_root=workspace_root,
        port=backend_port,
        log_dir=log_dir,
        extra_env={
            "BOXTEAM_TERMINAL_BACKEND_URL": runtime.service_urls[
                "terminal_manager"
            ],
            "BOXTEAM_BROWSER_BACKEND_URL": runtime.service_urls[
                "browser_manager"
            ],
            "BOXTEAM_DEFAULT_SKILL_GROUPS": json.dumps(
                list(default_skill_groups),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
        debug_port=runtime.backend_debug_port,
        connection_drain_timeout_seconds=connection_drain_timeout_seconds,
    )
    runtime.set_process("workspace_api", backend)
    try:
        await wait_for_http_ok(
            f"{runtime.service_urls['workspace_api']}/api/v1/health",
            backend.process,
            request_timeout_seconds=health_request_timeout_seconds,
            poll_interval_seconds=health_poll_interval_seconds,
        )
    except Exception:
        runtime.close_process("workspace_api")
        raise
