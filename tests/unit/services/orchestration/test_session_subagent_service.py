from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import (
    SessionCatalogStore,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_creation import SessionCreationService
from app.core.session_lifecycle_gate import NavigationTopologyGate
from app.schemas.internal_v2.session import SessionDTO
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)
from app.services.orchestration.session_subagent_service import (
    SessionSubagentService,
)

WORKSPACE_ID = "0197d9a3-7d2a-7c29-8d76-58b3cf3f8a21"


def make_session_id() -> str:
    import uuid

    return f"ses_{uuid.uuid4().hex}"


class _ParentReader:
    def __init__(self, parent: SessionDTO) -> None:
        self.parent = parent

    async def get(self, session_id: str) -> SessionDTO:
        assert session_id == self.parent.session_id
        return self.parent


class _MismatchedReader:
    def __init__(self, parent: SessionDTO) -> None:
        self.parent = parent

    async def get(self, session_id: str) -> SessionDTO:
        return self.parent.model_copy(
            update={"current_agent_id": "other-agent"}
        )


@pytest.fixture
def sessions_root(tmp_path: Path) -> Path:
    return tmp_path / ".boxteam" / "sessions"


@pytest.fixture
def catalog(tmp_path: Path, sessions_root: Path) -> SessionCatalogStore:
    store = SessionCatalogStore(
        tmp_path / ".boxteam" / "navigation" / "session-catalog.sqlite",
        sessions_root,
    )
    yield store
    store.close()


