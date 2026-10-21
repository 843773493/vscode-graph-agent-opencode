from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx

from app.core.mounted_workspaces import (
    MountedWorkspace,
    mounted_workspace_manifest_digest,
)
from app.gateway.runtime.managed_backend import (
    ManagedBackendContract,
    ManagedBackendMember,
    ManagedBackendOwner,
)
from app.gateway.runtime.process_lifecycle import AdoptedManagedProcess
from app.gateway.runtime.process_ports import (
    allocate_local_port,
    is_local_port_available,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.runtime.workspace_process import (
    GATEWAY_PROCESS_READY_TIMEOUT_SECONDS,
    resolve_backend_workspace_id,
    start_local_backend_process,
    start_local_node_service_process,
)

if TYPE_CHECKING:
    from app.gateway.registry import GatewayWorkspaceRegistry


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


def _validate_group_health(
    payload: dict[str, object], contract: ManagedBackendContract, mount: MountedWorkspace,
    *, manifest_digest: str, process_id: int | None = None, startup_contract: dict[str, object] | None = None,
) -> int:
    expected = {
        "workspace_id": mount.workspace_id, "backend_generation": contract.generation,
        "mount_manifest_digest": manifest_digest, "status": "ok",
    }
    mismatches = {key: (value, payload.get(key)) for key, value in expected.items() if payload.get(key) != value}
    if mismatches:
        raise RuntimeError(f"Backend 完整组健康证明不匹配: {mismatches}")
    actual_pid = payload.get("process_id")
    if isinstance(actual_pid, bool) or not isinstance(actual_pid, int) or actual_pid <= 0:
        raise TypeError("Backend 健康证明缺少合法 process_id")
    if process_id is not None and actual_pid != process_id:
        raise RuntimeError("Backend 完整组响应来自不同进程")
    if startup_contract is not None:
        proof = payload.get("config_proof")
        if not isinstance(proof, dict):
            raise TypeError("Workspace 健康响应缺少 config_proof")
        expected_proof = workspace_config_proof_expectation(startup_contract)
        mismatch = {key: (value, proof.get(key)) for key, value in expected_proof.items() if proof.get(key) != value}
        if mismatch:
            raise RuntimeError(f"Workspace config proof 不匹配: {mismatch}")
    return actual_pid


async def verify_managed_backend_group(
    contract: ManagedBackendContract, *, mounts: tuple[MountedWorkspace, ...], process: object | None = None,
    startup_contracts: dict[str, dict[str, object]] | None = None,
    request_timeout_seconds: float = 2, poll_interval_seconds: float = 0.5,
) -> int:
    """只在完整集合的 UUID、根摘要、generation、健康与候选 proof 全部通过后返回。"""
    if tuple(mount.workspace_id for mount in mounts) != tuple(member.backend_workspace_id for member in contract.members):
        raise RuntimeError("Backend 启动输入与完整成员身份不匹配")
    manifest_digest = mounted_workspace_manifest_digest(mounts)
    process_id = None
    for member, mount in zip(contract.members, mounts, strict=True):
        payload = await wait_for_workspace_health(
            f"{contract.backend_url.rstrip('/')}/api/v1/health", mount.workspace_id,
            process, request_timeout_seconds=request_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        process_id = _validate_group_health(
            payload, contract, mount, manifest_digest=manifest_digest, process_id=process_id,
            startup_contract=(startup_contracts or {}).get(member.workspace_id),
        )
    if process_id is None:
        raise RuntimeError("managed Backend 完整组没有成员")
    return process_id


async def _adopt_workspace_backend(
    *, contract: ManagedBackendContract, mounts: tuple[MountedWorkspace, ...],
) -> AdoptedManagedProcess | None:
    parsed = urlparse(contract.backend_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port is None:
        raise ValueError(f"持久化 Workspace API URL 必须是本机 HTTP 地址: {contract.backend_url}")
    process_id = None
    async with httpx.AsyncClient(timeout=2) as client:
        for mount in mounts:
            try:
                response = await client.get(
                    f"{contract.backend_url.rstrip('/')}/api/v1/health",
                    headers=_workspace_backend_headers(mount.workspace_id),
                )
            except httpx.HTTPError:
                return None
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError("Backend 健康响应必须是 JSON 对象")
            process_id = _validate_group_health(payload, contract, mount, manifest_digest=mounted_workspace_manifest_digest(mounts), process_id=process_id)
    if process_id is None:
        raise RuntimeError("Backend 接管合同没有成员")
    return AdoptedManagedProcess(pid=process_id)


async def _start_member_auxiliary_services(
    mount: MountedWorkspace, *, project_root: Path, log_dir: Path,
    request_timeout_seconds: float, poll_interval_seconds: float,
    runtime: WorkspaceRuntime | None = None,
    preserve_adopted_processes_on_failure: bool = False,
    preserve_mount_urls: bool = False,
) -> WorkspaceRuntime:
    runtime = runtime or WorkspaceRuntime(service_urls={}, backend_workspace_id=mount.workspace_id)
    started: list[str] = []
    try:
        for service, url, node_service in (
            ("terminal_manager", mount.terminal_backend_url, "terminal"),
            ("browser_manager", mount.browser_backend_url, "browser"),
        ):
            existing = runtime.processes.get(service)
            if existing is not None and not existing.is_alive():
                runtime.close_process(service)
                existing = None
            if existing is None:
                adopted = await _adopt_local_node_service(
                    service_url=url, service_name=service, backend_workspace_id=mount.workspace_id,
                ) if url else None
                # 同一 generation 的恢复必须保持原完整 manifest 的辅助服务地址。
                port = urlparse(url).port if url and (adopted is not None or preserve_mount_urls) else allocate_local_port()
                if port is None:
                    raise ValueError(f"辅助服务 URL 缺少端口: {url}")
                if adopted is None and not is_local_port_available(port):
                    raise RuntimeError(f"辅助服务恢复端口已被占用: {service}, 127.0.0.1:{port}")
                runtime.service_urls[service] = f"http://127.0.0.1:{port}"
                handle = adopted or start_local_node_service_process(
                    project_root=project_root, workspace_root=mount.workspace_root,
                    service=node_service, port=port, log_dir=log_dir, workspace_id=mount.workspace_id,
                )
                runtime.set_process(service, handle, adopted=adopted is not None)
                started.append(service)
            else:
                handle = existing
            await wait_for_workspace_health(
                f"{runtime.service_urls[service]}/health", mount.workspace_id,
                getattr(handle, "process", None),
                request_timeout_seconds=request_timeout_seconds, poll_interval_seconds=poll_interval_seconds,
            )
        return runtime
    except BaseException as error:
        for service in reversed(started):
            try:
                if preserve_adopted_processes_on_failure and service in runtime.adopted_process_names:
                    runtime.detach_process(service)
                else:
                    runtime.close_process(service)
            except BaseException as cleanup_error:  # noqa: BLE001 — 每个新句柄均尝试清理，保留首个失败。
                error.add_note(f"mount 辅助服务 {service} 清理失败: {cleanup_error!r}")
        raise


async def validate_managed_backend_bindings(mounts: tuple[MountedWorkspace, ...], project_root: Path) -> None:
    """完整组先只读核验；不得启动一个成员后才发现 sibling 根丢失。"""
    for mount in mounts:
        validate_workspace_root_access(mount.workspace_root)
        actual_id = await asyncio.to_thread(resolve_backend_workspace_id, project_root, mount.workspace_root, initialize=False)
        if actual_id != mount.workspace_id:
            raise RuntimeError(f"mount 根的 Backend UUID 不匹配: expected={mount.workspace_id}, actual={actual_id}")


async def start_managed_backend_runtime(
    *, owner: ManagedBackendOwner, registry: GatewayWorkspaceRegistry, project_root: Path, log_dir: Path,
    reusable_runtimes: dict[str, WorkspaceRuntime] | None = None,
    mount_contract: ManagedBackendContract | None = None,
    adopt_existing_backend: bool = False,
    preferred_backend_port: int | None = None,
    health_request_timeout_seconds: float = 2, health_poll_interval_seconds: float = 0.5,
    connection_drain_timeout_seconds: float = 2, default_skill_groups: Sequence[str] = (),
    startup_contracts: dict[str, dict[str, object]] | None = None,
    preserve_adopted_processes_on_failure: bool = False,
    preserve_generation: bool = False,
) -> tuple[ManagedBackendContract, dict[str, WorkspaceRuntime]]:
    """唯一 owner 持有启动句柄；完整候选合同仅由 registry 公布。"""
    previous = mount_contract or owner.contract
    mounts = registry.resolve_backend_mounts(previous)
    await validate_managed_backend_bindings(mounts, project_root)
    if owner.process is not None or owner.unpublished_runtimes:
        raise ValueError("managed Backend 仍持有句柄；先完成唯一 owner 的回收")
    if preferred_backend_port is not None and (
        isinstance(preferred_backend_port, bool) or not 1 <= preferred_backend_port <= 65535
    ):
        raise ValueError(f"Backend 首选端口非法: {preferred_backend_port}")
    adopted = await _adopt_workspace_backend(contract=previous, mounts=mounts) if previous.backend_url else None
    if adopted is not None and adopt_existing_backend and preferred_backend_port is not None and urlparse(previous.backend_url).port != preferred_backend_port:
        adopted.detach()
        raise RuntimeError(
            "持久化 Backend 端口与首选端口不匹配: "
            f"preferred={preferred_backend_port}, actual={urlparse(previous.backend_url).port}"
        )
    if adopted is not None:
        owner.set_process(adopted, adopted=True)
        if not adopt_existing_backend:
            owner.close(timeout_seconds=connection_drain_timeout_seconds)
            adopted = None
    port = urlparse(previous.backend_url).port if adopted is not None else preferred_backend_port or allocate_local_port()
    if port is None:
        raise ValueError("Backend URL 缺少端口")
    if adopted is None and not is_local_port_available(port):
        raise RuntimeError(f"Backend 端口已被占用: 127.0.0.1:{port}")
    runtimes = dict(reusable_runtimes or {})
    try:
        ready_mounts = []
        for member, mount in zip(previous.members, mounts, strict=True):
            runtime = runtimes.get(member.workspace_id)
            if runtime is None:
                runtime = WorkspaceRuntime(service_urls={}, backend_workspace_id=member.backend_workspace_id)
                owner.unpublished_runtimes[member.workspace_id] = runtime
                runtimes[member.workspace_id] = runtime
            await _start_member_auxiliary_services(
                mount, project_root=project_root, log_dir=log_dir, runtime=runtime,
                request_timeout_seconds=health_request_timeout_seconds,
                poll_interval_seconds=health_poll_interval_seconds,
                preserve_adopted_processes_on_failure=preserve_adopted_processes_on_failure,
                preserve_mount_urls=preserve_generation or adopted is not None,
            )
            mount = replace(
                mount,
                terminal_backend_url=runtime.service_urls["terminal_manager"],
                browser_backend_url=runtime.service_urls["browser_manager"],
            )
            ready_mounts.append(mount)
        contract = replace(
            previous, backend_url=f"http://127.0.0.1:{port}",
            generation=previous.generation if adopted is not None or preserve_generation else f"backend_generation_{uuid4().hex}",
        )
        startup_mounts = []
        for member, mount in zip(contract.members, ready_mounts, strict=True):
            startup = (startup_contracts or {}).get(member.workspace_id)
            startup_mounts.append(
                replace(mount, config_candidate_ref=str(startup["candidate_ref"]),
                        config_generation=str(startup["target_generation"]),
                        config_fencing_token=str(startup["fencing_token"]))
                if startup is not None else mount
            )
        if adopted is None:
            owner.set_process(start_local_backend_process(
                project_root=project_root, mounts=tuple(startup_mounts), backend_generation=contract.generation,
                port=port, log_dir=log_dir,
                extra_env={"BOXTEAM_DEFAULT_SKILL_GROUPS": json.dumps(list(default_skill_groups), ensure_ascii=False, separators=(",", ":"))},
                debug_port=contract.debug_port, connection_drain_timeout_seconds=connection_drain_timeout_seconds,
            ))
        await verify_managed_backend_group(
            contract, mounts=tuple(ready_mounts), process=getattr(owner.process, "process", None), startup_contracts=startup_contracts,
            request_timeout_seconds=health_request_timeout_seconds, poll_interval_seconds=health_poll_interval_seconds,
        )
        for member in contract.members:
            runtimes[member.workspace_id].backend_owner = owner
        return contract, {member.workspace_id: runtimes[member.workspace_id] for member in contract.members}
    except BaseException as error:
        try:
            if preserve_adopted_processes_on_failure:
                if owner.adopted:
                    owner.detach()
                for runtime in owner.unpublished_runtimes.values():
                    runtime.detach_adopted_processes()
            owner.close(timeout_seconds=connection_drain_timeout_seconds)
        except BaseException as cleanup_error:  # noqa: BLE001 — 保持原启动失败，未清句柄仍归唯一 owner。
            error.add_note(f"Backend 完整组清理失败: {cleanup_error!r}")
        raise


async def rebuild_managed_backend_runtime(
    *, owner: ManagedBackendOwner, registry: GatewayWorkspaceRegistry, desired_members: tuple[ManagedBackendMember, ...],
    runtimes: dict[str, WorkspaceRuntime], project_root: Path, log_dir: Path,
    health_request_timeout_seconds: float = 2, health_poll_interval_seconds: float = 0.5,
    connection_drain_timeout_seconds: float = 2, default_skill_groups: Sequence[str] = (),
    startup_contracts: dict[str, dict[str, object]] | None = None,
) -> tuple[ManagedBackendContract, dict[str, WorkspaceRuntime]]:
    """关闭旧进程后在同一 owner 上启动候选；完整恢复由组控制器统一执行。"""
    desired_contract = replace(owner.contract, members=desired_members, backend_url="")
    await validate_managed_backend_bindings(registry.resolve_backend_mounts(desired_contract), project_root)
    owner.close(timeout_seconds=connection_drain_timeout_seconds)
    return await start_managed_backend_runtime(
        owner=owner, registry=registry, mount_contract=desired_contract,
        project_root=project_root, log_dir=log_dir, reusable_runtimes=runtimes,
        health_request_timeout_seconds=health_request_timeout_seconds,
        health_poll_interval_seconds=health_poll_interval_seconds,
        connection_drain_timeout_seconds=connection_drain_timeout_seconds,
        default_skill_groups=default_skill_groups, startup_contracts=startup_contracts,
    )
