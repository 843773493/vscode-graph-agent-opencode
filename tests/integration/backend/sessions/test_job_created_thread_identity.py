"""真实 Job 创建入口到持久 Trace 的 Thread 身份保留。"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.abstractions.job_executor import JobRuntimeStateProtocol
from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
    compute_capability_profile_hash,
)
from app.api.deps import get_config_service, get_event_service, get_session_service
from app.api.jobs import router as jobs_router
from app.api.sessions import router as sessions_router
from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import EventType, JobEventBus
from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.common import MessageRole
from app.schemas.internal_v2.message import MessageDTO
from app.services.business.job.service import JobService
from app.services.business.message_service import MessageService
from app.services.business.session_service import SessionService
from app.services.event_service import EventService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.trace_event_recorder import TraceEventRecorder
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)
from tests.harness.python.run_context import TestRunContext
from tests.unit.core.catalog_workspace_helper import build_catalog_workspace


class _NoModelExecutor:
    """创建已提交后结束执行端口；本项不调用 Provider。"""

    async def run(self, job: JobRuntimeStateProtocol) -> str:
        return f"创建事件已写入 {job.thread_id}"


@dataclass(frozen=True)
class TraceProducerCase:
    bus: JobEventBus
    jobs: JobService
    input_owner: MessageService
    store: TraceEventStore
    session_id: str
    thread_ids: tuple[str, str]
    trace_dir: Path
    artifacts_dir: Path
    sessions: SessionService


@pytest.fixture
async def producer_case(request: pytest.FixtureRequest) -> AsyncIterator[TraceProducerCase]:
    context = TestRunContext.from_test_file(Path(request.node.path))
    artifacts_dir = context.artifacts_dir / (
        f"{request.node.name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
    )
    artifacts_dir.mkdir(parents=True)
    workspace_root = context.workspace_root
    if workspace_root.exists():
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True)
    workspace = build_catalog_workspace(
        context.output_root, workspace_id=load_or_create_workspace_id(workspace_root),
    )
    factory = OwnerThreadCreationFactory(
        sessions_root=workspace.sessions_root, workspace_id=workspace.workspace_id,
        path_resolver=workspace.resolver,
    )
    session_id = await workspace.create_session("创建事件的精确 Thread")
    main_thread_id = workspace.resolver.main_thread_id(session_id)
    profile = {**DEEP_AGENT_CAPABILITY_PROFILE, "thread_role": "child"}
    graph_binding = {
        **asdict(DEEP_AGENT_GRAPH_BINDING),
        "capability_profile_hash": compute_capability_profile_hash(capability_profile=profile),
    }
    created = await factory.for_owner_session(session_id).create(
        idempotency_key="trace-child", session_id=session_id, thread_id=None,
        delegation_id=None, initial_state="idle",
        session_metadata={
            "graph_binding": graph_binding, "capability_profile": profile,
            "task_seed": None, "task_reference": None,
        },
        artifact_manifest={},
    )
    child_thread_id = created.child_thread_id
    workspace.resolver.resolve_thread_node(session_id, child_thread_id)
    saver = RolloutCheckpointSaver(sessions_dir=workspace.sessions_root)
    input_owner = MessageService(
        main_thread_resolver=workspace.resolver.main_thread_id, checkpointer=saver,
        workspace_root=workspace_root,
    )

    def assert_active(target_session_id: str) -> None:
        workspace.resolver.resolve_session_node_for_runtime(target_session_id)

    bus = JobEventBus()
    store = TraceEventStore(sessions_dir=workspace.sessions_root)
    recorder = TraceEventRecorder(bus=bus, store=store)
    jobs = JobService(
        job_event_bus=bus, job_executor=_NoModelExecutor(),
        session_lifecycle_guard=assert_active, input_owner=input_owner,
        terminal_status_writer=saver,
    )
    sessions = SessionService(
        config_service=ConfigService(), trace_event_store=store,
        workspace_id=workspace.workspace_id, path_resolver=workspace.resolver,
        creation_service=workspace.creation_service,
    )
    await recorder.start()
    try:
        yield TraceProducerCase(
            bus, jobs, input_owner, store, session_id,
            (main_thread_id, child_thread_id),
            workspace.session_dir(session_id) / "logs" / "traces",
            artifacts_dir,
            sessions,
        )
    finally:
        tasks = [job.task for job in jobs._jobs.values() if job.task is not None]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        await recorder.stop()
        for service in factory._services.values():
            service.control_store.close()
        workspace.close()


@pytest.fixture
async def job_events_client(
    producer_case: TraceProducerCase,
) -> AsyncIterator[httpx.AsyncClient]:
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(jobs_router, prefix="/api/v1")
    application.include_router(sessions_router, prefix="/api/v1")
    event_service = EventService(bus=producer_case.bus)
    application.dependency_overrides[get_event_service] = lambda: event_service
    application.dependency_overrides[get_session_service] = lambda: producer_case.sessions
    application.dependency_overrides[get_config_service] = lambda: ConfigService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://workspace.test",
        headers={"X-Local-Token": "local-dev-token"},
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_real_job_created_preserves_main_and_child_thread_in_durable_trace(
    producer_case: TraceProducerCase,
    job_events_client: httpx.AsyncClient,
) -> None:
    case = producer_case
    expected: dict[str, str] = {}
    for thread_id in case.thread_ids:
        identity = case.jobs.reserve_user_execution_identity()
        now = datetime.now(UTC)
        message = MessageDTO(
            message_id=create_prefixed_id("msg"), session_id=case.session_id,
            thread_id=thread_id, role=MessageRole.user, content="相同正文的独立 Thread",
            created_at=now, updated_at=now,
        )
        case.input_owner.accept_user_message(message, execution_identity=identity)
        dispatch = await case.jobs.start_job(
            case.session_id, message.content, thread_id=thread_id,
            execution_binding=identity, message_id=message.message_id,
            message_created_at=now.isoformat(),
        )
        assert dispatch.thread_id == thread_id
        expected[dispatch.job_id] = thread_id

    assert len(set(expected.values())) == 2
    raw_trace = (case.trace_dir / "events.jsonl").read_bytes()
    (case.artifacts_dir / "events.jsonl").write_bytes(raw_trace)
    raw_events = [
        json.loads(line)
        for line in raw_trace.splitlines()
    ]
    created = [event for event in raw_events if event["type"] == EventType.JOB_CREATED]
    assert len(created) == 2
    assert {
        event["job_id"]: (event["payload"]["session_id"], event["payload"]["thread_id"])
        for event in created
    } == {job_id: (case.session_id, thread_id) for job_id, thread_id in expected.items()}
    assert {
        event.job_id: event.payload.thread_id
        for event in case.store.read_events(case.session_id)
        if event.type == EventType.JOB_CREATED
    } == expected
    for job_id, thread_id in expected.items():
        request_id = f"req_created_{job_id}"
        response = await job_events_client.get(
            f"/api/v1/jobs/{job_id}/events", headers={"X-Request-ID": request_id},
        )
        (case.artifacts_dir / f"{job_id}-http.json").write_bytes(response.content)
        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == request_id
        assert response.json()["request_id"] == request_id
        published = [
            event for event in response.json()["data"]
            if event["type"] == EventType.JOB_CREATED
        ]
        assert len(published) == 1
        assert published[0]["job_id"] == job_id
        assert published[0]["payload"]["session_id"] == case.session_id
        assert published[0]["payload"]["thread_id"] == thread_id

    tasks = [job.task for job in case.jobs._jobs.values() if job.task is not None]
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    # 冷重开读取端，不能依赖 Recorder 或 JobService 的进程内 owner map。
    cold_store = TraceEventStore(sessions_dir=case.store._sessions_dir)
    case.sessions._trace_event_store = cold_store
    base = f"/api/v1/sessions/{case.session_id}/traces"
    aggregate = await job_events_client.get(base)
    assert aggregate.status_code == 200, aggregate.text
    all_items = aggregate.json()["data"]["items"]
    assert {item["thread_id"] for item in all_items} == set(case.thread_ids)
    assert {item["job_id"] for item in all_items} == set(expected)
    for item in all_items:
        assert item["thread_id"] == expected[item["job_id"]]

    for thread_id in case.thread_ids:
        # limit 限定物理扫描条数，稀疏页即使为空也必须按 Session 水位向前。
        params = {"thread_id": thread_id, "limit": 1}
        collected = []
        first_cursor = None
        while True:
            response = await job_events_client.get(base, params=params)
            assert response.status_code == 200, response.text
            page = response.json()["data"]
            collected.extend(page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
            first_cursor = first_cursor or cursor
            params["cursor"] = cursor
        assert {item["event_id"] for item in collected} == {
            item["event_id"] for item in all_items if item["thread_id"] == thread_id
        }
        assert all(item["thread_id"] == thread_id for item in collected)
        assert len({item["event_id"] for item in collected}) == len(collected)
        other = next(value for value in case.thread_ids if value != thread_id)
        rejected = await job_events_client.get(base, params={"thread_id": other, "cursor": first_cursor})
        assert rejected.status_code == 410, rejected.text
        aggregate_rejected = await job_events_client.get(base, params={"cursor": first_cursor})
        assert aggregate_rejected.status_code == 410

        stream = case.sessions.stream_trace_events(case.session_id, thread_id=thread_id)
        first, stream_cursor = await asyncio.wait_for(anext(stream), timeout=2)
        assert first.thread_id == thread_id
        await stream.aclose()
        await case.sessions.ensure_trace_cursor(case.session_id, stream_cursor, thread_id=thread_id)
        resumed = case.sessions.stream_trace_events(case.session_id, stream_cursor, thread_id=thread_id)
        second, _ = await asyncio.wait_for(anext(resumed), timeout=2)
        assert second.event_id != first.event_id
        assert second.thread_id == thread_id
        await resumed.aclose()
        wrong_stream = await job_events_client.get(
            base + "/stream", params={"thread_id": other, "after_event_id": stream_cursor},
        )
        assert wrong_stream.status_code == 410, wrong_stream.text
        page_as_stream = await job_events_client.get(
            base + "/stream", params={"thread_id": thread_id, "after_event_id": first_cursor},
        )
        assert page_as_stream.status_code == 410


@pytest.mark.asyncio
async def test_job_created_missing_thread_is_rejected_before_durable_write(
    producer_case: TraceProducerCase,
) -> None:
    case = producer_case
    with pytest.raises(ValidationError, match="thread_id"):
        await case.bus.publish(
            job_id="job_missing_thread", event_type=EventType.JOB_CREATED,
            payload={"session_id": case.session_id, "message": "无 owner", "agent_id": "default"},
        )

    assert await case.bus.list_events("job_missing_thread") == []
    assert case.store.read_events(case.session_id) == []
    assert not case.trace_dir.exists()
