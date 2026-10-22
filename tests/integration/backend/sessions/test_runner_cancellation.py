"""真实 Saver 与 Job.control 的取消、终态和重启 owner 验收。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph.message import add_messages

from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.agents.graph_binding import compute_capability_profile_hash
from app.agents.tools.custom_invocation import (
    create_extension_tool_invoker_tool,
    seal_extension_catalog_binding_from_tools,
)
from app.container import AppContainer, build_app_container
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver, workspace_context
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import session_thread_owner_scope
from app.core.thread_creation import ThreadCreationService
from app.core.trace_middleware import get_current_gateway_id, get_current_request_id
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.prompting.factory import PromptSection, internal_message_factory
from app.schemas.event import JobStartedEvent, JobStartedPayload
from app.schemas.internal_v2.common import JobStatus, MessageRole
from app.schemas.internal_v2.job import JobControlRequest
from app.schemas.internal_v2.message import MessageDTO
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.reasoning_checkpoint_service import (
    persist_user_message_checkpoint,
)
from app.services.infrastructure.message_stream_store import (
    RECOVERED_TOOL_RESULT_UNKNOWN,
    MessageStreamStore,
    MessageStreamWriter,
)
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
from app.services.orchestration.execution_step.stream_bindings import CanonicalItemSink
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime
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
    binding = intent or case.container.job_service.reserve_user_execution_identity()
    created_at = datetime.now(UTC)
    if isinstance(binding, UserExecutionIdentity):
        queued_message = MessageDTO(
                message_id=message_id, session_id=session_id, thread_id=thread_id,
                role=MessageRole.user, content=content, metadata=metadata,
                created_at=created_at, updated_at=created_at,
            )
        case.container.message_service.accept_user_message(
            queued_message,
            execution_identity=binding, queue_input=ExecutionQueueInput(
            execution_binding=binding, message_id=queued_message.message_id, agent_id="default",
            provider_id=None, delivery_policy="after_turn", gateway_id=get_current_gateway_id(),
            request_id=get_current_request_id(),
            created_at=queued_message.created_at.isoformat(),
        ),
        )
    dispatch = await case.container.job_service.start_job(
        session_id,
        content,
        thread_id=thread_id,
        message_id=message_id,
        message_created_at=created_at.isoformat(),
        message_metadata=metadata,
        execution_binding=binding,
    )
    return case.container.job_service._jobs[dispatch.job_id]


def _facts(
    case: RunnerCase, session_id: str, thread_id: str
) -> tuple[list[tuple], list[tuple], int]:
    root = case.saver._storage.storage.root(session_id, thread_id=thread_id)
    database_path = root / "index.sqlite"
    if not database_path.exists() and not database_path.is_symlink():
        return [], [], 0
    if database_path.is_symlink() or not database_path.is_file():
        raise RuntimeError(f"测试 rollout index 必须是普通文件：{database_path}")
    with sqlite3.connect(database_path) as connection:
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
@pytest.mark.parametrize("window", ["input_consumption", "admission_readback"])
async def test_job_cancel_waits_for_real_admission_settlement(
    runner_case: RunnerCase,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    window: str,
) -> None:
    session_id, thread_id = await _session(runner_case)
    entered, release = threading.Event(), threading.Event()
    if window == "input_consumption":
        from app.services.orchestration.execution_step import runner

        original = runner.persist_user_message_checkpoint

        def blocked_write(*args, **kwargs):
            if threading.current_thread() is not threading.main_thread() and not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("测试 Saver writer 屏障未释放")
            return original(*args, **kwargs)

        monkeypatch.setattr(runner, "persist_user_message_checkpoint", blocked_write)
    else:
        from app.services.orchestration.execution_step import runner

        admission_written = threading.Event()
        original_write = runner.persist_user_message_checkpoint

        def mark_admission_written(*args, **kwargs):
            result = original_write(*args, **kwargs)
            admission_written.set()
            return result

        monkeypatch.setattr(
            runner, "persist_user_message_checkpoint", mark_admission_written
        )
        original = runner_case.saver.execution_admission_for_job

        def blocked_read(*args, **kwargs):
            if (
                admission_written.is_set()
                and threading.current_thread() is not threading.main_thread()
                and not entered.is_set()
            ):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("测试 admission 读回屏障未释放")
            return original(*args, **kwargs)

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
    created_at = datetime.now(UTC)
    if isinstance(binding, UserExecutionIdentity):
        queued_message = MessageDTO(
                message_id=message_id, session_id=session_id, thread_id=thread_id,
                role=MessageRole.user, content=content, metadata=metadata,
                created_at=created_at, updated_at=created_at,
            )
        case.container.message_service.accept_user_message(
            queued_message,
            execution_identity=binding, queue_input=ExecutionQueueInput(
            execution_binding=binding, message_id=queued_message.message_id, agent_id="default",
            provider_id=None, delivery_policy="after_turn", gateway_id=None, request_id=None,
            created_at=queued_message.created_at.isoformat(),
        ),
        )
        human = case.saver.accepted_user_message(
            session_id, thread_id=thread_id, job_id=job_id, message_id=message_id,
        )
    else:
        human = HumanMessage(
            id=message_id, content=content,
            response_metadata={
                "message_id": message_id, "created_at": created_at.isoformat(),
                "message_metadata": metadata,
            },
        )
        case.saver.admit_internal_execution(
            session_id, thread_id=thread_id, job_id=job_id, intent=binding, message=human,
            queue_input=ExecutionQueueInput(
                execution_binding=binding, message_id=message_id, agent_id="default",
                provider_id=None, delivery_policy="after_turn", gateway_id=None, request_id=None,
                created_at=created_at.isoformat(),
            ),
        )
        human = case.saver.accepted_internal_message(session_id, thread_id=thread_id, job_id=job_id, message_id=message_id, intent=binding)
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
            message=human,
        )
    return case.saver.execution_admission_for_job(
        session_id, thread_id=thread_id, job_id=job_id
    )


async def _settle_local_recovery_claim(case: RunnerCase, admission: ExecutionAdmission) -> None:
    """局部恢复 fixture 领取并启动原准入；无活 writer 时释放 holder，进程退出另有专项。"""
    store = case.container.pending_request_store
    record = await store.record(admission.session_id, admission.thread_id, admission.job_id)
    claim = await store.claim(record)
    assert claim is not None
    try:
        await claim.start()
    finally:
        await claim.close()


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
    frozen_provider_activation,
    frozen_provider_wire,
    origin: str,
    outcome: str,
) -> None:
    session_id, thread_id = await _session(runner_case)
    admission = _admit(runner_case, session_id, thread_id, origin)
    model_call_id = str(uuid4())
    plan_creation_idempotency_key = f"cancellation-terminal:{model_call_id}"
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
            session_id,
            thread_id,
            turn_id=admission.turn_id,
            execution_id=admission.execution_id,
            plan_creation_idempotency_key=plan_creation_idempotency_key,
            seal_idempotency_key=f"cancellation-terminal-seal:{model_call_id}",
            context_source_manager=manager,
            tool_set_supplier=lambda: selected,
            **frozen_provider_wire("runner-cancellation", "chat_completions"),
            **frozen_provider_activation(
                runner_case.saver,
                plan_creation_idempotency_key=plan_creation_idempotency_key,
                model_call_id=model_call_id,
            ),
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
        assembly_id=assembly_id,
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
async def test_cancelled_runtime_input_failure_preserves_only_accepted_facts(
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

    from app.services.orchestration.execution_step import runner

    monkeypatch.setattr(runner, "persist_user_message_checkpoint", rejected_write)
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
    executions, turns, _ = _facts(runner_case, session_id, thread_id)
    assert executions[0][3] == "cancelled"
    if origin == "user":
        assert turns == [(job.execution_binding.turn_id, "cancelled")]
        assert runner_case.saver.accepted_user_message(
            session_id, thread_id=thread_id, job_id=job.job_id, message_id=job.message_id,
        ).content == "真实准入输入"
    else:
        assert turns == []
        assert runner_case.saver.accepted_internal_message(
            session_id, thread_id=thread_id, job_id=job.job_id, message_id=job.message_id, intent=job.execution_binding,
        ).content == job.message


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
    with pytest.raises(RuntimeError, match="明确拒绝准入"):
        await _start_job(runner_case, origin, session_id, thread_id)
    assert runner_case.container.job_service._jobs == {}
    assert _facts(runner_case, session_id, thread_id)[:2] == ([], [])


@pytest.mark.asyncio
async def test_restart_restores_original_child_before_provider_dispatch(
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
        pending_tool_checkpoint_id=runner_case.container.agent_execution_service.pending_tool_checkpoint_id,
        pending_request_store=runner_case.container.pending_request_store,
    )
    await _settle_local_recovery_claim(runner_case, admission)
    try:
        assert await runtime.reconcile_stale_executions() == 0
        restored = runner_case.container.job_service._jobs[admission.job_id]
        assert (restored.session_id, restored.thread_id) == (session_id, child.child_thread_id)
        assert restored.execution_binding.execution_id == admission.execution_id
        assert restored.queue_claim is not None and restored.task is None
        assert _facts(runner_case, session_id, child.child_thread_id)[0] == [
            (admission.execution_id, "internal", None, "unknown")
        ]
        assert _facts(runner_case, session_id, child.child_thread_id)[1] == []
    finally:
        with workspace_context(
            runner_case.container.workspace_id, runner_case.workspace,
            session_catalog=runner_case.container.session_catalog,
        ):
            await runner_case.container.job_service.force_interrupt_active(reason="test_recovery_teardown")
            await runner_case.container.job_service.drain_finalizations()


@pytest.fixture
async def started_tool_attempt(
    runner_case: RunnerCase, frozen_provider_activation, frozen_provider_wire,
    registered_tool_set_producer_factory,
):
    """真实准入、prepare、双 carrier checkpoint 与持久 tool.started。"""
    session_id, thread_id = await _session(runner_case)
    admission = _admit(runner_case, session_id, thread_id, "user")
    await _settle_local_recovery_claim(runner_case, admission)
    assert admission.turn_id is not None
    owner = MutationIntentOwner(session_id, thread_id)
    saver = runner_case.saver
    provider_id, tool_name = "recovery-original-call", "invoke_extension_tool"
    arguments = {"tool_name": "task", "arguments": {}}
    provider_parameters = frozen_provider_wire("history-fixture", "chat_completions")
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        manager = ContextSourceManager(
            owner=ContextSourceOwnerKey(session_id, thread_id), control_state_port=saver,
        )
        envelope = create_extension_tool_invoker_tool(
            [], catalog_binding_resolver=seal_extension_catalog_binding_from_tools([]),
        )
        producer = registered_tool_set_producer_factory([envelope])
        producer.freeze_structured_output_tools({})
        selected = producer.snapshot()
        assert selected.tool_names == (tool_name,)
        prepared = saver.prepare_context_for_provider(
            session_id, thread_id, turn_id=admission.turn_id, execution_id=admission.execution_id,
            plan_creation_idempotency_key="tool-recovery-original", seal_idempotency_key="tool-recovery-original-seal",
            context_source_manager=manager, tool_set_supplier=lambda: selected,
            **provider_parameters,
            **frozen_provider_activation(saver, plan_creation_idempotency_key="tool-recovery-original"),
        )
        assert prepared["tools"] == list(selected.tools)
        snapshot = prepared["snapshot"]

        async def register(model_call_id: str, attempt: int, _model: str) -> str:
            saver.register_model_call(
                session_id, thread_id=thread_id, execution_id=admission.execution_id,
                model_call_id=model_call_id, assembly_id=snapshot.assembly_id,
                attempt=attempt, provider="history-fixture", dispatch_state="dispatched",
            )
            return admission.execution_id

        writer = await runner_case.container.message_stream_store.open(
            session_id=session_id, thread_id=thread_id, turn_id=admission.turn_id,
            job_id=admission.job_id,
        )
        sink = CanonicalItemSink(
            saver, session_id=session_id, thread_id=thread_id, checkpoint_ns="",
        )
        stream = MessageStreamRuntime(
            writer, canonical_item_sink=sink.append,
            tool_result_sink=sink.complete_tool_result,
            canonical_turn_id=admission.turn_id, model_call_registrar=register,
        )
        await stream.start_model(snapshot.model_call_id, "history-fixture")
        await stream.accept_message_chunk(AIMessageChunk(
            content="", tool_call_chunks=[{
                "id": provider_id, "name": tool_name,
                "args": json.dumps(arguments), "index": 0,
            }],
        ), model_call_id=snapshot.model_call_id)
        await stream.prepare_tool_execution(snapshot.model_call_id)
        root, = (
            item for item in saver._storage.storage.read_items(session_id, thread_id)
            if item.semantic_kind == "user_input"
        )
        codec = LangChainMessageCodec()
        user = codec.from_dict(codec.project_message((root,)))
        assert root.turn_id == admission.turn_id
        assert isinstance(user, HumanMessage)
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": [
            user,
            AIMessage(
                content="", id="recovery-checkpoint-call",
                tool_calls=[{"id": provider_id, "name": tool_name, "args": arguments}],
                response_metadata={"model_call_id": snapshot.model_call_id},
            ),
        ]}
        checkpoint["channel_versions"] = {"messages": "2"}
        checkpoint["updated_channels"] = ["messages"]
        fixed = saver.put(build_checkpoint_config(thread_id), checkpoint, {"source": "unit"}, {"messages": "2"})
        frozen = saver.get_tuple(fixed).checkpoint
        call_id = stream.claim_tool_call_id(snapshot.model_call_id, provider_id)
        assert call_id == f"{snapshot.model_call_id}:tool-call:{provider_id}"
        attempt_id = str(uuid4())
        await stream.start_tool(tool_execution_id=attempt_id, tool_call_id=call_id, tool_name="task")
    await runner_case.container.trace_event_store.append(session_id, JobStartedEvent(
        event_id=create_prefixed_id("evt"), job_id=admission.job_id, timestamp=datetime.now(UTC),
        payload=JobStartedPayload(session_id=session_id, thread_id=thread_id),
    ))
    completion = ToolResultCompletion(
        model_call_id=snapshot.model_call_id, tool_call_id=call_id,
        tool_invocation_id=f"tool-invocation:{snapshot.model_call_id}:{provider_id}",
        tool_attempt_id=attempt_id, tool_name="task",
        content=RECOVERED_TOOL_RESULT_UNKNOWN, tool_outcome="unknown",
    )
    return admission, completion, writer.turn_stream_id, fixed, frozen, selected, provider_parameters, stream


@pytest.mark.asyncio
@pytest.mark.parametrize("committed_outcome", [
    None, "success", "failure", "cancelled", "unknown",
    "request_success", "request_failure", "request_cancelled", "request_unknown", "request_structured",
])
async def test_restart_closes_original_tool_attempt_before_internal_prepare(
    runner_case: RunnerCase, started_tool_attempt,
    frozen_provider_activation,
    committed_outcome: str | None,
) -> None:
    """冷恢复不重放工具、不覆盖真终态，后继 internal 的真实 prepare 配对闭合。"""
    admission, completion, stream_id, fixed, frozen, selected, provider_parameters, _stream = started_tool_attempt
    session_id, thread_id = admission.session_id, admission.thread_id
    sessions_root = runner_case.workspace / ".boxteam" / "sessions"
    request_first = committed_outcome is not None and committed_outcome.startswith("request_")
    outcome = committed_outcome.removeprefix("request_") if request_first else committed_outcome or "unknown"
    structured = outcome == "structured"
    outcome = "success" if structured else outcome
    content = RECOVERED_TOOL_RESULT_UNKNOWN if committed_outcome is None else f"原工具终态：{outcome}"
    projected_content = content
    if structured:
        content = "原始第一块原始第二块"
        projected_content = [{"type": "text", "text": "原始第一块"}, {"type": "text", "text": "原始第二块"}]
    carrier = None
    if request_first:
        # 真实 checkpoint writer 可先于外层 on_tool_end/下一次请求边界落盘。
        # A 已来自原 tool.started；P 是独立的 LangGraph message ID。
        message = ToolMessage(
            id="recovery-request-result", tool_call_id="recovery-original-call", name="task",
            content=projected_content, status="success" if outcome == "success" else "error",
            response_metadata={"tool_outcome": outcome, "execution_confirmed": outcome != "unknown"},
        )
        assert message.id != completion.tool_attempt_id
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": [*frozen["channel_values"]["messages"], message]}
        checkpoint["channel_versions"] = {"messages": "3"}
        checkpoint["updated_channels"] = ["messages"]
        with (
            session_thread_owner_scope(MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id),
            execution_admission_scope(admission, langgraph_thread_id=thread_id),
        ):
            runner_case.saver.put(fixed, checkpoint, {"source": "loop"}, {"messages": "3"})
        carrier, = [item for item in runner_case.saver._storage.storage.read_items(session_id, thread_id)
                    if item.semantic_kind == "tool_result"]
        assert carrier.item_id == f"item-{message.id}"
        assert carrier.projection_identity.projection_message_id == message.id
        assert "tool_attempt_id" not in carrier.payload
        original_events = await runner_case.container.message_stream_store.list_events(
            session_id=session_id, turn_stream_id=stream_id,
        )
        assert [event["tool_attempt_id"] for event in original_events if event["type"] == "tool.started"] == [completion.tool_attempt_id]
        assert not [event for event in original_events if event["type"] == "tool.completed"]
        committed = None
    elif committed_outcome is not None:
        # 模拟 canonical 已提交、tool.completed 镜像尚未写入时崩溃。
        committed = runner_case.saver.commit_tool_result(
            session_id=session_id, thread_id=thread_id, job_id=admission.job_id,
            completion=replace(completion, content=content, tool_outcome=outcome),
        )
    else:
        committed = None
    jsonl_path = runner_case.saver._storage.storage.jsonl_path(session_id, thread_id=thread_id)
    original_bytes = jsonl_path.read_bytes()
    restarted = RolloutCheckpointSaver(sessions_root)
    cold_streams = MessageStreamStore(
        path_resolver=get_session_path_resolver(sessions_root),
        workspace_id=runner_case.container.workspace_service.workspace_id,
    )
    runtime = RuntimeService(
        workspace_id=runner_case.container.workspace_service.workspace_id,
        workspace_root=runner_case.workspace,
        job_service=runner_case.container.job_service,
        background_task_registry=runner_case.container.background_task_registry,
        trace_event_store=runner_case.container.trace_event_store,
        message_stream_store=cold_streams, terminal_status_writer=restarted,
        pending_tool_checkpoint_id=runner_case.container.agent_execution_service.pending_tool_checkpoint_id,
        pending_request_store=runner_case.container.pending_request_store,
    )
    assert await runtime.reconcile_stale_executions() == 2
    terminals = [item for item in restarted._storage.storage.read_items(session_id, thread_id)
                 if item.semantic_kind == "tool_result"]
    assert len(terminals) == (2 if carrier is not None else 1)
    terminal, = [item for item in terminals if item.item_id == completion.item_id]
    if carrier is not None:
        restored_carrier, = [item for item in terminals if item.item_id != completion.item_id]
        assert restored_carrier.to_dict() == carrier.to_dict()
    assert terminal.item_id == completion.item_id
    assert terminal.turn_id == admission.turn_id
    assert terminal.projection_identity.model_call_id == completion.model_call_id
    assert terminal.producer_ref == {
        "producer_kind": "tool", "producer_id": completion.tool_attempt_id,
        "invocation_id": completion.model_call_id,
    }
    assert terminal.payload == {
        "tool_call_id": completion.tool_call_id,
        "tool_invocation_id": completion.tool_invocation_id,
        "tool_attempt_id": completion.tool_attempt_id,
        "result_id": completion.tool_attempt_id, "name": "invoke_extension_tool",
        "content": content, "tool_outcome": outcome,
        "execution_confirmed": outcome != "unknown",
    }
    if committed is not None:
        assert terminal.to_dict() == committed.to_dict()
        assert jsonl_path.read_bytes() == original_bytes
    else:
        assert jsonl_path.read_bytes().startswith(original_bytes)
    with restarted._storage.storage.read_connection(session_id, thread_id) as connection:
        assert connection.execute(
            "SELECT execution_id,turn_id FROM item_catalog WHERE item_id=?", (terminal.item_id,),
        ).fetchone() == (admission.execution_id, admission.turn_id)
        assert connection.execute(
            "SELECT outcome FROM model_calls WHERE model_call_id=?", (completion.model_call_id,),
        ).fetchone() == ("unknown",)
    assert _facts(runner_case, session_id, thread_id)[0][0][3] == "execution_lost"
    events = await cold_streams.list_events(session_id=session_id, turn_stream_id=stream_id)
    completed = [event for event in events if event["type"] == "tool.completed"]
    assert len(completed) == 1
    event, = completed
    assert event["event_id"] == f"tool-terminal:{terminal.item_id}"
    assert event["tool_attempt_id"] == completion.tool_attempt_id
    assert event["payload"]["result"] == content
    assert event["payload"]["outcome"] == (
        "outcome_unknown" if outcome == "unknown" else "success" if outcome == "success" else "provider_error"
    )
    assert event["payload"]["completion_reason"] == (
        "execution_lost" if outcome == "unknown" else "user_interrupt" if outcome == "cancelled" else "tool_completed"
    )
    assert event["event_seq"] < events[-1]["event_seq"]
    assert events[-1]["type"] == "stream.failed"
    recovered_bytes = jsonl_path.read_bytes()
    assert await runtime.reconcile_stale_executions() == 0
    assert jsonl_path.read_bytes() == recovered_bytes
    reopened = await cold_streams.open_existing(
        session_id=session_id, thread_id=thread_id,
        turn_id=admission.turn_id, turn_stream_id=stream_id,
    )
    assert reopened.turn_stream_id == stream_id
    assert await cold_streams.list_events(session_id=session_id, turn_stream_id=stream_id) == events

    following = _admit(runner_case, session_id, thread_id, "internal")
    assert following.turn_id is None and following.execution_id != admission.execution_id
    cold = RolloutCheckpointSaver(sessions_root)
    with (
        session_thread_owner_scope(MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id),
        execution_admission_scope(following, langgraph_thread_id=thread_id),
    ):
        restored = cold.get_tuple(build_checkpoint_config(thread_id))
        assert restored is not None
        restored_messages = restored.checkpoint["channel_values"]["messages"]
        results = [message for message in restored_messages if isinstance(message, ToolMessage)]
        assert len(results) == 1
        assert results[0].content == projected_content
        assert results[0].tool_call_id == "recovery-original-call"
        assert results[0].status == ("success" if outcome == "success" else "error")
        assert results[0].response_metadata["execution_confirmed"] is (outcome != "unknown")
        with cold._storage.storage.read_connection(session_id, thread_id) as connection:
            (notice_id,), = connection.execute(
                "SELECT item_id FROM item_catalog WHERE execution_id=? AND semantic_kind='runtime_notice'",
                (following.execution_id,),
            ).fetchall()
            notice, = cold._storage.storage.read_items(
                session_id, thread_id, item_ids=(notice_id,), connection=connection,
            )
        message_id = notice.producer_ref["producer_id"]
        internal_input = cold.accepted_internal_message(
            session_id, thread_id=thread_id, job_id=following.job_id, message_id=message_id,
            intent=InternalExecutionIntent(
                message_id=message_id, execution_id=following.execution_id,
                admission_idempotency_key=following.admission_idempotency_key,
            ),
        )
        # 与 graph 的消息 reducer 一致：先恢复 checkpoint，再合入本次已接受输入。
        request_messages = tuple(add_messages(restored_messages, [internal_input]))
        frozen_request_messages = [message.model_dump(mode="json") for message in request_messages]
        assert [message.type for message in request_messages] == ["human", "ai", "tool", "human"]
        assert request_messages[-1].id == message_id
        manager = ContextSourceManager(
            owner=ContextSourceOwnerKey(session_id, thread_id), control_state_port=cold,
        )
        prepare_key = "tool-recovery-following-internal"
        activation = frozen_provider_activation(cold, plan_creation_idempotency_key=prepare_key)
        for _ in range(2):
            prepared = cold.prepare_context_for_provider(
                session_id, thread_id, turn_id=None, execution_id=following.execution_id,
                plan_creation_idempotency_key=prepare_key, seal_idempotency_key=f"seal:{prepare_key}",
                request_messages=request_messages,
                context_source_manager=manager, tool_set_supplier=lambda: selected,
                **provider_parameters, **activation,
            )
            snapshot = prepared["snapshot"]
            assert (snapshot.session_id, snapshot.thread_id, snapshot.execution_id, snapshot.turn_id) == (
                session_id, thread_id, following.execution_id, None,
            )
            assert snapshot.model_call_id != completion.model_call_id
            assert snapshot.provider_version == provider_parameters["provider_version"]
            assert snapshot.target_format == provider_parameters["target_format"]
            assert prepared["tools"] == list(selected.tools)
            prepared_results = [message for message in prepared["messages"] if isinstance(message, ToolMessage)]
            assert len(prepared_results) == 1
            assert prepared_results[0].content == projected_content
            assert prepared_results[0].response_metadata["tool_outcome"] == outcome
            assert [message.model_dump(mode="json") for message in request_messages] == frozen_request_messages
        recovered_terminals = [item for item in cold._storage.storage.read_items(session_id, thread_id)
                               if item.semantic_kind == "tool_result"]
        assert [item.to_dict() for item in recovered_terminals] == [item.to_dict() for item in terminals]
    with session_thread_owner_scope(MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id):
        assert cold.get_tuple(fixed).checkpoint == frozen
    assert jsonl_path.read_bytes().startswith(recovered_bytes)


@pytest.mark.asyncio
async def test_request_tool_message_commits_only_the_real_started_attempt(
    runner_case: RunnerCase, started_tool_attempt,
) -> None:
    """请求先到仍先提交原 A，P 随 checkpoint 独立保留，迟到 end 不造第二 attempt。"""
    admission, completion, stream_id, fixed, frozen, _selected, _parameters, stream = started_tool_attempt
    session_id, thread_id = admission.session_id, admission.thread_id
    message = ToolMessage(
        id="request-before-tool-end", tool_call_id="recovery-original-call",
        name="task", content="请求边界的真实结果", status="success",
    )
    with (
        session_thread_owner_scope(MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        await stream.complete_tool_from_message(message, model_call_id=completion.model_call_id)
        actual, = [item for item in runner_case.saver._storage.storage.read_items(session_id, thread_id)
                   if item.semantic_kind == "tool_result"]
        assert actual.item_id == completion.item_id
        assert actual.payload["tool_attempt_id"] == completion.tool_attempt_id
        assert actual.payload["content"] == message.content
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": [*frozen["channel_values"]["messages"], message]}
        checkpoint["channel_versions"] = {"messages": "3"}
        checkpoint["updated_channels"] = ["messages"]
        runner_case.saver.put(fixed, checkpoint, {"source": "loop"}, {"messages": "3"})
        await stream.complete_tool(
            tool_execution_id=completion.tool_attempt_id, tool_call_id=completion.tool_call_id,
            tool_name="task", status="succeeded", result=message.content,
        )
    results = [item for item in runner_case.saver._storage.storage.read_items(session_id, thread_id)
               if item.semantic_kind == "tool_result"]
    assert [item.item_id for item in results] == [completion.item_id, f"item-{message.id}"]
    assert results[0].to_dict() == actual.to_dict()
    assert results[1].projection_identity.projection_message_id == message.id
    events = await runner_case.container.message_stream_store.list_events(
        session_id=session_id, turn_stream_id=stream_id,
    )
    completed = [event for event in events if event["type"] == "tool.completed"]
    assert len(completed) == 1
    assert completed[0]["tool_attempt_id"] == completion.tool_attempt_id
    assert completed[0]["payload"]["result"] == message.content
