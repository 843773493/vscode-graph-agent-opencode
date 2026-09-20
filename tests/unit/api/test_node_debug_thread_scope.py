"""Node 调试 API 的 Session/Thread 语义单元测试。

覆盖 Node 调试 API 的显式 SessionThread owner 契约：
- main thread 必须显式传入 ``thread_id="main"``；
- 显式 thread_id 定位 child thread；
- API mutation 经 NodeDebugService 的 Session 生命周期准入，Session 缺失或
  已删除时返回 404，不会落到磁盘上凭空创建调试数据。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
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
from app.core.session_paths import SessionPathResolver
from app.schemas.internal_v2.node_debug import (
    NodeDebugActionRequest,
    NodeDebugConfigurationCreateRequest,
    NodeDebugStartRequest,
    NodeDebugStateDTO,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug_service import NodeDebugService
from app.services.infrastructure.node_debug_session_admission import (
    NodeDebugSessionAdmission,
)
from app.services.infrastructure.node_debug_session_store import NodeDebugSessionStore

_SESSION_ID = "ses_api_debug"
_CHILD_THREAD_ID = "ses_api_debug_child"


def _create_session(
    resolver: SessionPathResolver,
    session_id: str,
    *,
    parent_session_id: str | None = None,
) -> Path:
    title = f"测试会话 {session_id}"
    session_dir = resolver.allocate_session_dir(
        session_id=session_id,
        title=title,
        parent_node_id=parent_session_id,
    )
    now = datetime.now(UTC).isoformat()
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "title": title,
                "parent_session_id": parent_session_id,
                "created_at": now,
                "updated_at": now,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    resolver.register_session(session_id, session_dir)
    return session_dir


class _ResolverSessionLifecycle:
    """以权威目录索引模拟 SessionService.get 的“存在且未删除”语义。"""

    def __init__(self, resolver: SessionPathResolver) -> None:
        self._resolver = resolver

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
            thread_id="main",
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
        thread_id="main",
        _="local-token",
        request_id="req_state_main",
        node_debug_service=node_debug_service,
    )

    assert response.request_id == "req_state_main"
    node_debug_service.get_state.assert_awaited_once_with(_SESSION_ID, "main")

    listed = await list_node_debug_configurations(
        session_id=_SESSION_ID,
        thread_id="main",
        _="local-token",
        request_id="req_configurations_main",
        node_debug_service=node_debug_service,
    )
    assert listed.data == []
    node_debug_service.list_configurations.assert_called_once_with(
        _SESSION_ID, "main"
    )


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
    node_debug_service.get_state.assert_awaited_once_with(
        _SESSION_ID, _CHILD_THREAD_ID
    )

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
    resolver = SessionPathResolver(tmp_path / ".boxteam" / "sessions")
    resolver.initialize()
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
            thread_id="main",
            name="API 准入方案",
            script_path="entry.mjs",
        ),
        _="local-token",
        request_id="req_create_admitted",
        node_debug_service=service,
    )
    assert admitted.data is not None
    assert admitted.data.active_configuration_name == "API 准入方案"
    assert (resolver.resolve_session_node(_SESSION_ID) / "debug" / "node").is_dir()

    with pytest.raises(HTTPException) as missing:
        await create_node_debug_configuration(
            payload=NodeDebugConfigurationCreateRequest(
                session_id="ses_api_missing",
                thread_id="main",
                name="缺失方案",
                script_path="entry.mjs",
            ),
            _="local-token",
            request_id="req_create_missing",
            node_debug_service=service,
        )
    assert missing.value.status_code == 404

    await resolver.delete_session_subtree(_SESSION_ID)
    with pytest.raises(HTTPException) as deleted:
        await create_node_debug_configuration(
            payload=NodeDebugConfigurationCreateRequest(
                session_id=_SESSION_ID,
                thread_id="main",
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
            thread_id="main",
            _="local-token",
            request_id="req_state_deleted",
            node_debug_service=service,
        )
    assert deleted_state.value.status_code == 404

    with pytest.raises(HTTPException) as deleted_action:
        await apply_node_debug_action(
            payload=NodeDebugActionRequest(
                session_id=_SESSION_ID,
                thread_id="main",
                action="set_breakpoint",
                params={"path": "entry.mjs", "line": 1},
            ),
            _="local-token",
            request_id="req_action_deleted",
            node_debug_service=service,
        )
    assert deleted_action.value.status_code == 404


@pytest.mark.asyncio
async def test_child_thread_address_folds_to_child_session_main_owner(
    tmp_path: Path,
) -> None:
    """API 两个地址（parent+child / 裸 child）命中同一 owner，返回一致状态。"""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "child.mjs").write_text(
        "console.log('child');\n", encoding="utf-8"
    )
    resolver = SessionPathResolver(tmp_path / ".boxteam" / "sessions")
    resolver.initialize()
    _create_session(resolver, _SESSION_ID)
    _create_session(resolver, _CHILD_THREAD_ID, parent_session_id=_SESSION_ID)
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

    created = await create_node_debug_configuration(
        payload=NodeDebugConfigurationCreateRequest(
            session_id=_SESSION_ID,
            thread_id=_CHILD_THREAD_ID,
            name="子线程方案",
            script_path="child.mjs",
        ),
        _="local-token",
        request_id="req_create_child_thread",
        node_debug_service=service,
    )
    assert created.data is not None
    assert created.data.session_id == _CHILD_THREAD_ID
    assert created.data.thread_id == "main"

    via_child_thread = await get_node_debug_state(
        session_id=_SESSION_ID,
        thread_id=_CHILD_THREAD_ID,
        _="local-token",
        request_id="req_state_via_parent",
        node_debug_service=service,
    )
    via_child_session = await get_node_debug_state(
        session_id=_CHILD_THREAD_ID,
        thread_id="main",
        _="local-token",
        request_id="req_state_via_child",
        node_debug_service=service,
    )
    assert via_child_thread.data == via_child_session.data
    assert via_child_session.data is not None
    assert via_child_session.data.active_configuration_id == (
        created.data.active_configuration_id
    )
    assert via_child_session.data.active_configuration_name == "子线程方案"
