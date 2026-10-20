from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.identifier import create_uuid_hex
from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import (
    SessionCatalogStore,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_creation import SessionCreationService
from app.core.session_lifecycle_gate import NavigationTopologyGate
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.schemas.internal_v2.session import SessionDTO
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from app.services.mapping.itemized.thread_seed import frozen_thread_seed
from app.services.orchestration.initial_execution_binding_worker import (
    InitialExecutionBindingWorker,
    InitialExecutionBindOutcome,
)
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)
from app.services.orchestration.session_subagent_service import (
    SessionSubagentService,
    SessionSubagentUnavailableError,
)
from tests.support.graph_binding import TEST_THREAD_GRAPH_BINDING

WORKSPACE_ID = "0197d9a3-7d2a-7c29-8d76-58b3cf3f8a21"


def make_session_id() -> str:
    return f"ses_{create_uuid_hex()}"


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
    record = await creation.prepare(
        idempotency_key="owner-key",
        title="父会话",
        purpose="normal",
        parent_node_id=None,
        graph_binding=TEST_THREAD_GRAPH_BINDING,
        session_metadata={
            "kind": "normal",
            "delegation": None,
            "generation_origin": None,
            "current_agent_id": "default",
            "current_provider_id": "default_provider",
            "context_source_session_id": None,
        },
    )
    result = await creation.publish_prepared(record)
    now = datetime.now(UTC)
    return SessionDTO(
        session_id=result.session_id,
        thread_id=record.main_thread_id,
        workspace_id=WORKSPACE_ID,
        resource_identity=session_resource_identity(WORKSPACE_ID, result.session_id),
        uri=session_resource_uri(WORKSPACE_ID, result.session_id),
        scope="workspace",
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


class _RecordingBinder:
    def __call__(self, target):
        return InitialExecutionBindOutcome(execution_binding_id=target.execution_binding_id, job_id=target.job_id)


async def bind_owner_for_test(factory: OwnerThreadCreationFactory, session_id: str) -> None:
    worker = InitialExecutionBindingWorker(store=factory.for_owner_session(session_id).control_store,
                                           claim_owner="test-binder", binder=_RecordingBinder())
    worker.start()
    try:
        worker.bind_pending_once()
    finally:
        worker.stop()


def make_service(parent_session: SessionDTO, factory: OwnerThreadCreationFactory) -> SessionSubagentService:
    return SessionSubagentService(parent_session_reader=_ParentReader(parent_session), thread_creation_factory=factory,
                                  initial_execution_binder=_RecordingBinder(), bind_owner=lambda sid: bind_owner_for_test(factory, sid))


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
        parent_thread_id=parent_session.thread_id,
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

    gateway_token = set_current_gateway_id("gateway_9c65b6f74b8dc349e3f5b7c61da3e622")
    request_token = set_current_request_id("request-frozen-creation")
    try:
        accepted = await delegate_parent(service, parent_session)
    finally:
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)

    assert accepted.owner_session_id == parent_session.session_id
    assert accepted.child_thread_id.startswith("thr_")
    assert accepted.delegation_id.startswith("del_")
    assert accepted.admission_idempotency_key == accepted.delegation_id
    assert accepted.admission_state == "bound"
    assert accepted.execution_binding_id.startswith("tbind_")
    assert accepted.frozen_job_id.startswith("job_")

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
        assert intent.state == "bound"
        assert intent.execution_binding_id == accepted.execution_binding_id
        assert intent.job_id == accepted.frozen_job_id
        record = control.get_thread_creation_record(accepted.delegation_id)
        assert json.loads(record.task_seed)["gateway_id"] == "gateway_9c65b6f74b8dc349e3f5b7c61da3e622"
        assert json.loads(record.task_seed)["request_id"] == "request-frozen-creation"
        frozen = frozen_thread_seed(record)
        assert frozen.gateway_id == "gateway_9c65b6f74b8dc349e3f5b7c61da3e622"
        assert frozen.request_id == "request-frozen-creation"
        assert "gateway_9c65b6f74b8dc349e3f5b7c61da3e622" not in frozen.message.content
        assert "request-frozen-creation" not in frozen.message.content
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
    record = factory.for_owner_session(parent_session.session_id).control_store.get_thread_creation_record(first.delegation_id)
    assert json.loads(record.task_seed)["request_id"] is None
    assert frozen_thread_seed(record).request_id is None


@pytest.mark.asyncio
async def test_delegate_gateway_identity_is_part_of_creation_preimage(
    parent_session: SessionDTO, factory: OwnerThreadCreationFactory,
) -> None:
    service = make_service(parent_session, factory)
    gateway_token = set_current_gateway_id("gateway_8c772bdb36628078d9ec54d581cc8e77")
    try:
        first = await delegate_parent(service, parent_session)
        assert await delegate_parent(service, parent_session) == first
        changed_token = set_current_gateway_id("gateway_b168e092f12bbc103a1bb5632f45a37c")
        try:
            with pytest.raises(RuntimeError, match="冲突"):
                await delegate_parent(service, parent_session)
        finally:
            reset_current_gateway_id(changed_token)
    finally:
        reset_current_gateway_id(gateway_token)


