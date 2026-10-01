"""托管 runtime 配置应用与健康证明辅助函数（自 app/gateway/main.py 纯搬迁）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Callable
from datetime import (
    datetime,
    timezone,
)
from uuid import uuid4

from fastapi import FastAPI

from app.gateway.config import (
    REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS,
    GatewayConfig,
    GatewayConfigReloadService,
    GatewayConfigRuntimeRollback,
    record_gateway_restart_startup_failure,
)
from app.gateway.control.catalog_search import GatewaySessionCatalogSearchService
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.scheduler import SessionGeneratorScheduler
from app.gateway.control.user_access import UserAccessService
from app.gateway.control.view_state import UserViewStateStore
from app.gateway.federation.policy import (
    FederationPolicyStore,
    normalize_policy,
)
from app.gateway.federation.rpc import FederationRpcService
from app.gateway.federation.workspace_port import (
    WorkspaceCatalogPort,
    WorkspaceSessionMainPort,
)
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.consumer_protocol import (
    GatewayRuntimeConsumerStage,
    GatewayRuntimeConsumerTransaction,
    GatewayRuntimeHealthProof,
    runtime_fencing_token_digest,
)
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.port_forwarding import SshPortForwardManager

logger = logging.getLogger(__name__)


def _gateway_federation_policy_payload(config: GatewayConfig) -> dict[str, object]:
    """从 Gateway 配置域读取 ``permissions.federation`` 候选（缺省即内置默认）。"""

    raw = config.payload.get("permissions")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TypeError("Gateway 配置 permissions 必须是对象")
    federation = raw.get("federation")
    if federation is None:
        return {}
    if not isinstance(federation, dict):
        raise TypeError("Gateway 配置 permissions.federation 必须是对象")
    return federation


def _normalize_federation_policy(config: GatewayConfig) -> None:
    """候选校验：非法 federation 策略必须在 apply 前响亮失败且不部分生效。"""

    normalize_policy(_gateway_federation_policy_payload(config))


def _refresh_federation_workspace_ports(
    service: FederationRpcService,
    registry: GatewayWorkspaceRegistry,
) -> None:
    """把本地工作区 backend_url 注入联邦只读端口；远端子工作区不参与。"""

    if not isinstance(service.catalog, WorkspaceCatalogPort):
        return
    if not isinstance(service.session_main, WorkspaceSessionMainPort):
        return
    local_workspaces: list[tuple[str, str]] = []
    for target in registry.targets():
        if target.connection_kind != "local":
            continue
        backend_url = target.backend_url.strip()
        if not backend_url:
            continue
        local_workspaces.append((target.workspace_id, backend_url))
    projected = tuple(local_workspaces)
    service.catalog.project_workspaces(projected)
    service.session_main.project_workspaces(projected)


async def _cleanup_user_access_periodically(
    service: UserAccessService,
    view_state_store: UserViewStateStore,
) -> None:
    while True:
        await asyncio.sleep(3600)
        expired_leases, expired_guests = service.cleanup_expired()
        if expired_leases or expired_guests:
            logger.info(
                "Gateway 清理过期用户访问: leases=%s guests=%s",
                expired_leases,
                expired_guests,
            )
        expired_view_states = view_state_store.cleanup_expired()
        if expired_view_states:
            logger.info(
                "Gateway 清理过期视图状态: rows=%s", expired_view_states
            )


def _report_managed_runtime_restore_task(task: asyncio.Task[None]) -> None:
    """记录后台恢复任务的取消或未处理异常，避免列表静默显示 offline。"""
    if task.cancelled():
        logger.error("Gateway 托管 Workspace 恢复任务被取消")
        return
    error = task.exception()
    if error is not None:
        logger.error(
            "Gateway 托管 Workspace 恢复任务未处理异常",
            exc_info=(type(error), error, error.__traceback__),
        )


async def _apply_gateway_runtime_config(
    app: FastAPI,
    candidate: GatewayConfig,
    previous: GatewayConfig,
    fencing_token: str | None = None,
    fence_check: Callable[[], None] | None = None,
) -> GatewayConfigRuntimeRollback:
    """更新可安全即时读取的 Gateway 快照和下一轮调度参数。"""
    await _set_gateway_runtime_config(
        app,
        candidate,
        fencing_token=fencing_token,
        fence_check=fence_check,
    )

    async def rollback() -> None:
        await _set_gateway_runtime_config(
            app,
            previous,
            fencing_token=fencing_token,
            fence_check=fence_check,
        )

    return rollback


def _gateway_runtime_consumer_stages(
    app: FastAPI,
    candidate: GatewayConfig,
    previous: GatewayConfig,
    fencing_token: str | None = None,
    fence_check: Callable[[], None] | None = None,
) -> tuple[GatewayRuntimeConsumerStage, ...]:
    """构造可热更新 Gateway 消费者的完整协议阶段。"""
    # 配置 digest 只能标识内容，不能标识一次运行时切换；A→B→A 时也必须
    # 使用不同 generation，避免旧 apply 的 proof 与新 apply 混淆。
    generation = f"gateway-runtime:{candidate.revision}:{uuid4().hex}"
    fencing_token_digest = runtime_fencing_token_digest(fencing_token)
    stages: list[GatewayRuntimeConsumerStage] = []
    catalog_search_service = getattr(
        app.state,
        "session_catalog_search_service",
        None,
    )
    if isinstance(catalog_search_service, GatewaySessionCatalogSearchService):
        stages.append(
            GatewayRuntimeConsumerStage(
                consumer_id="session-catalog",
                generation=generation,
                prepare=lambda: catalog_search_service.prepare_runtime_config(
                    refresh_interval_seconds=(
                        candidate.session_catalog_refresh_interval_seconds
                    ),
                    max_concurrency=candidate.session_catalog_max_concurrency,
                    request_timeout_seconds=(
                        candidate.session_catalog_request_timeout_seconds
                    ),
                ),
                apply=lambda: catalog_search_service.update_runtime_config(
                    refresh_interval_seconds=(
                        candidate.session_catalog_refresh_interval_seconds
                    ),
                    max_concurrency=candidate.session_catalog_max_concurrency,
                    request_timeout_seconds=(
                        candidate.session_catalog_request_timeout_seconds
                    ),
                ),
                health=lambda: catalog_search_service.runtime_health_proof(
                    generation=generation,
                    fencing_token_digest=fencing_token_digest,
                ),
                promote=lambda: None,
                rollback=lambda: catalog_search_service.update_runtime_config(
                    refresh_interval_seconds=(
                        previous.session_catalog_refresh_interval_seconds
                    ),
                    max_concurrency=previous.session_catalog_max_concurrency,
                    request_timeout_seconds=(
                        previous.session_catalog_request_timeout_seconds
                    ),
                ),
                fencing_token_digest=fencing_token_digest,
                fence_check=fence_check,
            )
        )

    scheduler = getattr(app.state, "session_generator_scheduler", None)
    if isinstance(scheduler, SessionGeneratorScheduler):
        stages.append(
            GatewayRuntimeConsumerStage(
                consumer_id="session-generator-scheduler",
                generation=generation,
                prepare=lambda: scheduler.prepare_runtime_config(
                    poll_interval_seconds=(
                        candidate.session_generator_poll_interval_seconds
                    )
                ),
                apply=lambda: scheduler.update_runtime_config(
                    poll_interval_seconds=(
                        candidate.session_generator_poll_interval_seconds
                    )
                ),
                health=lambda: scheduler.runtime_health_proof(
                    generation=generation,
                    fencing_token_digest=fencing_token_digest,
                ),
                promote=lambda: None,
                rollback=lambda: scheduler.update_runtime_config(
                    poll_interval_seconds=(
                        previous.session_generator_poll_interval_seconds
                    )
                ),
                fencing_token_digest=fencing_token_digest,
                fence_check=fence_check,
            )
        )

    runtime_controller = getattr(app.state, "workspace_runtime_controller", None)
    if isinstance(runtime_controller, GatewayWorkspaceRuntimeController):
        stages.append(
            GatewayRuntimeConsumerStage(
                consumer_id="health-controller",
                generation=generation,
                prepare=lambda: runtime_controller.prepare_health_controller_config(
                    request_timeout_seconds=(
                        candidate.gateway_process_health_request_timeout_seconds
                    ),
                    poll_interval_seconds=(
                        candidate.gateway_process_health_poll_interval_seconds
                    ),
                ),
                apply=lambda: runtime_controller.update_health_controller_config(
                    request_timeout_seconds=(
                        candidate.gateway_process_health_request_timeout_seconds
                    ),
                    poll_interval_seconds=(
                        candidate.gateway_process_health_poll_interval_seconds
                    ),
                ),
                health=lambda: runtime_controller.runtime_health_proof(
                    generation=generation,
                    fencing_token_digest=fencing_token_digest,
                ),
                promote=lambda: None,
                rollback=lambda: runtime_controller.update_health_controller_config(
                    request_timeout_seconds=(
                        previous.gateway_process_health_request_timeout_seconds
                    ),
                    poll_interval_seconds=(
                        previous.gateway_process_health_poll_interval_seconds
                    ),
                ),
                fencing_token_digest=fencing_token_digest,
                fence_check=fence_check,
            )
        )

    federation_policy_store = getattr(app.state, "federation_policy_store", None)
    if isinstance(federation_policy_store, FederationPolicyStore):
        # 权限域属于 ``current`` 生效范围：候选在同一事务内原子热发布，
        # 不重启既有 channel，也不改 ToolSet/context/stable prefix。
        stages.append(
            GatewayRuntimeConsumerStage(
                consumer_id="federation-policy",
                generation=generation,
                prepare=lambda: _normalize_federation_policy(candidate),
                apply=lambda: federation_policy_store.publish(
                    _gateway_federation_policy_payload(candidate)
                ),
                health=lambda: GatewayRuntimeHealthProof(
                    consumer_id="federation-policy",
                    generation=generation,
                    state="healthy",
                    details={
                        "policy_revision": federation_policy_store.snapshot.revision
                    },
                    fencing_token_digest=fencing_token_digest,
                ),
                promote=lambda: None,
                rollback=lambda: federation_policy_store.publish(
                    _gateway_federation_policy_payload(previous)
                ),
                fencing_token_digest=fencing_token_digest,
                fence_check=fence_check,
            )
        )
    return tuple(stages)


async def _set_gateway_runtime_config(
    app: FastAPI,
    candidate: GatewayConfig,
    *,
    fencing_token: str | None = None,
    fence_check: Callable[[], None] | None = None,
) -> None:
    """按消费者协议 apply，并在任一阶段失败时逆序回滚。"""
    previous = app.state.gateway_config
    transaction = GatewayRuntimeConsumerTransaction(
        _gateway_runtime_consumer_stages(
            app,
            candidate,
            previous,
            fencing_token=fencing_token,
            fence_check=fence_check,
        )
    )
    try:
        result = await transaction.apply()
        app.state.gateway_config = candidate
        await result.promote()
    except BaseException as error:
        app.state.gateway_config = previous
        # transaction.apply 已负责 apply/health 阶段的补偿；promotion hook
        # 失败时 result 仍然可用，必须再次尝试补偿已经 prepare 的消费者。
        if "result" in locals():
            try:
                await result.rollback()
            except Exception as rollback_error:  # noqa: BLE001
                raise RuntimeError(
                    "Gateway runtime consumer promotion 失败且回滚不完整: "
                    f"{rollback_error}"
                ) from error
        raise


def _runtime_health_proof_digest(
    proofs: tuple[GatewayRuntimeHealthProof, ...],
) -> str:
    payload = [
        {
            "consumer_id": proof.consumer_id,
            "generation": proof.generation,
            "state": proof.state,
            "details": proof.details,
            "fencing_token_digest": proof.fencing_token_digest,
        }
        for proof in sorted(proofs, key=lambda item: item.consumer_id)
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _gateway_pending_consumer_health_digests(
    app: FastAPI,
    *,
    generation: str,
    fencing_token_digest: str,
) -> dict[str, str]:
    """收集 Gateway pending 启动的六类 consumer proof 摘要。"""

    registry = getattr(app.state, "registry", None)
    if not isinstance(registry, GatewayWorkspaceRegistry):
        raise RuntimeError("Gateway pending proof 缺少 registry")
    catalog = getattr(app.state, "session_catalog_search_service", None)
    if not isinstance(catalog, GatewaySessionCatalogSearchService):
        raise RuntimeError("Gateway pending proof 缺少 session catalog consumer")
    scheduler = getattr(app.state, "session_generator_scheduler", None)
    if not isinstance(scheduler, SessionGeneratorScheduler):
        raise RuntimeError("Gateway pending proof 缺少 generator scheduler consumer")
    controller = getattr(app.state, "workspace_runtime_controller", None)
    if not isinstance(controller, GatewayWorkspaceRuntimeController):
        raise RuntimeError("Gateway pending proof 缺少 health controller consumer")
    port_forward_manager = getattr(app.state, "port_forward_manager", None)
    if not isinstance(port_forward_manager, SshPortForwardManager):
        raise RuntimeError("Gateway pending proof 缺少 SSH tunnel consumer")

    catalog_proof = catalog.runtime_health_proof(
        generation=generation,
        fencing_token_digest=fencing_token_digest,
    )
    scheduler_proof = scheduler.runtime_health_proof(
        generation=generation,
        fencing_token_digest=fencing_token_digest,
    )
    digests = {
        "catalog-generator-scheduler": _runtime_health_proof_digest(
            (catalog_proof, scheduler_proof)
        ),
        "health-controller": _runtime_health_proof_digest(
            (
                controller.runtime_health_proof(
                    generation=generation,
                    fencing_token_digest=fencing_token_digest,
                ),
            )
        ),
        "registry-batch": _runtime_health_proof_digest(
            (
                registry.runtime_health_proof(
                    consumer_id="registry-batch",
                    generation=generation,
                    fencing_token_digest=fencing_token_digest,
                ),
            )
        ),
        "ssh-tunnel-proxy": _runtime_health_proof_digest(
            (
                port_forward_manager.runtime_health_proof(
                    generation=generation,
                    fencing_token_digest=fencing_token_digest,
                ),
            )
        ),
        "workspace-process": _runtime_health_proof_digest(
            (
                registry.runtime_health_proof(
                    consumer_id="workspace-process",
                    generation=generation,
                    fencing_token_digest=fencing_token_digest,
                ),
            )
        ),
        "remote-projection": _runtime_health_proof_digest(
            (
                registry.runtime_health_proof(
                    consumer_id="remote-projection",
                    generation=generation,
                    fencing_token_digest=fencing_token_digest,
                ),
            )
        ),
    }
    if set(digests) != REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS:
        raise RuntimeError(
            "Gateway pending consumer proof 集合不完整: "
            f"actual={sorted(digests)}, "
            f"expected={sorted(REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS)}"
        )
    return digests


def _gateway_pending_runtime_consumer_stages(
    app: FastAPI,
    *,
    generation: str,
    fencing_token_digest: str,
    fence_check: Callable[[], None],
) -> tuple[GatewayRuntimeConsumerStage, ...]:
    """构造 pending successor 的 registry/tunnel/process/projection 阶段。"""

    registry = getattr(app.state, "registry", None)
    if not isinstance(registry, GatewayWorkspaceRegistry):
        raise RuntimeError("Gateway pending consumer protocol 缺少 registry")
    port_forward_manager = getattr(app.state, "port_forward_manager", None)
    if not isinstance(port_forward_manager, SshPortForwardManager):
        raise RuntimeError("Gateway pending consumer protocol 缺少 SSH tunnel consumer")

    previous_registry_generation: str | None = None
    previous_workspace_generations: dict[str, str | None] = {}
    previous_remote_generations: dict[str, str | None] = {}

    def prepare_registry_batch() -> None:
        nonlocal previous_registry_generation
        previous_registry_generation = registry.prepare_runtime_generation(generation)

    def apply_registry_batch() -> None:
        nonlocal previous_registry_generation
        previous_registry_generation = registry.apply_runtime_generation(generation)

    def rollback_registry_batch() -> None:
        registry.rollback_runtime_generation(
            generation,
            previous_registry_generation,
        )

    def prepare_ssh_tunnel() -> None:
        port_forward_manager.prepare_runtime_generation(generation)

    previous_ssh_generation: str | None = None

    def apply_ssh_tunnel() -> None:
        nonlocal previous_ssh_generation
        previous_ssh_generation = port_forward_manager.apply_runtime_generation(
            generation
        )

    def promote_ssh_tunnel() -> None:
        port_forward_manager.promote_runtime_generation(generation)

    def rollback_ssh_tunnel() -> None:
        port_forward_manager.rollback_runtime_generation(
            generation,
            previous_ssh_generation,
        )

    def prepare_workspace_process() -> None:
        previous_workspace_generations.clear()
        previous_workspace_generations.update(
            registry.prepare_workspace_process_generation(generation)
        )

    def apply_workspace_process() -> None:
        previous_workspace_generations.clear()
        previous_workspace_generations.update(
            registry.apply_workspace_process_generation(generation)
        )

    def promote_workspace_process() -> None:
        registry.promote_workspace_process_generation(generation)

    def rollback_workspace_process() -> None:
        registry.rollback_workspace_process_generation(
            generation,
            previous_workspace_generations,
        )

    def prepare_remote_projection() -> None:
        previous_remote_generations.clear()
        previous_remote_generations.update(
            registry.prepare_remote_projection_generation(generation)
        )

    def apply_remote_projection() -> None:
        previous_remote_generations.clear()
        previous_remote_generations.update(
            registry.apply_remote_projection_generation(generation)
        )

    def promote_remote_projection() -> None:
        registry.promote_remote_projection_generation(generation)

    def rollback_remote_projection() -> None:
        registry.rollback_remote_projection_generation(
            generation,
            previous_remote_generations,
        )

    return (
        GatewayRuntimeConsumerStage(
            consumer_id="registry-batch",
            generation=generation,
            prepare=prepare_registry_batch,
            apply=apply_registry_batch,
            health=lambda: registry.runtime_health_proof(
                consumer_id="registry-batch",
                generation=generation,
                fencing_token_digest=fencing_token_digest,
            ),
            promote=lambda: registry.promote_runtime_generation(generation),
            rollback=rollback_registry_batch,
            fencing_token_digest=fencing_token_digest,
            fence_check=fence_check,
        ),
        GatewayRuntimeConsumerStage(
            consumer_id="ssh-tunnel-proxy",
            generation=generation,
            prepare=prepare_ssh_tunnel,
            apply=apply_ssh_tunnel,
            health=lambda: port_forward_manager.runtime_health_proof(
                generation=generation,
                fencing_token_digest=fencing_token_digest,
            ),
            promote=promote_ssh_tunnel,
            rollback=rollback_ssh_tunnel,
            fencing_token_digest=fencing_token_digest,
            fence_check=fence_check,
        ),
        GatewayRuntimeConsumerStage(
            consumer_id="workspace-process",
            generation=generation,
            prepare=prepare_workspace_process,
            apply=apply_workspace_process,
            health=lambda: registry.runtime_health_proof(
                consumer_id="workspace-process",
                generation=generation,
                fencing_token_digest=fencing_token_digest,
            ),
            promote=promote_workspace_process,
            rollback=rollback_workspace_process,
            fencing_token_digest=fencing_token_digest,
            fence_check=fence_check,
        ),
        GatewayRuntimeConsumerStage(
            consumer_id="remote-projection",
            generation=generation,
            prepare=prepare_remote_projection,
            apply=apply_remote_projection,
            health=lambda: registry.runtime_health_proof(
                consumer_id="remote-projection",
                generation=generation,
                fencing_token_digest=fencing_token_digest,
            ),
            promote=promote_remote_projection,
            rollback=rollback_remote_projection,
            fencing_token_digest=fencing_token_digest,
            fence_check=fence_check,
        ),
    )


def _should_preserve_gateway_generation_for_handoff(
    *,
    gateway_state: GatewayStateStore,
    gateway_config_reload: GatewayConfigReloadService,
    startup_candidate_ref: str | None,
    generation_id: str,
) -> bool:
    """判断旧 Gateway 是否正在等待新 pending generation 接管监听。"""

    generation = gateway_state.get_gateway_runtime_generation(
        generation_id=generation_id
    )
    if generation is not None and (
        startup_candidate_ref is not None
        and generation.state == "failed"
        and generation.listener_state == "closed"
    ):
        # Pending generation 启动失败时，它可能已经接管了旧 Workspace
        # 进程句柄；这些句柄必须脱离而不能终止旧 active 的真实进程。
        return True
    if generation is not None and (
        startup_candidate_ref is None
        and generation.state == "active"
        and generation.listener_state == "draining"
    ):
        # Promotion 先把旧 generation 标记为 draining，旧进程随后才收到
        # supervisor 的停止信号。此时不能让旧 Gateway 关闭新 generation
        # 已接管的 Workspace 进程。
        active_generation = gateway_state.active_gateway_runtime_generation()
        return bool(
            active_generation is not None
            and active_generation.generation_id != generation_id
        )
    if startup_candidate_ref is not None:
        return False
    status = gateway_config_reload.status()
    if status.candidate_ref is None or status.state not in {
        "pending_restart",
        "applying",
    }:
        return False
    intent = gateway_state.get_gateway_restart_intent(
        candidate_ref=status.candidate_ref
    )
    return bool(
        intent is not None
        and intent.state in {"pending", "applying"}
        and intent.old_generation == generation_id
    )


def _record_owned_gateway_startup_failure(
    *,
    gateway_state: GatewayStateStore,
    gateway_id: str,
    candidate_ref: str,
    error: str,
) -> bool:
    """仅记录当前 Gateway、未过期且 token 完整匹配的启动早期失败。"""

    intent = gateway_state.get_gateway_restart_intent(candidate_ref=candidate_ref)
    if intent is None or intent.gateway_id != gateway_id:
        return False
    if intent.state not in {"pending", "applying"}:
        return False
    if intent.expires_at is None or intent.expires_at <= datetime.now(timezone.utc):
        return False
    generation = os.environ.get("BOXTEAM_CONFIG_GENERATION")
    fencing_token = os.environ.get("BOXTEAM_CONFIG_FENCING_TOKEN")
    if (
        not generation
        or not fencing_token
        or intent.target_generation != generation
        or intent.fencing_token != fencing_token
    ):
        return False
    record_gateway_restart_startup_failure(
        state_store=gateway_state,
        candidate_ref=candidate_ref,
        error=f"Gateway pending generation 启动失败: {error}",
        gateway_id=gateway_id,
        target_generation=generation,
        fencing_token=fencing_token,
    )
    return True
