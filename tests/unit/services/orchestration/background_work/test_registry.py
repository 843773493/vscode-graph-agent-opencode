from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock

import pytest
from pydantic import JsonValue

from app.core.execution_pause import (
    ExecutionPause,
    bind_execution_pause,
    current_execution_pause,
    execution_pause_boundary,
)
from app.core.path_utils import get_session_creation_service, get_session_path_resolver
from app.core.session_lifecycle_gate import SessionLifecycleGate
from app.domain.background_work import (
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
    BackgroundTaskStep,
)
from app.domain.itemized.hashing import sha256_jcs
from app.domain.session_stop import SessionStopPolicy
from app.schemas.internal_v2.session import SessionInterruptResultDTO
from app.services.business.session_resources.providers import (
    BackgroundTaskResourceProvider,
)
from app.services.business.session_resources.registry import (
    SessionResourceProviderRegistry,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.background_work_store import BackgroundWorkStore
from app.services.infrastructure.session_trace.store import TraceEventStore
from app.services.orchestration.background_work.mailbox import BackgroundMailbox
from app.services.orchestration.background_work.registry import BackgroundTaskRegistry
from app.services.orchestration.background_work.system_time_emitter import (
    SystemTimeEmitterHandler,
)
from app.services.orchestration.session_stop import SessionStopService
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from configs.runtime import read_jsonc_object
from tests.support.background_work import ControlledBackgroundHandler
from tests.unit.services.business.session_service_test_support import (
    SessionServiceConfigStub,
)


@pytest.fixture
async def background_case(
    tmp_path: Path,
    session_bundle_factory,
):
    workspace_root = tmp_path / "workspace"
    sessions = workspace_root / ".boxteam" / "sessions"
    session_id = "ses_019c3df7f8527d568e834acf3c0f6612"
    session_bundle_factory(sessions, session_id)
    thread_id = get_session_path_resolver(sessions).main_thread_id(session_id)
    store = BackgroundWorkStore(sessions_dir=sessions)
    handler = ControlledBackgroundHandler()
    registries = []
    def registry(handlers=(handler,)):
        value = BackgroundTaskRegistry(store=store, residency_tracker=ThreadResidencyTracker(), handlers=handlers)
        registries.append(value)
        return value
    yield store, session_id, thread_id, handler, registry
    for value in registries:
        await value.stop()


@dataclass
class DerivedHandler:
    """rollout 结果端口替身；只保存结果引用，不伪造聊天消息或上游执行。"""

    kind: str = "compaction_test"
    version: int = 1
    effect: Literal["derived_request"] = "derived_request"
    registry: BackgroundTaskRegistry | None = None
    calls: list[str] = field(default_factory=list)
    started: dict[str, asyncio.Event] = field(default_factory=dict)
    finish: dict[str, asyncio.Event] = field(default_factory=dict)
    ready: dict[str, BackgroundTaskStep] = field(default_factory=dict)
    published: asyncio.Event = field(default_factory=asyncio.Event)
    after_publish: asyncio.Event | None = None
    late_after_cancel: bool = False
    rejected: asyncio.Event = field(default_factory=asyncio.Event)
    abandoned: list[tuple[str, str]] = field(default_factory=list)

    def initial_checkpoint(self, descriptor: BackgroundTaskDescriptor) -> dict[str, JsonValue]:
        if set(descriptor.payload) != {"deadline"}:
            raise ValueError("测试派生描述符必须固定 deadline")
        return {"done": False}

    def deadline(self, handle: BackgroundTaskHandle) -> datetime:
        return datetime.fromisoformat(str(handle.metadata["deadline"]))

    def validate_checkpoint(self, checkpoint: dict[str, JsonValue]) -> None:
        if set(checkpoint) != {"done"} or type(checkpoint["done"]) is not bool:
            raise ValueError("测试派生 checkpoint 非法")

    async def reconcile(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep | None:
        return self.ready.get(handle.task_id)

    async def abandon(self, handle: BackgroundTaskHandle, *, outcome: Literal["failed", "cancelled"]) -> None:
        self.abandoned.append((handle.task_id, outcome))

    async def step(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep:
        assert current_execution_pause() is None
        async with execution_pause_boundary():
            pass
        self.calls.append(handle.task_id)
        self.started.setdefault(handle.task_id, asyncio.Event()).set()
        try:
            await self.finish.setdefault(handle.task_id, asyncio.Event()).wait()
        except asyncio.CancelledError:
            if not self.late_after_cancel:
                raise
        assert self.registry is not None
        try:
            async with self.registry.derived_publication(handle):
                step = BackgroundTaskStep(checkpoint={"done": True}, completed=True,
                    result={"candidate_id": handle.task_id})
                self.ready[handle.task_id] = step
                self.published.set()
        except RuntimeError:
            self.rejected.set()
            raise
        if self.after_publish is not None:
            await self.after_publish.wait()
        return step


async def _wait_started(handler: DerivedHandler, task_id: str) -> None:
    await asyncio.wait_for(handler.started.setdefault(task_id, asyncio.Event()).wait(), 3)


def _derived_descriptor(handler: DerivedHandler, *, deadline: datetime | None = None) -> BackgroundTaskDescriptor:
    return BackgroundTaskDescriptor(kind=handler.kind, version=handler.version,
        payload={"deadline": (deadline or datetime.now(UTC) + timedelta(minutes=1)).isoformat()})


@pytest.fixture
def derived_clock(monkeypatch):
    clock = [datetime.now(UTC)]

    class Clock:
        @classmethod
        def now(cls, _timezone):
            return clock[0]

    monkeypatch.setattr("app.services.orchestration.background_work.registry.datetime", Clock)
    return clock


@pytest.fixture
def session_stop_factory(background_case):
    store, session_id, thread_id, _handler, _registry_factory = background_case
    resolver = get_session_path_resolver(store.sessions_dir)
    sessions = SessionService(config_service=SessionServiceConfigStub(),
        trace_event_store=TraceEventStore(sessions_dir=store.sessions_dir), workspace_id=resolver.workspace_id,
        path_resolver=resolver, creation_service=get_session_creation_service(store.sessions_dir))
    defaults = read_jsonc_object(Path.cwd() / "configs/workspace_inline.jsonc")["settings"]["workspace"]["runtime"]["session_stop"]

    async def run_control(_session_id, operation, *, deadline):
        return await operation()

    def build(registry, *, interrupt_action=None, stop_action=None):
        policies = {name: SessionStopPolicy.model_validate(value) for name, value in defaults.items()}
        if interrupt_action is not None:
            policies["interrupt"] = policies["interrupt"].model_copy(update={"background_task": interrupt_action})
        if stop_action is not None:
            policies["stop"] = policies["stop"].model_copy(update={"background_task": stop_action})
        return SessionStopService(sessions=sessions,
            jobs=SimpleNamespace(run_session_control_operation=run_control,
                stop_session_jobs=AsyncMock(), notify_boundary=AsyncMock()),
            interrupt=SimpleNamespace(interrupt=AsyncMock(return_value=SessionInterruptResultDTO(
                session_id=session_id, thread_id=thread_id, job_id="main-job", status="cancelled",
                interrupt_request_id="interrupt-main", phase="completed"))),
            resources=SessionResourceProviderRegistry((BackgroundTaskResourceProvider(task_registry=registry),)),
            debug=SimpleNamespace(drain_session=AsyncMock()), policy=policies.__getitem__,
            drain_collaboration=AsyncMock(), pause_goal=AsyncMock(), close_runtime=AsyncMock(), holders=(),
            advancement_changed=lambda: None, pause_automations=AsyncMock(), resume_execution=AsyncMock())

    return build


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_action,stop_action", [(None, None), ("stop", None), (None, "keep")])
async def test_session_stop_uses_real_gate_and_background_provider_policy(
    background_case, session_stop_factory, interrupt_action, stop_action,
):
    _store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler(late_after_cancel=True)
    registry = handler.registry = registry_factory((handler,))
    handle = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
        task_id="controlled-derived", descriptor=_derived_descriptor(handler))
    await _wait_started(handler, handle.task_id)
    queued = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
        task_id="controlled-pending", descriptor=_derived_descriptor(handler))
    service = session_stop_factory(registry, interrupt_action=interrupt_action, stop_action=stop_action)
    await asyncio.wait_for(service.interrupt(session_id, thread_id=thread_id), 3)
    assert registry.get_handle(session_id, handle.task_id).status == ("cancelled" if interrupt_action == "stop" else "running")
    await asyncio.wait_for(service.stop(session_id), 3)
    expected = "running" if stop_action == "keep" else "cancelled"
    assert registry.get_handle(session_id, handle.task_id).status == expected
    assert registry.get_handle(session_id, queued.task_id).status == ("pending" if expected == "running" else "cancelled")
    assert handler.calls == [handle.task_id] and not handler.ready
    if expected == "running":
        assert await registry.cleanup_session(session_id, action="stop") == 2
        assert handler.calls == [handle.task_id]


@pytest.mark.asyncio
async def test_source_deletion_drains_derived_before_gate_and_physical_isolation(
    background_case, session_stop_factory, monkeypatch,
):
    store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler(late_after_cancel=True)
    registry = handler.registry = registry_factory((handler,))
    descriptor = _derived_descriptor(handler)
    for index in range(2):
        await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
            task_id=f"delete-derived-{index}", descriptor=descriptor)
    await _wait_started(handler, "delete-derived-0")
    resolver = get_session_path_resolver(store.sessions_dir)
    source = resolver.resolve_session_node(session_id)
    service = session_stop_factory(registry)
    waiting, draining = asyncio.Event(), asyncio.Event()

    async def observe(_operation_id, phase, **_details):
        if phase == f"deletion.drain_waiting/{session_id}":
            waiting.set()

    monkeypatch.setattr("app.core.session_subtree_delete.observe_phase", observe)

    async def drain(owner_session_id):
        draining.set()
        assert source.is_dir()
        await service.drain_for_deletion(owner_session_id)
        assert all(registry.get_handle(session_id, f"delete-derived-{index}").status == "deleted" for index in range(2))
        async with SessionLifecycleGate(store.sessions_dir).exclusive(owner_session_id):
            assert source.is_dir()

    resolver.bind_session_drain_callback(drain)
    async with SessionLifecycleGate(store.sessions_dir).shared(session_id):
        deleting = asyncio.create_task(resolver.delete_subtree(
            idempotency_key="delete-derived-source", root_node_id=session_id))
        await asyncio.wait_for(waiting.wait(), 3)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(draining.wait(), 0.1)
    result = await asyncio.wait_for(deleting, 3)
    assert result.record_state == "completed" and not source.exists()
    assert handler.calls == ["delete-derived-0"] and not handler.ready


