from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.gateway.runtime.process import (
    GATEWAY_PROCESS_READY_TIMEOUT_SECONDS,
    AdoptedManagedProcess,
    allocate_local_port,
    start_local_backend_process,
    start_local_node_service_process,
    wait_for_http_ok,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.workspace_ids import build_managed_local_workspace_id


async def wait_for_workspace_config_proof(
    url: str,
    process: object | None,
    *,
    expected_proof: dict[str, object],
    request_timeout_seconds: float = 2,
    poll_interval_seconds: float = 0.5,
) -> None:
    """等待 Workspace 返回与 pending candidate 完全匹配的健康证明。"""

    deadline = (
        asyncio.get_running_loop().time() + GATEWAY_PROCESS_READY_TIMEOUT_SECONDS
    )
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=request_timeout_seconds) as client:
        while asyncio.get_running_loop().time() < deadline:
            poll = getattr(process, "poll", None)
            if callable(poll) and (returncode := poll()) is not None:
                raise RuntimeError(
                    f"进程提前退出: returncode={returncode}, url={url}"
                )
            try:
                response = await client.get(
                    url,
                    headers={"X-Local-Token": "local-dev-token"},
                )
                if response.status_code != 200:
                    raise RuntimeError(
                        f"健康检查返回 {response.status_code}: {response.text[:300]}"
                    )
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("Workspace 健康响应必须是 JSON 对象")
                proof = payload.get("config_proof")
                if not isinstance(proof, dict):
                    raise RuntimeError("Workspace 健康响应缺少 config_proof")
                mismatches = {
                    key: (expected_proof[key], proof.get(key))
                    for key in expected_proof
                    if proof.get(key) != expected_proof[key]
                }
                if not mismatches:
                    return
                raise RuntimeError(f"Workspace config proof 不匹配: {mismatches}")
            except (httpx.HTTPError, RuntimeError, ValueError, TypeError) as error:
                last_error = error
            await asyncio.sleep(poll_interval_seconds)
    detail = f"，最后错误: {last_error}" if last_error else ""
    raise TimeoutError(
        f"Workspace config proof 在 {GATEWAY_PROCESS_READY_TIMEOUT_SECONDS} 秒内未匹配: "
        f"{url}{detail}"
    )


def workspace_config_proof_expectation(
    startup_contract: dict[str, object],
) -> dict[str, object]:
    """将 Gateway 收到的启动契约转换为不含秘密的 Workspace proof 期望值。"""

    fencing_token = startup_contract.get("fencing_token")
    if not isinstance(fencing_token, str) or not fencing_token:
        raise ValueError("Workspace 启动契约缺少 fencing_token")
    required_fields = (
        "candidate_id",
        "pending_revision",
        "candidate_digest",
        "effective_digest",
        "target_generation",
        "secret_binding_digest",
    )
    missing = [field for field in required_fields if field not in startup_contract]
    if missing:
        raise ValueError(
            "Workspace 启动契约缺少 config proof 字段: "
            + ", ".join(missing)
        )
    return {
        "config_domain": "workspace",
        "loaded_source": "pending",
        "candidate_id": startup_contract["candidate_id"],
        "loaded_commit_revision": startup_contract["pending_revision"],
        "effective_digest": startup_contract["effective_digest"],
        "candidate_digest": startup_contract["candidate_digest"],
        "secret_binding_digest": startup_contract["secret_binding_digest"],
        "generation_id": startup_contract["target_generation"],
        "fencing_token_digest": hashlib.sha256(
            fencing_token.encode("utf-8")
        ).hexdigest(),
    }


