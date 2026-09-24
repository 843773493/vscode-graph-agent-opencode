from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from uuid import uuid4

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.core.env import get_project_root, load_boxteam_env
from app.core.logging_config import configure_application_logging
from app.core.path_utils import (
    get_gateway_root,
    get_user_gateway_config_path,
    get_user_gateway_local_config_path,
)
from app.core.trace_middleware import TraceMiddleware, get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    get_gateway_local_token,
    verify_federation_token,
    verify_gateway_access,
    verify_gateway_token,
)
from app.gateway.auxiliary_proxy import router as auxiliary_proxy_router
from app.gateway.config import (
    REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS,
    GatewayConfig,
    GatewayConfigReloadService,
    GatewayConfigRuntimeRollback,
    load_gateway_config,
    record_gateway_restart_startup_failure,
)
from app.gateway.control.catalog_search import GatewaySessionCatalogSearchService
from app.gateway.control.coordinator import SessionGeneratorCoordinator
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.generators import SessionGeneratorStore
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.control.resource_catalog import GatewayResourceCatalogService
from app.gateway.control.router import router as gateway_control_router
from app.gateway.control.scheduler import SessionGeneratorScheduler
from app.gateway.control.user_access import (
    USER_ACCESS_COOKIE_NAME,
    UserAccessContext,
    UserAccessService,
    UserLeaseOccupiedError,
)
from app.gateway.control.user_profile import UserProfileStore
from app.gateway.control.view_state import UserViewStateRecord, UserViewStateStore
from app.gateway.credentials import (
    FederationCredential,
    FederationCredentialStore,
    load_or_create_gateway_id,
)
from app.gateway.device_connections import router as device_connections_router
from app.gateway.diagnostics import collect_gateway_diagnostics
from app.gateway.federation import (
    FEDERATION_PROTOCOL_VERSION,
    request_remote_gateway_management,
)
from app.gateway.federation.identity import load_or_create_signing_key
from app.gateway.federation.policy import FederationPolicyStore, normalize_policy
from app.gateway.federation.router import router as federation_router
from app.gateway.federation.rpc import FederationRpcService
from app.gateway.federation.store import (
    FederationControlStore,
    federation_control_database,
)
from app.gateway.federation.workspace_port import (
    WorkspaceCatalogPort,
    WorkspaceSessionMainPort,
)
from app.gateway.managed_workspaces import (
    create_direct_managed_workspace,
    list_direct_managed_workspaces,
    remove_direct_managed_workspace,
)
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceTarget,
)
from app.gateway.remote_gateway import (
    refresh_remote_gateway_projections,
    register_remote_gateway,
)
from app.gateway.runtime.consumer_protocol import (
    GatewayRuntimeConsumerStage,
    GatewayRuntimeConsumerTransaction,
    GatewayRuntimeHealthProof,
    runtime_fencing_token_digest,
)
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.development_restart import (
    RESTART_DELAY_MS,
    resolve_development_restart_command,
    start_development_restart,
)
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.gateway.runtime.process import (
    wait_for_http_ok,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.bootstrap import (
    _restore_managed_local_runtimes,
    create_registry,
)
from app.gateway.server.port_forwarding import (
    get_port_forward_manager,
)
from app.gateway.server.port_forwarding import (
    router as port_forwards_router,
)
from app.gateway.server.static_ui import install_static_web_ui
from app.gateway.server.workspace_proxy import router as workspace_proxy_router
from app.gateway.ssh_connections import (
    list_ssh_connection_options,
    resolve_ssh_connection_request,
)
from app.gateway.theme import (
    MAX_UI_ASSET_BYTES,
    delete_ui_asset,
    import_ui_asset,
    list_ui_assets,
    load_validated_theme_config,
    referenced_asset_ids,
    resolve_settings_theme,
    resolve_theme,
    resolve_ui_asset,
    synchronize_theme_asset_references,
    theme_catalog,
)
from app.gateway.ui_settings import (
    merge_web_ui_settings_values,
)
from app.gateway.workspace_ids import build_workspace_id
from app.schemas.gateway import (
    AcquireGatewayUserRequest,
    ActivateGatewayWorkspaceResultDTO,
    AddLocalWorkspaceRequest,
    AddRemoteGatewayRequest,
    CreateFederationManagedWorkspaceRequest,
    CreateGatewayGuestRequest,
    CreateGatewayManagedWorkspaceRequest,
    CreateGatewayUserRequest,
    DevelopmentRuntimeRestartDTO,
    FederationProtocolManifestDTO,
    FederationWorkspaceDTO,
    FederationWorkspaceListDTO,
    GatewayConfigEventDTO,
    GatewayConfigEventsDTO,
    GatewayConfigPendingDiscardRequest,
    GatewayConfigPendingHealthProofRequest,
    GatewayConfigReloadStatusDTO,
    GatewayConfigSourceDTO,
    GatewayConfigSourcesDTO,
    GatewayDiagnosticsDTO,
    GatewayDirectoryEntryDTO,
    GatewayDirectoryListDTO,
    GatewayHealthDTO,
    GatewayInboundAccessListDTO,
    GatewayInboundPeerDTO,
    GatewayInboundWorkspaceDTO,
    GatewayManagedWorkspaceListDTO,
    GatewayRuntimeRestartResultDTO,
    GatewayRuntimeStateResultDTO,
    GatewayThemeCatalogDTO,
    GatewayUIAssetDTO,
    GatewayUIAssetListDTO,
    GatewayUserAccessDTO,
    GatewayUserDTO,
    GatewayUserLeaseDTO,
    GatewayUserListDTO,
    GatewayUserViewStateDTO,
    GatewayUserViewStateUpdateRequest,
    GatewayWorkspaceListDTO,
    ReorderGatewayWorkspacesRequest,
    SshConnectionOptionListDTO,
    UpdateGatewayWorkspaceRequest,
    WebUISettingsDTO,
    WebUISettingsUpdateDTO,
)
from app.schemas.internal_v2.common import APIResponse
from app.services.infrastructure.config.policy import gateway_config_policy
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventCursorGoneError,
    build_secret_binding_summary,
)

logger = logging.getLogger(__name__)


def _gateway_root() -> Path:
    return get_gateway_root()


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
    for target in registry.targets():
        if target.connection_kind != "local":
            continue
        backend_url = target.backend_url.strip()
        if not backend_url:
            continue
        service.catalog.register_workspace(
            workspace_id=target.workspace_id, backend_url=backend_url
        )
        service.session_main.register_workspace(
            workspace_id=target.workspace_id, backend_url=backend_url
        )


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


def _resolve_local_directory(raw_path: str | None) -> Path:
    target_path = Path(raw_path).expanduser() if raw_path else Path.home()
    resolved_path = target_path.resolve()
    if not resolved_path.exists():
        raise HTTPException(status_code=400, detail=f"本机目录不存在: {resolved_path}")
    if not resolved_path.is_dir():
        raise HTTPException(status_code=400, detail=f"路径不是目录: {resolved_path}")
    return resolved_path


def _scan_local_directories(
    root_path: Path,
    limit: int,
) -> tuple[list[GatewayDirectoryEntryDTO], bool]:
    with os.scandir(root_path) as directory_iterator:
        directories = [
            entry for entry in directory_iterator if entry.is_dir(follow_symlinks=False)
        ]
    directories.sort(key=lambda entry: (entry.name.lower(), entry.name))
    entries = [
        GatewayDirectoryEntryDTO(
            name=entry.name,
            path=str(Path(entry.path).resolve()),
        )
        for entry in directories[:limit]
    ]
    return entries, len(directories) > limit


async def _directory_listing(
    raw_path: str | None,
    *,
    limit: int,
) -> GatewayDirectoryListDTO:
    root_path = _resolve_local_directory(raw_path)
    entries, truncated = await asyncio.to_thread(
        _scan_local_directories,
        root_path,
        limit,
    )
    parent_path = root_path.parent if root_path.parent != root_path else None
    return GatewayDirectoryListDTO(
        path=str(root_path),
        parent_path=str(parent_path) if parent_path is not None else None,
        home_path=str(Path.home().resolve()),
        entries=entries,
        truncated=truncated,
        limit=limit,
    )


def _workspace_name(root_path: str, fallback: str = "workspace") -> str:
    name = Path(root_path).name
    return name or fallback


async def _managed_workspace_list(
    registry: GatewayWorkspaceRegistry,
) -> GatewayManagedWorkspaceListDTO:
    return GatewayManagedWorkspaceListDTO(
        gateway_id=load_or_create_gateway_id(_gateway_root() / "identity.json"),
        gateway_name="本机 Gateway",
        connection_kind="local",
        items=await list_direct_managed_workspaces(registry),
    )


async def _inbound_gateway_access_list(
    registry: GatewayWorkspaceRegistry,
) -> GatewayInboundAccessListDTO:
    gateway_id = load_or_create_gateway_id(_gateway_root() / "identity.json")
    credentials = FederationCredentialStore(
        storage_path=_gateway_root() / "credentials" / "federation.json"
    ).list_valid()
    peers = [
        GatewayInboundPeerDTO(
            connection_id=credential.connection_id,
            peer_gateway_id=credential.peer_gateway_id,
            credential_expires_at=credential.expires_at.isoformat(),
        )
        for credential in credentials
        if credential.peer_gateway_id != gateway_id
    ]
    workspaces = [
        GatewayInboundWorkspaceDTO(
            workspace_id=workspace.workspace_id,
            name=workspace.name,
            root_path=workspace.root_path,
            status=workspace.status,
            managed=workspace.managed,
            system_default=workspace.system_default,
        )
        for workspace in await registry.list_dtos()
        if workspace.connection_kind == "local"
    ]
    return GatewayInboundAccessListDTO(
        gateway_id=gateway_id,
        peers=peers,
        items=workspaces if peers else [],
    )


def _remote_gateway_credential(connection_id: str) -> FederationCredential:
    return FederationCredentialStore(
        storage_path=_gateway_root() / "credentials" / "federation.json"
    ).get(connection_id)


def _remote_managed_workspace_list(
    registry: GatewayWorkspaceRegistry,
    connection_id: str,
    remote_data: dict[str, object],
) -> GatewayManagedWorkspaceListDTO:
    connection = registry.remote_gateway_connection(connection_id)
    remote_result = GatewayManagedWorkspaceListDTO.model_validate(remote_data)
    return remote_result.model_copy(
        update={
            "gateway_connection_id": connection_id,
            "gateway_name": connection.name,
            "connection_kind": "remote_gateway",
        }
    )