@pytest.mark.asyncio
async def test_two_interactive_tasks_share_thread_without_parent_pause_and_busy_does_not_register(background_case):
    store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler()
    registry = handler.registry = registry_factory((handler,))
    descriptor = _derived_descriptor(handler)
    pause = ExecutionPause()
    pause.request()
    with bind_execution_pause(pause):
        handles = await asyncio.gather(*(registry.spawn_derived(
            session_id=session_id, thread_id=thread_id, agent_id="agent", task_id=f"interactive-{index}",
            descriptor=descriptor, foreground=True,
        ) for index in range(2)))
        await asyncio.gather(*(_wait_started(handler, handle.task_id) for handle in handles))
        busy = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
            task_id="interactive-busy", descriptor=descriptor, foreground=True)
        assert busy is None
        assert registry.get_handle(session_id, "interactive-busy") is None
        assert len(registry._running_tasks) == 2 and not registry._derived_queue
        with store.control(session_id, read_only=True) as control:
            assert len(control.list_background_tasks()) == 2
            assert len(control.list_non_terminal_leases()) == 2
        pause.resume()
        retry = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
            task_id=handles[0].task_id, descriptor=descriptor, foreground=True)
        assert retry.task_id == handles[0].task_id
        assert handler.calls == [handle.task_id for handle in handles]
    with pytest.raises(RuntimeError, match="descriptor 冲突"):
        await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="other-agent",
            task_id=handles[0].task_id, descriptor=descriptor, foreground=True)


