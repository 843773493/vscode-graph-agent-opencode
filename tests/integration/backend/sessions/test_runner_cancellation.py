"""真实 Saver 与 Job.control 的取消、终态和重启 owner 验收。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.graph_binding import compute_capability_profile_hash
from app.container import AppContainer, build_app_container
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import session_thread_owner_scope
from app.core.thread_creation import ThreadCreationService
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.prompting.factory import PromptSection, internal_message_factory
from app.schemas.event import JobStartedEvent, JobStartedPayload
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.job import JobControlRequest
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.reasoning_checkpoint_service import (
    persist_user_message_checkpoint,
)
from app.services.infrastructure.message_stream_store import MessageStreamWriter
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.runtime_service import RuntimeService
from app.services.orchestration.session_title_service import SessionTitleService
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_test_workspace import (
    prepare_rollout_workspace,
)


@dataclass(frozen=True)
class RunnerCase:
    container: AppContainer
    workspace: Path

    @property
    def saver(self) -> RolloutCheckpointSaver:
        return self.container.checkpointer


@pytest.fixture
def runner_case(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> RunnerCase:
    workspace = prepare_rollout_workspace(request)
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("BOXTEAM_HOME", str(context.boxteam_home))
    container = build_app_container(project_root=Path.cwd(), workspace_root=workspace)
    monkeypatch.setattr(
        SessionTitleService,
        "maybe_auto_title_before_first_message",
        AsyncMock(),
    )
    container.job_service._execution_cancel_timeout_seconds = 0.02
    container.job_service._job_startup_timeout_seconds = 10
    container.job_service._job_timeout_seconds = 10
    return RunnerCase(container, workspace)


async def _session(case: RunnerCase) -> tuple[str, str]:
    session = await case.container.session_service.create(
        SessionCreateRequest(title="Runner 取消")
    )
    return (
        session.session_id,
        case.container.session_service.path_resolver.main_thread_id(session.session_id),
    )


def _message(
    origin: str,
) -> tuple[str, str, dict[str, object], InternalExecutionIntent | None]:
    message_id = create_prefixed_id("msg")
    if origin == "user":
        return message_id, "真实准入输入", {}, None
    prepared = internal_message_factory.build(
        kind="generated_session_result",
        control="内部任务已完成",
        sections=(
            PromptSection("control_context", {"source": "runner_test"}),
            PromptSection("generated_session_result", "真实内部输入"),
        ),
        display_content="内部任务已完成。",
    )
    return (
        message_id,
        prepared.content,
        prepared.metadata,
        InternalExecutionIntent(
            message_id=message_id,
            execution_id=create_prefixed_id("exe"),
            admission_idempotency_key=create_prefixed_id("adm"),
        ),
    )


async def _start_job(case: RunnerCase, origin: str, session_id: str, thread_id: str):
    message_id, content, metadata, intent = _message(origin)
    dispatch = await case.container.job_service.start_job(
        session_id,
        content,
        thread_id=thread_id,
        message_id=message_id,
        message_created_at=datetime.now(UTC).isoformat(),
        message_metadata=metadata,
        execution_binding=intent,
    )
    return case.container.job_service._jobs[dispatch.job_id]


def _facts(
    case: RunnerCase, session_id: str, thread_id: str
) -> tuple[list[tuple], list[tuple], int]:
    root = case.saver._storage.storage.root(session_id, thread_id=thread_id)
    with sqlite3.connect(root / "index.sqlite") as connection:
        executions = connection.execute(
            "SELECT execution_id, execution_origin, turn_id, outcome FROM executions"
        ).fetchall()
        turns = connection.execute(
            "SELECT turn_id, status FROM turn_records"
        ).fetchall()
        commits = connection.execute("SELECT COUNT(*) FROM storage_commits").fetchone()[
            0
        ]
    return executions, turns, commits


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["user", "internal"])
@pytest.mark.parametrize("window", ["admission_writer", "admission_readback"])
async def test_job_cancel_waits_for_real_admission_settlement(
    runner_case: RunnerCase,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    window: str,
) -> None:
    session_id, thread_id = await _session(runner_case)
    entered, release = threading.Event(), threading.Event()
    storage = runner_case.saver._storage
    if window == "admission_writer":
        original = storage.storage._append_v2_records_transaction

        def blocked_write(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("测试 Saver writer 屏障未释放")
            return original(*args, **kwargs)

        monkeypatch.setattr(storage.storage, "_append_v2_records_transaction", blocked_write)
    else:
        original = runner_case.saver.execution_admission_for_job

        def blocked_read(*args, **kwargs):
            admission = original(*args, **kwargs)
            if not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("测试 admission 读回屏障未释放")
            return admission

        monkeypatch.setattr(
            runner_case.saver, "execution_admission_for_job", blocked_read
        )
    job = await _start_job(runner_case, origin, session_id, thread_id)
    try:
        assert await asyncio.to_thread(entered.wait, 3), (job.status, job.error_message)
        await runner_case.container.job_service.control(
            job.job_id, JobControlRequest(action="cancel")
        )
        await asyncio.sleep(0.08)
        await runner_case.container.job_service.control(
            job.job_id, JobControlRequest(action="cancel")
        )
        await asyncio.sleep(0.04)
        assert job.status == JobStatus.cancelling
        assert job.task is not None and not job.task.done()
        assert (
            runner_case.container.job_service._thread_current_job[(job.session_id, job.thread_id)]
            == job.job_id
        )
    finally:
        release.set()
        if job.task is not None:
            await asyncio.wait_for(asyncio.shield(job.task), 3)
    assert job.status == JobStatus.cancelled
    admission = runner_case.saver.execution_admission_for_job(
        job.session_id, thread_id=job.thread_id, job_id=job.job_id
    )
    executions, turns, commits = _facts(runner_case, job.session_id, job.thread_id)
    assert executions == [
        (
            admission.execution_id,
            admission.execution_origin,
            admission.turn_id,
            "cancelled",
        )
    ]
    assert turns == ([(admission.turn_id, "cancelled")] if origin == "user" else [])
    assert (job.session_id, job.thread_id) not in runner_case.container.job_service._thread_current_job
    await asyncio.sleep(0.04)
    assert _facts(runner_case, job.session_id, job.thread_id)[2] == commits


@pytest.mark.asyncio
async def test_cancel_waits_for_durable_stream_open_and_closes_returned_writer(
    runner_case: RunnerCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id, thread_id = await _session(runner_case)
    entered, release = asyncio.Event(), asyncio.Event()
    opened: list[MessageStreamWriter] = []
    original = MessageStreamWriter.commit

    async def blocked_commit(writer, event_type, payload=None):
        result = await original(writer, event_type, payload)
        if event_type == "stream.opened":
            opened.append(writer)
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(MessageStreamWriter, "commit", blocked_commit)
    job = await _start_job(runner_case, "user", session_id, thread_id)
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await runner_case.container.job_service.control(
            job.job_id, JobControlRequest(action="cancel")
        )
        await asyncio.sleep(0.08)
        await runner_case.container.job_service.control(
            job.job_id, JobControlRequest(action="cancel")
        )
        assert job.status == JobStatus.cancelling
        assert job.task is not None and not job.task.done()
    finally:
        release.set()
        if job.task is not None:
            await asyncio.wait_for(asyncio.shield(job.task), 3)
    state = await runner_case.container.message_stream_store.get_state(
        opened[0].turn_stream_id
    )
    assert state["stream_status"] == "failed"
    assert job.status == JobStatus.cancelled
    assert _facts(runner_case, job.session_id, job.thread_id)[0][0][3] == "cancelled"
    assert (
        runner_case.container.agent_execution_service.execution_scope_registry.get(
            opened[0].turn_stream_id
        )
        is None
    )


def _admit(
    case: RunnerCase, session_id: str, thread_id: str, origin: str
) -> ExecutionAdmission:
    message_id, content, metadata, intent = _message(origin)
    binding = intent or UserExecutionIdentity(
        job_id=create_prefixed_id("job"),
        turn_id=create_prefixed_id("turn"),
        admission_idempotency_key=create_prefixed_id("adm"),
    )
    job_id = (
        binding.job_id
        if isinstance(binding, UserExecutionIdentity)
        else create_prefixed_id("job")
    )
    if isinstance(binding, UserExecutionIdentity):
        metadata = {**metadata, "turn_id": binding.turn_id, "job_id": job_id}
    with session_thread_owner_scope(
        MutationIntentOwner(session_id=session_id, thread_id=thread_id),
        langgraph_thread_id=thread_id,
    ):
        persist_user_message_checkpoint(
            checkpointer=case.saver,
            session_id=session_id,
            thread_id=thread_id,
            job_id=job_id,
            admission_idempotency_key=binding.admission_idempotency_key,
            execution_intent=intent,
            message=HumanMessage(
                id=message_id,
                content=content,
                response_metadata={
                    "message_id": message_id,
                    "created_at": datetime.now(UTC).isoformat(),
                    "message_metadata": metadata,
                },
            ),
        )
    return case.saver.execution_admission_for_job(
        session_id, thread_id=thread_id, job_id=job_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["user", "internal"])
@pytest.mark.parametrize(
    "status,outcome", [("cancelled", "cancelled"), ("timed_out", "interrupted")]
)
async def test_control_corrects_execution_lost_and_failed_preserves_recovery(frozen_provider_activation,
    frozen_provider_wire, runner_case: RunnerCase,
    origin: str,
    status: str,
    outcome: str,
) -> None:
    session_id, thread_id = await _session(runner_case)
    admission = _admit(runner_case, session_id, thread_id, origin)
    model_call_id = str(uuid4())
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(session_id, thread_id),
        control_state_port=runner_case.saver,
    )
    selected = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())
    with (
        session_thread_owner_scope(
            MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id,
        ),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        prepared = runner_case.saver.prepare_context_for_provider(
            session_id, thread_id,
            turn_id=admission.turn_id, execution_id=admission.execution_id,
            plan_creation_idempotency_key=f"cancellation-create:{model_call_id}",
            seal_idempotency_key=f"cancellation-seal:{model_call_id}",
            context_source_manager=manager, tool_set_supplier=lambda: selected,
            **frozen_provider_wire("runner-cancellation", "chat_completions"),
            **frozen_provider_activation(runner_case.saver, plan_creation_idempotency_key=f'cancellation-create:{model_call_id}', model_call_id=model_call_id),
        )
    assembly_id = str(prepared["assembly_id"])
    runner_case.saver.register_model_call(
        session_id,
        thread_id=thread_id,
        execution_id=admission.execution_id,
        model_call_id=model_call_id,
        assembly_id=assembly_id,
        attempt=1,
        provider="runner-cancellation",
        dispatch_state="dispatched",
    )
    runner_case.saver.mark_execution_lost(
        session_id,
        thread_id=thread_id,
        turn_id=admission.turn_id,
        execution_id=admission.execution_id,
        reason="runner_test",
    )
    assert runner_case.saver.mark_job_terminal_status(
        session_id=session_id,
        thread_id=thread_id,
        job_id=admission.job_id,
        status="failed",
    )
    assert _facts(runner_case, session_id, thread_id)[0][0][3] == "execution_lost"
    assert runner_case.saver.mark_job_terminal_status(
        session_id=session_id,
        thread_id=thread_id,
        job_id=admission.job_id,
        status=status,
    )
    executions, turns, _commits = _facts(runner_case, session_id, thread_id)
    assert executions[0][3] == outcome
    assert turns == ([(admission.turn_id, outcome)] if origin == "user" else [])
    with sqlite3.connect(
        runner_case.saver._storage.storage.root(session_id, thread_id=thread_id)
        / "index.sqlite"
    ) as connection:
        assert connection.execute(
            "SELECT status, outcome FROM context_assemblies WHERE assembly_id = ?",
            (assembly_id,),
        ).fetchone() == ("terminal", outcome)
        assert connection.execute(
            "SELECT outcome FROM model_calls WHERE model_call_id = ?", (model_call_id,)
        ).fetchone() == (outcome,)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["user", "internal"])
@pytest.mark.parametrize("outcome", ["completed", "completed_empty"])
async def test_completed_execution_absorbs_late_job_control(
    runner_case: RunnerCase,
    origin: str,
    outcome: str,
) -> None:
    session_id, thread_id = await _session(runner_case)
    admission = _admit(runner_case, session_id, thread_id, origin)
    final_message_id = create_prefixed_id("msg")
    items = (
        LangChainMessageCodec().items_for_message(
            AIMessage(
                id=final_message_id,
                content="真实最终回复",
                response_metadata={"phase": "final_answer"},
            ),
            item_sequence=1,
            message_id=final_message_id,
            turn_id=admission.turn_id,
            execution_id=admission.execution_id,
            timestamp=datetime.now(UTC).isoformat(),
        )
        if outcome == "completed"
        else ()
    )
    runner_case.saver.converge_execution(
        session_id,
        thread_id=thread_id,
        turn_id=admission.turn_id,
        execution_id=admission.execution_id,
        outcome=outcome,
        turn_status=outcome if origin == "user" else None,
        items=items,
        final_item_id=items[0].item_id if items and origin == "user" else None,
    )
    before = _facts(runner_case, session_id, thread_id)
    for status in ("cancelled", "timed_out", "failed", "execution_lost"):
        assert runner_case.saver.mark_job_terminal_status(
            session_id=session_id,
            thread_id=thread_id,
            job_id=admission.job_id,
            status=status,
        )
    assert _facts(runner_case, session_id, thread_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["user", "internal"])
async def test_cancelled_writer_without_committed_admission_creates_no_facts(
    runner_case: RunnerCase,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    session_id, thread_id = await _session(runner_case)
    entered, release = threading.Event(), threading.Event()

    def rejected_write(*_args, **_kwargs):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("测试拒绝准入屏障未释放")
        raise RuntimeError("真实准入事务未提交")

    monkeypatch.setattr(
        runner_case.saver._storage.storage, "_append_v2_records_transaction", rejected_write
    )
    job = await _start_job(runner_case, origin, session_id, thread_id)
    try:
        assert await asyncio.to_thread(entered.wait, 3), job.error_message
        await runner_case.container.job_service.control(
            job.job_id, JobControlRequest(action="cancel")
        )
        await asyncio.sleep(0.08)
        assert job.status == JobStatus.cancelling
    finally:
        release.set()
        if job.task is not None:
            await asyncio.wait_for(asyncio.shield(job.task), 3)
    assert job.status == JobStatus.cancelled
    assert _facts(runner_case, session_id, thread_id)[:2] == ([], [])


@pytest.mark.asyncio
async def test_restart_rejects_same_job_started_owner_drift(
    runner_case: RunnerCase,
) -> None:
    session_id, thread_id = await _session(runner_case)
    for owner_thread in (thread_id, create_prefixed_id("thr")):
        await runner_case.container.trace_event_store.append(
            session_id,
            JobStartedEvent(
                event_id=create_prefixed_id("evt"),
                job_id="job-owner-conflict",
                timestamp=datetime.now(UTC),
                payload=JobStartedPayload(
                    session_id=session_id, thread_id=owner_thread
                ),
            ),
        )
    with pytest.raises(RuntimeError, match="started owner 漂移"):
        await runner_case.container.runtime_service.reconcile_stale_executions()


@pytest.mark.asyncio
async def test_restart_rejects_started_trace_missing_owner(
    runner_case: RunnerCase,
) -> None:
    session_id, _thread_id = await _session(runner_case)
    trace_file = (
        runner_case.container.session_service.path_resolver.resolve_session_node(
            session_id
        )
        / "logs"
        / "traces"
        / "events.jsonl"
    )
    trace_file.parent.mkdir(parents=True, exist_ok=True)
    trace_file.write_text(
        json.dumps(
            {
                "event_id": create_prefixed_id("evt"),
                "job_id": "job-missing-owner",
                "timestamp": datetime.now(UTC).isoformat(),
                "type": "job_started",
                "payload": {"session_id": session_id},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="Trace 事件协议无效"):
        await runner_case.container.runtime_service.reconcile_stale_executions()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["user", "internal"])
async def test_failed_before_admission_does_not_create_execution_or_turn(
    runner_case: RunnerCase,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    session_id, thread_id = await _session(runner_case)

    def reject(*_args, **_kwargs):
        raise RuntimeError("明确拒绝准入")

    monkeypatch.setattr(
        runner_case.saver,
        "accept_turn" if origin == "user" else "admit_internal_execution",
        reject,
    )
    job = await _start_job(runner_case, origin, session_id, thread_id)
    assert job.task is not None
    await asyncio.wait_for(asyncio.shield(job.task), 3)
    assert job.status == JobStatus.failed
    assert _facts(runner_case, job.session_id, job.thread_id)[:2] == ([], [])


@pytest.mark.asyncio
async def test_restart_converges_real_child_job_using_started_owner(
    runner_case: RunnerCase,
) -> None:
    session_id, main_thread_id = await _session(runner_case)
    sessions_root = runner_case.workspace / ".boxteam" / "sessions"
    control = SessionControlStore(
        get_session_path_resolver(sessions_root).resolve_session_node(session_id)
        / "session-control.sqlite"
    )
    profile = {"thread_role": "child", "goal_enabled": False}
    graph = {
        "graph_id": "deep-agent",
        "graph_revision": 1,
        "graph_schema_hash": "sha256:" + "a" * 64,
        "capability_profile_hash": compute_capability_profile_hash(
            capability_profile=profile
        ),
    }
    try:
        child = await ThreadCreationService(
            store=SessionCatalogStore(
                runner_case.workspace
                / ".boxteam"
                / "navigation"
                / "session-catalog.sqlite",
                sessions_root,
            ),
            control_store=control,
            sessions_root=sessions_root,
            workspace_id=runner_case.container.workspace_service.workspace_id,
            compute_capability_profile_hash=compute_capability_profile_hash,
        ).create(
            idempotency_key="runner-child-restart",
            session_id=session_id,
            thread_id=None,
            delegation_id=None,
            initial_state="idle",
            session_metadata={
                "graph_binding": graph,
                "capability_profile": profile,
                "task_seed": None,
                "task_reference": None,
            },
            artifact_manifest={},
        )
    finally:
        control.close()
    assert child.child_thread_id != main_thread_id
    admission = _admit(runner_case, session_id, child.child_thread_id, "internal")
    await runner_case.container.trace_event_store.append(
        session_id,
        JobStartedEvent(
            event_id=create_prefixed_id("evt"),
            job_id=admission.job_id,
            timestamp=datetime.now(UTC),
            payload=JobStartedPayload(
                session_id=session_id, thread_id=child.child_thread_id
            ),
        ),
    )
    restarted = RolloutCheckpointSaver(sessions_root)
    runtime = RuntimeService(
        workspace_id=runner_case.container.workspace_service.workspace_id,
        workspace_root=runner_case.workspace,
        job_service=runner_case.container.job_service,
        background_task_registry=runner_case.container.background_task_registry,
        trace_event_store=runner_case.container.trace_event_store,
        message_stream_store=runner_case.container.message_stream_store,
        terminal_status_writer=restarted,
    )
    assert await runtime.reconcile_stale_executions() == 1
    assert _facts(runner_case, session_id, child.child_thread_id)[0] == [
        (admission.execution_id, "internal", None, "execution_lost")
    ]
    assert _facts(runner_case, session_id, child.child_thread_id)[1] == []
