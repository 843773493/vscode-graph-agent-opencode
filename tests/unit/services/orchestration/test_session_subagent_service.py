from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import (
    SessionCatalogStore,
)
from app.core.session_creation import SessionCreationService
from app.core.session_lifecycle_gate import NavigationTopologyGate
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.session import SessionDTO
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from app.services.mapping.itemized.thread_seed import frozen_thread_seed
from app.services.orchestration.initial_execution_binding_worker import (
    InitialExecutionBindingTarget,
    InitialExecutionBindingWorker,
    InitialExecutionBindOutcome,
)
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)
from app.services.orchestration.session_subagent_service import (
    SessionSubagentAccepted,
    SessionSubagentService,
    SessionSubagentUnavailableError,
)
from tests.support.graph_binding import TEST_THREAD_GRAPH_BINDING


class _ParentSessionReader:
    def __init__(self, parent_session: SessionDTO) -> None:
        self.parent_session = parent_session

    async def get(self, session_id: str) -> SessionDTO:
        assert session_id == self.parent_session.session_id
        return self.parent_session


class _MismatchedParentSessionReader:
    def __init__(self, parent_session: SessionDTO) -> None:
        self.parent_session = parent_session

    async def get(self, session_id: str) -> SessionDTO:
        assert session_id == self.parent_session.session_id
        return self.parent_session.model_copy(
            update={"current_agent_id": "other-agent"}
        )


@pytest.fixture
def workspace_id(tmp_path: Path) -> str:
    return load_or_create_workspace_id(tmp_path)


@pytest.fixture
def sessions_root(tmp_path: Path) -> Path:
    return tmp_path / ".boxteam" / "sessions"


@pytest.fixture
def session_catalog_store(
    tmp_path: Path,
    sessions_root: Path,
) -> SessionCatalogStore:
    store = SessionCatalogStore(
        tmp_path / ".boxteam" / "navigation" / "session-catalog.sqlite",
        sessions_root,
    )
    yield store
    store.close()


