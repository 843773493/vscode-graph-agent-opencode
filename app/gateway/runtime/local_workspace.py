from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import httpx

from app.gateway.runtime.process import (
    GATEWAY_PROCESS_READY_TIMEOUT_SECONDS,
    AdoptedManagedProcess,
    allocate_local_port,
    is_local_port_available,
    resolve_backend_workspace_id,
    start_local_backend_process,
    start_local_node_service_process,
    wait_for_http_ok,
)
from app.gateway.runtime.workspace import WorkspaceRuntime


def validate_workspace_root_access(workspace_root: Path) -> None:
    """在启动任何工作区子服务前确认业务目录具备写入权限。"""

    if not workspace_root.is_dir():
        raise FileNotFoundError(f"工作区目录不存在: {workspace_root}")
    if not os.access(workspace_root, os.W_OK | os.X_OK):
        raise PermissionError(
            "工作区目录不可写，无法创建 .boxteam 运行时数据: "
            f"{workspace_root}；请将目录所有者或写权限授予当前用户"
        )
    metadata_root = workspace_root / ".boxteam"
    if metadata_root.exists() and not metadata_root.is_dir():
        raise NotADirectoryError(
            f"工作区运行时目录不是目录: {metadata_root}"
        )
    if metadata_root.is_dir() and not os.access(
        metadata_root,
        os.W_OK | os.X_OK,
    ):
        raise PermissionError(
            "工作区运行时目录不可写，无法保存会话和服务状态: "
            f"{metadata_root}；请将目录所有者或写权限授予当前用户"
        )


async def wait_for_workspace_config_proof(
    url: str,
    process: object | None,
    *,
    backend_workspace_id: str,
    expected_proof: dict[str, object],
    request_timeout_seconds: float = 2,
    poll_interval_seconds: float = 0.5,
) -> None:
    """等待 Workspace 返回身份及 pending candidate 均匹配的健康证明。"""

    _validate_backend_workspace_id(backend_workspace_id)
    deadline = asyncio.get_running_loop().time() + GATEWAY_PROCESS_READY_TIMEOUT_SECONDS
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=request_timeout_seconds) as client:
        while asyncio.get_running_loop().time() < deadline:
            poll = getattr(process, "poll", None)
            if callable(poll) and (returncode := poll()) is not None:
                raise RuntimeError(f"进程提前退出: returncode={returncode}, url={url}")
            try:
                response = await client.get(
                    url,
                    headers=_workspace_backend_headers(backend_workspace_id),
                )
            except httpx.HTTPError as error:
                last_error = error
            else:
                if response.status_code != 200:
                    last_error = RuntimeError(
                        f"健康检查返回 {response.status_code}: {response.text[:300]}"
                    )
                else:
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise TypeError("Workspace 健康响应必须是 JSON 对象")
                    actual_workspace_id = payload.get("workspace_id")
                    if actual_workspace_id != backend_workspace_id:
                        raise RuntimeError(
                            "Workspace 健康响应 backend UUID 不匹配: "
                            f"expected={backend_workspace_id}, "
                            f"actual={actual_workspace_id!r}"
                        )
                    proof = payload.get("config_proof")
                    if not isinstance(proof, dict):
                        raise TypeError("Workspace 健康响应缺少 config_proof")
                    mismatches = {
                        key: (expected_proof[key], proof.get(key))
                        for key in expected_proof
                        if proof.get(key) != expected_proof[key]
                    }
                    if not mismatches:
                        return
                    last_error = RuntimeError(
                        f"Workspace config proof 不匹配: {mismatches}"
                    )
            await asyncio.sleep(poll_interval_seconds)
    detail = f"，最后错误: {last_error}" if last_error else ""
    raise TimeoutError(
        f"Workspace config proof 在 {GATEWAY_PROCESS_READY_TIMEOUT_SECONDS} 秒内未匹配: "
        f"{url}{detail}"
    )


