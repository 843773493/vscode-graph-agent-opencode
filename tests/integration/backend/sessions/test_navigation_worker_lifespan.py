"""真实 AppContainer 与 FastAPI lifespan 的导航 worker 就绪、恢复和失败清理。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app import main
from app.container import AppContainer, WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import get_session_path_resolver
from app.core.sqlite_state import utc_now_text
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
    NavigationMutationIntentDTO,
)
from app.services.business.session_navigation.executor import NavigationMutationExecutor
from app.services.business.session_navigation.operations_service import (
    local_navigation_scope,
)
from app.services.business.session_navigation.queue_store import (
    NavigationMutationQueueStore,
    NavigationQueueOwnerError,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.canonical_id_at import session_id_at
from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.support.workspaces import prepare_default_test_workspace


def _build_owned_container(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest | None = None,
) -> AppContainer:
    """预装配使用真实 registry owner，main lifespan 排空后由同一 registry 关闭。"""
    registry = WorkspaceContainerRegistry(
        mounted_workspaces=MountedWorkspaceRegistry([
            MountedWorkspace(load_or_create_workspace_id(workspace_root), workspace_root)
        ]),
        project_root=Path.cwd(),
    )
    container = registry.get(registry.workspace_ids[0])
    if request is not None:
        request.addfinalizer(registry.close)
    monkeypatch.setattr(main, "WorkspaceContainerRegistry", lambda **_: registry)
    return container


def _isolate_non_navigation_services(
    container: AppContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, AsyncMock]:
    """只隔离 lifespan 周边 owner；真实 AppContainer 与目录 worker 保持工作。"""
    monkeypatch.setenv(
        "BOXTEAM_MOUNTED_WORKSPACES",
        json.dumps([{
            "workspace_id": container.workspace_id,
            "workspace_root": str(container.workspace_root), "terminal_backend_url": None, "browser_backend_url": None, "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None}]),
    )
    async_stubs: dict[str, tuple[object, str, object]] = {
        "mcp_start": (container.mcp_catalog_owner, "start", None),
        "registry_start": (
            container.workspace_file_resource_registry,
            "start",
            None,
        ),
        "config_watch_start": (container.config_service, "start_watching", None),
        "trace_start": (container.trace_event_recorder, "start", None),
        "listener_register": (container.job_event_bus, "register_durable_listener", None),
        "listener_unregister": (
            container.job_event_bus,
            "unregister_durable_listener",
            None,
        ),
        "job_reconcile": (container.runtime_service, "reconcile_stale_executions", 0),
        "generation_start": (container.session_generation_service, "start", None),
        "generation_stop": (container.session_generation_service, "shutdown", None),
        "terminal_start": (container.terminal_steering_service, "start", None),
        "terminal_stop": (container.terminal_steering_service, "shutdown", None),
        "goal_resume": (container.goal_runtime_service, "resume_active_goals", None),
        "node_debug_close": (container.node_debug_service, "close", None),
        "config_close": (container.config_service, "close", None),
        "agent_start": (container.agent_execution_service, "start", None),
        "agent_shutdown": (container.agent_execution_service, "shutdown", None),
        "tool_test_shutdown": (container.tool_test_service, "shutdown", None),
        "trace_stop": (container.trace_event_recorder, "stop", None),
        "mcp_stop": (container.mcp_catalog_owner, "shutdown", None),
    }
    stubs: dict[str, AsyncMock] = {}
    for name, (owner, method_name, result) in async_stubs.items():
        stub = AsyncMock(return_value=result)
        monkeypatch.setattr(owner, method_name, stub)
        stubs[name] = stub

    for owner, method_name, result in (
        (container.config_service, "validate_workspace_config", None),
        (container.config_service, "get_logger_level", "INFO"),
        (container.config_service, "get_logger_pretty", False),
        (container.config_service, "set_mcp_tool_names", None),
        (container.mcp_catalog_owner, "get_tool_ids", []),
        (container.workspace_activity_service, "prune", 0),
    ):
        stub = Mock(return_value=result)
        setattr(owner, method_name, stub)
    return stubs


@pytest.mark.asyncio
async def test_main_lifespan_recovers_running_operation_and_cleans_up_owner_conflict(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace_root = context.workspace_root
    prepare_default_test_workspace(
        workspace_root=workspace_root,
        template_root=Path.cwd() / "tests" / "fixtures" / "workspaces" / "default_test_workspace",
    )
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("BOXTEAM_HOME", str(context.boxteam_home))

    container = _build_owned_container(workspace_root, monkeypatch, request)
    stubs = _isolate_non_navigation_services(container, monkeypatch)
    monkeypatch.setattr(main, "configure_application_logging", lambda **_: None)
    monkeypatch.setattr(main, "_model_stream_controller", None)

    operations = container.session_catalog_service.operations
    queue = NavigationMutationQueueStore(operations._store)
    scope = local_navigation_scope(container.session_service.workspace_id)
    intent = NavigationMutationIntentDTO(
        client_operation_id="op_" + "c" * 32,
        client_sequence=1,
        kind="create_folder",
        base_catalog_revision=container.session_service.path_resolver.revision,
        name="启动恢复",
    )
    with operations._store.write_transaction() as connection:
        queue.enqueue_batch(
            connection,
            gateway_id=scope.gateway_id,
            workspace_id=scope.workspace_id,
            actor=scope.actor,
            intents=[intent],
            now=utc_now_text(),
        )
        connection.execute(
            "UPDATE navigation_mutation_records SET state = 'running', "
            "holder_id = 'crashed:9', fencing_token = 9 WHERE operation_id = ?",
            (intent.client_operation_id,),
        )

    async with main.lifespan(main.app):
        registry = main.app.state.workspace_container_registry
        assert registry is not None
        assert registry.get(container.workspace_id) is container
        assert operations.worker_state == "running"
        recovered = queue.get_record(
            gateway_id=scope.gateway_id,
            workspace_id=scope.workspace_id,
            actor=scope.actor,
            operation_id=intent.client_operation_id,
        )
        assert recovered is not None and recovered.state == "committed"

    assert operations._worker_task is None
    assert not operations._executor.has_queue_owner
    assert main.app.state.workspace_container_registry is None

    # 前一 lifespan 已关闭连接；重启用新 registry，外部 queue owner 独立持有连接。
    external_resolver = get_session_path_resolver(workspace_root / ".boxteam" / "sessions")
    external_store = external_resolver.catalog_store
    external_executor = NavigationMutationExecutor(
        store=external_store,
        workspace_id=scope.workspace_id,
        queue=NavigationMutationQueueStore(external_store),
        path_resolver=external_resolver,
    )
    restarted = _build_owned_container(workspace_root, monkeypatch, request)
    restart_stubs = _isolate_non_navigation_services(restarted, monkeypatch)
    operations = restarted.session_catalog_service.operations
    original_start = operations.start

    async def start_with_short_conflict_window() -> None:
        await original_start(owner_timeout_seconds=0.05)

    monkeypatch.setattr(operations, "start", start_with_short_conflict_window)
    async with external_executor.queue_owner():
        with pytest.raises(NavigationQueueOwnerError, match="启动恢复超时"):
            async with main.lifespan(main.app):
                raise AssertionError("owner 冲突时 lifespan 不得进入 ready")

    assert operations._worker_task is None
    assert not operations._executor.has_queue_owner
    assert main.app.state.workspace_container_registry is None
    for group in (stubs, restart_stubs):
        for name in ("generation_start", "generation_stop", "terminal_stop", "config_close", "mcp_stop"):
            assert group[name].await_count == 1


@pytest.mark.asyncio
async def test_rejected_retained_session_delete_does_not_block_lifespan_restart(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace_root = context.workspace_root
    prepare_default_test_workspace(
        workspace_root=workspace_root,
        template_root=Path.cwd() / "tests" / "fixtures" / "workspaces" / "default_test_workspace",
    )
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("BOXTEAM_HOME", str(context.boxteam_home))
    monkeypatch.setattr(main, "configure_application_logging", lambda **_: None)
    monkeypatch.setattr(main, "_model_stream_controller", None)

    container = _build_owned_container(workspace_root, monkeypatch, request)
    release_workspace_state = container.workspace_activity_service.close
    _isolate_non_navigation_services(container, monkeypatch)
    resolver = container.session_service.path_resolver
    store = resolver.catalog_store
    workspace_id = container.session_service.workspace_id
    moment = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    session_id = session_id_at(moment)
    target_session_id = session_id_at(
        datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    )
    seed_catalog_session_bundle(
        workspace_root / ".boxteam" / "sessions",
        session_id,
        workspace_id=workspace_id,
        title="被 pinned fork 保留的会话",
    )
    claim_id = "retained-delete-lifespan-claim"
    store.create_or_get_fork_retention_claim(
        claim_id=claim_id,
        workspace_id=workspace_id,
        source_session_id=session_id,
        target_session_id=target_session_id,
        source_lifecycle_generation=1,
    )
    store.activate_fork_retention_claim(claim_id, expected_generation=1)

    scope = local_navigation_scope(workspace_id)
    intent = NavigationMutationIntentDTO(
        client_operation_id="op_" + "d" * 32,
        client_sequence=1,
        kind="delete_session",
        base_catalog_revision=resolver.revision,
        target_node_id=session_id,
    )
    enqueue_request = NavigationMutationEnqueueRequest(intents=[intent])
    operations = container.session_catalog_service.operations

    async with main.lifespan(main.app):
        assert operations.worker_state == "running"
        accepted = await operations.enqueue(enqueue_request, scope)
        assert accepted.accepted_count == 1
        rejected = await operations.await_terminal(intent.client_operation_id, scope)
        assert rejected.state == "rejected"
        assert rejected.error_detail is not None
        assert "source_retained_by_fork" in rejected.error_detail

        delete_record = store.get_subtree_delete_record(intent.client_operation_id)
        assert delete_record.state == "aborted"
        assert delete_record.abort_reason is not None
        assert "source_retained_by_fork" in delete_record.abort_reason
        assert store.get_node(session_id).state == "active"
        assert store.list_pending_subtree_delete_records(workspace_id) == []

    release_workspace_state()
    restart_script = r"""