@pytest.mark.asyncio
async def test_derived_freeze_factory_runs_once_inside_admission_and_skips_busy(background_case):
    store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler()
    registry = handler.registry = registry_factory((handler,))
    calls, observed, released = [], [], []

    @asynccontextmanager
    async def freeze(existing):
        assert registry._derived_admission.locked()
        observed.append(None if existing is None else existing.task_id)
        if existing is not None:
            yield existing.descriptor
            return
        calls.append("freeze")
        try:
            yield _derived_descriptor(handler)
        except BaseException:
            released.append("release")
            raise

    first, retry = await asyncio.gather(*(registry.spawn_derived(
        session_id=session_id, thread_id=thread_id, agent_id=agent, task_id="same-key",
        descriptor=freeze, foreground=True,
    ) for agent in ("first-agent", "changed-agent")))
    assert first.task_id == retry.task_id and retry.agent_id == "first-agent"
    assert first.descriptor == retry.descriptor and calls == ["freeze"]
    assert observed == [None, "same-key"]
    await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
        task_id="second-key", descriptor=freeze, foreground=True)
    busy = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
        task_id="busy-key", descriptor=freeze, foreground=True)
    assert busy is None and calls == ["freeze", "freeze"] and released == []
    with store.control(session_id, read_only=True) as control:
        assert len(control.list_background_tasks()) == 2