async def _adopt_local_node_service(
    *,
    service_url: str,
    workspace_root: Path,
    service_name: str,
    health_path: str = "/health",
) -> AdoptedManagedProcess | None:
    parsed = urlparse(service_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError(f"持久化 {service_name} URL 必须是本机 HTTP 地址: {service_url}")
    if parsed.port is None:
        raise ValueError(f"持久化 {service_name} URL 缺少端口: {service_url}")
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(
                f"{service_url.rstrip('/')}{health_path}"
            )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        raise RuntimeError(
            f"持久化 {service_name} 健康检查失败: "
            f"url={service_url}, status={response.status_code}, body={response.text[:300]}"
        )
    payload = response.json()
    expected_workspace = str(workspace_root.resolve())
    if payload.get("workspace_root") != expected_workspace:
        raise RuntimeError(
            f"持久化 {service_name} 工作区身份不匹配: "
            f"url={service_url}, expected={expected_workspace}, "
            f"actual={payload.get('workspace_root')!r}"
        )
    process_id = payload.get("process_id")
    if isinstance(process_id, bool) or not isinstance(process_id, int):
        raise TypeError(
            f"持久化 {service_name} 健康响应缺少 process_id: url={service_url}"
        )
    return AdoptedManagedProcess(pid=process_id)


async def _adopt_browser_manager(
    *,
    service_url: str,
    workspace_root: Path,
) -> AdoptedManagedProcess | None:
    return await _adopt_local_node_service(
        service_url=service_url,
        workspace_root=workspace_root,
        service_name="Browser Manager",
    )


async def _adopt_terminal_manager(
    *,
    service_url: str,
    workspace_root: Path,
) -> AdoptedManagedProcess | None:
    return await _adopt_local_node_service(
        service_url=service_url,
        workspace_root=workspace_root,
        service_name="Terminal Manager",
    )


async def _adopt_workspace_backend(
    *,
    service_url: str,
    workspace_root: Path,
) -> AdoptedManagedProcess | None:
    return await _adopt_local_node_service(
        service_url=service_url,
        workspace_root=workspace_root,
        service_name="Workspace API",
        health_path="/api/v1/health",
    )


async def start_managed_local_workspace_runtime(
    *,
    project_root: Path,
    workspace_root: Path,
    log_dir: Path,
    backend_debug_port: int | None = None,
    reusable_backend_url: str | None = None,
    adopt_existing_backend: bool = True,
    reusable_service_urls: dict[str, str] | None = None,
    health_request_timeout_seconds: float = 2,
    health_poll_interval_seconds: float = 0.5,
    connection_drain_timeout_seconds: float = 2,
    default_skill_groups: Sequence[str] = (),
    config_candidate_ref: str | None = None,
    config_generation: str | None = None,
    config_fencing_token: str | None = None,
    preserve_adopted_processes_on_failure: bool = False,
) -> WorkspaceRuntime:
    workspace_id = build_managed_local_workspace_id(str(workspace_root.resolve()))
    allocated_ports: set[int] = set()

    def next_port() -> int:
        port = allocate_local_port()
        while port in allocated_ports:
            port = allocate_local_port()
        allocated_ports.add(port)
        return port

    adopted_backend = None
    if reusable_backend_url:
        candidate_backend = await _adopt_workspace_backend(
            service_url=reusable_backend_url,
            workspace_root=workspace_root,
        )
        if adopt_existing_backend:
            adopted_backend = candidate_backend
        elif candidate_backend is not None:
            await asyncio.to_thread(candidate_backend.close)
    backend_port = (
        urlparse(reusable_backend_url).port
        if reusable_backend_url and adopted_backend is not None
        else next_port()
    )
    if backend_port is None:
        raise RuntimeError(f"持久化 Workspace API URL 缺少端口: {reusable_backend_url}")
    reusable_terminal_url = (reusable_service_urls or {}).get("terminal_manager")
    adopted_terminal = (
        await _adopt_terminal_manager(
            service_url=reusable_terminal_url,
            workspace_root=workspace_root,
        )
        if reusable_terminal_url
        else None
    )
    terminal_port = (
        urlparse(reusable_terminal_url).port
        if reusable_terminal_url and adopted_terminal is not None
        else next_port()
    )
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
        if adopted_terminal is None:
            terminal = start_local_node_service_process(
                project_root=project_root,
                workspace_root=workspace_root,
                workspace_id=workspace_id,
                service="terminal",
                port=terminal_port,
                log_dir=log_dir,
            )
            terminal_process = terminal.process
        else:
            terminal = adopted_terminal
            terminal_process = None
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
            terminal_process,
            request_timeout_seconds=health_request_timeout_seconds,
            poll_interval_seconds=health_poll_interval_seconds,
        )
        await wait_for_http_ok(
            f"{service_urls['browser_manager']}/health",
            browser_process,
            request_timeout_seconds=health_request_timeout_seconds,
            poll_interval_seconds=health_poll_interval_seconds,
        )

        if adopted_backend is None:
            config_env: dict[str, str] = {}
            if config_candidate_ref is not None:
                if not config_generation or not config_fencing_token:
                    raise ValueError(
                        "Workspace pending 启动必须同时提供 candidate_ref、generation 和 fencing token"
                    )
                config_env = {
                    "BOXTEAM_CONFIG_CANDIDATE_REF": config_candidate_ref,
                    "BOXTEAM_CONFIG_GENERATION": config_generation,
                    "BOXTEAM_CONFIG_FENCING_TOKEN": config_fencing_token,
                }
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
                    **config_env,
                },
                debug_port=backend_debug_port,
                connection_drain_timeout_seconds=connection_drain_timeout_seconds,
            )
            backend_process = backend.process
        else:
            backend = adopted_backend
            backend_process = None
        runtime.set_process("workspace_api", backend)
        await wait_for_http_ok(
            f"{service_urls['workspace_api']}/api/v1/health",
            backend_process,
            request_timeout_seconds=health_request_timeout_seconds,
            poll_interval_seconds=health_poll_interval_seconds,
        )
        return runtime
    except (Exception, asyncio.CancelledError):
        # 启动失败或任务取消时没有确认的后继 Gateway，不能把已接管的进程
        # 脱离后遗留为孤儿；真正的旧 generation handoff 在 registry.close()
        # 中按 pending intent 单独决定保留哪些已运行服务。
        if preserve_adopted_processes_on_failure:
            if adopted_backend is not None:
                runtime.detach_process("workspace_api")
            if adopted_terminal is not None:
                runtime.detach_process("terminal_manager")
            if adopted_browser is not None:
                runtime.detach_process("browser_manager")
        runtime.close()
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
    config_candidate_ref: str | None = None,
    config_generation: str | None = None,
    config_fencing_token: str | None = None,
    config_startup_contract: dict[str, object] | None = None,
) -> None:
    backend_url = runtime.service_urls["workspace_api"]
    parsed_backend_url = urlparse(backend_url)
    if parsed_backend_url.port is None:
        raise ValueError(f"Workspace API URL 缺少端口: {backend_url}")
    old_backend = runtime.processes.get("workspace_api")
    candidate_port = allocate_local_port()
    candidate_url = f"http://127.0.0.1:{candidate_port}"
    backend = start_local_backend_process(
        project_root=project_root,
        workspace_root=workspace_root,
        port=candidate_port,
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
            **(
                {
                    "BOXTEAM_CONFIG_CANDIDATE_REF": config_candidate_ref,
                    "BOXTEAM_CONFIG_GENERATION": config_generation,
                    "BOXTEAM_CONFIG_FENCING_TOKEN": config_fencing_token,
                }
                if config_candidate_ref is not None
                else {}
            ),
        },
        debug_port=runtime.backend_debug_port,
        connection_drain_timeout_seconds=connection_drain_timeout_seconds,
    )
    try:
        if config_startup_contract is None:
            await wait_for_http_ok(
                f"{candidate_url}/api/v1/health",
                backend.process,
                request_timeout_seconds=health_request_timeout_seconds,
                poll_interval_seconds=health_poll_interval_seconds,
            )
        else:
            await wait_for_workspace_config_proof(
                f"{candidate_url}/api/v1/health",
                backend.process,
                expected_proof=workspace_config_proof_expectation(
                    config_startup_contract
                ),
                request_timeout_seconds=health_request_timeout_seconds,
                poll_interval_seconds=health_poll_interval_seconds,
            )
        if old_backend is None:
            raise RuntimeError("Workspace 重启缺少当前后端进程句柄")
        old_backend.close(timeout_seconds=connection_drain_timeout_seconds)
        runtime.processes["workspace_api"] = backend
        runtime.service_urls["workspace_api"] = candidate_url
    except Exception:
        backend.close()
        raise
