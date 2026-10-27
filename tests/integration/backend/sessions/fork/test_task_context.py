"""真实 task/Provider 上下文，以及同一委派创建记录的中断恢复。"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock

import pytest

from app.core import thread_creation
from app.core.resource_uri import thread_resource_uri
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.schemas.internal_v2.session import SessionForkRequest, SessionTaskContext
from app.services.business import session_context_fork_service as fork_module
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.orchestration.initial_execution_binding_worker import (
    InitialExecutionBindingWorker,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
    _wait_until,
    _wire_text,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414
)
from tests.support.api_helpers import create_session
from tests.support.e2e_control.runtime_ports import control_request
from tests.support.fork_deadline import ForkDeadlineRegistry

pytestmark = pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)


@pytest.fixture
def deadlines(source_case: RuntimeSourceCase):
    registry = ForkDeadlineRegistry(source_case.artifacts_dir)
    try:
        yield registry
    finally:
        registry.close()


async def _completed_parent(case: RuntimeSourceCase) -> tuple[str, str]:
    session_id = await create_session(case.client, "task 上下文来源")
    await case.plan(session_id, [{"content": "task-history-answer-unique"}])
    accepted = await _send(case, session_id, "task-history-question-unique")
    job = await _job(case, accepted["job_id"])
    return session_id, job["thread_id"]


async def _send_thread(case: RuntimeSourceCase, session_id: str, thread_id: str, content: str):
    response = await case.client.post(f"/api/v1/sessions/{session_id}/threads/{thread_id}/messages", json={
        "message": {"content": content}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _idle_child(case: RuntimeSourceCase, deadlines: ForkDeadlineRegistry, session_id: str, main: str, key: str):
    uri = thread_resource_uri(case.container.session_service.workspace_id, session_id, main)
    deadline = deadlines.for_operation(target=session_id, operation_key=key)
    source = case.container.checkpointer.forks.source
    point = await deadline.wait(source.preflight_fork(
        source_session_id=session_id, source_owner=source.resolve_thread_owner(session_id, main), mode="context_fork",
    ), "public fork source")
    request = SessionForkRequest(
        idempotency_key=key, source_uri=uri, target_container_uri=uri, mode="context_fork",
        source_view_id=point.source_view_id, source_view_revision=point.source_view_revision, anchor_item_id=point.anchor_item_id,
    )
    with _seed_trace({"gateway_id": case.gateway_id, "request_id": None}):
        result = await deadline.wait(case.container.session_context_fork_service.fork(request), "public idle fork")
    deadline.complete()
    with case.container.thread_initial_execution_service._factory.for_owner_session(session_id) as owner:
        record = owner.control_store.get_thread_creation_record(case.container.session_context_fork_service._creation_key(key))
        assert (record.purpose, record.initial_state, record.delegation_id, record.admission_intent) == ("fork", "idle", None, None)
        assert not owner.control_store.list_initial_execution_intents()
        assert not owner.control_store.list_collaboration_members(coordinator_session_id=session_id)
    return result.thread_id


@contextmanager
def _seed_trace(seed):
    gateway = set_current_gateway_id(seed["gateway_id"])
    request = set_current_request_id(seed["request_id"])
    try:
        yield
    finally:
        reset_current_request_id(request)
        reset_current_gateway_id(gateway)


@pytest.mark.parametrize("mode", ["empty", "context_fork"])
async def test_model_task_first_request_uses_selected_history(
    source_case: RuntimeSourceCase, deadlines: ForkDeadlineRegistry, mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    session_id, parent_thread = await _completed_parent(case)
    selection = None
    if mode == "context_fork":
        selection = {"mode": "context_fork"}
    arguments = {"description": "task-private-seed-unique：独立完成检查"}
    if selection is not None:
        arguments["context"] = selection
    case.control.arm_match("task-child", "thread.runtime.acquired", {
        "session_id": session_id, "execution_origin": "internal",
    })
    await case.plan(session_id, [
        _tool_reply("task", arguments, "call_task_context"),
        {"content": "task-parent-dispatched"}, {"content": "task-parent-report-ack"},
    ])
    deadline = deadlines.for_operation(target=f"{session_id}/{parent_thread}", operation_key="model-task")
    accepted = await _send(case, session_id, "task-parent-inflight-unique")
    child = await deadline.wait(asyncio.to_thread(case.control.acknowledged_match, "task-child"), "child published and admitted")
    deadline.complete()
    child_thread = child["details"]["thread_id"]
    try:
        child_call, = await case.plan(session_id, [{"content": "task-child-result-unique"}], thread_id=child_thread)
    finally:
        control_request(json.loads(case.control.environment["BOXTEAM_E2E_TEST_CONTROL"]), {
            "action": "release_match", "barrier_id": "task-child",
        })
    await _job(case, accepted["job_id"])
    factory = case.container.thread_initial_execution_service._factory
    with factory.for_owner_session(session_id) as owner:
        intent, = owner.control_store.list_initial_execution_intents()
        record = owner.control_store.get_thread_creation_record(intent.creation_idempotency_key)
        member = owner.control_store.get_collaboration_member(record.delegation_id)
    await _job(case, intent.job_id)
    await _wait_until(lambda: case.sql(session_id, parent_thread,
        "SELECT COUNT(*) FROM executions WHERE execution_origin='internal' AND outcome='completed'")[0][0] == 1)
    wire = _wire_text(case.request(child_call)["messages"])
    assert "task-private-seed-unique" in wire
    assert ("task-history-question-unique" in wire) == (mode != "empty")
    assert ("task-history-answer-unique" in wire) == (mode == "context_fork")
    assert "task-parent-inflight-unique" not in wire
    assert "call_task_context" not in wire
    assert member.state == "published" and member.child_thread_id == child_thread
    assert record.purpose == "task" and record.initial_state == "running"
    assert (record.request_intent_json is not None) == (mode != "empty")
    assert (record.fork_completion_json is not None) == (mode != "empty")
    assert case.sql(session_id, child_thread,
        "SELECT execution_id,outcome FROM executions WHERE thread_id=?", (child_thread,)) == [
            (intent.execution_binding_id, "completed"),
        ]
    capture = case.container.session_service.path_resolver.catalog_store.get_fork_source_capture(
        creation_key=record.thread_creation_idempotency_key,
    )
    assert (capture is not None) == (mode != "empty")
    storage = case.container.checkpointer._storage.storage
    parent_lines = storage.jsonl_path(session_id, thread_id=parent_thread).read_bytes().splitlines()
    child_lines = set(storage.jsonl_path(session_id, thread_id=child_thread).read_bytes().splitlines())
    historical_lines = [line for line in parent_lines
                        if b"task-history-question-unique" in line or b"task-history-answer-unique" in line]
    assert historical_lines
    assert all((line in child_lines) == (mode == "context_fork") for line in historical_lines)

    # 父线程已经新增 Turn/报告；同调用重试不得再取来源 latest。
    async def unexpected_preflight(**_kwargs):
        raise AssertionError("已冻结 task 重试不得再次 preflight")

    monkeypatch.setattr(case.container.checkpointer.forks.source, "preflight_fork", unexpected_preflight)
    seed, reference = json.loads(record.task_seed), json.loads(record.task_reference)
    kwargs = {"parent_session_id": session_id, "parent_thread_id": parent_thread,
              "parent_agent_id": seed["agent_id"], "parent_job_id": seed["parent_job_id"],
              "parent_tool_call_id": seed["parent_tool_call_id"], "description": seed["description"],
              "subagent_type": seed["subagent_type"]}
    with _seed_trace(seed):
        replay = deadlines.for_operation(target=child_thread, operation_key="task-replay")
        repeated = await replay.wait(case.container.session_subagent_service.delegate(
            **kwargs, context=SessionTaskContext.model_validate(seed["context"])), "task replay")
        replay.complete()
        assert repeated.child_thread_id == child_thread and repeated.frozen_job_id == intent.job_id
        with pytest.raises(RuntimeError, match="冲突|conflict"):
            await case.container.session_subagent_service.delegate(
                **kwargs, context=SessionTaskContext(mode="context_fork" if mode == "empty" else "empty"),
            )
    assert reference["parent_thread_id"] == parent_thread
    with factory.for_owner_session(session_id) as owner:
        assert len(owner.control_store.list_initial_execution_intents()) == 1
        assert len(owner.control_store.list_child_thread_rows()) == 1
    case.write_actual(f"task-context-{mode}", {"child_request": case.request(child_call),
                      "child_thread_id": child_thread, "delegation_id": record.delegation_id,
                      "job_id": intent.job_id})


@pytest.mark.parametrize("phase", ["thread.creation.prepared", "copy.target.prepared", "materialized", "published"])
async def test_task_context_recovers_original_creation_and_initial_intent(
    source_case: RuntimeSourceCase, deadlines: ForkDeadlineRegistry, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    case, container = source_case, source_case.container
    session_id, parent_thread = await _completed_parent(case)
    # 此组只验证创建与真实准入，Job 执行已由上组验证。
    monkeypatch.setattr(container.session_subagent_service, "_bind_owner", AsyncMock())
    forks = container.session_context_fork_service
    delegate = {"parent_session_id": session_id, "parent_thread_id": parent_thread, "parent_agent_id": "default",
                "parent_job_id": "task-recovery-parent", "parent_tool_call_id": "task-recovery-call",
                "description": "task-recovery-seed", "subagent_type": "general-purpose", "context": SessionTaskContext(mode="context_fork")}
    deadline = deadlines.for_operation(target=session_id, operation_key="task-recovery")
    original_materialize = forks._materialize
    original_publish = SessionControlStore.publish_thread_creation_record

    async def stop_at_phase(key, current_phase, **_kwargs):
        if current_phase == phase:
            raise asyncio.CancelledError("模拟创建 owner 中断")

    async def materialize_then_stop(*args, **kwargs):
        await original_materialize(*args, **kwargs)
        raise asyncio.CancelledError("模拟物化后中断")

    def publish_then_stop(control, *args, **kwargs):
        original_publish(control, *args, **kwargs)
        raise asyncio.CancelledError("模拟发布后中断")

    with monkeypatch.context() as failure:
        if phase in {"thread.creation.prepared", "copy.target.prepared"}:
            failure.setattr(thread_creation if phase.startswith("thread") else fork_module, "observe_phase", stop_at_phase)
        elif phase == "materialized":
            failure.setattr(forks, "_materialize", materialize_then_stop)
        else:
            failure.setattr(SessionControlStore, "publish_thread_creation_record", publish_then_stop)
        with pytest.raises(asyncio.CancelledError), _seed_trace({"gateway_id": case.gateway_id, "request_id": None}):
            await deadline.wait(container.session_subagent_service.delegate(**delegate), "injected task interruption")
    factory = container.thread_initial_execution_service._factory
    with factory.for_owner_session(session_id) as owner:
        rows = owner.control_store.connection.execute("SELECT thread_creation_idempotency_key FROM thread_creation_records").fetchall()
        key, = rows[0]
        frozen = owner.control_store.get_thread_creation_record(key)
        assert frozen.state == ("published" if phase == "published" else "preparing")
        assert len(owner.control_store.list_child_thread_rows()) == (1 if phase == "published" else 0)
        assert len(owner.control_store.list_initial_execution_intents()) == (1 if phase == "published" else 0)
    if phase == "materialized":
        await case.plan(session_id, [{"content": "task-recovery-later-answer"}])
        later = await _send(case, session_id, "task-recovery-later-question")
        await _job(case, later["job_id"])

    async def no_new_preflight(**_kwargs):
        raise AssertionError("task 启动恢复不能重选父 Thread latest")

    monkeypatch.setattr(container.checkpointer.forks.source, "preflight_fork", no_new_preflight)
    # 新编排实例读取原持久记录，不依赖首次调用的闭包或内存 intent。
    restarted = SessionContextForkService(
        session_service=container.session_service, checkpointer=container.checkpointer,
        thread_creation_factory=factory, goal_service=container.goal_service,
        terminal_resource_owner=container.terminal_resource_owner, browser_resource_owner=container.browser_resource_owner,
    )
    await deadline.wait(restarted.recover_prepared_forks(), "recover frozen task creation")
    deadline.complete()
    replay = deadlines.for_operation(target=session_id, operation_key="task-recovery-replay")
    await replay.wait(restarted.recover_prepared_forks(), "idempotent recovery")
    replay.complete()
    with factory.for_owner_session(session_id) as owner:
        record = owner.control_store.get_thread_creation_record(key)
        intent, = owner.control_store.list_initial_execution_intents()
        assert (record.child_thread_id, record.request_intent_json, record.preimage_hash) == (
            frozen.child_thread_id, frozen.request_intent_json, frozen.preimage_hash,
        )
        assert record.state == "published" and record.fork_completion_json is not None
        assert owner.control_store.get_collaboration_member(record.delegation_id).state == "published"
        assert len(owner.control_store.list_child_thread_rows()) == 1
        for _ in range(2):
            worker = InitialExecutionBindingWorker(store=owner.control_store, claim_owner="task-test", binder=container.thread_initial_execution_service.binder)
            worker.start()
            try:
                worker.bind_pending_once()
            finally:
                worker.stop()
        assert owner.control_store.get_initial_execution_intent(key).job_id == intent.job_id
        assert owner.control_store.get_initial_execution_intent(key).state == "bound"
    admission = container.checkpointer.execution_admission(session_id, thread_id=record.child_thread_id, execution_id=intent.execution_binding_id)
    assert admission.job_id == intent.job_id
    assert b"task-recovery-later" not in container.checkpointer._storage.storage.jsonl_path(
        session_id, thread_id=record.child_thread_id,
    ).read_bytes()


async def test_child_task_uses_exact_caller_history_and_reports_to_caller(
    source_case: RuntimeSourceCase, deadlines: ForkDeadlineRegistry,
) -> None:
    case = source_case
    session_id, main = await _completed_parent(case)
    parent = await _idle_child(case, deadlines, session_id, main, "task-parent-idle")
    sibling = await _idle_child(case, deadlines, session_id, main, "task-sibling-idle")
    for thread_id, marker in ((parent, "caller-only-history"), (sibling, "sibling-only-history"), (main, "main-later-history")):
        await case.plan(session_id, [{"content": marker + "-answer"}], thread_id=thread_id)
        accepted = await _send_thread(case, session_id, thread_id, marker + "-question")
        await _job(case, accepted["job_id"])
    case.control.arm_match("nested-task-child", "thread.runtime.acquired", {
        "session_id": session_id, "execution_origin": "internal",
    })
    await case.plan(session_id, [
        _tool_reply("task", {"description": "nested-task-private-seed", "context": {"mode": "context_fork"}}, "call_nested_task"),
        {"content": "nested-task-dispatched"}, {"content": "nested-task-report-ack"},
    ], thread_id=parent)
    deadline = deadlines.for_operation(target=parent, operation_key="nested-model-task")
    accepted = await _send_thread(case, session_id, parent, "nested-parent-inflight")
    event = await deadline.wait(asyncio.to_thread(case.control.acknowledged_match, "nested-task-child"), "nested child admitted")
    deadline.complete()
    child = event["details"]["thread_id"]
    try:
        child_call, = await case.plan(session_id, [{"content": "nested-task-result"}], thread_id=child)
    finally:
        control_request(json.loads(case.control.environment["BOXTEAM_E2E_TEST_CONTROL"]), {
            "action": "release_match", "barrier_id": "nested-task-child",
        })
    await _job(case, accepted["job_id"])
    with case.container.thread_initial_execution_service._factory.for_owner_session(session_id) as owner:
        intent, = owner.control_store.list_initial_execution_intents()
        record = owner.control_store.get_thread_creation_record(intent.creation_idempotency_key)
        member = owner.control_store.get_collaboration_member(record.delegation_id)
    await _job(case, intent.job_id)
    await _wait_until(lambda: case.sql(session_id, parent,
        "SELECT COUNT(*) FROM executions WHERE execution_origin='internal' AND outcome='completed'")[0][0] == 1)
    wire = _wire_text(case.request(child_call)["messages"])
    for marker in ("caller-only-history-question", "caller-only-history-answer", "nested-task-private-seed"):
        assert marker in wire
    for marker in ("sibling-only-history", "main-later-history", "nested-parent-inflight", "call_nested_task"):
        assert marker not in wire
    assert member.coordinator_thread_id == parent
    assert json.loads(record.request_intent_json)["source_thread_id"] == parent
    for thread_id in (main, sibling):
        assert not case.sql(session_id, thread_id, "SELECT execution_id FROM executions WHERE execution_origin='internal'")


@pytest.mark.parametrize("phase", ["materialized_error", "publish_error", "delete_live", "delete_abandoned"])
async def test_task_context_abort_and_session_deletion_use_original_owner(
    source_case: RuntimeSourceCase, deadlines: ForkDeadlineRegistry, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    case, container = source_case, source_case.container
    session_id, parent = await _completed_parent(case)
    factory = container.thread_initial_execution_service._factory
    resolver = container.session_service.path_resolver
    monkeypatch.setattr(container.session_subagent_service, "_bind_owner", AsyncMock())
    original_materialize = container.session_context_fork_service._materialize
    prepared, release = asyncio.Event(), asyncio.Event()
    key_holder = []

    async def materialize_then_interrupt(*args, **kwargs):
        await original_materialize(*args, **kwargs)
        key_holder.append(kwargs["child"].thread_creation_idempotency_key)
        prepared.set()
        if phase == "materialized_error":
            raise RuntimeError("injected task materialization error")
        if phase == "delete_abandoned":
            raise asyncio.CancelledError("injected task owner interruption")
        if phase == "delete_live":
            await release.wait()

    def reject_publish(*_args, **_kwargs):
        raise RuntimeError("injected task publication error")

    monkeypatch.setattr(container.session_context_fork_service, "_materialize", materialize_then_interrupt)
    if phase == "publish_error":
        monkeypatch.setattr(SessionControlStore, "publish_thread_creation_record", reject_publish)
    deadline = deadlines.for_operation(target=session_id, operation_key="task-abort")

    async def delegate():
        with _seed_trace({"gateway_id": case.gateway_id, "request_id": None}):
            return await container.session_subagent_service.delegate(
                parent_session_id=session_id, parent_thread_id=parent, parent_agent_id="default",
                parent_job_id="task-abort-parent", parent_tool_call_id="task-abort-call",
                description="task-abort-seed", subagent_type="general-purpose", context=SessionTaskContext(mode="context_fork"),
            )

    source_bytes = container.checkpointer._storage.storage.jsonl_path(session_id, thread_id=parent).read_bytes()
    creation = asyncio.create_task(delegate())
    deleting = None
    with factory.for_owner_session(session_id) as owner:
        try:
            if phase == "delete_live":
                await deadline.wait(prepared.wait(), "materialized task holder")
                deleting = asyncio.create_task(resolver.delete_subtree(idempotency_key="delete-task-owner", root_node_id=session_id))
                await _wait_until(lambda: resolver.catalog_store.get_node(session_id).state == "deleting")
                assert not deleting.done()
                release.set()
                with pytest.raises(SessionDeletionPendingError):
                    await deadline.wait(creation, "task refuses deleting owner")
            elif phase == "delete_abandoned":
                with pytest.raises(asyncio.CancelledError):
                    await deadline.wait(creation, "abandoned task")
                deleting = asyncio.create_task(resolver.delete_subtree(idempotency_key="delete-task-owner", root_node_id=session_id))
            else:
                with pytest.raises(RuntimeError) as failure:
                    await deadline.wait(creation, "task abort")
                if phase == "materialized_error":
                    assert str(failure.value) == "injected task materialization error"
                else:
                    assert "stage=publish" in str(failure.value)
                    assert str(failure.value.__cause__) == "injected task publication error"
                assert container.checkpointer._storage.storage.jsonl_path(session_id, thread_id=parent).read_bytes() == source_bytes
            if deleting is not None:
                result = await deadline.wait(deleting, "delete task owner session")
                assert result.record_state == "completed"
            key, = key_holder
            record = owner.control_store.get_thread_creation_record(key)
            assert record.state == "aborted"
            assert owner.control_store.get_collaboration_member(key).state == "cancelled"
            assert not owner.control_store.list_initial_execution_intents()
            assert not owner.control_store.list_child_thread_rows()
            capture = resolver.catalog_store.get_fork_source_capture(creation_key=key)
            assert capture.state == "aborted" and capture.target_thread_id == record.child_thread_id
            deadline.complete()
        finally:
            release.set()
            for task in (creation, deleting):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (creation, deleting) if task is not None), return_exceptions=True)
            # 创建已经持久中止时，释放 watchdog 才能保留原断言错误；未知状态仍由原期限终结。
            if key_holder and owner.control_store.get_thread_creation_record(key_holder[0]).state == "aborted" and (
                deleting is None or deleting.done()
            ):
                deadline.complete()
