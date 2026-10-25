"""真实挂载 catalog 的实例复用、关闭和构造失败释放。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import Context
from pathlib import Path
from uuid import uuid4

import pytest

from app import container as container_module
from app.container import WorkspaceContainerRegistry
from app.core import path_utils
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import (
    catalog_provider_scope,
    get_session_creation_service,
    get_session_path_resolver,
    initialize_directories,
)
from app.core.session_catalog_store import SessionCatalogStore
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture
def catalog_registry(request: pytest.FixtureRequest):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    mounts = []
    for number in range(34):
        root = context.workspace_root / f"workspace-{number}"
        prepare_test_workspace(
            workspace_root=root,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
            template_items=(),
        )
        mounts.append(MountedWorkspace(str(uuid4()), root))
    registry = WorkspaceContainerRegistry(
        mounted_workspaces=MountedWorkspaceRegistry(mounts),
        project_root=Path.cwd(),
    )
    try:
        yield registry
    finally:
        registry.close()


def test_34_real_mounts_reuse_one_component_and_close_without_revival(
    catalog_registry: WorkspaceContainerRegistry,
) -> None:
    registry = catalog_registry
    resolvers = {}
    with catalog_provider_scope(registry):
        for mount in registry.mounted_workspaces.mounts:
            initialize_directories(mount.workspace_id, mount.workspace_root)
            sessions_root = mount.workspace_root / ".boxteam" / "sessions"
            resolver = get_session_path_resolver(
                sessions_root, workspace_id=mount.workspace_id
            )
            resolvers[mount.workspace_id] = resolver
            components = registry.get_catalog_components(sessions_root, mount.workspace_id)
            assert components.resolver is resolver
            assert get_session_creation_service(sessions_root) is components.creation_service
        assert len({id(resolver.catalog_store) for resolver in resolvers.values()}) == 34
        for mount in registry.mounted_workspaces.mounts:
            sessions_root = mount.workspace_root / ".boxteam" / "sessions"
            assert get_session_path_resolver(sessions_root) is resolvers[mount.workspace_id]
        first = registry.mounted_workspaces.mounts[0]
        sessions_root = first.workspace_root / ".boxteam" / "sessions"
        components = registry.get_catalog_components(sessions_root, first.workspace_id)
        with pytest.raises(RuntimeError, match="根目录与已挂载工作区不一致"):
            registry.get_catalog_components(
                registry.mounted_workspaces.mounts[1].workspace_root / ".boxteam" / "sessions",
                first.workspace_id,
            )
        registry.close()
        with pytest.raises(RuntimeError, match="已关闭"):
            get_session_path_resolver(sessions_root)
        with pytest.raises(RuntimeError, match="已关闭"):
            registry.get(first.workspace_id)
        with (
            catalog_provider_scope(components),
            pytest.raises(RuntimeError, match="已关闭"),
        ):
            get_session_path_resolver(sessions_root)
    for resolver in resolvers.values():
        with pytest.raises(RuntimeError, match="已关闭|closed"):
            _ = resolver.catalog_store.connection


def test_concurrent_first_access_has_one_catalog_owner(
    catalog_registry: WorkspaceContainerRegistry,
) -> None:
    registry = catalog_registry
    mount = registry.mounted_workspaces.mounts[0]
    sessions_root = mount.workspace_root / ".boxteam" / "sessions"
    with ThreadPoolExecutor(max_workers=8) as executor:
        components = list(executor.map(
            lambda _: registry.get_catalog_components(sessions_root, mount.workspace_id),
            range(32),
        ))
    assert all(component is components[0] for component in components)


def test_partial_component_failure_closes_store_and_preserves_prior_mount(
    catalog_registry: WorkspaceContainerRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = catalog_registry
    first, second = registry.mounted_workspaces.mounts[:2]
    prior = registry.get_catalog_components(
        first.workspace_root / ".boxteam" / "sessions", first.workspace_id
    )
    stores = []

    def capture_store(*args, **kwargs):
        store = SessionCatalogStore(*args, **kwargs)
        stores.append(store)
        return store

    def fail_resolver(**_kwargs):
        raise RuntimeError("resolver 构造失败")

    monkeypatch.setattr(path_utils, "SessionCatalogStore", capture_store)
    monkeypatch.setattr(path_utils, "SessionCatalogPathResolver", fail_resolver)
    with pytest.raises(RuntimeError, match="resolver 构造失败"):
        registry.get_catalog_components(
            second.workspace_root / ".boxteam" / "sessions", second.workspace_id
        )
    assert len(stores) == 1
    with pytest.raises(RuntimeError, match="已关闭|closed"):
        _ = stores[0].connection
    assert registry.get_catalog_components(prior.sessions_root, first.workspace_id) is prior
    registry.close()
    with pytest.raises(RuntimeError, match="已关闭|closed"):
        _ = prior.resolver.catalog_store.connection


def test_close_failure_preserves_original_lifecycle_error(
    catalog_registry: WorkspaceContainerRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = catalog_registry
    mount = registry.mounted_workspaces.mounts[0]
    components = registry.get_catalog_components(
        mount.workspace_root / ".boxteam" / "sessions", mount.workspace_id
    )
    real_close = components.resolver.catalog_store.close
    original_close = SessionCatalogStore.close

    def fail_close(_store):
        real_close()
        raise RuntimeError("catalog 关闭失败")

    monkeypatch.setattr(SessionCatalogStore, "close", fail_close)
    with pytest.raises(ExceptionGroup) as caught, registry:
        raise ValueError("原始 lifespan 失败")
    assert isinstance(caught.value.exceptions[0], ValueError)
    assert "原始 lifespan 失败" in str(caught.value.exceptions[0])
    assert isinstance(caught.value.exceptions[1], ExceptionGroup)
    with pytest.raises(RuntimeError, match="已关闭"):
        registry.get(mount.workspace_id)
    monkeypatch.setattr(SessionCatalogStore, "close", original_close)


def test_second_service_graph_failure_releases_only_its_catalog(
    catalog_registry: WorkspaceContainerRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = catalog_registry
    first, second = registry.mounted_workspaces.mounts[:2]
    prior = registry.get_catalog_components(
        first.workspace_root / ".boxteam" / "sessions", first.workspace_id
    )
    failed = []

    def fail_build(**kwargs):
        root = Path(kwargs["workspace_root"]) / ".boxteam" / "sessions"
        resolver = get_session_path_resolver(root)
        failed.append(resolver)
        raise RuntimeError("第二服务图装配失败")

    monkeypatch.setattr(container_module, "build_app_container", fail_build)
    with pytest.raises(RuntimeError, match="第二服务图装配失败"):
        registry.get(second.workspace_id)
    with pytest.raises(RuntimeError, match="已关闭|closed"):
        _ = failed[0].catalog_store.connection
    assert registry.get_catalog_components(prior.sessions_root, first.workspace_id) is prior
    with pytest.raises(RuntimeError, match="必须绑定显式生命周期 owner"):
        Context().run(get_session_path_resolver, prior.sessions_root)
