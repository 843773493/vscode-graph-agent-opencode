"""Node 调试 API 的 Session/Thread 语义单元测试。

覆盖 Node 调试 API 的显式 SessionThread owner 契约：
- Session-only 入口经 catalog 解析并返回 main thread canonical ID；
- 显式 thread_id 定位 child thread；
- API mutation 经 NodeDebugService 的 Session 生命周期准入，Session 缺失或
  已删除时返回 404，不会落到磁盘上凭空创建调试数据。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.node_debug import (
    apply_node_debug_action,
    create_node_debug_configuration,
    get_node_debug_state,
    list_node_debug_configurations,
    start_node_debug,
)
from app.core.exceptions import NotFoundError
from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_lifecycle_gate import (
    NavigationTopologyGate,
    SessionDeletionPendingError,
    SessionLifecycleGate,
)
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationCreateRequest,
    NodeDebugSetBreakpointActionRequest,
    NodeDebugSetBreakpointParams,
    NodeDebugStartRequest,
    NodeDebugStateDTO,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.node_debug.session.session_admission import (
    NodeDebugSessionAdmission,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.support.catalog_session_bundle import (
    fixture_main_thread_id,
    seed_catalog_session_bundle,
)

_SESSION_ID = "ses_00000000400070008000000000000001"
_MAIN_THREAD_ID = fixture_main_thread_id(_SESSION_ID)
_CHILD_THREAD_ID = "thr_00000000700070008000000000000002"
_CHILD_SESSION_ID = "ses_00000000400070008000000000000002"
_MISSING_SESSION_ID = "ses_00000000400070008000000000000003"


def _create_session(
    resolver: SessionCatalogPathResolver,
    session_id: str,
    *,
    parent_session_id: str | None = None,
) -> Path:
    return seed_catalog_session_bundle(
        resolver.sessions_root,
        session_id,
        parent_node_id=parent_session_id,
        main_thread_id=fixture_main_thread_id(session_id),
    ).directory


class _ResolverSessionLifecycle:
    """以权威目录索引和正式 gate 模拟 Session 写准入。"""

    def __init__(self, resolver: SessionCatalogPathResolver) -> None:
        self._resolver = resolver
        self._topology_gate = NavigationTopologyGate(resolver.sessions_root)
        self._lifecycle_gate = SessionLifecycleGate(resolver.sessions_root)

    @asynccontextmanager
    async def session_write_admission(self, session_id: str) -> AsyncIterator[None]:
        async with AsyncExitStack() as session_gate:
            async with self._topology_gate.shared():
                await session_gate.enter_async_context(
                    self._lifecycle_gate.exclusive(session_id)
                )
                try:
                    node = self._resolver.get_node(session_id)
                except KeyError as error:
                    raise NotFoundError(f"Session {session_id} not found") from error
                if node.kind != "session":
                    raise NotFoundError(f"Session {session_id} not found")
                if node.state != "active":
                    raise SessionDeletionPendingError(
                        "session_deletion_pending: catalog owner 正在删除，"
                        f"拒绝新业务准入: session_id={session_id!r}, state={node.state!r}"
                    )
            yield

    async def get(self, session_id: str) -> object:
        try:
            return self._resolver.resolve_session_node_for_runtime(session_id)
        except KeyError as error:
            raise NotFoundError(f"Session {session_id} not found") from error


@pytest.fixture
def node_debug_service() -> MagicMock:
    service = MagicMock(spec=NodeDebugService)
    service.get_state = AsyncMock(
        side_effect=lambda session_id, thread_id: NodeDebugStateDTO(
            session_id=session_id,
            thread_id=thread_id,
            status="idle",
        )
    )
    service.list_configurations = MagicMock(return_value=[])
    service.start = AsyncMock(
        return_value=NodeDebugStateDTO(
            session_id=_SESSION_ID,
            thread_id=_CHILD_THREAD_ID,
            status="running",
        )
    )
    service.create_configuration = AsyncMock(
        return_value=NodeDebugStateDTO(
            session_id=_SESSION_ID,
            thread_id=_MAIN_THREAD_ID,
            status="idle",
        )
    )
    return service


@pytest.mark.asyncio
async def test_explicit_main_thread_reads_main_owner(
    node_debug_service: MagicMock,
) -> None:
    response = await get_node_debug_state(
        session_id=_SESSION_ID,
        thread_id=_MAIN_THREAD_ID,
        _="local-token",
        request_id="req_state_main",
        node_debug_service=node_debug_service,
    )

    assert response.request_id == "req_state_main"
    node_debug_service.get_state.assert_awaited_once_with(_SESSION_ID, _MAIN_THREAD_ID)

    listed = await list_node_debug_configurations(
        session_id=_SESSION_ID,
        thread_id=_MAIN_THREAD_ID,
        _="local-token",
        request_id="req_configurations_main",
        node_debug_service=node_debug_service,
    )
    assert listed.data == []
    node_debug_service.list_configurations.assert_called_once_with(
        _SESSION_ID, _MAIN_THREAD_ID
    )


@pytest.mark.asyncio
async def test_session_only_api_resolves_catalog_main_thread(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "entry.mjs").write_text(
        "console.log('entry');\n", encoding="utf-8"
    )
    resolver = get_session_path_resolver(tmp_path / ".boxteam" / "sessions")
    _create_session(resolver, _SESSION_ID)
    service = NodeDebugService(
        workspace_root=workspace_root,
        config_service=ConfigService(workspace_root=workspace_root),
        session_store=NodeDebugSessionStore(resolver),
        session_admission=NodeDebugSessionAdmission(
            session_service=_ResolverSessionLifecycle(resolver),
            path_resolver=resolver,
        ),
        external_resource_leases=ExternalResourceLeaseLedger(),
    )

    state = await get_node_debug_state(
        session_id=_SESSION_ID,
        _="local-token",
        request_id="req_state_session_only",
        node_debug_service=service,
    )
    assert state.data is not None
    assert state.data.thread_id == _MAIN_THREAD_ID

    created = await create_node_debug_configuration(
        payload=NodeDebugConfigurationCreateRequest(
            session_id=_SESSION_ID,
            name="Session-only 方案",
            script_path="entry.mjs",
        ),
        _="local-token",
        request_id="req_create_session_only",
        node_debug_service=service,
    )
    assert created.data is not None
    assert created.data.thread_id == _MAIN_THREAD_ID
    assert created.data.active_configuration_name == "Session-only 方案"
    assert resolver.resolve_thread_node(_SESSION_ID, _MAIN_THREAD_ID).is_dir()


@pytest.mark.asyncio
async def test_explicit_thread_parameter_locates_child_thread(
    node_debug_service: MagicMock,
) -> None:
    state = await get_node_debug_state(
        session_id=_SESSION_ID,
        thread_id=_CHILD_THREAD_ID,
        _="local-token",
        request_id="req_state_child",
        node_debug_service=node_debug_service,
    )
    assert state.data is not None
    assert state.data.thread_id == _CHILD_THREAD_ID
    node_debug_service.get_state.assert_awaited_once_with(_SESSION_ID, _CHILD_THREAD_ID)

    payload = NodeDebugStartRequest(
        session_id=_SESSION_ID,
        thread_id=_CHILD_THREAD_ID,
        path="child.mjs",
    )
    await start_node_debug(
        payload=payload,
        _="local-token",
        request_id="req_start_child",
        node_debug_service=node_debug_service,
    )
    forwarded: dict[str, Any] = node_debug_service.start.await_args.kwargs
    assert forwarded["session_id"] == _SESSION_ID
    assert forwarded["thread_id"] == _CHILD_THREAD_ID


@pytest.mark.asyncio
async def test_mutation_requires_existing_session(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "entry.mjs").write_text(
        "console.log('entry');\n", encoding="utf-8"
    )
    resolver = get_session_path_resolver(tmp_path / ".boxteam" / "sessions")
    _create_session(resolver, _SESSION_ID)
    service = NodeDebugService(
        workspace_root=workspace_root,
        config_service=ConfigService(workspace_root=workspace_root),
        session_store=NodeDebugSessionStore(resolver),
        session_admission=NodeDebugSessionAdmission(
            session_service=_ResolverSessionLifecycle(resolver),
            path_resolver=resolver,
        ),
        external_resource_leases=ExternalResourceLeaseLedger(),
    )

    admitted = await create_node_debug_configuration(
        payload=NodeDebugConfigurationCreateRequest(
            session_id=_SESSION_ID,
            thread_id=_MAIN_THREAD_ID,
            name="API 准入方案",
            script_path="entry.mjs",
        ),
        _="local-token",
        request_id="req_create_admitted",
        node_debug_service=service,
    )
    assert admitted.data is not None
    assert admitted.data.active_configuration_name == "API 准入方案"
    main_thread_node = resolver.resolve_thread_node(
        _SESSION_ID, resolver.main_thread_id(_SESSION_ID)
    )
    assert (main_thread_node / "debug" / "node").is_dir()

    with pytest.raises(HTTPException) as missing:
        await create_node_debug_configuration(
            payload=NodeDebugConfigurationCreateRequest(
                session_id=_MISSING_SESSION_ID,
                thread_id=_MAIN_THREAD_ID,
                name="缺失方案",
                script_path="entry.mjs",
            ),
            _="local-token",
            request_id="req_create_missing",
            node_debug_service=service,
        )
    assert missing.value.status_code == 404

    await resolver.delete_subtree(idempotency_key="node-debug-scope-delete", root_node_id=_SESSION_ID)
    with pytest.raises(HTTPException) as deleted:
        await create_node_debug_configuration(
            payload=NodeDebugConfigurationCreateRequest(
                session_id=_SESSION_ID,
                thread_id=_MAIN_THREAD_ID,
                name="已删除方案",
                script_path="entry.mjs",
            ),
            _="local-token",
            request_id="req_create_deleted",
            node_debug_service=service,
        )
    assert deleted.value.status_code == 404

    with pytest.raises(HTTPException) as deleted_state:
        await get_node_debug_state(
            session_id=_SESSION_ID,
            thread_id=_MAIN_THREAD_ID,
            _="local-token",
            request_id="req_state_deleted",
            node_debug_service=service,
        )
    assert deleted_state.value.status_code == 404

    with pytest.raises(HTTPException) as deleted_action:
        await apply_node_debug_action(
            payload=NodeDebugSetBreakpointActionRequest(
                session_id=_SESSION_ID,
                thread_id=_MAIN_THREAD_ID,
                action="set_breakpoint",
                params=NodeDebugSetBreakpointParams(path="entry.mjs", line=1),
            ),
            _="local-token",
            request_id="req_action_deleted",
            node_debug_service=service,
        )
    assert deleted_action.value.status_code == 404


@pytest.mark.asyncio
async def test_session_only_addresses_resolve_distinct_session_owners(
    tmp_path: Path,
) -> None:
    """父、子 Session 的 Session-only 地址各自解析 catalog main owner。"""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "child.mjs").write_text(
        "console.log('child');\n", encoding="utf-8"
    )
    resolver = get_session_path_resolver(tmp_path / ".boxteam" / "sessions")
    _create_session(resolver, _SESSION_ID)
    _create_session(resolver, _CHILD_SESSION_ID, parent_session_id=_SESSION_ID)
    service = NodeDebugService(
        workspace_root=workspace_root,
        config_service=ConfigService(workspace_root=workspace_root),
        session_store=NodeDebugSessionStore(resolver),
        session_admission=NodeDebugSessionAdmission(
            session_service=_ResolverSessionLifecycle(resolver),
            path_resolver=resolver,
        ),
        external_resource_leases=ExternalResourceLeaseLedger(),
    )

    parent_created = await create_node_debug_configuration(
        payload=NodeDebugConfigurationCreateRequest(
            session_id=_SESSION_ID,
            name="父 Session 方案",
            script_path="child.mjs",
        ),
        _="local-token",
        request_id="req_create_parent_session",
        node_debug_service=service,
    )
    child_thread_id = resolver.main_thread_id(_CHILD_SESSION_ID)
    child_created = await create_node_debug_configuration(
        payload=NodeDebugConfigurationCreateRequest(
            session_id=_CHILD_SESSION_ID,
            name="子 Session 方案",
            script_path="child.mjs",
        ),
        _="local-token",
        request_id="req_create_child_session",
        node_debug_service=service,
    )
    assert parent_created.data is not None
    assert child_created.data is not None
    assert parent_created.data.session_id == _SESSION_ID
    assert parent_created.data.thread_id == _MAIN_THREAD_ID
    assert child_created.data.session_id == _CHILD_SESSION_ID
    assert child_created.data.thread_id == child_thread_id
    assert child_thread_id != _MAIN_THREAD_ID

    parent_state = await get_node_debug_state(
        session_id=_SESSION_ID,
        _="local-token",
        request_id="req_state_parent_session",
        node_debug_service=service,
    )
    child_state = await get_node_debug_state(
        session_id=_CHILD_SESSION_ID,
        _="local-token",
        request_id="req_state_child_session",
        node_debug_service=service,
    )
    assert parent_state.data is not None
    assert child_state.data is not None
    assert parent_state.data.thread_id == _MAIN_THREAD_ID
    assert child_state.data.thread_id == child_thread_id
    assert child_state.data.active_configuration_name == "子 Session 方案"
