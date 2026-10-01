"""Gateway 生命周期（lifespan）实现（自 app/gateway/main.py 纯搬迁）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.core.env import (
    get_project_root,
    load_boxteam_env,
)
from app.core.logging_config import configure_application_logging
from app.core.path_utils import (
    get_user_gateway_config_path,
    get_user_gateway_local_config_path,
)
from app.gateway.auth import get_gateway_local_token
from app.gateway.config import (
    GatewayConfigReloadService,
    load_gateway_config,
)
from app.gateway.control.catalog_search import GatewaySessionCatalogSearchService
from app.gateway.control.coordinator import SessionGeneratorCoordinator
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.generators import SessionGeneratorStore
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.control.resource_catalog import GatewayResourceCatalogService
from app.gateway.control.scheduler import SessionGeneratorScheduler
from app.gateway.control.user_access import UserAccessService
from app.gateway.control.user_profile import UserProfileStore
from app.gateway.control.view_state import UserViewStateStore
from app.gateway.credentials import (
    FederationCredentialStore,
    load_or_create_gateway_id,
)
from app.gateway.federation.identity import load_or_create_signing_key
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.federation.rpc import FederationRpcService
from app.gateway.federation.store import (
    FederationControlStore,
    federation_control_database,
)
from app.gateway.federation.workspace_port import (
    WorkspaceCatalogPort,
    WorkspaceSessionMainPort,
)
from app.gateway.routes._shared import (
    _gateway_root,
    _wait_for_managed_runtime_restore_tasks,
)
from app.gateway.runtime.consumer_protocol import (
    GatewayRuntimeConsumerTransaction,
    runtime_fencing_token_digest,
)
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.gateway.runtime_proof import (
    _apply_gateway_runtime_config,
    _cleanup_user_access_periodically,
    _gateway_federation_policy_payload,
    _gateway_pending_consumer_health_digests,
    _gateway_pending_runtime_consumer_stages,
    _record_owned_gateway_startup_failure,
    _refresh_federation_workspace_ports,
    _report_managed_runtime_restore_task,
    _should_preserve_gateway_generation_for_handoff,
)
from app.gateway.server.bootstrap import (
    _restore_managed_local_runtimes,
    create_registry,
)
from app.services.infrastructure.config.state import build_secret_binding_summary

logger = logging.getLogger(__name__)


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
