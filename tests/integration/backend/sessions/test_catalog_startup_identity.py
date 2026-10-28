"""真实 Session 创建后，启动必须先拒绝 catalog 中漂移的冻结存储身份。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import container as container_module
from app.container import AppContainer, WorkspaceContainerRegistry
from app.core.identifier import create_prefixed_id
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.session_catalog_store import (
    CatalogMaintenanceRequiredError,
    SessionCatalogStore,
)
from app.core.session_catalog_store.queries import _register_session_list_functions
from app.core.sqlite_state import SQLiteProcessOwnership
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
)
from tests.support.workspaces import (
    prepare_test_workspace,
    render_test_workspace_config,
)


@pytest.fixture
def startup_container(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> AppContainer:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    prepare_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(),
    )
    # 身份/恢复测试不调用模型，不要求本机提供个人Provider凭据。
    config = json.loads(render_test_workspace_config(
        (Path.cwd() / "configs/tests/workspace/default.jsonc").read_text(encoding="utf-8"),
    ))
    for provider in config["llm"]["providers"]:
        provider["api_key"] = "startup-contract-unused-key"
        provider.pop("auth", None)
    (context.workspace_root / ".boxteam").mkdir()
    (context.workspace_root / ".boxteam/workspace.jsonc").write_text(
        json.dumps(config, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    monkeypatch.setenv("WORKSPACE_ROOT", str(context.workspace_root))
    monkeypatch.setenv("BOXTEAM_HOME", str(context.boxteam_home))
    return _build_owned_container(context.workspace_root, monkeypatch, request)


def _catalog_dump(path: Path) -> str:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        return "\n".join(connection.iterdump())


@pytest.fixture
def construction_registry(
    startup_container: AppContainer, request: pytest.FixtureRequest,
) -> WorkspaceContainerRegistry:
    root = startup_container.workspace_root / "candidate"
    prepare_test_workspace(
        workspace_root=root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(),
    )
    (root / ".boxteam").mkdir()
    (root / ".boxteam/workspace.jsonc").write_bytes(
        (startup_container.workspace_root / ".boxteam/workspace.jsonc").read_bytes()
    )
    workspace_id = load_or_create_workspace_id(root)
    registry = container_module.WorkspaceContainerRegistry(
        mounted_workspaces=MountedWorkspaceRegistry([MountedWorkspace(workspace_id, root)]),
        project_root=Path.cwd(),
    )
    request.addfinalizer(registry.close)
    return registry


@pytest.mark.parametrize("stage", ["catalog", "config", "files"])
async def test_failed_container_construction_releases_owned_stores_and_allows_retry(
    startup_container: AppContainer, construction_registry: WorkspaceContainerRegistry,
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    registry = construction_registry
    mount, = registry.mounted_workspaces.mounts
    root, workspace_id = mount.workspace_root, mount.workspace_id
    activity_owners = []
    attachment_owners = []
    original_activity = container_module.WorkspaceActivityService
    original_attachments = container_module.AttachmentBlobStore
    failure = RuntimeError(f"构造失败边界-{stage}")

    def activity(**arguments):
        owner = original_activity(**arguments)
        activity_owners.append(owner)
        request.addfinalizer(owner.close)
        return owner

    def attachments(*arguments, **keywords):
        owner = original_attachments(*arguments, **keywords)
        attachment_owners.append(owner)
        request.addfinalizer(owner.close)
        return owner

    def fail(*_arguments, **_keywords):
        raise failure

    with monkeypatch.context() as faults:
        faults.setattr(container_module, "WorkspaceActivityService", activity)
        faults.setattr(container_module, "AttachmentBlobStore", attachments)
        faults.setattr(container_module, {
            "catalog": "get_session_catalog_components", "config": "ConfigService", "files": "WorkspaceService",
        }[stage], fail)
        with pytest.raises(RuntimeError, match="构造失败边界") as caught:
            registry.get(workspace_id)
    assert caught.value is failure, "保留原错误和traceback，不能依赖GC释放资源"
    assert len(activity_owners) == 1
    assert len(attachment_owners) == (1 if stage == "files" else 0)
    probe = SQLiteProcessOwnership(root / ".boxteam/state/workspace.sqlite")
    probe.acquire()
    probe.release()
    for owner in activity_owners:
        with pytest.raises(RuntimeError, match="已关闭"):
            owner.diagnostics()
    for owner in attachment_owners:
        with pytest.raises(RuntimeError, match="已关闭"):
            owner._catalog.connection.execute("SELECT 1")

    recovered = registry.get(workspace_id)
    request.addfinalizer(recovered.attachment_blob_store.close)
    request.addfinalizer(recovered.workspace_activity_service.close)
    created = await recovered.session_service.create(SessionCreateRequest(title="构造失败后重试"))
    assert (await recovered.session_service.get(created.session_id)).session_id == created.session_id
    assert registry.get(workspace_id) is recovered
    peer = await startup_container.session_service.create(SessionCreateRequest(title="旁系工作区继续可用"))
    assert (await startup_container.session_service.get(peer.session_id)).session_id == peer.session_id
    assert recovered.workspace_id != startup_container.workspace_id


@pytest.mark.parametrize("close_phase", ["before_native_close", "after_native_close"])
@pytest.mark.parametrize("recovery", ["rebuild", "shutdown"])
async def test_failed_construction_retains_catalog_until_original_close_succeeds(
    startup_container: AppContainer, construction_registry: WorkspaceContainerRegistry,
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
    close_phase: str, recovery: str,
) -> None:
    registry = construction_registry
    mount, = registry.mounted_workspaces.mounts
    sessions_root = mount.workspace_root / ".boxteam/sessions"
    original_close = SessionCatalogStore.close
    construction_error = RuntimeError("配置装配失败")
    close_error = OSError("原catalog关闭失败")
    construction_attempts: list[None] = []
    closing_stores: list[SessionCatalogStore] = []

    def fail_config(**_arguments: object) -> None:
        construction_attempts.append(None)
        raise construction_error

    def fail_close(store: SessionCatalogStore) -> None:
        if store.sessions_root != sessions_root:
            original_close(store)
            return
        closing_stores.append(store)
        if len(closing_stores) == 1:
            # 失败的产品实现也必须由装具归还真实连接，避免污染后续测试。
            request.addfinalizer(lambda: original_close(store))
        if close_phase == "after_native_close":
            original_close(store)
        raise close_error

    with monkeypatch.context() as faults:
        faults.setattr(container_module, "ConfigService", fail_config)
        faults.setattr(SessionCatalogStore, "close", fail_close)
        with pytest.raises(ExceptionGroup) as initial:
            registry.get(mount.workspace_id)
        assert initial.value.exceptions == (construction_error, close_error)
        original_store, = closing_stores
        with pytest.raises(OSError) as graph_retry:
            registry.get(mount.workspace_id)
        assert graph_retry.value is close_error
        with pytest.raises(OSError) as catalog_retry:
            registry.get_catalog_components(sessions_root, mount.workspace_id)
        assert catalog_retry.value is close_error
        assert construction_attempts == [None], "未关闭原owner时不得重新装配或打开catalog"
        assert closing_stores == [original_store] * 3
        if recovery == "shutdown":
            with pytest.raises(ExceptionGroup) as shutdown:
                registry.close()
            assert shutdown.value.exceptions == (close_error,)
            assert closing_stores == [original_store] * 4

    if recovery == "shutdown":
        registry.close()
        with pytest.raises(RuntimeError, match="已关闭"):
            registry.get(mount.workspace_id)
    else:
        fresh = registry.get_catalog_components(sessions_root, mount.workspace_id)
        assert fresh.resolver.catalog_store is not original_store
        recovered = registry.get(mount.workspace_id)
        request.addfinalizer(recovered.attachment_blob_store.close)
        request.addfinalizer(recovered.workspace_activity_service.close)
        assert recovered.session_catalog is fresh
        created = await recovered.session_service.create(SessionCreateRequest(title="原catalog关闭后重建"))
        assert (await recovered.session_service.get(created.session_id)).session_id == created.session_id
        assert registry.get(mount.workspace_id) is recovered

    with pytest.raises(RuntimeError, match="已关闭|closed"):
        _ = original_store.connection
    peer = await startup_container.session_service.create(SessionCreateRequest(title="关闭失败不影响旁系"))
    assert (await startup_container.session_service.get(peer.session_id)).session_id == peer.session_id


@pytest.mark.parametrize("owner", ["node", "preparing"])
@pytest.mark.parametrize("corruption", ["date", "naive_time", "locator_leaf"])
async def test_container_startup_rejects_storage_identity_drift_without_repair(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, owner: str, corruption: str,
) -> None:
    container = startup_container
    service = container.session_service
    # 通过产品创建入口生成 catalog、manifest、main 和完整控制库。
    source = await service.create(SessionCreateRequest(title="启动身份核验"))
    if owner == "preparing":
        _, record = await service.prepare_context_fork(
            idempotency_key="startup-identity-prepared", title="未发布副本",
            agent_id=source.current_agent_id, parent_session_id=None,
            context_source_session_id=source.session_id,
        )
        session_id, created_at = record.session_id, datetime.fromisoformat(record.created_at)
        directory = service.path_resolver.resolve_prepared_session_node(record)
        table, identity_column = "session_creation_records", "session_id"
    else:
        session_id, created_at = source.session_id, source.created_at
        directory = service.path_resolver.resolve_session_node(session_id)
        table, identity_column = "nodes", "node_id"
    store = service.path_resolver.catalog_store
    path = store.database_path
    container.workspace_activity_service.store.close()
    container.session_catalog.close()
    # 人工损坏是测试输入，启动恢复不得替测试修复它或移动目录。
    with closing(sqlite3.connect(path)) as connection:
        _register_session_list_functions(connection)
        if corruption == "locator_leaf":
            other_id = create_prefixed_id("ses")
            locator = f"sessions/{created_at:%Y/%m/%d}/{other_id}"
            if owner == "node":
                target = directory.with_name(other_id)
                directory.rename(target)
                directory = target
            column, value = "storage_relative_locator", locator
        else:
            value = (created_at + timedelta(days=1)).isoformat() if corruption == "date" else created_at.replace(tzinfo=None).isoformat()
            column = "created_at"
            if owner == "node":
                manifest = directory / "session.json"
                payload = json.loads(manifest.read_text())
                payload["created_at"] = value
                manifest.write_text(json.dumps(payload))
        connection.execute(
            f"UPDATE {table} SET {column}=? WHERE {identity_column}=?", (value, session_id),
        )
        connection.commit()
    before = _catalog_dump(path)
    artifacts = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    with pytest.raises(CatalogMaintenanceRequiredError, match="存储身份") as error:
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert session_id in str(error.value)
    assert _catalog_dump(path) == before
    assert directory.is_dir()
    assert {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()} == artifacts


@pytest.mark.parametrize("renamed", [False, True])
async def test_container_startup_accepts_original_preparing_storage_identity(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, renamed: bool,
) -> None:
    container = startup_container
    source = await container.session_service.create(SessionCreateRequest(title="保留原创建意图"))
    _, record = await container.session_service.prepare_context_fork(
        idempotency_key="healthy-prepared", title="仍待发布的副本",
        agent_id=source.current_agent_id, parent_session_id=None,
        context_source_session_id=source.session_id,
    )
    if renamed:
        creation = container.session_catalog.creation_service
        creation._rename_staging_to_target(record, creation.read_prepared_session_metadata(record))
    path = container.session_service.path_resolver.catalog_store.database_path
    container.workspace_activity_service.store.close()
    container.session_catalog.close()
    before = _catalog_dump(path)
    restarted = _build_owned_container(container.workspace_root, monkeypatch, request)
    assert restarted.session_service.path_resolver.catalog_store.get_creation_record("healthy-prepared") == record
    assert _catalog_dump(path) == before
    assert (await restarted.session_service.get(source.session_id)).session_id == source.session_id