@pytest.mark.asyncio
async def test_failed_registration_releases_frozen_input_before_admission_reopens(background_case):
    _store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler()
    registry = handler.registry = registry_factory((handler,))
    released = []

    @asynccontextmanager
    async def invalid_input(existing):
        assert existing is None
        try:
            yield BackgroundTaskDescriptor(kind=handler.kind, version=handler.version, payload={})
        except BaseException:
            assert registry._derived_admission.locked()
            released.append(True)
            raise

    with pytest.raises(ValueError, match="deadline"):
        await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
            task_id="invalid-frozen-input", descriptor=invalid_input, foreground=True)
    assert released == [True] and registry.get_handle(session_id, "invalid-frozen-input") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("query_expiry", [False, True])
async def test_queued_deadline_is_preserved_on_promotion_and_expires_without_upstream(
    background_case, derived_clock, query_expiry: bool,
):
    store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler()
    registry = handler.registry = registry_factory((handler,))
    descriptor = _derived_descriptor(handler)
    for index in range(2):
        await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
            task_id=f"interactive-{index}", descriptor=descriptor, foreground=True)
        await _wait_started(handler, f"interactive-{index}")
    deadline = derived_clock[0] + timedelta(seconds=5)
    queued = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
        task_id="queued-expired", descriptor=_derived_descriptor(handler, deadline=deadline))
    promoted = await registry.promote_derived(session_id, queued.task_id)
    assert promoted.status == "pending" and handler.deadline(promoted) == deadline
    assert registry.get_task(session_id, queued.task_id) is None
    following = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
        task_id="queued-next", descriptor=descriptor)
    derived_clock[0] = deadline + timedelta(seconds=1)
    if query_expiry:
        expired, = await registry.recover_derived_thread(session_id, thread_id)
        assert expired.task_id == queued.task_id and expired.status == "failed"
    handler.finish["interactive-0"].set()
    await _wait_started(handler, following.task_id)
    expired = registry.get_handle(session_id, queued.task_id)
    assert expired.status == "failed" and expired.started_at is None
    assert queued.task_id not in handler.calls
    assert (queued.task_id, "failed") in handler.abandoned
    assert handler.deadline(expired) == deadline
    assert len(registry._running_tasks) == 2
    with store.control(session_id, read_only=True) as control:
        assert control.get_lease(expired.lease_id).state == "failed"


@pytest.mark.asyncio
async def test_running_deadline_stops_wait_and_releases_slot(background_case):
    store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler()
    registry = handler.registry = registry_factory((handler,))
    handle = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
        task_id="running-expired", descriptor=_derived_descriptor(handler,
            deadline=datetime.now(UTC) + timedelta(seconds=1)))
    await _wait_started(handler, handle.task_id)
    task = registry.get_task(session_id, handle.task_id)
    with pytest.raises(TimeoutError):
        await task
    expired = registry.get_handle(session_id, handle.task_id)
    assert expired.status == "failed" and handler.ready == {}
    assert registry.get_task(session_id, handle.task_id) is None
    assert handler.abandoned == [(handle.task_id, "failed")]
    assert (await registry.promote_derived(session_id, handle.task_id)).status == "failed"
    assert handler.abandoned == [(handle.task_id, "failed")]
    assert handler.calls == [handle.task_id]
    with store.control(session_id, read_only=True) as control:
        assert control.list_non_terminal_leases() == ()