@pytest.mark.asyncio
async def test_delegate_request_identity_is_part_of_creation_preimage(
    parent_session: SessionDTO, factory: OwnerThreadCreationFactory,
) -> None:
    service = make_service(parent_session, factory)
    request_token = set_current_request_id("request-original")
    try:
        first = await delegate_parent(service, parent_session)
        assert await delegate_parent(service, parent_session) == first
        changed_token = set_current_request_id("request-changed")
        try:
            with pytest.raises(RuntimeError, match="冲突"):
                await delegate_parent(service, parent_session)
        finally:
            reset_current_request_id(changed_token)
    finally:
        reset_current_request_id(request_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["gateway_id", "request_id"])
@pytest.mark.parametrize("replacement", [{}, {"value": ""}, {"value": " "}, {"value": 42}])
async def test_frozen_child_seed_rejects_missing_or_invalid_request_identity(
    parent_session: SessionDTO, factory: OwnerThreadCreationFactory,
    field: str, replacement: dict[str, object],
) -> None:
    accepted = await delegate_parent(make_service(parent_session, factory), parent_session)
    record = factory.for_owner_session(parent_session.session_id).control_store.get_thread_creation_record(accepted.delegation_id)
    seed = json.loads(record.task_seed)
    seed.pop(field)
    if replacement:
        seed[field] = replacement["value"]
    with pytest.raises(ValueError, match=field):
        frozen_thread_seed(replace(record, task_seed=json.dumps(seed)))


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
async def test_delegate_binding_failure_preserves_published_pending_intent(parent_session: SessionDTO, factory: OwnerThreadCreationFactory) -> None:
    async def fail_binding(_session_id: str) -> None:
        raise RuntimeError("真实绑定失败")
    service = SessionSubagentService(parent_session_reader=_ParentReader(parent_session), thread_creation_factory=factory,
                                    initial_execution_binder=_RecordingBinder(), bind_owner=fail_binding)
    with pytest.raises(RuntimeError, match="真实绑定失败"):
        await delegate_parent(service, parent_session)
    control = factory.for_owner_session(parent_session.session_id).control_store
    assert len(control.list_child_thread_rows()) == 1
    assert control.list_initial_execution_intents()[0].state == "pending"


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
        parent_thread_id=parent_session.thread_id,
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
        initial_execution_binder=_RecordingBinder(),
        bind_owner=lambda sid: bind_owner_for_test(factory, sid),
    )
    with pytest.raises(RuntimeError, match="不一致"):
        await service.delegate(
            parent_session_id=parent_session.session_id,
        parent_thread_id=parent_session.thread_id,
            parent_agent_id="default",
            parent_job_id="job_parent",
            parent_tool_call_id="call_task",
            description="做事",
            subagent_type="general-purpose",
        )


@pytest.mark.asyncio
async def test_delegate_without_real_binder_fails_closed_before_side_effects(
    parent_session: SessionDTO,
    factory: OwnerThreadCreationFactory,
    catalog: SessionCatalogStore,
) -> None:
    """生产未装配真实 binder 时必须在任何持久化副作用前 fail closed。

    OpenSpec 主 spec 第 244 行：缺少 binder 时 MUST 可观测地 fail-closed
    报告 ``unavailable``，MUST NOT 静默停留 pending、MUST NOT 返回虚假
    accepted。此处断言：抛出的异常携带确定性 delegation identity，且没有
    创建任何 thread_catalog child row / admission intent。
    """
    service = SessionSubagentService(
        parent_session_reader=_ParentReader(parent_session),
        thread_creation_factory=factory,
        initial_execution_binder=None,
        bind_owner=lambda sid: bind_owner_for_test(factory, sid),
    )

    with pytest.raises(SessionSubagentUnavailableError) as raised:
        await delegate_parent(service, parent_session)

    error = raised.value
    assert error.delegation_id.startswith("del_")
    assert error.parent_session_id == parent_session.session_id
    assert error.subagent_type == "general-purpose"
    assert "unavailable" in str(error)

    # 无持久化副作用：不创建 child row，也不写 admission intent。
    node_count = int(
        catalog.connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    )
    assert node_count == 1
    control = SessionControlStore(
        factory._session_dir_for(parent_session.session_id)
        / "session-control.sqlite"
    )
    try:
        assert control.list_child_thread_rows() == ()
        assert control.list_initial_execution_intents() == ()
    finally:
        control.close()