@pytest.fixture
async def parent_session(
    catalog: SessionCatalogStore, sessions_root: Path
) -> SessionDTO:
    creation = SessionCreationService(
        store=catalog,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        gate=NavigationTopologyGate(sessions_root),
    )
    result = await creation.create(
        idempotency_key="owner-key",
        title="父会话",
        parent_node_id=None,
        session_metadata={
            "kind": "normal",
            "delegation": None,
            "generation_origin": None,
            "current_agent_id": "default",
            "current_provider_id": "default_provider",
            "context_source_session_id": None,
        },
    )
    now = datetime.now(UTC)
    return SessionDTO(
        session_id=result.session_id,
        workspace_id=WORKSPACE_ID,
        title="父会话",
        current_agent_id="default",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def factory(sessions_root: Path) -> OwnerThreadCreationFactory:
    resolver = get_session_path_resolver(sessions_root)
    assert isinstance(resolver, SessionCatalogPathResolver)
    return OwnerThreadCreationFactory(
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        path_resolver=resolver,
    )


def make_service(
    parent_session: SessionDTO,
    factory: OwnerThreadCreationFactory,
) -> SessionSubagentService:
    return SessionSubagentService(
        parent_session_reader=_ParentReader(parent_session),
        thread_creation_factory=factory,
    )


async def delegate_parent(
    service: SessionSubagentService,
    parent_session: SessionDTO,
    *,
    tool_call_id: str = "call_task",
    description: str = "检查认证模块，并把结论发回父会话。",
    title: str | None = "认证审查员",
    trusted_context: dict[str, object] | None = None,
) -> object:
    return await service.delegate(
        parent_session_id=parent_session.session_id,
        parent_agent_id="default",
        parent_job_id="job_parent",
        parent_tool_call_id=tool_call_id,
        description=description,
        subagent_type="general-purpose",
        title=title,
        trusted_context=trusted_context,
    )


@pytest.mark.asyncio
async def test_delegate_creates_durable_child_thread_with_pending_intent(
    parent_session: SessionDTO,
    factory: OwnerThreadCreationFactory,
    catalog: SessionCatalogStore,
) -> None:
    service = make_service(parent_session, factory)

    accepted = await delegate_parent(service, parent_session)

    assert accepted.owner_session_id == parent_session.session_id
    assert accepted.child_thread_id.startswith("thr_")
    assert accepted.delegation_id.startswith("del_")
    assert accepted.admission_idempotency_key == accepted.delegation_id
    assert accepted.admission_state == "pending"
    assert accepted.execution_binding_id.startswith("tbind_")
    assert accepted.frozen_job_id.startswith("job_")

    # 唯一可见性：workspace catalog 节点数不变（没有第二个 Session）。
    # 唯一可见性：workspace catalog 节点数不变（没有第二个 Session）。
    node_count = int(
        catalog.connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    )
    assert node_count == 1

    # owner control store：child row + published member + pending intent。
    control = SessionControlStore(
        factory._session_dir_for(parent_session.session_id)
        / "session-control.sqlite"
    )
    try:
        rows = control.list_child_thread_rows()
        assert [row.thread_id for row in rows] == [accepted.child_thread_id]
        member = control.get_collaboration_member(accepted.delegation_id)
        assert member.state == "published"
        assert member.child_thread_id == accepted.child_thread_id
        assert member.role == "delegated_subagent"
        assert member.subagent_type == "general-purpose"
        intent = control.get_initial_execution_intent(
            accepted.admission_idempotency_key
        )
        assert intent.state == "pending"
        assert intent.execution_binding_id == accepted.execution_binding_id
        assert intent.job_id == accepted.frozen_job_id
        # child thread 只有一条初始 intent。
        assert len(control.list_initial_execution_intents()) == 1
    finally:
        control.close()


@pytest.mark.asyncio
async def test_delegate_same_tool_call_retry_converges(
    parent_session: SessionDTO,
    factory: OwnerThreadCreationFactory,
) -> None:
    service = make_service(parent_session, factory)

    first = await delegate_parent(service, parent_session)
    again = await delegate_parent(service, parent_session)

    assert again == first


@pytest.mark.asyncio
async def test_delegate_same_identity_different_preimage_conflicts(
    parent_session: SessionDTO,
    factory: OwnerThreadCreationFactory,
) -> None:
    service = make_service(parent_session, factory)

    await delegate_parent(service, parent_session)
    with pytest.raises(RuntimeError, match="冲突"):
        await delegate_parent(
            service,
            parent_session,
            description="同 tool call 不同 preimage 的委派内容。",
        )


@pytest.mark.asyncio
async def test_delegate_different_tool_call_creates_sibling_thread(
    parent_session: SessionDTO,
    factory: OwnerThreadCreationFactory,
) -> None:
    service = make_service(parent_session, factory)

    first = await delegate_parent(
        service, parent_session, tool_call_id="call_task_1"
    )
    second = await delegate_parent(
        service, parent_session, tool_call_id="call_task_2"
    )

    assert first.child_thread_id != second.child_thread_id
    assert first.delegation_id != second.delegation_id


@pytest.mark.asyncio
async def test_delegate_before_start_failure_exposes_thread_id(
    parent_session: SessionDTO,
    factory: OwnerThreadCreationFactory,
) -> None:
    async def failing_before_start(accepted) -> None:
        raise RuntimeError("启动前准备失败")

    service = SessionSubagentService(
        parent_session_reader=_ParentReader(parent_session),
        thread_creation_factory=factory,
    )
    with pytest.raises(RuntimeError, match="child_thread_id="):
        await service.delegate(
            parent_session_id=parent_session.session_id,
            parent_agent_id="default",
            parent_job_id="job_parent",
            parent_tool_call_id="call_task",
            description="做事",
            subagent_type="general-purpose",
            before_start=failing_before_start,
        )


@pytest.mark.asyncio
async def test_delegate_rejects_unknown_subagent_type_before_creation(
    parent_session: SessionDTO,
    factory: OwnerThreadCreationFactory,
    catalog: SessionCatalogStore,
) -> None:
    service = make_service(parent_session, factory)

    with pytest.raises(ValueError, match="当前仅支持 general-purpose"):
        await service.delegate(
            parent_session_id=parent_session.session_id,
            parent_agent_id="default",
            parent_job_id="job_parent",
            parent_tool_call_id="call_task",
            description="做事",
            subagent_type="unknown",
        )
    node_count = int(
        catalog.connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    )
    assert node_count == 1


@pytest.mark.asyncio
async def test_delegate_rejects_agent_mismatch(
    parent_session: SessionDTO,
    factory: OwnerThreadCreationFactory,
) -> None:
    service = SessionSubagentService(
        parent_session_reader=_MismatchedReader(parent_session),
        thread_creation_factory=factory,
    )
    with pytest.raises(RuntimeError, match="不一致"):
        await service.delegate(
            parent_session_id=parent_session.session_id,
            parent_agent_id="default",
            parent_job_id="job_parent",
            parent_tool_call_id="call_task",
            description="做事",
            subagent_type="general-purpose",
        )