@pytest.mark.asyncio
async def test_failed_attempt_can_use_one_new_deadline_without_mutating_frozen_descriptor(
    background_case, derived_clock, monkeypatch,
):
    store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler()
    registry = handler.registry = registry_factory((handler,))

    async def upstream_failure(handle):
        handler.calls.append(handle.task_id)
        raise RuntimeError("首个上游调用失败")

    monkeypatch.setattr(handler, "step", upstream_failure)
    original_deadline = derived_clock[0] + timedelta(seconds=5)
    descriptor = _derived_descriptor(handler, deadline=original_deadline)
    original = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
                                            task_id="failed-retry-deadline", descriptor=descriptor)
    with pytest.raises(RuntimeError, match="首个上游"):
        await registry.get_task(session_id, original.task_id)
    failed = registry.get_handle(session_id, original.task_id)
    derived_clock[0] = original_deadline + timedelta(seconds=1)
    assert (await registry.promote_derived(session_id, original.task_id)).to_dict() == failed.to_dict()
    assert handler.abandoned == [(original.task_id, "failed")]
    new_deadline = derived_clock[0] + timedelta(seconds=10)
    retried = await registry.retry_derived(session_id, original.task_id, deadline=new_deadline, validate=lambda _: None)
    assert retried.descriptor == descriptor and retried.created_at == original.created_at
    assert handler.deadline(retried) == new_deadline
    assert retried.metadata["deadline"] == new_deadline.isoformat()
    with pytest.raises(RuntimeError, match="首个上游"):
        await registry.get_task(session_id, original.task_id)
    with pytest.raises(RuntimeError, match="已使用补尝试"):
        await registry.retry_derived(session_id, original.task_id, deadline=new_deadline + timedelta(seconds=10), validate=lambda _: None)
    assert handler.calls == [original.task_id, original.task_id]
    with store.control(session_id, read_only=True) as control:
        current = control.get_background_task(original.task_id)
        assert control.get_lease(failed.lease_id).state == "failed"
        assert control.get_lease(current.lease_id).operation_identity == f"{original.task_id}:foreground-retry:1"
        assert control.get_lease(current.lease_id).state == "failed"
        assert handler.deadline(current) == new_deadline
        assert control.list_non_terminal_leases() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("published_first", [False, True])
async def test_deadline_publication_keeps_only_result_committed_before_expiry(
    background_case, derived_clock, published_first: bool,
):
    _store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler(after_publish=asyncio.Event() if published_first else None)
    registry = handler.registry = registry_factory((handler,))
    deadline = derived_clock[0] + timedelta(seconds=10)
    handle = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
        task_id="deadline-publication", descriptor=_derived_descriptor(handler, deadline=deadline))
    await _wait_started(handler, handle.task_id)
    task = registry.get_task(session_id, handle.task_id)
    if published_first:
        handler.finish[handle.task_id].set()
        await asyncio.wait_for(handler.published.wait(), 3)
    derived_clock[0] = deadline + timedelta(seconds=1)
    if published_first:
        recovered, = await registry.recover_derived_thread(session_id, thread_id)
        assert recovered.status == "completed"
    else:
        handler.finish[handle.task_id].set()
        with pytest.raises(TimeoutError):
            await task
        assert registry.get_handle(session_id, handle.task_id).status == "failed"
    assert bool(handler.ready) is published_first


@pytest.mark.asyncio
async def test_derived_queue_is_bounded_and_promotes_same_task_with_total_limit(
    background_case, session_bundle_factory,
):
    store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler()
    registry = handler.registry = registry_factory((handler,))
    owners = [(session_id, thread_id)]
    for index in range(1, 6):
        session = f"ses_019c3df7f8527d568e834acf3c0f66{index:02x}"
        session_bundle_factory(store.sessions_dir, session)
        owners.append((session, get_session_path_resolver(store.sessions_dir).main_thread_id(session)))
    descriptor = _derived_descriptor(handler)
    for index, (session, thread) in enumerate(owners[:5]):
        accepted = await registry.spawn_derived(session_id=session, thread_id=thread, agent_id="agent",
            task_id=f"derived-{index}", descriptor=descriptor)
        assert accepted is not None
    await _wait_started(handler, "derived-0")
    assert handler.calls == ["derived-0"]
    assert all(registry.get_task(session, f"derived-{index}") is None for index, (session, _thread) in enumerate(owners[1:5], 1))
    full = await registry.spawn_derived(session_id=owners[5][0], thread_id=owners[5][1], agent_id="agent",
        task_id="derived-full", descriptor=descriptor)
    assert full is None and registry.get_handle(owners[5][0], "derived-full") is None
    duplicate = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
        task_id="derived-0", descriptor=descriptor)
    assert duplicate.task_id == "derived-0" and registry.get_handle(session_id, "other-trigger") is None
    promoted = await registry.promote_derived(owners[2][0], "derived-2")
    assert promoted.task_id == "derived-2"
    await _wait_started(handler, "derived-2")
    await registry.promote_derived(owners[2][0], "derived-2")
    assert handler.calls == ["derived-0", "derived-2"]
    still_pending = await registry.promote_derived(owners[3][0], "derived-3")
    assert still_pending.status == "pending"
    assert registry.get_task(owners[3][0], "derived-3") is None
    assert still_pending.descriptor.payload["deadline"] == descriptor.payload["deadline"]
    handler.finish["derived-0"].set()
    await _wait_started(handler, "derived-1")
    assert handler.calls == ["derived-0", "derived-2", "derived-1"]
    await registry.cancel(owners[3][0], "derived-3")
    assert registry.get_handle(owners[3][0], "derived-3").status == "cancelled"
    assert registry.residency_blockers(session_id, thread_id) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [False, True])
