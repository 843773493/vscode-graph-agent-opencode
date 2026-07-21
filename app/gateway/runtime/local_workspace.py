from __future__ import annotations

from pathlib import Path

from app.gateway.runtime.process import (
    allocate_local_port,
    start_local_backend_process,
    start_local_node_service_process,
    wait_for_http_ok,
)
from app.gateway.runtime.workspace import WorkspaceRuntime


async def start_managed_local_workspace_runtime(
    *,
    project_root: Path,
    workspace_root: Path,
    log_dir: Path,
    backend_debug_port: int | None = None,
) -> WorkspaceRuntime:
    allocated_ports: set[int] = set()

    def next_port() -> int:
        port = allocate_local_port()
        while port in allocated_ports:
            port = allocate_local_port()
        allocated_ports.add(port)
        return port

    backend_port = next_port()
    terminal_port = next_port()
    browser_port = next_port()
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
            service="terminal",
            port=terminal_port,
            log_dir=log_dir,
        )
        runtime.set_process("terminal_manager", terminal)
        browser = start_local_node_service_process(
            project_root=project_root,
            workspace_root=workspace_root,
            service="browser",
            port=browser_port,
            log_dir=log_dir,
        )
        runtime.set_process("browser_manager", browser)
        await wait_for_http_ok(f"{service_urls['terminal_manager']}/health", terminal.process)
        await wait_for_http_ok(f"{service_urls['browser_manager']}/health", browser.process)

        backend = start_local_backend_process(
            project_root=project_root,
            workspace_root=workspace_root,
            port=backend_port,
            log_dir=log_dir,
            extra_env={
                "BOXTEAM_TERMINAL_BACKEND_URL": service_urls["terminal_manager"],
                "BOXTEAM_BROWSER_BACKEND_URL": service_urls["browser_manager"],
            },
            debug_port=backend_debug_port,
        )
        runtime.set_process("workspace_api", backend)
        await wait_for_http_ok(
            f"{service_urls['workspace_api']}/api/v1/health",
            backend.process,
        )
        return runtime
    except Exception:
        runtime.close()
        raise


async def restart_managed_workspace_backend(
    *,
    runtime: WorkspaceRuntime,
    project_root: Path,
    workspace_root: Path,
    log_dir: Path,
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
        },
        debug_port=runtime.backend_debug_port,
    )
    runtime.set_process("workspace_api", backend)
    try:
        await wait_for_http_ok(
            f"{runtime.service_urls['workspace_api']}/api/v1/health",
            backend.process,
        )
    except Exception:
        runtime.close_process("workspace_api")
        raise
