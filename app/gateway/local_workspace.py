from __future__ import annotations

from pathlib import Path

from app.gateway.processes import (
    allocate_local_port,
    start_local_backend_process,
    start_local_node_service_process,
    wait_for_http_ok,
)
from app.gateway.service_runtime import WorkspaceRuntime


async def start_managed_local_workspace_runtime(
    *,
    project_root: Path,
    workspace_root: Path,
    log_dir: Path,
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
    runtime = WorkspaceRuntime(service_urls=service_urls)
    try:
        terminal = start_local_node_service_process(
            project_root=project_root,
            workspace_root=workspace_root,
            service="terminal",
            port=terminal_port,
            log_dir=log_dir,
        )
        runtime.processes.append(terminal)
        browser = start_local_node_service_process(
            project_root=project_root,
            workspace_root=workspace_root,
            service="browser",
            port=browser_port,
            log_dir=log_dir,
        )
        runtime.processes.append(browser)
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
        )
        runtime.processes.append(backend)
        await wait_for_http_ok(
            f"{service_urls['workspace_api']}/api/v1/health",
            backend.process,
        )
        return runtime
    except Exception:
        runtime.close()
        raise