async def test_derived_restart_only_recovers_accessed_thread_without_upstream_replay(background_case, ready: bool):
    store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler()
    descriptor = _derived_descriptor(handler)
    with store.control(session_id) as control:
        handle = control.create_background_task(task_id="derived-cold", session_id=session_id, thread_id=thread_id,
            agent_id="agent", descriptor=descriptor, checkpoint={"done": False}, preimage_hash="c" * 64)
        handle = control.claim_background_task(handle.task_id, expected_token=0, runtime_generation=0)
    if ready:
        handler.ready[handle.task_id] = BackgroundTaskStep(checkpoint={"done": True}, completed=True,
            result={"candidate_id": "already-published"})
    registry = handler.registry = registry_factory((handler,))
    await registry.start()
    assert handler.calls == [] and registry.get_task(session_id, handle.task_id) is None
    assert registry.get_handle(session_id, handle.task_id).status == "running"
    assert registry.residency_blockers(session_id, thread_id) == ()
    async with registry._holders.lock_for(f"{session_id}:{handle.task_id}"):
        assert await registry.recover_derived_thread(session_id, thread_id) == ()
    recovered, = await registry.recover_derived_thread(session_id, thread_id)
    assert recovered.status == ("completed" if ready else "failed")
    assert recovered.fencing_token > handle.fencing_token
    with pytest.raises(RuntimeError, match="fencing"):
        async with registry.derived_publication(handle):
            pytest.fail("冷恢复后旧 token 仍能发布")
    assert handler.calls == [] and not recovered.external_effect_pending
    assert await registry.recover_derived_thread(session_id, thread_id) == ()
    with store.control(session_id, read_only=True) as control:
        assert control.list_non_terminal_leases() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", [False, True])
async def test_retry_live_holder_only_observes_without_scheduling_replacement(background_case, claimed: bool):
    store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler()
    registry = handler.registry = registry_factory((handler,))
    descriptor = _derived_descriptor(handler)
    task_id = "live-derived"
    preimage_hash = sha256_jcs({"session_id": session_id, "thread_id": thread_id, "agent_id": "agent",
        "descriptor": descriptor.model_dump(mode="json")}).split(":")[-1]
    with store.control(session_id) as control:
        original = control.create_background_task(task_id=task_id, session_id=session_id, thread_id=thread_id,
            agent_id="agent", descriptor=descriptor, checkpoint={"done": False}, preimage_hash=preimage_hash)
        if claimed:
            original = control.claim_background_task(task_id, expected_token=0, runtime_generation=0)
    async with registry._holders.lock_for(f"{session_id}:{task_id}"):
        for foreground in (False, True):
            observed = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
                task_id=task_id, descriptor=descriptor, foreground=foreground)
            assert observed.to_dict() == original.to_dict()
        await registry.promote_derived(session_id, task_id)
        assert registry.get_task(session_id, task_id) is None
        assert not registry._derived_queue and handler.calls == []
    recovered, = await registry.recover_derived_thread(session_id, thread_id)
    assert recovered.status == "failed" and handler.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_first", [False, True])
async def test_derived_shutdown_settles_queued_and_unclaimed_tasks_without_replay(background_case, claim_first: bool):
    store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler()
    registry = handler.registry = registry_factory((handler,))
    handle = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
        task_id="derived-stop", descriptor=_derived_descriptor(handler))
    if claim_first:
        await _wait_started(handler, handle.task_id)
    await registry.stop()
    assert registry.get_handle(session_id, handle.task_id).status == "cancelled"
    assert registry.residency_blockers(session_id, thread_id) == ()
    with store.control(session_id, read_only=True) as control:
        assert control.list_non_terminal_leases() == ()
    with pytest.raises(RuntimeError, match="停止准入"):
        await registry.promote_derived(session_id, handle.task_id)