def _remote_http_error_detail(error: httpx.HTTPStatusError) -> str:
    try:
        payload = error.response.json()
    except ValueError:
        return error.response.text[:1000]
    if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
        return payload["detail"]
    return error.response.text[:1000]


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_application_logging()
    load_boxteam_env()
    get_gateway_local_token()
    logger.info("Gateway 日志已初始化: gateway_root=%s", _gateway_root())
    startup_candidate_ref = os.environ.get("BOXTEAM_CONFIG_CANDIDATE_REF")
    gateway_state = GatewayStateStore(
        path=_gateway_root() / "gateway.sqlite",
        allow_shared_processes=bool(
            startup_candidate_ref and startup_candidate_ref.strip()
        ),
    )
    gateway_id = load_or_create_gateway_id(_gateway_root() / "identity.json")
    app.state.gateway_state = gateway_state
    app.state.gateway_id = gateway_id
    app.state.user_access_service = UserAccessService(state=gateway_state)
    app.state.user_profile_store = UserProfileStore(gateway_root=_gateway_root())
    app.state.user_view_state_store = UserViewStateStore(state=gateway_state)
    app.state.user_access_service.cleanup_expired()
    app.state.user_view_state_store.cleanup_expired()
    startup_generation = (
        os.environ.get("BOXTEAM_CONFIG_GENERATION") or f"gateway_runtime_{os.getpid()}"
    )
    startup_fencing_token = os.environ.get("BOXTEAM_CONFIG_FENCING_TOKEN")
    try:
        gateway_config = load_gateway_config(
            state_store=gateway_state,
            startup=True,
            gateway_id=gateway_id,
        )
    except Exception as error:
        recovery_error: Exception | None = None
        if startup_candidate_ref:
            try:
                _record_owned_gateway_startup_failure(
                    gateway_state=gateway_state,
                    gateway_id=gateway_id,
                    candidate_ref=startup_candidate_ref,
                    error=str(error),
                )
            except Exception as failure_error:
                recovery_error = failure_error
        gateway_state.close()
        if recovery_error is not None:
            raise RuntimeError(
                "Gateway pending generation 启动失败，且早期恢复状态写入失败: "
                f"{recovery_error}"
            ) from recovery_error
        raise
    registry = await create_registry(
        gateway_config,
        state_store=gateway_state,
        preserve_existing_managed_runtimes=startup_candidate_ref is not None,
    )
    app.state.registry = registry
    app.state.gateway_config = gateway_config
    gateway_config_reload = GatewayConfigReloadService(
        state_store=gateway_state,
        config=gateway_config,
        config_path=get_user_gateway_config_path(),
        local_config_path=get_user_gateway_local_config_path(),
        on_runtime_config=lambda candidate, previous, fencing_token, fence_check: (
            _apply_gateway_runtime_config(
                app,
                candidate,
                previous,
                fencing_token,
                fence_check,
            )
        ),
        gateway_id=gateway_id,
    )
    app.state.gateway_config_reload = gateway_config_reload
    federation_policy = FederationPolicyStore(
        initial=_gateway_federation_policy_payload(gateway_config)
    )
    federation_control = FederationControlStore(
        database=federation_control_database(gateway_root=_gateway_root())
    )
    app.state.federation_credential_store = FederationCredentialStore(
        storage_path=_gateway_root() / "credentials" / "federation.json"
    )
    app.state.federation_gateway_root = _gateway_root()
    app.state.federation_policy_store = federation_policy
    app.state.federation_control_store = federation_control
    app.state.federation_rpc_service = FederationRpcService(
        gateway_id=gateway_id,
        policy_store=federation_policy,
        control_store=federation_control,
        signing_key=load_or_create_signing_key(_gateway_root()),
        catalog=WorkspaceCatalogPort(),
        session_main=WorkspaceSessionMainPort(),
        spoke_directory=None,
    )
    _refresh_federation_workspace_ports(
        app.state.federation_rpc_service, registry
    )
    app.state.port_forward_manager = SshPortForwardManager(
        registry=registry,
        storage_path=_gateway_root() / "port-forwards.json",
        log_dir=_gateway_root() / "logs",
    )
    await app.state.port_forward_manager.reconcile_workspaces()
    await app.state.port_forward_manager.restore()
    app.state.workspace_runtime_controller = GatewayWorkspaceRuntimeController(
        registry=registry,
        project_root=get_project_root(),
        log_dir=_gateway_root() / "logs",
        on_registry_reconciled=(app.state.port_forward_manager.reconcile_workspaces),
        health_request_timeout_seconds=(
            gateway_config.gateway_process_health_request_timeout_seconds
        ),
        health_poll_interval_seconds=(
            gateway_config.gateway_process_health_poll_interval_seconds
        ),
        connection_drain_timeout_seconds=(
            gateway_config.gateway_process_connection_drain_timeout_seconds
        ),
        default_skill_groups=gateway_config.default_workspace_skill_groups,
    )
    # Gateway 代理本机工作区时必须直连，不能把本地后端请求送入用户的 HTTP 代理。
    app.state.http_client = httpx.AsyncClient(timeout=None, trust_env=False)
    # SSE 会长期占用到工作区后端的连接。单独使用一个连接池，避免大量
    # 会话事件流耗尽普通 API 请求的连接额度，导致 bootstrap 等请求排队。
    app.state.streaming_http_client = httpx.AsyncClient(
        timeout=None,
        trust_env=False,
        limits=httpx.Limits(
            max_connections=1000,
            max_keepalive_connections=100,
        ),
    )
    app.state.workspace_navigation_store = WorkspaceNavigationStore(
        storage_path=_gateway_root() / "navigation" / "workspace-tree.json"
    )
    app.state.session_generator_store = SessionGeneratorStore(root=_gateway_root())
    app.state.session_generator_coordinator = SessionGeneratorCoordinator(
        registry=registry,
        store=app.state.session_generator_store,
        http_client=app.state.http_client,
    )
    app.state.session_catalog_search_service = GatewaySessionCatalogSearchService(
        registry=registry,
        http_client=app.state.http_client,
        cache_dir=_gateway_root() / "indexes" / "session-catalogs",
        navigation_store=app.state.workspace_navigation_store,
        refresh_interval_seconds=(
            gateway_config.session_catalog_refresh_interval_seconds
        ),
        max_concurrency=gateway_config.session_catalog_max_concurrency,
        request_timeout_seconds=(
            gateway_config.session_catalog_request_timeout_seconds
        ),
    )
    app.state.gateway_resource_catalog_service = GatewayResourceCatalogService(
        registry=registry,
        http_client=app.state.http_client,
    )
    app.state.session_generator_scheduler = SessionGeneratorScheduler(
        store=app.state.session_generator_store,
        coordinator=app.state.session_generator_coordinator,
        poll_interval_seconds=gateway_config.session_generator_poll_interval_seconds,
    )
    coordinator_started = False
    catalog_search_started = False
    scheduler_started = False
    user_access_cleanup_task = asyncio.create_task(
        _cleanup_user_access_periodically(
            app.state.user_access_service,
            app.state.user_view_state_store,
        )
    )
    default_workspace_id = next(
        (target.workspace_id for target in registry.targets() if target.system_default),
        "",
    )
    active_workspace_id = registry.active_workspace_id
    active_workspace_target = (
        registry.resolve(active_workspace_id)
        if active_workspace_id is not None and registry.has_target(active_workspace_id)
        else None
    )
    logger.info(
        "Gateway 启动恢复计划: active_workspace_id=%s, target=%s, managed=%s, "
        "desired_running=%s, has_runtime=%s",
        active_workspace_id,
        active_workspace_target.workspace_id if active_workspace_target else None,
        active_workspace_target.managed if active_workspace_target else None,
        active_workspace_target.desired_running if active_workspace_target else None,
        (
            registry.has_runtime(active_workspace_target.workspace_id)
            if active_workspace_target is not None
            else None
        ),
    )
    active_runtime_restore_task: asyncio.Task[None] | None = None
    if (
        active_workspace_target is not None
        and active_workspace_target.connection_kind == "local"
        and active_workspace_target.managed
        and active_workspace_target.desired_running
        and not registry.has_runtime(active_workspace_target.workspace_id)
    ):
        # Gateway 必须先完成自己的 lifespan，不能把一个慢启动的工作区后端
        # 绑定到 uvicorn 的 Application startup。工作区代理会等待这个任务的
        # 有界结果，因此首个 /api/v1 请求仍能在后端 ready 后进入，而 Gateway
        # health、维护态和诊断接口不会被托管工作区阻塞。
        active_runtime_restore_task = asyncio.create_task(
            _restore_managed_local_runtimes(
                registry=registry,
                default_workspace_id=default_workspace_id,
                gateway_root=_gateway_root(),
                gateway_config=gateway_config,
                only_workspace_ids={active_workspace_target.workspace_id},
                preserve_existing_managed_runtimes=startup_candidate_ref is not None,
            )
        )
        active_runtime_restore_task.add_done_callback(
            _report_managed_runtime_restore_task
        )
    managed_runtime_restore_task = asyncio.create_task(
        _restore_managed_local_runtimes(
            registry=registry,
            default_workspace_id=default_workspace_id,
            gateway_root=_gateway_root(),
            gateway_config=gateway_config,
            exclude_workspace_ids=(
                {active_workspace_target.workspace_id}
                if active_workspace_target is not None
                else None
            ),
            preserve_existing_managed_runtimes=startup_candidate_ref is not None,
        )
    )
    managed_runtime_restore_task.add_done_callback(
        _report_managed_runtime_restore_task
    )
    managed_runtime_restore_tasks: dict[str, asyncio.Task[None]] = {}
    if active_runtime_restore_task is not None and active_workspace_target is not None:
        managed_runtime_restore_tasks[active_workspace_target.workspace_id] = (
            active_runtime_restore_task
        )
    for target in registry.targets():
        if (
            target.workspace_id
            != (
                active_workspace_target.workspace_id
                if active_workspace_target is not None
                else None
            )
            and target.connection_kind == "local"
            and target.managed
            and target.desired_running
        ):
            managed_runtime_restore_tasks[target.workspace_id] = (
                managed_runtime_restore_task
            )
    app.state.managed_runtime_restore_tasks = managed_runtime_restore_tasks
    app.state.managed_runtime_restore_task = managed_runtime_restore_task
    logger.info(
        "Gateway 托管 Workspace 恢复任务已登记: workspace_ids=%s",
        sorted(managed_runtime_restore_tasks),
    )
    runtime_generation_record = None
    pending = None
    pending_promotion_committed = False
    try:
        await gateway_config_reload.start()
        await app.state.session_generator_coordinator.start()
        coordinator_started = True
        await app.state.session_catalog_search_service.start()
        catalog_search_started = True
        await app.state.session_generator_scheduler.start()
        scheduler_started = True
        app.state.attach_frontend_urls = {
            "terminal": os.environ.get(
                "BOXTEAM_TERMINAL_FRONTEND_URL",
                "http://127.0.0.1:8013",
            ).rstrip("/"),
            "browser": os.environ.get(
                "BOXTEAM_BROWSER_FRONTEND_URL",
                "http://127.0.0.1:8016",
            ).rstrip("/"),
        }
        active_snapshot = gateway_state.get_active_config_snapshot("gateway")
        if active_snapshot is None:
            raise RuntimeError("Gateway 启动后缺少 active config snapshot")
        secret_bindings = build_secret_binding_summary(gateway_config.payload)
        secret_binding_digest = hashlib.sha256(
            json.dumps(
                secret_bindings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        pending_candidate_id = None
        pending_revision = None
        loaded_source = "active"
        pending_apply_claim = None
        if startup_candidate_ref:
            intent = gateway_state.get_gateway_restart_intent(
                candidate_ref=startup_candidate_ref
            )
            if intent is None:
                raise RuntimeError(
                    f"Gateway pending 启动缺少 restart intent: {startup_candidate_ref}"
                )
            pending_candidate_id = intent.candidate_id
            pending = gateway_state.get_pending_config_candidate(
                config_domain="gateway",
                candidate_id=intent.candidate_id,
            )
            if pending is None:
                raise RuntimeError(
                    f"Gateway pending 启动缺少 candidate: {intent.candidate_id}"
                )
            pending = gateway_config_reload.begin_pending_restart(
                candidate_ref=startup_candidate_ref
            )
            pending_apply_claim = gateway_state.get_config_apply_claim(
                config_domain="gateway"
            )
            if pending_apply_claim is None:
                raise RuntimeError("Gateway pending 启动缺少 apply claim")
            pending_apply_journal = gateway_state.get_config_apply_journal(
                apply_id=pending_apply_claim.apply_id
            )
            if (
                pending_apply_journal is None
                or pending_apply_journal.registry_revision is None
            ):
                raise RuntimeError("Gateway pending 启动缺少 registry CAS 基线")
            pending_revision = pending.pending_revision
            loaded_source = "pending"
        runtime_generation_record = gateway_state.record_gateway_runtime_generation(
            generation_id=startup_generation,
            process_id=os.getpid(),
            loaded_source=loaded_source,
            candidate_id=pending_candidate_id,
            active_revision=active_snapshot.active_revision,
            pending_revision=pending_revision,
            candidate_digest=(
                pending.candidate_digest
                if startup_candidate_ref and pending is not None
                else None
            ),
            effective_digest=gateway_config.revision,
            secret_binding_digest=secret_binding_digest,
            fencing_token=None,
            listener_state="reserved",
            state="starting",
        )
        if startup_candidate_ref:
            await _wait_for_managed_runtime_restore_tasks(
                getattr(app.state, "managed_runtime_restore_tasks", {})
            )
            if pending_apply_journal is None:
                raise RuntimeError("Gateway pending 启动缺少 apply journal")
            if pending_apply_journal.registry_revision is None:
                raise RuntimeError("Gateway pending 启动缺少 registry CAS 基线")
            gateway_state.rebase_config_apply_registry_revision(
                apply_id=pending_apply_claim.apply_id,
                expected_registry_revision=pending_apply_journal.registry_revision,
            )
            def assert_pending_apply_claim() -> None:
                gateway_state.assert_config_apply_claim(
                    config_domain="gateway",
                    apply_id=pending_apply_claim.apply_id,
                    fencing_token=pending_apply_claim.fencing_token,
                )

            pending_consumer_result = None
            try:
                pending_consumer_transaction = GatewayRuntimeConsumerTransaction(
                    _gateway_pending_runtime_consumer_stages(
                        app,
                        generation=startup_generation,
                        fencing_token_digest=(
                            runtime_fencing_token_digest(
                                pending_apply_claim.fencing_token
                            )
                            or ""
                        ),
                        fence_check=assert_pending_apply_claim,
                    )
                )
                pending_consumer_result = (
                    await pending_consumer_transaction.apply()
                )
                await pending_consumer_result.promote()
                for consumer_id in (
                    "registry-batch",
                    "ssh-tunnel-proxy",
                    "workspace-process",
                    "remote-projection",
                ):
                    gateway_state.append_config_apply_side_effect(
                        apply_id=pending_apply_claim.apply_id,
                        side_effect={
                            "resource": consumer_id,
                            "action": "generation_handoff",
                            "status": "succeeded",
                            "generation": startup_generation,
                        },
                    )
                gateway_state.rebase_config_apply_registry_revision(
                    apply_id=pending_apply_claim.apply_id,
                    expected_registry_revision=pending_apply_journal.registry_revision,
                )
            except BaseException as error:
                if pending_consumer_result is not None:
                    try:
                        await pending_consumer_result.rollback()
                    except Exception as rollback_error:
                        raise RuntimeError(
                            "Gateway pending consumer protocol 回退不完整: "
                            f"{rollback_error}"
                        ) from error
                raise
            registry.assert_runtime_consumers_healthy()
            app.state.port_forward_manager.assert_healthy()
            if isinstance(
                app.state.session_catalog_search_service,
                GatewaySessionCatalogSearchService,
            ):
                app.state.session_catalog_search_service.assert_healthy()
            if isinstance(
                app.state.session_generator_scheduler,
                SessionGeneratorScheduler,
            ):
                app.state.session_generator_scheduler.assert_healthy()
            if isinstance(
                app.state.workspace_runtime_controller,
                GatewayWorkspaceRuntimeController,
            ):
                app.state.workspace_runtime_controller.assert_health_controller_healthy()
            claim = pending_apply_claim
            if claim is None:
                raise RuntimeError("Gateway pending health proof 缺少 apply claim")
            consumer_health_digests = _gateway_pending_consumer_health_digests(
                app,
                generation=startup_generation,
                fencing_token_digest=(
                    runtime_fencing_token_digest(claim.fencing_token) or ""
                ),
            )
            health_proof = gateway_config_reload.build_pending_restart_health_proof(
                candidate_ref=startup_candidate_ref,
                generation=startup_generation,
                consumer_health_digests=consumer_health_digests,
            )
            runtime_generation_record = gateway_state.record_gateway_runtime_generation(
                generation_id=startup_generation,
                process_id=os.getpid(),
                loaded_source="pending",
                candidate_id=pending_candidate_id,
                active_revision=active_snapshot.active_revision,
                pending_revision=pending_revision,
                candidate_digest=(
                    pending.candidate_digest if pending is not None else None
                ),
                effective_digest=gateway_config.revision,
                secret_binding_digest=secret_binding_digest,
                fencing_token=claim.fencing_token,
                listener_state="reserved",
                state="healthy",
                health_proof=health_proof,
            )
            try:
                gateway_config_reload.record_pending_restart_proof(
                    candidate_ref=startup_candidate_ref,
                    health_proof=health_proof,
                    runtime_generation_id=startup_generation,
                    old_generation_id=intent.old_generation,
                )
                pending_promotion_committed = True
                runtime_generation_record = (
                    gateway_state.get_gateway_runtime_generation(
                        generation_id=startup_generation
                    )
                )
                if runtime_generation_record is None:
                    raise RuntimeError(
                        "Gateway pending promotion 后缺少 runtime generation"
                    )
            except Exception:
                if pending_promotion_committed:
                    raise
                if runtime_generation_record is not None:
                    try:
                        gateway_state.close_gateway_runtime_generation(
                            generation_id=startup_generation,
                            expected_states=("starting", "healthy"),
                            fencing_token=claim.fencing_token,
                        )
                    except Exception as rollback_error:
                        raise RuntimeError(
                            "Gateway pending generation 失败，且新 generation "
                            "关闭失败: "
                            f"{rollback_error}"
                        ) from rollback_error
                raise
        else:
            runtime_generation_record = gateway_state.update_gateway_runtime_generation(
                generation_id=startup_generation,
                expected_state="starting",
                state="active",
                listener_state="serving",
            )
        yield
    except Exception as error:
        recovery_error: Exception | None = None
        if startup_candidate_ref and not pending_promotion_committed:
            try:
                gateway_config_reload.record_pending_restart_failure(
                    candidate_ref=startup_candidate_ref,
                    target_generation=startup_generation,
                    fencing_token=startup_fencing_token or "",
                    error=f"Gateway pending generation 启动失败: {error}",
                )
            except Exception as failure_error:
                recovery_error = failure_error
        if runtime_generation_record is not None:
            runtime_generation_record = gateway_state.update_gateway_runtime_generation(
                generation_id=startup_generation,
                expected_state=runtime_generation_record.state,
                state="failed",
                listener_state="closed",
            )
        if recovery_error is not None:
            raise RuntimeError(
                "Gateway pending generation 启动失败，且恢复状态写入失败: "
                f"{recovery_error}"
            ) from recovery_error
        raise
    finally:
        if (
            runtime_generation_record is not None
            and runtime_generation_record.state
            in {
                "starting",
                "healthy",
                "active",
            }
        ):
            if (
                runtime_generation_record.state == "active"
                and _should_preserve_gateway_generation_for_handoff(
                    gateway_state=gateway_state,
                    gateway_config_reload=gateway_config_reload,
                    startup_candidate_ref=startup_candidate_ref,
                    generation_id=startup_generation,
                )
            ):
                runtime_generation_record = (
                    gateway_state.update_gateway_runtime_generation(
                        generation_id=startup_generation,
                        expected_state="active",
                        state="active",
                        listener_state="draining",
                    )
                )
            else:
                gateway_state.update_gateway_runtime_generation(
                    generation_id=startup_generation,
                    expected_state=runtime_generation_record.state,
                    state="closed",
                    listener_state="closed",
                )
        if active_runtime_restore_task is not None:
            active_runtime_restore_task.cancel()
        managed_runtime_restore_task.cancel()
        restore_tasks = [managed_runtime_restore_task]
        if active_runtime_restore_task is not None:
            restore_tasks.append(active_runtime_restore_task)
        await asyncio.gather(*restore_tasks, return_exceptions=True)
        user_access_cleanup_task.cancel()
        await asyncio.gather(user_access_cleanup_task, return_exceptions=True)
        if scheduler_started:
            logger.info("Gateway 正在停止会话生成器调度器")
            await app.state.session_generator_scheduler.stop()
        if catalog_search_started:
            logger.info("Gateway 正在停止会话目录索引同步器")
            await app.state.session_catalog_search_service.stop()
        if coordinator_started:
            logger.info("Gateway 正在停止会话生成协调器")
            await app.state.session_generator_coordinator.stop()
        logger.info("Gateway 正在关闭 HTTP 连接池")
        await app.state.http_client.aclose()
        await app.state.streaming_http_client.aclose()
        shutdown_errors: list[Exception] = []
        logger.info("Gateway 正在关闭 SSH 端口转发")
        try:
            await app.state.port_forward_manager.close()
        except Exception as error:
            shutdown_errors.append(error)
            logger.exception("Gateway 关闭 SSH 端口转发失败")
        logger.info("Gateway 正在关闭托管工作区运行时")
        preserve_runtime_for_handoff = _should_preserve_gateway_generation_for_handoff(
            gateway_state=gateway_state,
            gateway_config_reload=gateway_config_reload,
            startup_candidate_ref=startup_candidate_ref,
            generation_id=startup_generation,
        )
        try:
            registry.close(
                preserve_browser_managers=preserve_runtime_for_handoff,
                preserve_terminal_managers=preserve_runtime_for_handoff,
                preserve_workspace_backends=preserve_runtime_for_handoff,
            )
            if (
                preserve_runtime_for_handoff
                and runtime_generation_record is not None
                and runtime_generation_record.state == "active"
            ):
                gateway_state.update_gateway_runtime_generation(
                    generation_id=startup_generation,
                    expected_state="active",
                    state="closed",
                    listener_state="closed",
                )
        except Exception as error:
            shutdown_errors.append(error)
            logger.exception("Gateway 关闭托管工作区运行时失败")
        await gateway_config_reload.stop()
        federation_control_store = getattr(
            app.state, "federation_control_store", None
        )
        if isinstance(federation_control_store, FederationControlStore):
            federation_control_store.close()
        gateway_state.close()
        if shutdown_errors:
            raise RuntimeError(
                "Gateway lifespan 清理失败: "
                + "; ".join(str(error) for error in shutdown_errors)
            ) from shutdown_errors[0]
        logger.info("Gateway lifespan 清理完成")


app = FastAPI(
    title="BoxTeam Workspace Gateway",
    version="1.0.0",
    docs_url="/api/gateway/docs",
    openapi_url="/api/gateway/openapi.json",
    redoc_url="/api/gateway/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TraceMiddleware)


@app.exception_handler(HTTPException)
async def gateway_http_exception_handler(
    request: Request,
    error: HTTPException,
) -> JSONResponse:
    """让依赖注入阶段的错误也遵守 Gateway request_id 响应约定。"""
    request_id = get_request_id(request)
    return JSONResponse(
        status_code=error.status_code,
        headers=error.headers,
        content={
            "detail": error.detail,
            "request_id": request_id,
        },
    )


def get_registry(request: Request) -> GatewayWorkspaceRegistry:
    registry = getattr(request.app.state, "registry", None)
    if not isinstance(registry, GatewayWorkspaceRegistry):
        raise RuntimeError("Gateway registry 尚未初始化")
    return registry


async def _wait_for_managed_runtime_restore_tasks(restore_tasks: object) -> None:
    """等待托管 Workspace 恢复任务完成，并保留明确的超时状态。"""
    if not isinstance(restore_tasks, dict):
        return
    pending_tasks = {
        task
        for task in restore_tasks.values()
        if isinstance(task, asyncio.Task) and not task.done()
    }
    logger.info(
        "Gateway 等待托管 Workspace 恢复: task_count=%s, pending_count=%s",
        len(restore_tasks),
        len(pending_tasks),
    )
    for task in pending_tasks:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=120.0)
        except TimeoutError:
            # 启动恢复超时不是隐藏错误；list_dtos 会返回 connection_error，
            # 让调用方看到工作区仍未就绪以及具体的恢复状态。
            return


async def _wait_for_managed_runtime_restores(request: Request) -> None:
    """让工作区列表在启动恢复完成后反映真实的运行时状态。"""
    restore_tasks = getattr(
        getattr(request.app, "state", None),
        "managed_runtime_restore_tasks",
        {},
    )
    await _wait_for_managed_runtime_restore_tasks(restore_tasks)


def get_gateway_config_reload_service(
    request: Request,
) -> GatewayConfigReloadService:
    service = getattr(request.app.state, "gateway_config_reload", None)
    if not isinstance(service, GatewayConfigReloadService):
        raise RuntimeError("Gateway 配置重载服务尚未初始化")
    return service


def get_user_access_service(request: Request) -> UserAccessService:
    service = getattr(request.app.state, "user_access_service", None)
    if not isinstance(service, UserAccessService):
        raise RuntimeError("Gateway 用户访问服务尚未初始化")
    return service


def get_user_profile_store(request: Request) -> UserProfileStore:
    store = getattr(request.app.state, "user_profile_store", None)
    if not isinstance(store, UserProfileStore):
        raise RuntimeError("Gateway 用户 profile 存储尚未初始化")
    return store


def get_user_view_state_store(request: Request) -> UserViewStateStore:
    store = getattr(request.app.state, "user_view_state_store", None)
    if not isinstance(store, UserViewStateStore):
        raise RuntimeError("Gateway 用户视图状态存储尚未初始化")
    return store


def _user_access_dto(
    context: UserAccessContext,
    service: UserAccessService,
    *,
    takeover: bool = False,
) -> GatewayUserAccessDTO:
    return GatewayUserAccessDTO(
        kind=context.kind,  # type: ignore[arg-type]
        user_id=context.user_id,
        lease_generation=context.lease_generation,
        expires_at=service.expires_at(context),
        takeover=takeover,
    )


def _set_user_access_cookie(response: Response, context: UserAccessContext) -> None:
    response.set_cookie(
        key=USER_ACCESS_COOKIE_NAME,
        value=context.access_session_id,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _release_replaced_user_access(
    request: Request,
    service: UserAccessService,
    replacement: UserAccessContext,
) -> None:
    previous = service.resolve_cookie(request.cookies.get(USER_ACCESS_COOKIE_NAME))
    if previous is None or previous.access_session_id == replacement.access_session_id:
        return
    service.release(previous)


def _current_user_access(
    request: Request,
    service: UserAccessService,
) -> UserAccessContext:
    context = service.resolve_cookie(request.cookies.get(USER_ACCESS_COOKIE_NAME))
    if context is None:
        raise HTTPException(status_code=401, detail="user_session_required")
    return context


def _view_state_dto(record: UserViewStateRecord) -> GatewayUserViewStateDTO:
    return GatewayUserViewStateDTO(
        user_id=record.user_id,
        workspace_id=record.workspace_id,
        session_id=record.session_id,
        turn_anchor=record.turn_anchor,
        scroll_offset=record.scroll_offset,
        follow_latest=record.follow_latest,
        projection_version=record.projection_version,
        tool_details_expanded=record.tool_details_expanded,
        updated_at=record.updated_at,
    )


def _read_current_ui_settings(
    request: Request,
    *,
    access_service: UserAccessService,
    profiles: UserProfileStore,
) -> tuple[UserAccessContext, WebUISettingsDTO]:
    context = _current_user_access(request, access_service)
    if context.kind == "guest":
        return context, WebUISettingsDTO()
    if context.user_id is None:
        raise RuntimeError("普通用户访问上下文缺少 user_id")
    return context, profiles.read_ui_settings(user_id=context.user_id)


def _theme_asset_root(
    context: UserAccessContext,
    profiles: UserProfileStore,
) -> Path:
    if context.kind == "guest":
        # 游客不创建 profile；游客视图只能使用内置主题或网络背景。
        return _gateway_root() / "guest-theme-assets"
    if context.user_id is None:
        raise RuntimeError("普通用户访问上下文缺少 user_id")
    return profiles.theme_assets_path(user_id=context.user_id)


def _theme_config_for_access(
    context: UserAccessContext,
    *,
    profiles: UserProfileStore,
    base_config: GatewayConfig | None,
    gateway_root: Path,
) -> GatewayConfig:
    config = base_config or load_validated_theme_config(gateway_root=gateway_root)
    if context.kind == "guest":
        return profiles.guest_theme_config(config)
    if context.user_id is None:
        raise RuntimeError("普通用户访问上下文缺少 user_id")
    return profiles.theme_config(user_id=context.user_id, base_config=config)


def get_workspace_runtime_controller(
    request: Request,
) -> GatewayWorkspaceRuntimeController:
    controller = getattr(request.app.state, "workspace_runtime_controller", None)
    if not isinstance(controller, GatewayWorkspaceRuntimeController):
        raise RuntimeError("Gateway 工作区运行时控制器尚未初始化")
    return controller


@app.get("/api/gateway/health", response_model=APIResponse[GatewayHealthDTO])
async def health(
    request: Request,
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    scheduler = getattr(request.app.state, "session_generator_scheduler", None)
    if not isinstance(scheduler, SessionGeneratorScheduler):
        raise RuntimeError("会话生成器调度器尚未初始化")
    scheduler.assert_healthy()
    return APIResponse(
        data=GatewayHealthDTO(
            active_workspace_id=registry.active_workspace_id,
            process_id=os.getpid(),
            development_restart_available=(
                resolve_development_restart_command() is not None
            ),
        ),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/runtime/restart-development",
    response_model=APIResponse[DevelopmentRuntimeRestartDTO],
)
async def restart_development_runtime(
    auth: GatewayAuthContext = Depends(verify_gateway_access),
    request_id: str = Depends(get_request_id),
):
    if auth.kind != "local":
        raise HTTPException(status_code=403, detail="远程 Gateway 无权重启本机开发服务")
    command = resolve_development_restart_command()
    if command is None:
        raise HTTPException(status_code=409, detail="当前不是可重启的源码开发环境")
    restart_environment: dict[str, str] = {}
    gateway_state = getattr(app.state, "gateway_state", None)
    gateway_reload = getattr(app.state, "gateway_config_reload", None)
    if isinstance(gateway_state, GatewayStateStore) and isinstance(
        gateway_reload, GatewayConfigReloadService
    ):
        candidate_ref = gateway_reload.status().candidate_ref
        if candidate_ref:
            intent = gateway_state.get_gateway_restart_intent(
                candidate_ref=candidate_ref
            )
            if intent is None or intent.state not in {"pending", "applying"}:
                raise HTTPException(
                    status_code=409,
                    detail="Gateway pending restart intent 已不可恢复",
                )
            if intent.expires_at is None or intent.expires_at <= datetime.now(timezone.utc):
                raise HTTPException(
                    status_code=409,
                    detail="Gateway pending restart intent 已过期，请先显式 retry",
                )
            restart_environment = {
                "BOXTEAM_CONFIG_CANDIDATE_REF": intent.candidate_ref,
                "BOXTEAM_CONFIG_GENERATION": intent.target_generation,
                "BOXTEAM_CONFIG_FENCING_TOKEN": intent.fencing_token,
            }
    helper_process_id = start_development_restart(
        command,
        log_path=_gateway_root() / "logs" / "development-restart.log",
        environment=restart_environment,
    )
    return APIResponse(
        data=DevelopmentRuntimeRestartDTO(
            previous_process_id=os.getpid(),
            helper_process_id=helper_process_id,
            delay_ms=RESTART_DELAY_MS,
        ),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/config/sources",
    response_model=APIResponse[GatewayConfigSourcesDTO],
)
async def gateway_config_sources(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    try:
        gateway_state = getattr(app.state, "gateway_state", None)
        config = (
            load_gateway_config(
                state_store=gateway_state,
                persist_migrations=False,
            )
            if isinstance(gateway_state, GatewayStateStore)
            else load_gateway_config()
        )
    except (FileNotFoundError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if config.schema_path is None:
        raise RuntimeError("Gateway 配置快照缺少 schema 来源")
    return APIResponse(
        data=GatewayConfigSourcesDTO(
            revision=config.revision,
            schema_path=str(config.schema_path),
            sources=[
                GatewayConfigSourceDTO(
                    path=str(source.path),
                    layer=source.layer,
                    precedence=source.precedence,
                    loaded=source.loaded,
                    source_key=source.source_key,
                    presence=source.presence,
                    layer_revision=source.layer_revision,
                    layer_digest=source.layer_digest,
                    source_generation=source.source_generation,
                )
                for source in config.source_details
            ],
            policy_manifest=list(gateway_config_policy().policy_manifest()),
        ),
        request_id=request_id,
    )


def _gateway_config_event_dto(event) -> GatewayConfigEventDTO:
    return GatewayConfigEventDTO(
        event_seq=event.event_seq,
        event_id=event.event_id,
        config_domain=event.config_domain,
        candidate_id=event.candidate_id,
        attempt_id=event.attempt_id,
        apply_id=event.apply_id,
        idempotency_key=event.idempotency_key,
        commit_revision=event.commit_revision,
        active_revision=event.active_revision,
        pending_revision=event.pending_revision,
        source=event.source,
        result=event.result,
        activation_scope=event.activation_scope,
        changed_paths=list(event.changed_paths),
        applied_paths=list(event.applied_paths),
        deferred_paths=list(event.deferred_paths),
        error=event.error,
        occurred_at=event.occurred_at.isoformat(),
    )


@app.get(
    "/api/gateway/config/reload-status",
    response_model=APIResponse[GatewayConfigReloadStatusDTO],
)
async def gateway_config_reload_status(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    status = get_gateway_config_reload_service(request).status()
    return APIResponse(
        data=GatewayConfigReloadStatusDTO(
            available=True,
            healthy=status.healthy,
            revision=status.revision,
            restart_required=status.restart_required,
            reason=status.reason,
            changed_sections=list(status.changed_sections),
            last_error=status.last_error,
            state=status.state,
            active_revision=status.active_revision,
            pending_revision=status.pending_revision,
            candidate_id=status.candidate_id,
            candidate_ref=status.candidate_ref,
            attempt_id=status.attempt_id,
            apply_id=status.apply_id,
            layer_digests=status.layer_digests or {},
            applied_paths=list(status.applied_paths),
            deferred_paths=list(status.deferred_paths),
        ),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/config/retry-restart",
    response_model=APIResponse[GatewayConfigReloadStatusDTO],
)
async def retry_gateway_config_restart(
    request: Request,
    candidate_ref: str = Query(..., min_length=1),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    try:
        status = get_gateway_config_reload_service(request).retry_pending_restart(
            candidate_ref=candidate_ref,
            requested_by=f"gateway-api:{request_id}",
        )
    except (ConfigConflictError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=GatewayConfigReloadStatusDTO(
            available=True,
            healthy=status.healthy,
            revision=status.revision,
            restart_required=status.restart_required,
            reason=status.reason,
            changed_sections=list(status.changed_sections),
            last_error=status.last_error,
            state=status.state,
            active_revision=status.active_revision,
            pending_revision=status.pending_revision,
            candidate_id=status.candidate_id,
            candidate_ref=status.candidate_ref,
            attempt_id=status.attempt_id,
            apply_id=status.apply_id,
            layer_digests=status.layer_digests or {},
            applied_paths=list(status.applied_paths),
            deferred_paths=list(status.deferred_paths),
        ),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/config/resolve-restart",
    response_model=APIResponse[GatewayConfigReloadStatusDTO],
)
async def resolve_gateway_config_restart(
    request: Request,
    payload: GatewayConfigPendingHealthProofRequest,
    candidate_ref: str = Query(..., min_length=1),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    try:
        status = get_gateway_config_reload_service(request).resolve_pending_restart(
            candidate_ref=candidate_ref,
            health_proof=payload.model_dump(exclude_none=True),
        )
    except (ConfigConflictError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=GatewayConfigReloadStatusDTO(
            available=True,
            healthy=status.healthy,
            revision=status.revision,
            restart_required=status.restart_required,
            reason=status.reason,
            changed_sections=list(status.changed_sections),
            last_error=status.last_error,
            state=status.state,
            active_revision=status.active_revision,
            pending_revision=status.pending_revision,
            candidate_id=status.candidate_id,
            candidate_ref=status.candidate_ref,
            attempt_id=status.attempt_id,
            apply_id=status.apply_id,
            layer_digests=status.layer_digests or {},
            applied_paths=list(status.applied_paths),
            deferred_paths=list(status.deferred_paths),
        ),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/config/discard-restart",
    response_model=APIResponse[GatewayConfigReloadStatusDTO],
)
async def discard_gateway_config_restart(
    request: Request,
    payload: GatewayConfigPendingDiscardRequest,
    candidate_ref: str = Query(..., min_length=1),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    try:
        status = get_gateway_config_reload_service(request).discard_pending_restart(
            candidate_ref=candidate_ref,
            expected_active_revision=payload.expected_active_revision,
            expected_active_digest=payload.expected_active_digest,
        )
    except (ConfigConflictError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=GatewayConfigReloadStatusDTO(
            available=True,
            healthy=status.healthy,
            revision=status.revision,
            restart_required=status.restart_required,
            reason=status.reason,
            changed_sections=list(status.changed_sections),
            last_error=status.last_error,
            state=status.state,
            active_revision=status.active_revision,
            pending_revision=status.pending_revision,
            candidate_id=status.candidate_id,
            candidate_ref=status.candidate_ref,
            attempt_id=status.attempt_id,
            apply_id=status.apply_id,
            layer_digests=status.layer_digests or {},
            applied_paths=list(status.applied_paths),
            deferred_paths=list(status.deferred_paths),
        ),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/config/events",
    response_model=APIResponse[GatewayConfigEventsDTO],
)
async def gateway_config_events(
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=2000),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    service = get_gateway_config_reload_service(request)
    try:
        events = service.list_events(after=after, limit=limit)
    except ConfigEventCursorGoneError as error:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "snapshot_required",
                "message": str(error),
                "first_available": error.first_available,
            },
        ) from error
    return APIResponse(
        data=GatewayConfigEventsDTO(
            cursor=events[-1].event_seq if events else after,
            events=[_gateway_config_event_dto(event) for event in events],
            has_more=len(events) == limit,
        ),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/config/events/stream",
    response_class=StreamingResponse,
)
async def gateway_config_event_stream(
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _: str = Depends(verify_gateway_token),
):
    if last_event_id is not None:
        try:
            header_cursor = int(last_event_id)
        except ValueError as error:
            raise HTTPException(
                status_code=400, detail="Last-Event-ID 必须是整数游标"
            ) from error
        if after and after != header_cursor:
            raise HTTPException(status_code=409, detail="after 与 Last-Event-ID 不一致")
        after = header_cursor
    service = get_gateway_config_reload_service(request)
    try:
        service.ensure_event_cursor(after=after)
    except ConfigEventCursorGoneError as error:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "snapshot_required",
                "message": str(error),
                "first_available": error.first_available,
            },
        ) from error

    async def event_generator():
        cursor = after
        consumer_id = f"gateway-config-sse:{uuid4().hex}"
        while not await request.is_disconnected():
            events = service.claim_events_for_consumer(
                after=cursor,
                consumer_id=consumer_id,
                limit=2000,
            )
            if events:
                for event in events:
                    cursor = event.event_seq
                    dto = _gateway_config_event_dto(event)
                    yield f"id: {cursor}\nevent: config\ndata: {dto.model_dump_json()}\n\n"
                    service.mark_event_delivered_for_consumer(
                        event_id=event.event_id,
                        consumer_id=consumer_id,
                    )
            else:
                yield ": config-heartbeat\n\n"
                await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/gateway/auth/local-credential")
async def local_credential(
    request: Request,
    request_id: str = Depends(get_request_id),
):
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site not in {None, "same-origin", "same-site"}:
        raise HTTPException(
            status_code=403,
            detail="Gateway 本地凭据只允许同站点 Web UI 获取",
        )
    return APIResponse(
        data={"token": get_gateway_local_token()},
        request_id=request_id,
    )


@app.get("/api/gateway/users", response_model=APIResponse[GatewayUserListDTO])
async def list_gateway_users(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    return APIResponse(
        data=GatewayUserListDTO(
            items=[
                GatewayUserDTO(
                    user_id=record.user.user_id,
                    display_name=record.user.display_name,
                    created_at=record.user.created_at,
                    lease=GatewayUserLeaseDTO(
                        occupied=record.lease.occupied,
                        client_label=record.lease.client_label,
                        heartbeat_at=record.lease.heartbeat_at,
                        expires_at=record.lease.expires_at,
                    ),
                )
                for record in service.list_users()
            ]
        ),
        request_id=request_id,
    )


@app.post("/api/gateway/users", response_model=APIResponse[GatewayUserDTO])
async def create_gateway_user(
    payload: CreateGatewayUserRequest,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        user = service.create_user(
            display_name=payload.display_name,
            user_id=payload.user_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    try:
        gateway_config = getattr(request.app.state, "gateway_config", None)
        initial_custom_themes = (
            gateway_config.custom_themes
            if isinstance(gateway_config, GatewayConfig)
            else ()
        )
        profiles.ensure_user(
            user_id=user.user_id,
            display_name=user.display_name,
            initial_custom_themes=initial_custom_themes,
        )
    except (OSError, ValueError) as error:
        # 用户记录没有租约，创建 profile 失败时可以安全回滚，避免产生半成品用户。
        service.delete_user(user.user_id)
        raise HTTPException(
            status_code=500, detail=f"用户 profile 初始化失败: {error}"
        ) from error
    return APIResponse(
        data=GatewayUserDTO(
            user_id=user.user_id,
            display_name=user.display_name,
            created_at=user.created_at,
        ),
        request_id=request_id,
    )


@app.delete("/api/gateway/users/{user_id}")
async def delete_gateway_user(
    user_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        service.delete_user(user_id)
    except UserLeaseOccupiedError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "user_lease_occupied",
                "client_label": error.summary.client_label,
                "expires_at": error.summary.expires_at,
            },
        ) from error
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    try:
        profiles.delete_user(user_id=user_id)
    except OSError as error:
        raise HTTPException(
            status_code=500, detail=f"用户 profile 删除失败: {error}"
        ) from error
    return APIResponse(data={"user_id": user_id}, request_id=request_id)


@app.post(
    "/api/gateway/users/{user_id}/access",
    response_model=APIResponse[GatewayUserAccessDTO],
)
async def acquire_gateway_user(
    user_id: str,
    payload: AcquireGatewayUserRequest,
    request: Request,
    response: Response,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    try:
        context = service.acquire_user(
            user_id=user_id,
            client_label=payload.client_label,
        )
    except UserLeaseOccupiedError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "user_lease_occupied",
                "client_label": error.summary.client_label,
                "expires_at": error.summary.expires_at,
            },
        ) from error
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    _release_replaced_user_access(request, service, context)
    _set_user_access_cookie(response, context)
    return APIResponse(
        data=_user_access_dto(context, service),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/users/{user_id}/takeover",
    response_model=APIResponse[GatewayUserAccessDTO],
)
async def takeover_gateway_user(
    user_id: str,
    payload: AcquireGatewayUserRequest,
    request: Request,
    response: Response,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    try:
        context = service.acquire_user(
            user_id=user_id,
            client_label=payload.client_label,
            takeover=True,
        )
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    _release_replaced_user_access(request, service, context)
    _set_user_access_cookie(response, context)
    return APIResponse(
        data=_user_access_dto(context, service, takeover=True),
        request_id=request_id,
    )


@app.post("/api/gateway/users/guest", response_model=APIResponse[GatewayUserAccessDTO])
async def acquire_gateway_guest(
    payload: CreateGatewayGuestRequest,
    request: Request,
    response: Response,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    context = service.acquire_guest(tracking=payload.tracking)
    _release_replaced_user_access(request, service, context)
    _set_user_access_cookie(response, context)
    return APIResponse(
        data=_user_access_dto(context, service),
        request_id=request_id,
    )


@app.get("/api/gateway/users/current", response_model=APIResponse[GatewayUserAccessDTO])
async def current_gateway_user(
    request: Request,
    response: Response,
    x_local_token: str | None = Header(default=None),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    # 首次页面加载会在拿到本地凭据前探测当前用户；本机没有 cookie 时应直接
    # 建立游客态，而不是让浏览器产生一个无意义的 401 资源错误。若调用方
    # 主动带 token，仍必须经过同一凭据校验，不能借此放宽已认证请求。
    if x_local_token is not None and x_local_token != get_gateway_local_token():
        raise HTTPException(status_code=401, detail="invalid local token")
    context = service.resolve_cookie(request.cookies.get(USER_ACCESS_COOKIE_NAME))
    if context is None:
        # 首次页面加载完全没有 cookie 时按首载契约建立游客态，避免业务
        # 初始化先看到一次无意义的 401；cookie 存在但已失效（被接管/释放/
        # 过期）时必须返回 401，不得静默重建游客态——否则并发到达的陈旧
        # 请求会让游客 Set-Cookie 覆盖同客户端刚完成的用户切换（takeover
        # 竞态，gateway_user_view 双浏览器集成实证：接管 POST 200 后轮询
        # /users/current 的陈旧 cookie 把刚写入的用户 cookie 覆盖回游客）。
        if USER_ACCESS_COOKIE_NAME in request.cookies:
            raise HTTPException(status_code=401, detail="user_session_required")
        context = service.acquire_guest()
        _set_user_access_cookie(response, context)
    return APIResponse(
        data=_user_access_dto(context, service),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/users/current/heartbeat",
    response_model=APIResponse[GatewayUserAccessDTO],
)
async def heartbeat_gateway_user(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    context = _current_user_access(request, service)
    try:
        context = service.heartbeat(context)
    except PermissionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=_user_access_dto(context, service),
        request_id=request_id,
    )


@app.delete("/api/gateway/users/current")
async def release_gateway_user(
    request: Request,
    response: Response,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    context = _current_user_access(request, service)
    service.release(context)
    response.delete_cookie(key=USER_ACCESS_COOKIE_NAME, path="/")
    return APIResponse(data={"released": True}, request_id=request_id)


@app.get("/api/gateway/users/current/view-state")
async def get_gateway_user_view_state(
    request: Request,
    workspace_id: str = Query(min_length=1, max_length=256),
    session_id: str = Query(min_length=1, max_length=256),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    store: UserViewStateStore = Depends(get_user_view_state_store),
):
    context = _current_user_access(request, service)
    try:
        record = store.get(
            context=context,
            workspace_id=workspace_id,
            session_id=session_id,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return APIResponse(
        data=_view_state_dto(record) if record is not None else None,
        request_id=request_id,
    )


@app.get("/api/gateway/users/current/view-state/latest")
async def get_latest_gateway_user_view_state(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    store: UserViewStateStore = Depends(get_user_view_state_store),
):
    context = _current_user_access(request, service)
    try:
        record = store.get_latest(context=context)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return APIResponse(
        data=_view_state_dto(record) if record is not None else None,
        request_id=request_id,
    )


@app.put("/api/gateway/users/current/view-state")
async def put_gateway_user_view_state(
    payload: GatewayUserViewStateUpdateRequest,
    request: Request,
    workspace_id: str = Query(min_length=1, max_length=256),
    session_id: str = Query(min_length=1, max_length=256),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    store: UserViewStateStore = Depends(get_user_view_state_store),
):
    context = _current_user_access(request, service)
    try:
        record = store.put(
            context=context,
            workspace_id=workspace_id,
            session_id=session_id,
            turn_anchor=payload.turn_anchor,
            scroll_offset=payload.scroll_offset,
            follow_latest=payload.follow_latest,
            projection_version=payload.projection_version,
            tool_details_expanded=payload.tool_details_expanded,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return APIResponse(data=_view_state_dto(record), request_id=request_id)


@app.get("/api/gateway/workspaces", response_model=APIResponse[GatewayWorkspaceListDTO])
async def list_workspaces(
    request: Request,
    check_health: bool = Query(
        default=True,
        description="是否探测所有工作区及其附属服务的健康状态",
    ),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    await _wait_for_managed_runtime_restores(request)
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(check_health=check_health),
        ),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/federation/manifest",
    response_model=APIResponse[FederationProtocolManifestDTO],
)
async def federation_manifest(
    request: Request,
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
):
    gateway_state = getattr(request.app.state, "gateway_state", None)
    if not isinstance(gateway_state, GatewayStateStore):
        raise RuntimeError("Gateway state 尚未初始化")
    config_status = get_gateway_config_reload_service(request).status()
    _, config_event_cursor = gateway_state.config_event_bounds(config_domain="gateway")
    return APIResponse(
        data=FederationProtocolManifestDTO(
            protocol_version=FEDERATION_PROTOCOL_VERSION,
            gateway_id=load_or_create_gateway_id(_gateway_root() / "identity.json"),
            capabilities=[
                "workspace_discovery",
                "workspace_proxy",
                "auxiliary_proxy",
                "diagnostics_logs",
                "managed_backend_restart",
                "managed_workspace_admin",
            ],
            config_event_cursor=config_event_cursor,
            config_reload_state=config_status.state,
            config_reload_restart_required=config_status.restart_required,
            config_reload_candidate_ref=config_status.candidate_ref,
        ),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/federation/workspaces",
    response_model=APIResponse[FederationWorkspaceListDTO],
)
async def federation_workspaces(
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    def services_for(workspace_id: str) -> list[str]:
        services = ["workspace_api"]
        for service, public_name in (
            ("terminal_manager", "terminal_manager"),
            ("browser_manager", "browser_manager"),
        ):
            try:
                registry.resolve_service_url(workspace_id, service)
            except LookupError:
                continue
            services.append(public_name)
        return services

    direct = [
        FederationWorkspaceDTO(
            workspace_id=target.workspace_id,
            name=target.name,
            root_path=target.root_path,
            managed=target.managed,
            connection_kind="local",
            services=services_for(target.workspace_id),
        )
        for target in registry.targets()
        if target.connection_kind == "local"
    ]
    excluded = [
        (f"{target.workspace_id}: bounded federation 不导出从其他 Gateway 导入的工作区")
        for target in registry.targets()
        if target.connection_kind == "remote_gateway"
    ]
    return APIResponse(
        data=FederationWorkspaceListDTO(
            protocol_version=FEDERATION_PROTOCOL_VERSION,
            gateway_id=load_or_create_gateway_id(_gateway_root() / "identity.json"),
            items=direct,
            excluded=excluded,
        ),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/federation/diagnostics",
    response_model=APIResponse[GatewayDiagnosticsDTO],
)
async def federation_diagnostics(
    remote_workspace_id: str | None = Query(default=None),
    log_id: str | None = Query(default=None),
    tail_lines: int = Query(default=300, ge=20, le=1000),
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        result = await collect_gateway_diagnostics(
            registry,
            selected_workspace_id=remote_workspace_id,
            selected_log_id=log_id,
            tail_lines=tail_lines,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@app.get(
    "/api/gateway/federation/managed-workspaces",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def federation_managed_workspaces(
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/federation/managed-workspaces",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def create_federation_managed_workspace(
    payload: CreateFederationManagedWorkspaceRequest,
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        await create_direct_managed_workspace(
            registry=registry,
            project_root=get_project_root(),
            log_dir=_gateway_root() / "logs",
            root_path=payload.root_path,
            name=payload.name,
            create_directory=payload.create_directory,
        )
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@app.delete(
    "/api/gateway/federation/managed-workspaces/{workspace_id}",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def remove_federation_managed_workspace(
    workspace_id: str,
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        remove_direct_managed_workspace(registry, workspace_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/federation/directories",
    response_model=APIResponse[GatewayDirectoryListDTO],
)
async def list_federation_directories(
    path: str | None = Query(default=None),
    limit: int = Query(default=120, ge=1, le=500),
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
):
    try:
        listing = await _directory_listing(path, limit=limit)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(
            status_code=400,
            detail=f"读取远程 Gateway 目录失败: {error}",
        ) from error
    return APIResponse(data=listing, request_id=request_id)


@app.get(
    "/api/gateway/managed-workspaces",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def gateway_managed_workspaces(
    gateway_connection_id: str | None = Query(default=None),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    port_forward_manager: SshPortForwardManager = Depends(get_port_forward_manager),
):
    if gateway_connection_id is not None:
        try:
            remote_data = await request_remote_gateway_management(
                gateway_url=registry.remote_gateway_url(gateway_connection_id),
                credential=_remote_gateway_credential(gateway_connection_id),
                method="GET",
                path="/api/gateway/federation/managed-workspaces",
                request_id=request_id,
            )
            await refresh_remote_gateway_projections(
                registry=registry,
                connection_id=gateway_connection_id,
            )
            await port_forward_manager.reconcile_workspaces()
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except httpx.HTTPStatusError as error:
            status_code = error.response.status_code
            raise HTTPException(
                status_code=status_code if 400 <= status_code < 500 else 502,
                detail=_remote_http_error_detail(error),
            ) from error
        except (PermissionError, RuntimeError, httpx.HTTPError) as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return APIResponse(
            data=_remote_managed_workspace_list(
                registry,
                gateway_connection_id,
                remote_data,
            ),
            request_id=request_id,
        )
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/inbound-access",
    response_model=APIResponse[GatewayInboundAccessListDTO],
)
async def gateway_inbound_access(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    return APIResponse(
        data=await _inbound_gateway_access_list(registry),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/managed-workspaces",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def create_gateway_managed_workspace(
    payload: CreateGatewayManagedWorkspaceRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    port_forward_manager: SshPortForwardManager = Depends(get_port_forward_manager),
):
    connection_id = payload.gateway_connection_id
    remote_data: dict[str, object] | None = None
    try:
        if connection_id is None:
            await create_direct_managed_workspace(
                registry=registry,
                project_root=get_project_root(),
                log_dir=_gateway_root() / "logs",
                root_path=payload.root_path,
                name=payload.name,
                create_directory=payload.create_directory,
            )
        else:
            remote_data = await request_remote_gateway_management(
                gateway_url=registry.remote_gateway_url(connection_id),
                credential=_remote_gateway_credential(connection_id),
                method="POST",
                path="/api/gateway/federation/managed-workspaces",
                request_id=request_id,
                payload={
                    "root_path": payload.root_path,
                    "name": payload.name,
                    "create_directory": payload.create_directory,
                },
            )
            await refresh_remote_gateway_projections(
                registry=registry,
                connection_id=connection_id,
            )
            await port_forward_manager.reconcile_workspaces()
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        raise HTTPException(
            status_code=status_code if 400 <= status_code < 500 else 502,
            detail=_remote_http_error_detail(error),
        ) from error
    except (PermissionError, OSError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if connection_id is not None and remote_data is not None:
        return APIResponse(
            data=_remote_managed_workspace_list(
                registry,
                connection_id,
                remote_data,
            ),
            request_id=request_id,
        )
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@app.delete(
    "/api/gateway/managed-workspaces/{workspace_id}",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def remove_gateway_managed_workspace(
    workspace_id: str,
    gateway_connection_id: str | None = Query(default=None),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    port_forward_manager: SshPortForwardManager = Depends(get_port_forward_manager),
):
    remote_data: dict[str, object] | None = None
    try:
        if gateway_connection_id is None:
            remove_direct_managed_workspace(registry, workspace_id)
        else:
            remote_data = await request_remote_gateway_management(
                gateway_url=registry.remote_gateway_url(gateway_connection_id),
                credential=_remote_gateway_credential(gateway_connection_id),
                method="DELETE",
                path=(
                    "/api/gateway/federation/managed-workspaces/"
                    f"{quote(workspace_id, safe='')}"
                ),
                request_id=request_id,
            )
            await refresh_remote_gateway_projections(
                registry=registry,
                connection_id=gateway_connection_id,
            )
            await port_forward_manager.reconcile_workspaces()
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        raise HTTPException(
            status_code=status_code if 400 <= status_code < 500 else 502,
            detail=_remote_http_error_detail(error),
        ) from error
    except (OSError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if gateway_connection_id is not None and remote_data is not None:
        return APIResponse(
            data=_remote_managed_workspace_list(
                registry,
                gateway_connection_id,
                remote_data,
            ),
            request_id=request_id,
        )
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@app.get("/api/gateway/ui-settings", response_model=APIResponse[WebUISettingsDTO])
async def get_web_ui_settings(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, user_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        theme_asset_root = _theme_asset_root(context, profiles)
        theme_config = _theme_config_for_access(
            context,
            profiles=profiles,
            base_config=getattr(request.app.state, "gateway_config", None),
            gateway_root=theme_asset_root,
        )
        settings = resolve_settings_theme(
            user_settings,
            config=load_validated_theme_config(
                gateway_root=theme_asset_root,
                config=theme_config,
            ),
            gateway_root=theme_asset_root,
        )
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=settings, request_id=request_id)


@app.put("/api/gateway/ui-settings", response_model=APIResponse[WebUISettingsDTO])
async def update_web_ui_settings(
    payload: WebUISettingsUpdateDTO,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, current_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        theme_asset_root = _theme_asset_root(context, profiles)
        config = load_validated_theme_config(
            gateway_root=theme_asset_root,
            config=_theme_config_for_access(
                context,
                profiles=profiles,
                base_config=getattr(request.app.state, "gateway_config", None),
                gateway_root=theme_asset_root,
            ),
        )
        if payload.theme is not None:
            theme_id = (
                payload.theme.theme_id
                or current_settings.theme.theme_id
                or config.default_theme_id
            )
            background = (
                payload.theme.background
                if "background" in payload.theme.model_fields_set
                else current_settings.theme.background
            )
            resolve_theme(
                theme_id,
                config=config,
                gateway_root=theme_asset_root,
                background_override=background,
            )
        updated = merge_web_ui_settings_values(current_settings, payload)
        if context.kind == "user":
            if context.user_id is None:
                raise RuntimeError("普通用户访问上下文缺少 user_id")
            profiles.write_ui_settings(user_id=context.user_id, settings=updated)
        resolved = resolve_settings_theme(
            updated,
            config=config,
            gateway_root=theme_asset_root,
        )
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=resolved, request_id=request_id)


@app.get("/api/gateway/themes", response_model=APIResponse[GatewayThemeCatalogDTO])
async def get_gateway_themes(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, user_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        theme_asset_root = _theme_asset_root(context, profiles)
        theme_config = _theme_config_for_access(
            context,
            profiles=profiles,
            base_config=getattr(request.app.state, "gateway_config", None),
            gateway_root=theme_asset_root,
        )
        catalog = theme_catalog(
            user_settings,
            config=load_validated_theme_config(
                gateway_root=theme_asset_root,
                config=theme_config,
            ),
            gateway_root=theme_asset_root,
        )
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=catalog, request_id=request_id)


@app.get("/api/gateway/ui-assets", response_model=APIResponse[GatewayUIAssetListDTO])
async def get_gateway_ui_assets(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, user_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        if context.kind == "guest":
            assets = []
        else:
            theme_asset_root = _theme_asset_root(context, profiles)
            theme_config = _theme_config_for_access(
                context,
                profiles=profiles,
                base_config=getattr(request.app.state, "gateway_config", None),
                gateway_root=theme_asset_root,
            )
            synchronize_theme_asset_references(
                load_validated_theme_config(
                    gateway_root=theme_asset_root,
                    config=theme_config,
                ),
                user_settings,
                gateway_root=theme_asset_root,
            )
            assets = list_ui_assets(theme_asset_root)
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=GatewayUIAssetListDTO(items=assets),
        request_id=request_id,
    )


@app.post("/api/gateway/ui-assets", response_model=APIResponse[GatewayUIAssetDTO])
async def upload_gateway_ui_asset(
    request: Request,
    file: UploadFile = File(),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    context = _current_user_access(request, access_service)
    if context.kind == "guest":
        raise HTTPException(status_code=403, detail="游客不能上传主题资源")
    content = await file.read(MAX_UI_ASSET_BYTES + 1)
    try:
        asset = import_ui_asset(
            content,
            original_filename=file.filename or "background",
            gateway_root=_theme_asset_root(context, profiles),
            declared_content_type=file.content_type,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=asset, request_id=request_id)


@app.get("/api/gateway/ui-assets/{asset_id}", response_class=FileResponse)
async def get_gateway_ui_asset(
    asset_id: str,
    request: Request,
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    context = _current_user_access(request, access_service)
    try:
        path, asset = resolve_ui_asset(
            asset_id,
            gateway_root=_theme_asset_root(context, profiles),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return FileResponse(
        path,
        media_type=asset.content_type,
        filename=asset.original_filename,
        headers={
            "ETag": f'"{asset.sha256}"',
            "Cache-Control": "public, max-age=31536000, immutable",
        },
        content_disposition_type="inline",
    )


@app.delete(
    "/api/gateway/ui-assets/{asset_id}",
    response_model=APIResponse[GatewayUIAssetListDTO],
)
async def remove_gateway_ui_asset(
    asset_id: str,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, user_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        if context.kind == "guest":
            raise HTTPException(status_code=403, detail="游客不能删除主题资源")
        theme_asset_root = _theme_asset_root(context, profiles)
        theme_config = _theme_config_for_access(
            context,
            profiles=profiles,
            base_config=getattr(request.app.state, "gateway_config", None),
            gateway_root=theme_asset_root,
        )
        references = referenced_asset_ids(
            load_validated_theme_config(
                gateway_root=theme_asset_root,
                config=theme_config,
            ),
            user_settings,
            gateway_root=theme_asset_root,
        ).get(asset_id, [])
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if references:
        raise HTTPException(
            status_code=409,
            detail=f"背景资源正在被主题引用，不能删除: {', '.join(references)}",
        )
    try:
        delete_ui_asset(asset_id, gateway_root=theme_asset_root)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(
        data=GatewayUIAssetListDTO(items=list_ui_assets(theme_asset_root)),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/local-directories",
    response_model=APIResponse[GatewayDirectoryListDTO],
)
async def list_local_directories(
    path: str | None = Query(
        default=None,
        description="要浏览的本机目录；为空时使用用户主目录",
    ),
    limit: int = Query(default=120, ge=1, le=500),
    gateway_connection_id: str | None = Query(default=None),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    if gateway_connection_id is not None:
        query = urlencode(
            {
                **({"path": path} if path is not None else {}),
                "limit": limit,
            }
        )
        try:
            remote_data = await request_remote_gateway_management(
                gateway_url=registry.remote_gateway_url(gateway_connection_id),
                credential=_remote_gateway_credential(gateway_connection_id),
                method="GET",
                path=f"/api/gateway/federation/directories?{query}",
                request_id=request_id,
            )
            listing = GatewayDirectoryListDTO.model_validate(remote_data)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except httpx.HTTPStatusError as error:
            status_code = error.response.status_code
            raise HTTPException(
                status_code=status_code if 400 <= status_code < 500 else 502,
                detail=_remote_http_error_detail(error),
            ) from error
        except (PermissionError, RuntimeError, httpx.HTTPError) as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return APIResponse(data=listing, request_id=request_id)

    try:
        listing = await _directory_listing(path, limit=limit)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(
            status_code=403,
            detail=str(error),
        ) from error
    except OSError as error:
        raise HTTPException(
            status_code=400,
            detail=f"读取本机目录失败: {error}",
        ) from error
    return APIResponse(data=listing, request_id=request_id)


@app.get(
    "/api/gateway/ssh-connections",
    response_model=APIResponse[SshConnectionOptionListDTO],
)
async def list_ssh_connections(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        options = await asyncio.to_thread(list_ssh_connection_options, registry)
    except (OSError, RuntimeError, ValueError) as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    return APIResponse(
        data=SshConnectionOptionListDTO(items=options),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/workspaces/local",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def add_local_workspace(
    payload: AddLocalWorkspaceRequest,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    workspace_root = Path(payload.root_path).expanduser().resolve()
    if not workspace_root.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"本机工作区不存在: {workspace_root}",
        )

    if payload.backend_url is None:
        try:
            await create_direct_managed_workspace(
                registry=registry,
                project_root=get_project_root(),
                log_dir=_gateway_root() / "logs",
                root_path=str(workspace_root),
                name=payload.name,
                create_directory=False,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    else:
        backend_url = payload.backend_url.rstrip("/")
        gateway_config = request.app.state.gateway_config
        await wait_for_http_ok(
            f"{backend_url}/api/v1/health",
            request_timeout_seconds=(
                gateway_config.gateway_process_health_request_timeout_seconds
            ),
            poll_interval_seconds=(
                gateway_config.gateway_process_health_poll_interval_seconds
            ),
        )
        registry.upsert(
            WorkspaceTarget(
                workspace_id=build_workspace_id(
                    "local",
                    str(workspace_root),
                    backend_url,
                ),
                name=payload.name or _workspace_name(str(workspace_root)),
                name_customized=bool(payload.name),
                root_path=str(workspace_root),
                backend_url=backend_url,
                connection_kind="local",
                owner="manual",
                managed=False,
            ),
            runtime=WorkspaceRuntime(service_urls={"workspace_api": backend_url}),
            activate=False,
        )
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/remote-gateways",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def add_remote_gateway(
    payload: AddRemoteGatewayRequest,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    port_forward_manager: SshPortForwardManager = Depends(get_port_forward_manager),
):
    try:
        connection = resolve_ssh_connection_request(payload, registry)
    except (LookupError, RuntimeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        gateway_config = request.app.state.gateway_config
        await register_remote_gateway(
            registry=registry,
            log_dir=_gateway_root() / "logs",
            name=payload.name,
            host=connection.host,
            port=connection.port,
            username=connection.username,
            private_key_path=connection.private_key_path,
            ssh_config_host=connection.ssh_config_host,
            remote_gateway_port=connection.remote_gateway_port,
            remote_pair_command=connection.remote_pair_command,
            activate=False,
            health_request_timeout_seconds=(
                gateway_config.gateway_process_health_request_timeout_seconds
            ),
            health_poll_interval_seconds=(
                gateway_config.gateway_process_health_poll_interval_seconds
            ),
        )
        await port_forward_manager.reconcile_workspaces()
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@app.post("/api/gateway/workspaces/ssh")
async def reject_legacy_ssh_workspace(
    _: str = Depends(verify_gateway_token),
):
    raise HTTPException(
        status_code=410,
        detail=(
            "SSH 直连 Workspace API 已移除。请调用 /api/gateway/remote-gateways，"
            "只连接远端 Gateway；remote_workspace_path 与 remote_backend_* "
            "字段不再接受。"
        ),
    )


@app.post(
    "/api/gateway/workspaces/{workspace_id}/activate",
    response_model=APIResponse[ActivateGatewayWorkspaceResultDTO],
)
async def activate_workspace(
    workspace_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        registry.activate(workspace_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(
        data=ActivateGatewayWorkspaceResultDTO(active_workspace_id=workspace_id),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/workspaces/{workspace_id}/reconnect",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def reconnect_workspace(
    workspace_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    controller: GatewayWorkspaceRuntimeController = Depends(
        get_workspace_runtime_controller
    ),
):
    try:
        await controller.reconnect_ssh(workspace_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (FileNotFoundError, OSError, RuntimeError, httpx.HTTPError) as error:
        registry.mark_connection_error(workspace_id, str(error))
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/workspaces/{workspace_id}/runtime/start",
    response_model=APIResponse[GatewayRuntimeStateResultDTO],
)
async def start_managed_workspace_backend(
    workspace_id: str,
    auth: GatewayAuthContext = Depends(verify_gateway_access),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    controller: GatewayWorkspaceRuntimeController = Depends(
        get_workspace_runtime_controller
    ),
):
    try:
        if (
            auth.kind == "federation"
            and registry.resolve(workspace_id).connection_kind != "local"
        ):
            raise ValueError("bounded federation 禁止委托嵌套远程工作区启动")
        result = await controller.start_managed_backend(
            workspace_id,
            request_id=request_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        registry.mark_connection_error(workspace_id, str(error))
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (FileNotFoundError, OSError, RuntimeError, httpx.HTTPError) as error:
        registry.mark_connection_error(workspace_id, str(error))
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@app.post(
    "/api/gateway/workspaces/{workspace_id}/runtime/stop",
    response_model=APIResponse[GatewayRuntimeStateResultDTO],
)
async def stop_managed_workspace_backend(
    workspace_id: str,
    auth: GatewayAuthContext = Depends(verify_gateway_access),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    controller: GatewayWorkspaceRuntimeController = Depends(
        get_workspace_runtime_controller
    ),
):
    try:
        if (
            auth.kind == "federation"
            and registry.resolve(workspace_id).connection_kind != "local"
        ):
            raise ValueError("bounded federation 禁止委托嵌套远程工作区关闭")
        result = await controller.stop_managed_backend(
            workspace_id,
            request_id=request_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (OSError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@app.post(
    "/api/gateway/workspaces/{workspace_id}/runtime/restart-safe",
    response_model=APIResponse[GatewayRuntimeRestartResultDTO],
)
async def safe_restart_managed_workspace_backend(
    workspace_id: str,
    auth: GatewayAuthContext = Depends(verify_gateway_access),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    controller: GatewayWorkspaceRuntimeController = Depends(
        get_workspace_runtime_controller
    ),
):
    try:
        if (
            auth.kind == "federation"
            and registry.resolve(workspace_id).connection_kind != "local"
        ):
            raise ValueError("bounded federation 禁止委托嵌套远程工作区重启")
        result = await controller.safe_restart_managed_backend(
            workspace_id,
            request_id=request_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (FileNotFoundError, OSError, RuntimeError, httpx.HTTPError) as error:
        registry.mark_connection_error(workspace_id, str(error))
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@app.post(
    "/api/gateway/workspaces/{workspace_id}/runtime/restart-force",
    response_model=APIResponse[GatewayRuntimeRestartResultDTO],
)
async def force_restart_managed_workspace_backend(
    workspace_id: str,
    auth: GatewayAuthContext = Depends(verify_gateway_access),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    controller: GatewayWorkspaceRuntimeController = Depends(
        get_workspace_runtime_controller
    ),
):
    try:
        if (
            auth.kind == "federation"
            and registry.resolve(workspace_id).connection_kind != "local"
        ):
            raise ValueError("bounded federation 禁止委托嵌套远程工作区重启")
        result = await controller.force_restart_managed_backend(
            workspace_id,
            request_id=request_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (FileNotFoundError, OSError, RuntimeError, httpx.HTTPError) as error:
        registry.mark_connection_error(workspace_id, str(error))
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@app.post(
    "/api/gateway/workspaces/{workspace_id}/probe",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def probe_external_workspace_backend(
    workspace_id: str,
    auth: GatewayAuthContext = Depends(verify_gateway_access),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    controller: GatewayWorkspaceRuntimeController = Depends(
        get_workspace_runtime_controller
    ),
):
    try:
        if (
            auth.kind == "federation"
            and registry.resolve(workspace_id).connection_kind != "local"
        ):
            raise ValueError("bounded federation 禁止探测嵌套远程工作区")
        await controller.probe_external_backend(workspace_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (OSError, RuntimeError) as error:
        registry.mark_connection_error(workspace_id, str(error))
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@app.put(
    "/api/gateway/workspaces/order",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def reorder_workspaces(
    payload: ReorderGatewayWorkspacesRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        registry.reorder(payload.workspace_ids)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@app.patch(
    "/api/gateway/workspaces/{workspace_id}",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def update_workspace(
    workspace_id: str,
    payload: UpdateGatewayWorkspaceRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        update_fields: dict[str, str | None] = {}
        if "name" in payload.model_fields_set:
            update_fields["name"] = payload.name
        if "parent_workspace_id" in payload.model_fields_set:
            update_fields["parent_workspace_id"] = payload.parent_workspace_id
        registry.update(workspace_id, **update_fields)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@app.delete(
    "/api/gateway/workspaces/{workspace_id}",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def remove_workspace(
    workspace_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    port_forward_manager: SshPortForwardManager = Depends(get_port_forward_manager),
):
    try:
        await port_forward_manager.remove_workspace(workspace_id)
        registry.remove(workspace_id, owner="manual_crud")
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


# 两个代理 Router 含通配路由，必须晚于 Gateway 自有接口注册，否则会吞掉
# `/api/gateway/workspaces/{id}/runtime/*` 等更具体的控制面路由。
app.include_router(gateway_control_router)
app.include_router(device_connections_router)
app.include_router(port_forwards_router)
app.include_router(auxiliary_proxy_router)
app.include_router(workspace_proxy_router)
app.include_router(federation_router)

# 静态 UI 必须最后挂载，确保 Gateway API、工作区代理、SSE 和 WebSocket
# 路由优先匹配；源码开发未声明 BOXTEAM_WEB_ASSETS 时由 Vite 提供页面。
install_static_web_ui(app)