import asyncio
import os
from pathlib import Path

import pytest

from app import main
from app.services.business.session_navigation.operations_service import local_navigation_scope
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
    _isolate_non_navigation_services,
)

workspace_root = Path(os.environ["WORKSPACE_ROOT"])
patches = pytest.MonkeyPatch()
container = _build_owned_container(workspace_root, patches)
operations = container.session_catalog_service.operations
workspace_id = container.session_service.workspace_id
scope = local_navigation_scope(workspace_id)
operation_id = "op_" + "d" * 32
release_workspace_state = container.workspace_activity_service.close
_isolate_non_navigation_services(container, patches)
patches.setattr(main, "configure_application_logging", lambda **_: None)
patches.setattr(main, "_model_stream_controller", None)

async def restart_worker():
    async with main.lifespan(main.app):
        assert operations.worker_state == "running"
        assert operations.record(operation_id, scope).state == "rejected"
        store = container.session_service.path_resolver.catalog_store
        assert store.get_subtree_delete_record(operation_id).state == "aborted"
        assert store.list_pending_subtree_delete_records(workspace_id) == []
        print("navigation_worker_ready")
    assert operations._worker_task is None
    assert main.app.state.workspace_container_registry is None

try:
    asyncio.run(restart_worker())
finally:
    release_workspace_state()
    patches.undo()
"""
    restarted = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", restart_script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    (context.artifacts_dir / "restarted-worker.stdout.log").write_text(
        restarted.stdout, encoding="utf-8"
    )
    (context.artifacts_dir / "restarted-worker.stderr.log").write_text(
        restarted.stderr, encoding="utf-8"
    )
    (context.artifacts_dir / "restarted-worker.exitcode.txt").write_text(
        f"{restarted.returncode}\n", encoding="utf-8"
    )
    assert restarted.returncode == 0, restarted.stderr
    assert restarted.stdout.splitlines().count("navigation_worker_ready") == 1