@pytest.mark.asyncio
async def test_derived_delete_fence_can_cancel_without_reopening_publication(background_case):
    store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler(late_after_cancel=True)
    registry = handler.registry = registry_factory((handler,))
    handle = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
        task_id="derived-delete", descriptor=_derived_descriptor(handler))
    await _wait_started(handler, handle.task_id)
    with store.control(session_id) as control:
        assert control.cas_fence_transition(control.get_fence()[1], "deleting")
    assert (await registry.cancel(session_id, handle.task_id)).status == "cancelled"
    assert handler.ready == {} and handler.rejected.is_set()
    assert handler.abandoned == [(handle.task_id, "cancelled")]
    with store.control(session_id, read_only=True) as control:
        assert control.list_non_terminal_leases() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("published_first", [False, True])
async def test_derived_cancel_and_publication_use_same_fence(background_case, published_first: bool):
    store, session_id, thread_id, _handler, registry_factory = background_case
    handler = DerivedHandler(late_after_cancel=not published_first,
        after_publish=asyncio.Event() if published_first else None)
    registry = handler.registry = registry_factory((handler,))
    handle = await registry.spawn_derived(session_id=session_id, thread_id=thread_id, agent_id="agent",
        task_id="derived-race", descriptor=_derived_descriptor(handler))
    await _wait_started(handler, handle.task_id)
    if published_first:
        handler.finish[handle.task_id].set()
        await asyncio.wait_for(handler.published.wait(), 3)
    cancelled = await registry.cancel(session_id, handle.task_id)
    assert cancelled.status == ("completed" if published_first else "cancelled")
    assert bool(handler.ready) is published_first
    assert handler.rejected.is_set() is not published_first
    assert registry.residency_blockers(session_id, thread_id) == ()
    with store.control(session_id, read_only=True) as control:
        assert control.list_non_terminal_leases() == ()


@pytest.mark.asyncio
async def test_closed_task_history_and_cold_delete_survive_restart(background_case):
    store, session_id, thread_id, handler, registry_factory = background_case
    registry = registry_factory()
    handle = await registry.spawn(session_id=session_id, thread_id=thread_id, agent_id="agent", task_id="bgt_closed",
                                  descriptor=BackgroundTaskDescriptor(kind=handler.kind, version=1, payload={}))
    await handler.started.wait()
    await registry.cancel(session_id, handle.task_id)
    cold = registry_factory()
    assert cold.list_handles(session_id) == []
    assert cold.get_handle(session_id, handle.task_id).status == "cancelled"
    deleted = await cold.delete(session_id, handle.task_id)
    assert deleted.status == "deleted"
    with store.control(session_id, read_only=True) as control:
        assert len(control.list_background_tasks()) == 1
        assert control.list_non_terminal_leases() == ()


@pytest.mark.asyncio
async def test_unsupported_descriptor_is_rejected_before_acceptance(background_case):
    store, session_id, thread_id, _handler, registry_factory = background_case
    registry = registry_factory()
    with pytest.raises(ValueError, match="不具备"):
        await registry.spawn(session_id=session_id, thread_id=thread_id, agent_id="agent", task_id="bgt_bad",
                             descriptor=BackgroundTaskDescriptor(kind="unknown", version=1, payload={}))
    with store.control(session_id, read_only=True) as control:
        assert control.list_background_tasks() == []
        assert control.list_non_terminal_leases() == ()


@pytest.mark.asyncio
async def test_unknown_external_effect_is_not_replayed_or_deleted(background_case):
    store, session_id, thread_id, _handler, registry_factory = background_case
    external = ControlledBackgroundHandler(effect="external")
    registry = registry_factory((external,))
    handle = await registry.spawn(session_id=session_id, thread_id=thread_id, agent_id="agent", task_id="bgt_external",
                                  descriptor=BackgroundTaskDescriptor(kind=external.kind, version=1, payload={}))
    await external.started.wait()
    await registry.stop()
    assert registry.get_handle(session_id, handle.task_id).status == "reconcile_required"
    replacement = ControlledBackgroundHandler(effect="external")
    restarted = registry_factory((replacement,))
    await restarted.start()
    await asyncio.sleep(0)
    assert replacement.calls == 0
    with pytest.raises(RuntimeError, match="不能删除"):
        await restarted.delete(session_id, handle.task_id)
    with store.control(session_id, read_only=True) as control:
        assert len(control.list_non_terminal_leases()) == 1
    with pytest.raises(ValueError, match="尚未确认"):
        await restarted.reconcile(session_id, handle.task_id)
    assert restarted.get_handle(session_id, handle.task_id).external_effect_pending
    replacement.reconciliation = BackgroundTaskStep(checkpoint={"done": True}, completed=True,
        result={"confirmed": True})
    confirmed = await restarted.reconcile(session_id, handle.task_id)
    assert confirmed.status == "completed"
    assert not confirmed.external_effect_pending
    assert replacement.calls == 0
    assert (await restarted.reconcile(session_id, handle.task_id)).to_dict() == confirmed.to_dict()
    with store.control(session_id, read_only=True) as control:
        assert control.list_non_terminal_leases() == ()