async def wait_for_workspace_health(
    url: str,
    backend_workspace_id: str,
    process: object | None = None,
    *,
    request_timeout_seconds: float = 2,
    poll_interval_seconds: float = 0.5,
) -> dict[str, object]:
    """等待显式寻址的 Workspace API 健康，并核对后端 UUID。"""

    _validate_backend_workspace_id(backend_workspace_id)
    deadline = asyncio.get_running_loop().time() + GATEWAY_PROCESS_READY_TIMEOUT_SECONDS
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=request_timeout_seconds) as client:
        while asyncio.get_running_loop().time() < deadline:
            poll = getattr(process, "poll", None)
            if callable(poll) and (returncode := poll()) is not None:
                raise RuntimeError(f"进程提前退出: returncode={returncode}, url={url}")
            try:
                response = await client.get(
                    url,
                    headers=_workspace_backend_headers(backend_workspace_id),
                )
            except httpx.HTTPError as error:
                last_error = error
            else:
                if response.status_code != 200:
                    last_error = RuntimeError(
                        f"健康检查返回 {response.status_code}: {response.text[:300]}"
                    )
                else:
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise TypeError("Workspace 健康响应必须是 JSON 对象")
                    actual_workspace_id = payload.get("workspace_id")
                    if actual_workspace_id != backend_workspace_id:
                        raise RuntimeError(
                            "Workspace 健康响应 backend UUID 不匹配: "
                            f"expected={backend_workspace_id}, "
                            f"actual={actual_workspace_id!r}"
                        )
                    return payload
            await asyncio.sleep(poll_interval_seconds)
    detail = f"，最后错误: {last_error}" if last_error else ""
    raise TimeoutError(
        f"Workspace backend UUID 健康检查在 "
        f"{GATEWAY_PROCESS_READY_TIMEOUT_SECONDS} 秒内未通过: {url}{detail}"
    )


def _validate_backend_workspace_id(workspace_id: str) -> str:
    try:
        parsed_workspace_id = UUID(workspace_id)
    except ValueError as error:
        raise ValueError(f"Workspace backend UUID 非法: {workspace_id!r}") from error
    if str(parsed_workspace_id) != workspace_id:
        raise ValueError(f"Workspace backend UUID 必须是标准文本: {workspace_id!r}")
    return workspace_id