@pytest.fixture
async def parent_session(
    session_catalog_store: SessionCatalogStore,
    sessions_root: Path,
    workspace_id: str,
) -> SessionDTO:
    creation = SessionCreationService(
        store=session_catalog_store,
        sessions_root=sessions_root,
        workspace_id=workspace_id,
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
    created_session = await creation.publish_prepared(record)
    created_at = datetime.now(UTC)
    return SessionDTO(
        session_id=created_session.session_id,
        thread_id=record.main_thread_id,
        workspace_id=workspace_id,
        resource_identity=session_resource_identity(
            workspace_id, created_session.session_id
        ),
        uri=session_resource_uri(workspace_id, created_session.session_id),
        scope="workspace",
        title="父会话",
        current_agent_id="default",
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.fixture
def owner_thread_creation_factory(sessions_root: Path, workspace_id: str) -> OwnerThreadCreationFactory:
    resolver = get_session_path_resolver(sessions_root, workspace_id=workspace_id)
    assert isinstance(resolver, SessionCatalogPathResolver)
    return OwnerThreadCreationFactory(
        sessions_root=sessions_root,
        workspace_id=workspace_id,
        path_resolver=resolver,
    )


class _RecordingBinder:
    def __call__(
        self, target: InitialExecutionBindingTarget
    ) -> InitialExecutionBindOutcome:
        return InitialExecutionBindOutcome(
            execution_binding_id=target.execution_binding_id, job_id=target.job_id
        )


async def _bind_initial_execution_for_test(
    owner_thread_creation_factory: OwnerThreadCreationFactory, session_id: str
) -> None:
    worker = InitialExecutionBindingWorker(
        store=owner_thread_creation_factory.for_owner_session(session_id).control_store,
        claim_owner="test-binder",
        binder=_RecordingBinder(),
    )
    worker.start()
    try:
        worker.bind_pending_once()
    finally:
        worker.stop()


@pytest.fixture
def session_subagent_service(
    parent_session: SessionDTO,
    owner_thread_creation_factory: OwnerThreadCreationFactory,
) -> SessionSubagentService:
    return SessionSubagentService(
        parent_session_reader=_ParentSessionReader(parent_session),
        thread_creation_factory=owner_thread_creation_factory,
        initial_execution_binder=_RecordingBinder(),
        bind_owner=lambda session_id: _bind_initial_execution_for_test(
            owner_thread_creation_factory, session_id
        ),
    )


async def _delegate_from_parent_session(
    session_subagent_service: SessionSubagentService,
    parent_session: SessionDTO,
    *,
    parent_tool_call_id: str = "call_task",
    description: str = "检查认证模块，并把结论发回父会话。",
    title: str | None = "认证审查员",
    trusted_context: dict[str, object] | None = None,
) -> SessionSubagentAccepted:
    return await session_subagent_service.delegate(
        parent_session_id=parent_session.session_id,
        parent_thread_id=parent_session.thread_id,
        parent_agent_id="default",
        parent_job_id="job_parent",
        parent_tool_call_id=parent_tool_call_id,
        description=description,
        subagent_type="general-purpose",
        title=title,
        trusted_context=trusted_context,
    )


@pytest.mark.asyncio
async def test_delegate_creates_durable_child_thread_with_bound_intent(
    parent_session: SessionDTO,
    session_subagent_service: SessionSubagentService,
    owner_thread_creation_factory: OwnerThreadCreationFactory,
    session_catalog_store: SessionCatalogStore,
) -> None:
    gateway_token = set_current_gateway_id("gateway_9c65b6f74b8dc349e3f5b7c61da3e622")
    request_token = set_current_request_id("request-frozen-creation")
    try:
        accepted = await _delegate_from_parent_session(
            session_subagent_service, parent_session
        )
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
        session_catalog_store.connection.execute(
            "SELECT COUNT(*) FROM nodes"
        ).fetchone()[0]
    )
    assert node_count == 1

    # owner control store：child row + published member + bound intent。
    control = owner_thread_creation_factory.for_owner_session(
        parent_session.session_id
    ).control_store
    rows = control.list_child_thread_rows()
    assert [row.thread_id for row in rows] == [accepted.child_thread_id]
    member = control.get_collaboration_member(accepted.delegation_id)
    assert member.state == "published"
    assert member.child_thread_id == accepted.child_thread_id
    assert member.role == "delegated_subagent"
    assert member.subagent_type == "general-purpose"
    intent = control.get_initial_execution_intent(accepted.admission_idempotency_key)
    assert intent.state == "bound"
    assert intent.execution_binding_id == accepted.execution_binding_id
    assert intent.job_id == accepted.frozen_job_id
    record = control.get_thread_creation_record(accepted.delegation_id)
    assert (
        json.loads(record.task_seed)["gateway_id"]
        == "gateway_9c65b6f74b8dc349e3f5b7c61da3e622"
    )
    assert json.loads(record.task_seed)["request_id"] == "request-frozen-creation"
    frozen = frozen_thread_seed(record)
    assert frozen.gateway_id == "gateway_9c65b6f74b8dc349e3f5b7c61da3e622"
    assert frozen.request_id == "request-frozen-creation"
    assert "gateway_9c65b6f74b8dc349e3f5b7c61da3e622" not in frozen.message.content
    assert "request-frozen-creation" not in frozen.message.content
    # child thread 只有一条初始 intent。
    assert len(control.list_initial_execution_intents()) == 1


@pytest.mark.asyncio
async def test_delegate_same_tool_call_retry_converges(
    parent_session: SessionDTO,
    session_subagent_service: SessionSubagentService,
    owner_thread_creation_factory: OwnerThreadCreationFactory,
) -> None:
    first = await _delegate_from_parent_session(
        session_subagent_service, parent_session
    )
    again = await _delegate_from_parent_session(
        session_subagent_service, parent_session
    )

    assert again == first
    record = owner_thread_creation_factory.for_owner_session(
        parent_session.session_id
    ).control_store.get_thread_creation_record(first.delegation_id)
    assert json.loads(record.task_seed)["request_id"] is None
    assert frozen_thread_seed(record).request_id is None


@pytest.mark.asyncio
async def test_delegate_gateway_identity_is_part_of_creation_preimage(
    parent_session: SessionDTO,
    session_subagent_service: SessionSubagentService,
) -> None:
    gateway_token = set_current_gateway_id("gateway_8c772bdb36628078d9ec54d581cc8e77")
    try:
        first = await _delegate_from_parent_session(
            session_subagent_service, parent_session
        )
        assert (
            await _delegate_from_parent_session(
                session_subagent_service, parent_session
            )
            == first
        )
        changed_token = set_current_gateway_id(
            "gateway_b168e092f12bbc103a1bb5632f45a37c"
        )
        try:
            with pytest.raises(RuntimeError, match="冲突"):
                await _delegate_from_parent_session(
                    session_subagent_service, parent_session
                )
        finally:
            reset_current_gateway_id(changed_token)
    finally:
        reset_current_gateway_id(gateway_token)


@pytest.mark.asyncio
async def test_delegate_request_identity_is_part_of_creation_preimage(
    parent_session: SessionDTO,
    session_subagent_service: SessionSubagentService,
) -> None:
    request_token = set_current_request_id("request-original")
    try:
        first = await _delegate_from_parent_session(
            session_subagent_service, parent_session
        )
        assert (
            await _delegate_from_parent_session(
                session_subagent_service, parent_session
            )
            == first
        )
        changed_token = set_current_request_id("request-changed")
        try:
            with pytest.raises(RuntimeError, match="冲突"):
                await _delegate_from_parent_session(
                    session_subagent_service, parent_session
                )
        finally:
            reset_current_request_id(changed_token)
    finally:
        reset_current_request_id(request_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_field", ["gateway_id", "request_id"])
@pytest.mark.parametrize(
    "identity_override", [{}, {"value": ""}, {"value": " "}, {"value": 42}]
)
async def test_frozen_child_seed_rejects_missing_or_invalid_request_identity(
    parent_session: SessionDTO,
    session_subagent_service: SessionSubagentService,
    owner_thread_creation_factory: OwnerThreadCreationFactory,
    identity_field: str,
    identity_override: dict[str, object],
) -> None:
    accepted = await _delegate_from_parent_session(
        session_subagent_service, parent_session
    )
    record = owner_thread_creation_factory.for_owner_session(
        parent_session.session_id
    ).control_store.get_thread_creation_record(accepted.delegation_id)
    task_seed = json.loads(record.task_seed)
    task_seed.pop(identity_field)
    if identity_override:
        task_seed[identity_field] = identity_override["value"]
    with pytest.raises(ValueError, match=identity_field):
        frozen_thread_seed(replace(record, task_seed=json.dumps(task_seed)))


@pytest.mark.asyncio
async def test_delegate_same_identity_different_preimage_conflicts(
    parent_session: SessionDTO,
    session_subagent_service: SessionSubagentService,
) -> None:
    await _delegate_from_parent_session(session_subagent_service, parent_session)
    with pytest.raises(RuntimeError, match="冲突"):
        await _delegate_from_parent_session(
            session_subagent_service,
            parent_session,
            description="同 tool call 不同 preimage 的委派内容。",
        )


@pytest.mark.asyncio
async def test_delegate_different_tool_call_creates_sibling_thread(
    parent_session: SessionDTO,
    session_subagent_service: SessionSubagentService,
) -> None:
    first = await _delegate_from_parent_session(
        session_subagent_service, parent_session, parent_tool_call_id="call_task_1"
    )
    second = await _delegate_from_parent_session(
        session_subagent_service, parent_session, parent_tool_call_id="call_task_2"
    )

    assert first.child_thread_id != second.child_thread_id
    assert first.delegation_id != second.delegation_id


@pytest.mark.asyncio
async def test_delegate_binding_failure_preserves_published_pending_intent(
    parent_session: SessionDTO,
    owner_thread_creation_factory: OwnerThreadCreationFactory,
) -> None:
    async def fail_binding(_session_id: str) -> None:
        raise RuntimeError("真实绑定失败")

    service = SessionSubagentService(
        parent_session_reader=_ParentSessionReader(parent_session),
        thread_creation_factory=owner_thread_creation_factory,
        initial_execution_binder=_RecordingBinder(),
        bind_owner=fail_binding,
    )
    with pytest.raises(RuntimeError, match="真实绑定失败"):
        await _delegate_from_parent_session(service, parent_session)
    control = owner_thread_creation_factory.for_owner_session(
        parent_session.session_id
    ).control_store
    assert len(control.list_child_thread_rows()) == 1
    assert control.list_initial_execution_intents()[0].state == "pending"


@pytest.mark.asyncio
async def test_delegate_rejects_unknown_subagent_type_before_creation(
    parent_session: SessionDTO,
    session_subagent_service: SessionSubagentService,
    session_catalog_store: SessionCatalogStore,
) -> None:
    with pytest.raises(ValueError, match="当前仅支持 general-purpose"):
        await session_subagent_service.delegate(
            parent_session_id=parent_session.session_id,
            parent_thread_id=parent_session.thread_id,
            parent_agent_id="default",
            parent_job_id="job_parent",
            parent_tool_call_id="call_task",
            description="做事",
            subagent_type="unknown",
        )
    node_count = int(
        session_catalog_store.connection.execute(
            "SELECT COUNT(*) FROM nodes"
        ).fetchone()[0]
    )
    assert node_count == 1


@pytest.mark.asyncio
async def test_delegate_rejects_agent_mismatch(
    parent_session: SessionDTO,
    owner_thread_creation_factory: OwnerThreadCreationFactory,
) -> None:
    service = SessionSubagentService(
        parent_session_reader=_MismatchedParentSessionReader(parent_session),
        thread_creation_factory=owner_thread_creation_factory,
        initial_execution_binder=_RecordingBinder(),
        bind_owner=lambda session_id: _bind_initial_execution_for_test(
            owner_thread_creation_factory, session_id
        ),
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
    owner_thread_creation_factory: OwnerThreadCreationFactory,
    session_catalog_store: SessionCatalogStore,
) -> None:
    """生产未装配真实 binder 时必须在任何持久化副作用前 fail closed。

    OpenSpec 主 spec 第 244 行：缺少 binder 时 MUST 可观测地 fail-closed
    报告 ``unavailable``，MUST NOT 静默停留 pending、MUST NOT 返回虚假
    accepted。此处断言：抛出的异常携带确定性 delegation identity，且没有
    创建任何 thread_catalog child row / admission intent。
    """
    service = SessionSubagentService(
        parent_session_reader=_ParentSessionReader(parent_session),
        thread_creation_factory=owner_thread_creation_factory,
        initial_execution_binder=None,
        bind_owner=lambda session_id: _bind_initial_execution_for_test(
            owner_thread_creation_factory, session_id
        ),
    )

    with pytest.raises(SessionSubagentUnavailableError) as raised:
        await _delegate_from_parent_session(service, parent_session)

    error = raised.value
    assert error.delegation_id.startswith("del_")
    assert error.parent_session_id == parent_session.session_id
    assert error.subagent_type == "general-purpose"
    assert "unavailable" in str(error)

    # 无持久化副作用：不创建 child row，也不写 admission intent。
    node_count = int(
        session_catalog_store.connection.execute(
            "SELECT COUNT(*) FROM nodes"
        ).fetchone()[0]
    )
    assert node_count == 1
    control = owner_thread_creation_factory.for_owner_session(
        parent_session.session_id
    ).control_store
    assert control.list_child_thread_rows() == ()
    assert control.list_initial_execution_intents() == ()
