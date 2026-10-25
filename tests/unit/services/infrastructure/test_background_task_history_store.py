from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_contracts import (
    BackgroundTaskDescriptor,
    BackgroundTaskStep,
)
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure.background_work_handlers import (
    SystemTimeEmitterHandler,
)
from app.services.infrastructure.background_work_store import BackgroundWorkStore
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from tests.support.background_work import ControlledBackgroundHandler


@pytest.fixture
async def background_case(
    empty_workspace_scope: tuple[str, Path],
    session_bundle_factory,
):
    _, workspace_root = empty_workspace_scope
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
    registry = registry_factory((SystemTimeEmitterHandler(),))
    descriptor = BackgroundTaskDescriptor(kind="emit_system_time_messages", version=1, payload={
        "interval_seconds": 0.05, "message_count": 3, "source_id": "clock-stream",
        "started_at": datetime.now(UTC).isoformat(),
    })
    handle = await registry.spawn(session_id=session_id, thread_id=thread_id, agent_id="agent", task_id="bgt_resume", descriptor=descriptor)
    bus = BackgroundMessageBus(store=store)
    for _ in range(100):
        messages = await bus.list_messages(session_id, thread_id, "agent")
        if len(messages) == 1:
            break
        await asyncio.sleep(0.001)
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