def _workspace_backend_headers(workspace_id: str) -> dict[str, str]:
    return {
        "X-Local-Token": "local-dev-token",
        "X-BoxTeam-Workspace-Id": _validate_backend_workspace_id(workspace_id),
    }


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
            "Workspace 启动契约缺少 config proof 字段: " + ", ".join(missing)
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
    service_name: str,
    backend_workspace_id: str,
) -> AdoptedManagedProcess | None:
    _validate_backend_workspace_id(backend_workspace_id)
    parsed = urlparse(service_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError(
            f"持久化 {service_name} URL 必须是本机 HTTP 地址: {service_url}"
        )
    if parsed.port is None:
        raise ValueError(f"持久化 {service_name} URL 缺少端口: {service_url}")
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(f"{service_url.rstrip('/')}/health")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        raise RuntimeError(
            f"持久化 {service_name} 健康检查失败: "
            f"url={service_url}, status={response.status_code}, body={response.text[:300]}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError(f"持久化 {service_name} 健康响应必须是 JSON 对象: {service_url}")
    if payload.get("workspace_id") != backend_workspace_id:
        raise RuntimeError(
            f"持久化 {service_name} backend UUID 不匹配: "
            f"url={service_url}, expected={backend_workspace_id}, "
            f"actual={payload.get('workspace_id')!r}"
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
    backend_workspace_id: str,
) -> AdoptedManagedProcess | None:
    return await _adopt_local_node_service(
        service_url=service_url,
        service_name="Browser Manager",
        backend_workspace_id=backend_workspace_id,
    )


async def _adopt_terminal_manager(
    *,
    service_url: str,
    backend_workspace_id: str,
) -> AdoptedManagedProcess | None:
    return await _adopt_local_node_service(
        service_url=service_url,
        service_name="Terminal Manager",
        backend_workspace_id=backend_workspace_id,
    )


async def _adopt_workspace_backend(
    *,
    service_url: str,
    backend_workspace_id: str,
) -> AdoptedManagedProcess | None:
    parsed = urlparse(service_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError(
            f"持久化 Workspace API URL 必须是本机 HTTP 地址: {service_url}"
        )
    if parsed.port is None:
        raise ValueError(f"持久化 Workspace API URL 缺少端口: {service_url}")
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(
                f"{service_url.rstrip('/')}/api/v1/health",
                headers=_workspace_backend_headers(backend_workspace_id),
            )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        raise RuntimeError(
            "持久化 Workspace API 健康检查失败: "
            f"url={service_url}, status={response.status_code}, "
            f"body={response.text[:300]}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError("持久化 Workspace API 健康响应必须是 JSON 对象")
    if payload.get("workspace_id") != backend_workspace_id:
        raise RuntimeError(
            "持久化 Workspace API backend UUID 不匹配: "
            f"url={service_url}, expected={backend_workspace_id}, "
            f"actual={payload.get('workspace_id')!r}"
        )
    process_id = payload.get("process_id")
    if isinstance(process_id, bool) or not isinstance(process_id, int):
        raise TypeError(
            "持久化 Workspace API 健康响应缺少 process_id: "
            f"url={service_url}, payload={payload!r}"
        )
    return AdoptedManagedProcess(pid=process_id)


async def start_managed_local_workspace_runtime(
    *,
    project_root: Path,
    workspace_root: Path,
    expected_backend_workspace_id: str | None = None,
    log_dir: Path,
    backend_debug_port: int | None = None,
    preferred_backend_port: int | None = None,
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
    validate_workspace_root_access(workspace_root)
    backend_workspace_id = await asyncio.to_thread(
        resolve_backend_workspace_id,
        project_root,
        workspace_root,
    )
    if (
        expected_backend_workspace_id is not None
        and expected_backend_workspace_id != backend_workspace_id
    ):
        raise RuntimeError(
            "托管 Workspace backend UUID 与 Gateway registry 不一致: "
            f"expected={expected_backend_workspace_id}, actual={backend_workspace_id}, "
            f"workspace_root={workspace_root}"
        )
    if preferred_backend_port is not None and not 1 <= preferred_backend_port <= 65535:
        raise ValueError(
            f"preferred_backend_port 必须是 1-65535 的端口号: {preferred_backend_port}"
        )
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
            backend_workspace_id=backend_workspace_id,
        )
        reusable_backend_port = urlparse(reusable_backend_url).port
        if reusable_backend_port is None:
            raise RuntimeError(
                f"持久化 Workspace API URL 缺少端口: {reusable_backend_url}"
            )
        if (
            preferred_backend_port is not None
            and candidate_backend is not None
            and reusable_backend_port != preferred_backend_port
        ):
            # 默认 Home 的端口是发布版稳定契约；旧版本留下的动态后端不能
            # 继续被接管，否则 workspaces.json 会一直保留错误端口。
            if adopt_existing_backend:
                raise RuntimeError(
                    "持久化 Workspace API 端口与首选端口不匹配: "
                    f"actual={reusable_backend_port}, "
                    f"preferred={preferred_backend_port}"
                )
            await asyncio.to_thread(candidate_backend.close)
            candidate_backend = None
        if adopt_existing_backend:
            adopted_backend = candidate_backend
        elif candidate_backend is not None:
            await asyncio.to_thread(candidate_backend.close)
    if preferred_backend_port is not None:
        backend_port = preferred_backend_port
        if adopted_backend is None and not is_local_port_available(backend_port):
            raise RuntimeError(
                f"默认 Workspace API 端口已被其它进程占用: 127.0.0.1:{backend_port}"
            )
    elif reusable_backend_url and adopted_backend is not None:
        backend_port = urlparse(reusable_backend_url).port
    else:
        backend_port = next_port()
    if backend_port is None:
        raise RuntimeError(f"持久化 Workspace API URL 缺少端口: {reusable_backend_url}")
    allocated_ports.add(backend_port)
    reusable_terminal_url = (reusable_service_urls or {}).get("terminal_manager")
    adopted_terminal = (
        await _adopt_terminal_manager(
            service_url=reusable_terminal_url,
            backend_workspace_id=backend_workspace_id,
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
            backend_workspace_id=backend_workspace_id,
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
        backend_workspace_id=backend_workspace_id,
    )
    try:
        if adopted_terminal is None:
            terminal = start_local_node_service_process(
                project_root=project_root,
                workspace_root=workspace_root,
                service="terminal",
                port=terminal_port,
                log_dir=log_dir,
                workspace_id=backend_workspace_id,
            )
            terminal_process = terminal.process
        else:
            terminal = adopted_terminal
            terminal_process = None
        runtime.set_process(
            "terminal_manager", terminal, adopted=adopted_terminal is not None
        )
        if adopted_browser is None:
            browser = start_local_node_service_process(
                project_root=project_root,
                workspace_root=workspace_root,
                service="browser",
                workspace_id=backend_workspace_id,
                port=browser_port,
                log_dir=log_dir,
            )
            browser_process = browser.process
        else:
            browser = adopted_browser
            browser_process = None
        runtime.set_process("browser_manager", browser, adopted=adopted_browser is not None)
        await wait_for_workspace_health(
            f"{service_urls['terminal_manager']}/health",
            backend_workspace_id,
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
                backend_workspace_id=backend_workspace_id,
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
        runtime.set_process("workspace_api", backend, adopted=adopted_backend is not None)
        await wait_for_workspace_health(
            f"{service_urls['workspace_api']}/api/v1/health",
            backend_workspace_id,
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
            runtime.detach_adopted_processes()
        runtime.close()
        raise


async def restart_managed_workspace_backend(
    *,
    runtime: WorkspaceRuntime,
    project_root: Path,
    workspace_root: Path,
    expected_backend_workspace_id: str | None = None,
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
    backend_workspace_id = await asyncio.to_thread(
        resolve_backend_workspace_id,
        project_root,
        workspace_root,
    )
    expected_id = expected_backend_workspace_id or runtime.backend_workspace_id
    if expected_id is not None and expected_id != backend_workspace_id:
        raise RuntimeError(
            "重启 Workspace backend UUID 与当前运行时不一致: "
            f"expected={expected_id}, actual={backend_workspace_id}, "
            f"workspace_root={workspace_root}"
        )
    backend_url = runtime.service_urls["workspace_api"]
    parsed_backend_url = urlparse(backend_url)
    if parsed_backend_url.port is None:
        raise ValueError(f"Workspace API URL 缺少端口: {backend_url}")
    old_backend = runtime.processes.get("workspace_api")
    if old_backend is None:
        raise RuntimeError("Workspace 重启缺少当前后端进程句柄")
    old_port = parsed_backend_url.port

    def backend_environment(*, include_config_startup_contract: bool) -> dict[str, str]:
        environment = {
            "BOXTEAM_TERMINAL_BACKEND_URL": runtime.service_urls["terminal_manager"],
            "BOXTEAM_BROWSER_BACKEND_URL": runtime.service_urls["browser_manager"],
            "BOXTEAM_DEFAULT_SKILL_GROUPS": json.dumps(
                list(default_skill_groups),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        if include_config_startup_contract and config_candidate_ref is not None:
            environment.update(
                {
                    "BOXTEAM_CONFIG_CANDIDATE_REF": config_candidate_ref,
                    "BOXTEAM_CONFIG_GENERATION": config_generation or "",
                    "BOXTEAM_CONFIG_FENCING_TOKEN": config_fencing_token or "",
                }
            )
        return environment

    # SQLite 状态库的进程所有权锁要求同一工作区同一时刻只有一个后端。
    # 因此候选后端必须在旧后端彻底退出后启动；旧后端若已成功退出但候选
    # 启动失败，则在原端口恢复一个当前配置的后端，交给上层取消 drain。
    old_backend.close(timeout_seconds=connection_drain_timeout_seconds)
    candidate_port = allocate_local_port()
    candidate_url = f"http://127.0.0.1:{candidate_port}"
    backend = None
    recovered_backend = None
    try:
        backend = start_local_backend_process(
            project_root=project_root,
            workspace_root=workspace_root,
            backend_workspace_id=backend_workspace_id,
            port=candidate_port,
            log_dir=log_dir,
            extra_env=backend_environment(include_config_startup_contract=True),
            debug_port=runtime.backend_debug_port,
            connection_drain_timeout_seconds=connection_drain_timeout_seconds,
        )
        if config_startup_contract is None:
            await wait_for_workspace_health(
                f"{candidate_url}/api/v1/health",
                backend_workspace_id,
                backend.process,
                request_timeout_seconds=health_request_timeout_seconds,
                poll_interval_seconds=health_poll_interval_seconds,
            )
        else:
            await wait_for_workspace_config_proof(
                f"{candidate_url}/api/v1/health",
                backend.process,
                backend_workspace_id=backend_workspace_id,
                expected_proof=workspace_config_proof_expectation(
                    config_startup_contract
                ),
                request_timeout_seconds=health_request_timeout_seconds,
                poll_interval_seconds=health_poll_interval_seconds,
            )
        runtime.replace_process("workspace_api", backend)
        runtime.service_urls["workspace_api"] = candidate_url
        runtime.backend_workspace_id = backend_workspace_id
    except Exception as error:
        if backend is not None:
            backend.close()
        try:
            recovered_backend = start_local_backend_process(
                project_root=project_root,
                workspace_root=workspace_root,
                backend_workspace_id=backend_workspace_id,
                port=old_port,
                log_dir=log_dir,
                extra_env=backend_environment(include_config_startup_contract=False),
                debug_port=runtime.backend_debug_port,
                connection_drain_timeout_seconds=connection_drain_timeout_seconds,
            )
            await wait_for_workspace_health(
                f"{backend_url.rstrip('/')}/api/v1/health",
                backend_workspace_id,
                recovered_backend.process,
                request_timeout_seconds=health_request_timeout_seconds,
                poll_interval_seconds=health_poll_interval_seconds,
            )
        except Exception as recovery_error:
            if recovered_backend is not None:
                recovered_backend.close()
            runtime.remove_process("workspace_api")
            raise RuntimeError(
                "Workspace 候选后端启动失败，且旧后端恢复失败: "
                f"candidate={error}; recovery={recovery_error}"
            ) from recovery_error
        runtime.replace_process("workspace_api", recovered_backend)
        runtime.service_urls["workspace_api"] = backend_url
        raise
