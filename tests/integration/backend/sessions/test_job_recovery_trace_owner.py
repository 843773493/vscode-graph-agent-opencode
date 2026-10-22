"""当前 Job writer 的持久 Trace 经冷读恢复时保持精确 Thread owner。"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.abstractions.job_executor import JobRuntimeStateProtocol
from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
    compute_capability_profile_hash,
)
from app.container import AppContainer, build_app_container
from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import JobEventBus
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.schemas.event import JobCreatedEvent, JobStartedEvent
from app.schemas.internal_v2.common import MessageRole
from app.schemas.internal_v2.message import MessageDTO
from app.services.business.job.service import JobService
from app.services.business.message_service import MessageService
from app.services.infrastructure.pending_request_store import PendingRequestStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.trace_event_recorder import TraceEventRecorder
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace
from tests.unit.core.catalog_workspace_helper import (
    CatalogWorkspaceContext,
    build_catalog_workspace,
)


class _HeldExecutor:
    """仅替换执行端口，在 Provider 前停住真实 Job 的后台任务。"""

    def __init__(self) -> None:
        self.started: asyncio.Queue[JobRuntimeStateProtocol] = asyncio.Queue()

    async def run(self, job: JobRuntimeStateProtocol) -> str:
        self.started.put_nowait(job)
        await asyncio.Future()
        raise AssertionError("测试执行屏障不应自行结束")


@dataclass(frozen=True)
class RecoveryCase:
    source: CatalogWorkspaceContext
    saver: RolloutCheckpointSaver
    store: TraceEventStore
    jobs: JobService
    workspace: Path
    session_id: str
    thread_ids: tuple[str, str]
    job_ids: tuple[str, str]
    artifacts: Path
    session_paths: dict[str, Path]


@pytest.fixture
async def recovery_case(request: pytest.FixtureRequest) -> AsyncIterator[RecoveryCase]:
    context = TestRunContext.from_test_file(Path(request.node.path))
    runtime_root = context.runtime_root_for_node(request.node.nodeid)
    workspace = prepare_default_test_workspace(
        workspace_root=runtime_root / "workspace",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    shutil.rmtree(workspace / ".boxteam")
    artifacts = context.artifacts_dir / (
        f"{request.node.name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
    )
    artifacts.mkdir(parents=True)
    source = build_catalog_workspace(
        runtime_root, workspace_id=load_or_create_workspace_id(workspace),
    )
    session_id = await source.create_session("重启精确 owner")
    resolver = source.resolver
    main_thread_id = resolver.main_thread_id(session_id)
    factory = OwnerThreadCreationFactory(
        sessions_root=source.sessions_root,
        workspace_id=source.workspace_id, path_resolver=resolver,
    )
    profile = {**DEEP_AGENT_CAPABILITY_PROFILE, "thread_role": "child"}
    graph = {
        **asdict(DEEP_AGENT_GRAPH_BINDING),
        "capability_profile_hash": compute_capability_profile_hash(capability_profile=profile),
    }
    child = await factory.for_owner_session(session_id).create(
        idempotency_key="recovery-child", session_id=session_id, thread_id=None,
        delegation_id=None, initial_state="idle",
        session_metadata={
            "graph_binding": graph, "capability_profile": profile,
            "task_seed": None, "task_reference": None,
        },
        artifact_manifest={},
    )
    thread_ids = (main_thread_id, child.child_thread_id)
    session_paths = {session_id: resolver.resolve_session_node(session_id)}
    saver = RolloutCheckpointSaver(sessions_dir=source.sessions_root)
    input_owner = MessageService(
        main_thread_resolver=resolver.main_thread_id, checkpointer=saver,
        workspace_root=workspace,
    )
    bus = JobEventBus()
    executor = _HeldExecutor()
    jobs = JobService(
        job_event_bus=bus, job_executor=executor,
        input_owner=input_owner, terminal_status_writer=saver,
        pending_request_store=PendingRequestStore(sessions_dir=source.sessions_root, owner=saver,
            input_reader=lambda ref: input_owner.accepted_job_message(ref, display=False)),
        session_lifecycle_guard=resolver.resolve_session_node_for_runtime,
    )
    store = TraceEventStore(sessions_dir=source.sessions_root)
    recorder = TraceEventRecorder(bus=bus, store=store)
    await recorder.start()
    try:
        job_ids: list[str] = []
        for thread_id in thread_ids:
            binding = jobs.reserve_user_execution_identity()
            now = datetime.now(UTC)
            message = MessageDTO(
                message_id=create_prefixed_id("msg"), session_id=session_id,
                thread_id=thread_id, role=MessageRole.user, content="相同正文的独立线程",
                created_at=now, updated_at=now,
            )
            input_owner.accept_user_message(message, execution_identity=binding, queue_input=ExecutionQueueInput(
                execution_binding=binding, message_id=message.message_id, agent_id="default",
                provider_id=None, delivery_policy="after_turn", gateway_id=None, request_id=None,
                created_at=message.created_at.isoformat(),
            ))
            dispatch = await jobs.start_job(
                session_id, message.content, thread_id=thread_id,
                execution_binding=binding, message_id=message.message_id,
                message_created_at=now.isoformat(),
            )
            started = await asyncio.wait_for(executor.started.get(), timeout=5)
            assert (started.session_id, started.thread_id, started.job_id) == (
                session_id, thread_id, dispatch.job_id,
            )
            job_ids.append(dispatch.job_id)
        # 截断旧运行面的持久事件写入，冷读实例只能读取当前 writer 的原件。
        await recorder.stop()
        yield RecoveryCase(
            source, saver, store, jobs, workspace, session_id, thread_ids,
            (job_ids[0], job_ids[1]), artifacts, session_paths,
        )
    finally:
        await recorder.stop()
        tasks = [job.task for job in jobs._jobs.values() if job.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        for service in factory._services.values():
            service.control_store.close()
        source.close()


@pytest.fixture
async def cold_container_factory(
    recovery_case: RecoveryCase,
) -> AsyncIterator[Callable[[], AppContainer]]:
    containers: list[AppContainer] = []

    def reopen() -> AppContainer:
        cold = build_app_container(project_root=Path.cwd(), workspace_root=recovery_case.workspace)
        containers.append(cold)
        return cold

    try:
        yield reopen
    finally:
        for cold in containers:
            await cold.workspace_session_context_transport.aclose()
            cold.workspace_activity_service.close()
            cold.session_catalog.close()


def _trace_path(case: RecoveryCase, session_id: str) -> Path:
    return case.session_paths[session_id] / "logs" / "traces" / "events.jsonl"


def _trace_snapshot(case: RecoveryCase) -> dict[str, bytes]:
    return {
        session_id: path.read_bytes() if path.exists() else b""
        for session_id in case.session_paths
        for path in [_trace_path(case, session_id)]
    }


def _execution_facts(case: RecoveryCase) -> dict[str, object]:
    result: dict[str, object] = {}
    for thread_id in case.thread_ids:
        with case.saver._storage.storage.read_connection(
            case.session_id, thread_id,
        ) as connection:
            result[thread_id] = {
                "executions": connection.execute(
                    "SELECT session_id,thread_id,job_id,outcome FROM executions ORDER BY job_id"
                ).fetchall(),
                "turns": connection.execute(
                    "SELECT turn_id,status FROM turn_records ORDER BY turn_id"
                ).fetchall(),
                "commits": connection.execute("SELECT COUNT(*) FROM storage_commits").fetchone()[0],
            }
    return result


def _producer_events(case: RecoveryCase) -> list[dict]:
    original = _trace_path(case, case.session_id).read_bytes()
    (case.artifacts / "producer-events.jsonl").write_bytes(original)
    events = [json.loads(line) for line in original.splitlines()]
    expected = dict(zip(case.job_ids, case.thread_ids, strict=True))
    for event_type in ("job_created", "job_started"):
        assert {
            event["job_id"]: (event["payload"]["session_id"], event["payload"]["thread_id"])
            for event in events if event["type"] == event_type
        } == {job_id: (case.session_id, thread_id) for job_id, thread_id in expected.items()}
    assert all(
        event["payload"]["execution_binding"] and event["payload"]["message_id"]
        for event in events if event["type"] == "job_created"
    )
    return events


@pytest.mark.asyncio
async def test_current_job_trace_cold_runtime_rejects_live_exact_thread_holder(
    recovery_case: RecoveryCase, cold_container_factory: Callable[[], AppContainer],
) -> None:
    case = recovery_case
    _producer_events(case)
    cold = cold_container_factory()
    assert cold.trace_event_store is not case.store
    assert cold.job_service is not case.jobs
    assert len(cold.trace_event_store.read_events(case.session_id)) >= 4

    # 旧 executor 仍在运行；新 runtime 必须在任何恢复写入前拒绝活 holder。
    traces_before = _trace_snapshot(case)
    facts_before = _execution_facts(case)
    with pytest.raises(RuntimeError, match="拒绝并存启动"):
        await cold.runtime_service.reconcile_stale_executions()

    facts = _execution_facts(case)
    assert facts == facts_before and _trace_snapshot(case) == traces_before
    for thread_id, job_id in zip(case.thread_ids, case.job_ids, strict=True):
        assert facts[thread_id]["executions"] == [
            (case.session_id, thread_id, job_id, "unknown")
        ]
        assert [status for _, status in facts[thread_id]["turns"]] == ["open"]
        source_job = case.jobs._jobs[job_id]
        assert source_job.thread_id == thread_id and source_job.task is not None
        assert not source_job.task.done() and source_job.queue_claim is not None
    assert await cold.job_service.drain_blockers() == []
    (case.artifacts / "recovered-facts.json").write_text(
        json.dumps(facts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (case.artifacts / "recovered-events.jsonl").write_bytes(_trace_path(case, case.session_id).read_bytes())


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [
    "created_main_started_child", "created_session_vs_trace", "duplicate_created_owner",
    "cross_trace_created_owner", "cross_trace_started_owner", "missing_created_thread",
])
async def test_current_job_trace_owner_conflict_fails_before_recovery_writes(
    recovery_case: RecoveryCase, cold_container_factory: Callable[[], AppContainer], fault: str,
) -> None:
    case = recovery_case
    events = _producer_events(case)
    created = next(
        event for event in events
        if event["type"] == "job_created" and event["job_id"] == case.job_ids[1]
    )
    target_session_id = case.session_id
    target_events = events
    if fault.startswith("cross_trace") or fault == "created_session_vs_trace":
        other_session_id = await case.source.create_session("另一会话 owner")
        other_thread_id = case.source.resolver.main_thread_id(other_session_id)
        case.session_paths[other_session_id] = case.source.resolver.resolve_session_node(
            other_session_id,
        )
    if fault.startswith("cross_trace"):
        event_type = "job_started" if fault == "cross_trace_started_owner" else "job_created"
        injected = copy.deepcopy(next(
            event for event in events
            if event["type"] == event_type and event["job_id"] == case.job_ids[1]
        ))
        injected["event_id"] = create_prefixed_id("evt")
        injected["payload"].update(session_id=other_session_id, thread_id=other_thread_id)
        target_session_id = other_session_id
        target_events = [injected]
    elif fault == "created_session_vs_trace":
        injected = copy.deepcopy(created)
        injected["event_id"] = create_prefixed_id("evt")
        injected["payload"].update(session_id=case.session_id, thread_id=case.thread_ids[1])
        target_session_id = other_session_id
    elif fault == "duplicate_created_owner":
        injected = copy.deepcopy(created)
        injected["event_id"] = create_prefixed_id("evt")
        injected["payload"]["thread_id"] = case.thread_ids[0]
    elif fault == "missing_created_thread":
        del created["payload"]["thread_id"]
    else:
        injected = copy.deepcopy(next(
            event for event in events
            if event["type"] == "job_started" and event["job_id"] == case.job_ids[1]
        ))
        injected["event_id"] = create_prefixed_id("evt")
        injected["payload"]["thread_id"] = case.thread_ids[0]
    # 合法 owner 冲突经正式 writer 追加 typed event，保持 Trace 派生索引同步。
    if fault == "missing_created_thread":
        trace_path = _trace_path(case, target_session_id)
        trace_path.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in target_events),
            encoding="utf-8",
        )
    else:
        event_type = JobStartedEvent if injected["type"] == "job_started" else JobCreatedEvent
        await case.store.append(target_session_id, event_type.model_validate(injected))
    (case.artifacts / "fault-events.jsonl").write_bytes(
        _trace_path(case, target_session_id).read_bytes(),
    )
    traces_before = _trace_snapshot(case)
    facts_before = _execution_facts(case)
    cold = cold_container_factory()
    expected_error = "Trace 事件协议无效" if fault == "missing_created_thread" else "owner"
    with pytest.raises(RuntimeError, match=expected_error) as rejected:
        await cold.runtime_service.reconcile_stale_executions()

    if fault == "missing_created_thread":
        assert "thread_id" in str(rejected.value.__cause__)
    assert _trace_snapshot(case) == traces_before
    assert _execution_facts(case) == facts_before
    assert await cold.job_service.drain_blockers() == []
    (case.artifacts / "rejected.json").write_text(json.dumps({
        "fault": fault, "error": str(rejected.value), "facts": facts_before,
        "trace_unchanged": True, "canonical_unchanged": True,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