@pytest.mark.asyncio
async def test_time_emitter_resumes_exact_checkpoint_without_duplicate_messages(background_case):
    store, session_id, thread_id, _handler, registry_factory = background_case
    first_committed, hold_second = asyncio.Event(), asyncio.Event()

    class PausedTimeEmitter(SystemTimeEmitterHandler):
        async def step(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep:
            if handle.checkpoint["next_index"] == 1:
                first_committed.set()
                await hold_second.wait()
            return await super().step(handle)

    registry = registry_factory((PausedTimeEmitter(),))
    descriptor = BackgroundTaskDescriptor(kind="emit_system_time_messages", version=1, payload={
        "interval_seconds": 0.05, "message_count": 3, "source_id": "clock-stream",
        "started_at": datetime.now(UTC).isoformat(),
    })
    handle = await registry.spawn(session_id=session_id, thread_id=thread_id, agent_id="agent", task_id="bgt_resume", descriptor=descriptor)
    bus = BackgroundMailbox(store=store)
    # 第二次 step 开始前，第一条消息与 checkpoint 已在同一事务提交。
    await asyncio.wait_for(first_committed.wait(), 5)
    messages = await bus.list_messages(session_id, thread_id, "agent")
    assert len(messages) == 1
    await registry.stop()
    assert registry.get_handle(session_id, handle.task_id).checkpoint["next_index"] == 1
    restarted = registry_factory((SystemTimeEmitterHandler(),))
    await restarted.start()
    task = restarted.get_task(session_id, handle.task_id)
    assert task is not None
    await task
    completed = restarted.get_handle(session_id, handle.task_id)
    assert completed.status == "completed"
    assert [message.message_id for message in await bus.list_messages(session_id, thread_id, "agent")] == [f"bgt_resume:message:{index}" for index in range(3)]
    assert completed.metadata["result"]["message_count"] == 3
    assert len(completed.metadata["result"]["messages"]) == 3
    retry = await restarted.spawn(session_id=session_id, thread_id=thread_id, agent_id="agent", task_id="bgt_resume", descriptor=descriptor)
    assert retry.to_dict() == completed.to_dict()

    cold_provider = BackgroundTaskResourceProvider(task_registry=registry_factory())
    listed = await cold_provider.list_resources(session_id)
    assert [item.resource_id for item in listed] == [handle.task_id]
    assert "result" not in listed[0].metadata
    detail = await cold_provider.get_resource(session_id, handle.task_id)
    assert detail.metadata["result"] == completed.metadata["result"]
    assert await cold_provider.list_resources(session_id, include_history=False) == []


@pytest.mark.asyncio
async def test_completed_task_recovers_unsettled_lease_without_reexecution(background_case):
    store, session_id, thread_id, handler, registry_factory = background_case
    with store.control(session_id) as control:
        handle = control.create_background_task(task_id="bgt_unsettled", session_id=session_id,
            thread_id=thread_id, agent_id="agent",
            descriptor=BackgroundTaskDescriptor(kind=handler.kind, version=1, payload={}),
            checkpoint={"done": False}, preimage_hash="a" * 64)
        handle = control.claim_background_task(handle.task_id, expected_token=0, runtime_generation=1)
        handle = control.commit_background_step(handle.task_id, token=handle.fencing_token,
            step=BackgroundTaskStep(checkpoint={"done": True}, completed=True, result={"value": "原结果"}))
        assert len(control.list_non_terminal_leases()) == 1
    restarted = registry_factory()
    await restarted.start()
    assert handler.calls == 0
    assert restarted.get_handle(session_id, handle.task_id).to_dict() == handle.to_dict()
    with store.control(session_id, read_only=True) as control:
        assert control.list_non_terminal_leases() == ()
