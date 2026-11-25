"""通过真实消息准入/Job 入口验证持久 FIFO 与任务启动崩溃窗口。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import JobEventBus
from app.core.path_utils import workspace_context
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.execution_admission import InternalExecutionIntent
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.prompting import PromptSection, internal_message_factory
from app.schemas.internal_v2.common import JobStatus, MessageRole
from app.schemas.internal_v2.job import JobControlRequest
from app.schemas.internal_v2.message import AttachmentRef, MessageDTO
from app.services.business.job.service import JobService
from app.services.business.message_service import MessageService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.execution_queue import ExecutionQueueService
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_test_workspace
from tests.unit.core.catalog_workspace_helper import (
    CatalogWorkspaceContext,
    build_catalog_workspace,
)


class HeldExecutor:
    def __init__(self) -> None:
        self.started: asyncio.Queue[str] = asyncio.Queue()

    async def run(self, job) -> str:
        job.progress_reporter("agent_loop_ready")
        self.started.put_nowait(job.job_id)
        await asyncio.Future()
        raise AssertionError("执行屏障只能由测试取消")


@dataclass
class QueueCase:
    workspace: CatalogWorkspaceContext
    session_id: str
    thread_id: str
    saver: RolloutCheckpointSaver
    messages: MessageService
    store: ExecutionQueueService
    jobs: JobService
    executor: HeldExecutor

    def accept(self, text: str, policy="after_turn", *, background: bool = False):
        identity = self.jobs.reserve_user_execution_identity()
        now = datetime.now(UTC)
        message = MessageDTO(
            message_id=create_prefixed_id("msg"), session_id=self.session_id,
            thread_id=self.thread_id, role=MessageRole.user, content=text, created_at=now, updated_at=now,
        )
        frozen = ExecutionQueueInput(
            execution_binding=identity, message_id=message.message_id, agent_id="default",
            model=None, delivery_policy=policy, gateway_id=None, request_id=None, created_at=now.isoformat(),
            background=background,
        )
        self.messages.accept_user_message(message, execution_identity=identity, queue_input=frozen)
        return message, identity

    async def start(self, message, identity, policy="after_turn"):
        return await self.jobs.start_job(
            self.session_id, message.content, thread_id=self.thread_id,
            execution_binding=identity, agent_id="default", message_id=message.message_id,
            message_created_at=message.created_at.isoformat(), delivery_policy=policy,
        )

    def accept_internal(self, text: str, *, background: bool = False, kind: str = "checkpoint_reminder"):
        now = datetime.now(UTC).isoformat()
        intent = InternalExecutionIntent(create_prefixed_id("msg"), create_prefixed_id("exec"), create_prefixed_id("admission"))
        job_id = self.jobs.reserve_internal_job_id()
        prepared = internal_message_factory.build(kind=kind, control=text)
        self.saver.admit_internal_execution(
            self.session_id, thread_id=self.thread_id, job_id=job_id, intent=intent,
            queue_input=ExecutionQueueInput(intent, intent.message_id, "default", None, "after_turn", None, None, now,
                                            background=background),
            message=HumanMessage(id=intent.message_id, content=prepared.content,
                response_metadata={"message_id": intent.message_id, "created_at": now, "message_metadata": prepared.metadata}),
        )
        return self.saver.execution_queue_record(self.session_id, self.thread_id, job_id)


@pytest.fixture
async def queue_case(request: pytest.FixtureRequest) -> AsyncIterator[QueueCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    prepare_test_workspace(
        workspace_root=context.workspace_root,
        template_root=(
            Path.cwd() / "tests" / "fixtures" / "workspaces" / "default_test_workspace"
        ),
        template_items=(),
    )
    workspace = build_catalog_workspace(context.output_root, workspace_id=load_or_create_workspace_id(context.workspace_root))
    session_id = await workspace.create_session("事务 FIFO 验证")
    thread_id = workspace.resolver.main_thread_id(session_id)
    saver = RolloutCheckpointSaver(sessions_dir=workspace.sessions_root)
    messages = MessageService(main_thread_resolver=workspace.resolver.main_thread_id, checkpointer=saver,
                              workspace_root=workspace.workspace_root)
    store = ExecutionQueueService(sessions_dir=workspace.sessions_root, owner=saver,
                                input_reader=lambda reference: messages.accepted_job_message(reference, display=False))
    executor = HeldExecutor()
    jobs = JobService(job_event_bus=JobEventBus(), job_executor=executor, input_owner=messages,
                      execution_queue_service=store, terminal_status_writer=saver,
                      session_lifecycle_guard=workspace.resolver.resolve_session_node_for_runtime)
    case = QueueCase(workspace, session_id, thread_id, saver, messages, store, jobs, executor)
    try:
        yield case
    finally:
        jobs.close_admission()
        with workspace_context(workspace.workspace_id, workspace.workspace_root):
            await jobs.force_interrupt_active(reason="test_queue_teardown")
        await jobs.drain_finalizations()
        workspace.close()


@pytest.mark.asyncio
async def test_user_job_without_acceptance_preserves_missing_input_error(queue_case: QueueCase) -> None:
    case = queue_case
    now = datetime.now(UTC)
    message = MessageDTO(
        message_id=create_prefixed_id("msg"), session_id=case.session_id, thread_id=case.thread_id,
        role=MessageRole.user, content="尚未接受的用户输入", created_at=now, updated_at=now,
    )
    identity = case.jobs.reserve_user_execution_identity()
    # 原单次 reader 对合法但尚未物化的 owner 保持精确缺 Job 错误。
    with pytest.raises(KeyError, match="Job execution admission 不存在") as original:
        case.saver.accepted_user_message(
            case.session_id, thread_id=case.thread_id, job_id=identity.job_id, message_id=message.message_id,
        )
    with pytest.raises(KeyError) as rejected:
        await case.start(message, identity)
    assert rejected.value.args == original.value.args
    assert case.executor.started.empty() and not case.jobs._jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["content", "message_id", "turn_id", "admission_key", "attachments"])
async def test_user_job_rejects_drift_from_accepted_input(queue_case: QueueCase, drift: str) -> None:
    case = queue_case
    message, identity = case.accept("只消费已接受的正文")
    original = case.saver.execution_queue_record(case.session_id, case.thread_id, identity.job_id)
    path = case.saver._storage.storage.jsonl_path(case.session_id, thread_id=case.thread_id)
    canonical = path.read_bytes()
    if drift == "content":
        message = message.model_copy(update={"content": message.content + "漂移"})
    elif drift == "message_id":
        message = message.model_copy(update={"message_id": create_prefixed_id("msg")})
    elif drift == "turn_id":
        identity = replace(identity, turn_id=create_prefixed_id("turn"))
    elif drift == "admission_key":
        identity = replace(identity, admission_idempotency_key=create_prefixed_id("adm"))
    else:
        message = message.model_copy(update={"attachments": [AttachmentRef(file_id="different.txt")]})
    with pytest.raises(ValueError, match="不一致"):
        await case.jobs.start_job(
            case.session_id, message.content, thread_id=case.thread_id, execution_binding=identity,
            agent_id="default", message_id=message.message_id, attachments=message.attachments,
            message_created_at=message.created_at.isoformat(),
        )
    assert case.saver.execution_queue_record(case.session_id, case.thread_id, identity.job_id) == original
    assert path.read_bytes() == canonical
    assert case.executor.started.empty() and not case.jobs._jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "hash"])
async def test_user_job_rejects_damaged_frozen_queue(queue_case: QueueCase, damage: str) -> None:
    case = queue_case
    message, identity = case.accept("队列损坏不能绕过冻结校验")
    storage = case.saver._storage.storage
    path = storage.jsonl_path(case.session_id, thread_id=case.thread_id)
    canonical = path.read_bytes()
    with storage._connect(case.thread_id, session_id=case.session_id) as connection:
        if damage == "missing":
            connection.execute("DELETE FROM execution_queue WHERE job_id=?", (identity.job_id,))
        else:
            connection.execute("UPDATE execution_queue SET input_hash='damaged' WHERE job_id=?", (identity.job_id,))
        connection.commit()
    error, message_match = (KeyError, "执行队列引用不存在") if damage == "missing" else (RuntimeError, "hash 不一致")
    with pytest.raises(error, match=message_match):
        await case.start(message, identity)
    assert path.read_bytes() == canonical
    assert case.executor.started.empty() and not case.jobs._jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["message", "queue"])
@pytest.mark.parametrize("fault", ["owner", "closed"])
async def test_user_input_readers_reject_invalid_borrowed_snapshot(
    queue_case: QueueCase, reader: str, fault: str,
) -> None:
    case = queue_case
    message, identity = case.accept("只借用同 owner 的存活快照")
    owner_session, owner_thread = case.session_id, case.thread_id
    if fault == "owner":
        owner_session = await case.workspace.create_session("另一快照 owner")
        owner_thread = case.workspace.resolver.main_thread_id(owner_session)
        case.saver._storage.storage.initialize(owner_session, thread_id=owner_thread)
    with case.saver._context_reader.open_snapshot(owner_session, owner_thread) as snapshot:
        if fault == "closed":
            snapshot.close()
        error, message_match = (ValueError, "read snapshot 与目标 rollout 不一致") if fault == "owner" else (RuntimeError, "snapshot 已关闭")
        with pytest.raises(error, match=message_match):
            if reader == "message":
                case.saver.accepted_user_message(
                    case.session_id, thread_id=case.thread_id, job_id=identity.job_id,
                    message_id=message.message_id, snapshot=snapshot,
                )
            else:
                case.saver.execution_queue_record(case.session_id, case.thread_id, identity.job_id, snapshot=snapshot)
    assert snapshot.closed
    assert case.saver.execution_queue_record(case.session_id, case.thread_id, identity.job_id).phase == "queued"


@pytest.mark.asyncio
async def test_acceptance_commit_is_already_a_cold_runnable_queue(queue_case: QueueCase) -> None:
    case = queue_case
    message, identity = case.accept("提交后尚未调用 start_job")
    restarted = RolloutCheckpointSaver(sessions_dir=case.workspace.sessions_root)
    snapshot = restarted.execution_queue_snapshot(case.session_id, case.thread_id)
    assert len(snapshot.records) == 1
    record = snapshot.records[0]
    assert record.phase == "queued" and record.cursor.admission.job_id == identity.job_id
    with restarted._storage.storage.read_connection(case.session_id, case.thread_id) as connection:
        assert connection.execute("SELECT commit_kind FROM storage_commits WHERE commit_id=?", (record.cursor.commit_id,)).fetchone() == ("acceptance",)
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (1,)
    assert not (case.workspace.session_dir(case.session_id) / "pending_requests.json").exists()
    await case.jobs.list_pending(case.session_id)
    assert await asyncio.wait_for(case.executor.started.get(), 5) == identity.job_id
    assert case.jobs._jobs[identity.job_id].message_id == message.message_id


async def test_user_completion_and_persistent_requests_share_one_non_preemptive_queue(queue_case: QueueCase) -> None:
    case = queue_case
    active = case.accept("正在执行的持续任务", background=True)
    await case.start(*active)
    active_id = await asyncio.wait_for(case.executor.started.get(), 5)
    active_task = case.jobs._jobs[active_id].task
    persistent = case.accept_internal("下一轮持续任务", background=True)
    completion = case.accept_internal("终端完成通知")
    user1, identity1 = case.accept("用户消息一")
    user2, identity2 = case.accept("用户消息二")
    await case.jobs.list_pending(case.session_id)
    snapshot = await case.store.snapshot(case.session_id, case.thread_id)
    await case.jobs.reorder_pending(case.session_id, case.thread_id,
        message_ids=(user2.message_id, user1.message_id), expected_thread_revision=snapshot.thread_revision)
    expected = [identity2.job_id, identity1.job_id, completion.cursor.admission.job_id, persistent.cursor.admission.job_id]
    with RolloutCheckpointSaver(sessions_dir=case.workspace.sessions_root) as cold:
        assert [r.cursor.admission.job_id for r in cold.execution_queue_snapshot(case.session_id, case.thread_id).records
                if r.phase == "queued"] == expected
    assert case.jobs._jobs[active_id].task is active_task and not active_task.done()
    assert case.executor.started.empty()
    # 真实领取必须服从与查询相同的优先级；不能只改 UI 顺序。
    previous = active_id
    for job_id in expected:
        await case.jobs.control(previous, JobControlRequest(action="cancel"))
        assert await asyncio.wait_for(case.executor.started.get(), 5) == job_id
        previous = job_id


async def test_stopped_background_request_is_cancelled_before_executor_and_queue_continues(
    queue_case: QueueCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = queue_case
    await case.jobs.control_thread_advancement(case.session_id, case.thread_id, action="pause", expected_execution_id=None)
    obsolete = case.accept_internal("已经关闭的持续任务", background=True)
    following = case.accept_internal("另一个有效任务", background=True)
    obsolete_id = obsolete.cursor.admission.job_id
    following_id = following.cursor.admission.job_id
    monkeypatch.setattr(case.jobs, "_dispatch_guard", lambda job: "持续任务已关闭" if job.job_id == obsolete_id else None)
    await case.jobs.list_pending(case.session_id)
    assert case.executor.started.empty()
    await case.jobs.control_thread_advancement(case.session_id, case.thread_id, action="resume", expected_execution_id=None)
    assert await asyncio.wait_for(case.executor.started.get(), 5) == following_id
    frozen = case.saver.execution_queue_record(case.session_id, case.thread_id, obsolete_id)
    assert (frozen.phase, frozen.outcome) == ("terminal", "cancelled")
    assert case.jobs._jobs[obsolete_id].error_message == "持续任务已关闭"
    assert case.executor.started.empty()


async def test_failed_user_execution_keeps_terminal_notification_after_queued_user(
    queue_case: QueueCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = queue_case
    active = case.accept("会失败的用户请求")
    active_id = active[1].job_id
    fail = asyncio.Event()
    original_run = case.executor.run

    async def fail_first(job):
        if job.job_id != active_id:
            return await original_run(job)
        job.progress_reporter("agent_loop_ready")
        case.executor.started.put_nowait(job.job_id)
        await fail.wait()
        # 真实执行器先提交执行终态，Job 再记录业务失败；替身也遵守这个边界。
        case.saver.mark_turn_terminal_status(session_id=case.session_id, thread_id=case.thread_id,
                                            turn_id=active[1].turn_id, status="failed")
        raise ValueError("测试用户请求失败，终端通知仍有效")

    monkeypatch.setattr(case.executor, "run", fail_first)
    await case.start(*active)
    assert await asyncio.wait_for(case.executor.started.get(), 5) == active_id
    notification = case.accept_internal("后台命令已完成", kind="terminal_execution_completed")
    notification_id = notification.cursor.admission.job_id
    pending = case.accept("优先处理的用户消息")
    await case.jobs.list_pending(case.session_id)
    active_task = case.jobs._jobs[active_id].task
    fail.set()
    await asyncio.wait_for(asyncio.shield(active_task), 5)
    assert case.jobs._jobs[active_id].status == JobStatus.failed
    assert case.jobs._thread_current_job.get((case.session_id, case.thread_id)) == pending[1].job_id, (
        case.jobs._pending_queue.queued_job_ids(case.session_id),
        case.saver.execution_queue_snapshot(case.session_id, case.thread_id),
    )
    assert await asyncio.wait_for(case.executor.started.get(), 5) == pending[1].job_id
    assert case.saver.execution_queue_record(case.session_id, case.thread_id, notification_id).phase == "queued"
    await case.jobs.control(pending[1].job_id, JobControlRequest(action="cancel"))
    assert await asyncio.wait_for(case.executor.started.get(), 5) == notification_id
    assert case.jobs._jobs[notification_id].status == JobStatus.running


async def test_explicit_resume_still_precedes_preserved_user_input(queue_case: QueueCase) -> None:
    case = queue_case
    _message, user = case.accept("暂停前保留的用户消息")
    continuation = case.accept_internal("显式继续原任务")
    pause = await case.store.pause(case.session_id, case.thread_id, execution_id=None)
    await case.store.resume(case.session_id, case.thread_id, pause_id=pause.pause_id,
                            continuation_job_id=continuation.cursor.admission.job_id)
    snapshot = await case.store.snapshot(case.session_id, case.thread_id)
    assert [row.job_id for row in snapshot.requests] == [continuation.cursor.admission.job_id, user.job_id]
    await case.jobs.list_pending(case.session_id)
    assert await asyncio.wait_for(case.executor.started.get(), 5) == continuation.cursor.admission.job_id


async def test_loaded_runtime_discovers_acceptance_without_start_job(queue_case: QueueCase) -> None:
    case = queue_case
    first = case.accept("原执行")
    await case.start(*first)
    assert await asyncio.wait_for(case.executor.started.get(), 5) == first[1].job_id
    running = case.jobs._jobs[first[1].job_id]
    task = running.task
    second = case.accept("模拟准入提交后请求断开")
    snapshot = await case.jobs.list_pending(case.session_id)
    assert [item.job_id for item in snapshot.requests] == [second[1].job_id]
    assert case.jobs._jobs[first[1].job_id] is running and running.task is task
    assert case.executor.started.empty()


@pytest.mark.asyncio
async def test_fifo_policy_and_cancel_use_original_admission_cursor(queue_case: QueueCase) -> None:
    case = queue_case
    first = case.accept("active")
    dispatch = await case.start(*first)
    assert await asyncio.wait_for(case.executor.started.get(), 5) == dispatch.job_id
    second = case.accept("queued")
    third = case.accept("tail", "after_interrupt")
    await case.start(*second)
    await case.start(*third, policy="after_interrupt")
    snapshot = await case.jobs.list_pending(case.session_id)
    assert [item.job_id for item in snapshot.requests] == [second[1].job_id, third[1].job_id]
    records = case.saver.execution_queue_snapshot(case.session_id, case.thread_id).records
    assert [item.enqueue_sequence for item in snapshot.requests] == [item.cursor.commit_id for item in records if item.phase == "queued"]
    changed = await case.jobs.update_pending_policy(case.session_id, third[0].message_id,
        delivery_policy="after_tool_result", expected_snapshot_version=snapshot.snapshot_version)
    assert changed.snapshot_version > snapshot.snapshot_version
    with pytest.raises(RuntimeError, match="过期"):
        await case.jobs.update_pending_policy(case.session_id, third[0].message_id,
            delivery_policy="after_turn", expected_snapshot_version=snapshot.snapshot_version)
    removed = await case.jobs.remove_pending(case.session_id, second[0].message_id)
    assert [item.job_id for item in removed.requests] == [third[1].job_id]
    assert case.saver.execution_queue_record(case.session_id, case.thread_id, second[1].job_id).outcome == "cancelled"


@pytest.mark.asyncio
async def test_task_creation_failure_retains_claim_and_retries_original_job(queue_case: QueueCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = queue_case
    message, identity = case.accept("task window")
    original = case.jobs._start_job_task
    failed = False

    def fail_once(job):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected create_task failure")
        original(job)

    monkeypatch.setattr(case.jobs, "_start_job_task", fail_once)
    with pytest.raises(OSError, match="injected"):
        await case.start(message, identity)
    job = case.jobs._jobs[identity.job_id]
    frozen = case.saver.execution_queue_record(case.session_id, case.thread_id, job.job_id)
    assert frozen.phase == "started" and frozen.claim_generation == 1
    assert await case.store.claim(frozen) is None
    await case.jobs.list_pending(case.session_id)
    assert case.jobs._jobs[job.job_id] is job
    assert await asyncio.wait_for(case.executor.started.get(), 5) == job.job_id
    assert case.executor.started.empty()


@pytest.mark.asyncio
async def test_queue_publication_error_rolls_back_acceptance(queue_case: QueueCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = queue_case
    def reject(*args, **kwargs):
        raise OSError("injected queue publication failure")
    monkeypatch.setattr(case.saver._storage.executions, "publish_execution_queue_input", reject)
    with pytest.raises(OSError, match="injected"):
        case.accept("不得留下接受一半的请求")
    with case.saver._storage.storage.read_connection(case.session_id, case.thread_id) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_acceptances").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM execution_queue").fetchone() == (0,)


@pytest.mark.parametrize("loaded", [False, True])
async def test_delete_cancels_durable_entries_before_discarding_jobs(queue_case: QueueCase, loaded: bool) -> None:
    case = queue_case
    first = case.accept("删除前已接受")
    if loaded:
        await case.start(*first)
        assert await asyncio.wait_for(case.executor.started.get(), 5) == first[1].job_id
    second = case.accept("尚未调用 start_job")
    deleted = await case.jobs.run_session_control_operation(
        case.session_id, lambda: case.jobs.stop_session_jobs(case.session_id),
        deadline=asyncio.get_running_loop().time() + 30,
    )
    assert deleted == 2
    assert case.saver.execution_queue_snapshot(case.session_id, case.thread_id).records == ()
    for _, identity in [first, second]:
        record = case.saver.execution_queue_record(case.session_id, case.thread_id, identity.job_id)
        assert record.phase == "terminal" and record.outcome == "cancelled"
        assert identity.job_id not in case.jobs._jobs
    assert case.executor.started.empty()
    assert not case.jobs._thread_current_job


async def test_stop_keeps_user_input_but_cancels_accepted_internal_continuation(queue_case: QueueCase) -> None:
    case = queue_case
    await case.jobs.control_thread_advancement(case.session_id, case.thread_id, action="pause", expected_execution_id=None)
    intent = InternalExecutionIntent(create_prefixed_id("msg"), create_prefixed_id("exec"), create_prefixed_id("admission"))
    job_id = case.jobs.reserve_internal_job_id()
    now = datetime.now(UTC).isoformat()
    prepared = internal_message_factory.build(kind="execution_resume", control="已接受但尚未释放暂停门的旧继续任务",
        sections=(PromptSection("control_context", {"previous_execution_id": "exec_lost"}),))
    case.saver.admit_internal_execution(
        case.session_id, thread_id=case.thread_id, job_id=job_id, intent=intent,
        queue_input=ExecutionQueueInput(intent, intent.message_id, "default", None, "after_turn", None, None, now),
        message=HumanMessage(id=intent.message_id, content=prepared.content,
            response_metadata={"message_id": intent.message_id, "created_at": now, "message_metadata": prepared.metadata}),
    )
    _, user = case.accept("完全打断仍须保留的用户输入")
    await case.jobs.run_session_control_operation(case.session_id, lambda: case.jobs.stop_session_jobs(
        case.session_id, delete_records=False, discard_pending=False, pause_pending=True,
    ), deadline=asyncio.get_running_loop().time() + 5)
    # 冷读原 owner，确认取消真正落盘，而非仅移除内存队列。
    cold = RolloutCheckpointSaver(sessions_dir=case.workspace.sessions_root)
    cancelled = cold.execution_queue_record(case.session_id, case.thread_id, job_id)
    assert (cancelled.phase, cancelled.outcome) == ("terminal", "cancelled")
    pending = cold.execution_queue_snapshot(case.session_id, case.thread_id)
    assert [row.cursor.admission.job_id for row in pending.records] == [user.job_id]
    assert pending.pause is not None and pending.pause.execution_id is None
    await case.jobs.control_thread_advancement(case.session_id, case.thread_id, action="resume", expected_execution_id=None)
    assert await asyncio.wait_for(case.executor.started.get(), 5) == user.job_id
    assert case.executor.started.empty()


async def test_delete_queue_write_failure_keeps_active_task_and_remaining_fifo(
    queue_case: QueueCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = queue_case
    first = case.accept("保持原执行")
    await case.start(*first)
    assert await asyncio.wait_for(case.executor.started.get(), 5) == first[1].job_id
    active = case.jobs._jobs[first[1].job_id]
    task = active.task
    original_status = active.status
    second = case.accept("先提交取消")
    third = case.accept("取消落盘失败")
    await case.start(*second)
    await case.start(*third)
    original_writer = case.saver.mark_job_terminal_status

    def fail_tail(**kwargs):
        if kwargs["job_id"] == third[1].job_id:
            raise OSError("injected delete queue terminal failure")
        return original_writer(**kwargs)

    monkeypatch.setattr(case.saver, "mark_job_terminal_status", fail_tail)
    with pytest.raises(OSError, match="injected delete queue terminal failure"):
        await case.jobs.run_session_control_operation(
            case.session_id, lambda: case.jobs.stop_session_jobs(case.session_id),
            deadline=asyncio.get_running_loop().time() + 30,
        )
    assert case.session_id not in case.jobs._closed_sessions
    assert active.status == original_status and active.cancellation_reason is None
    assert active.task is task and task is not None and not task.done()
    assert case.jobs._thread_current_job[(case.session_id, case.thread_id)] == first[1].job_id
    assert case.jobs._jobs[second[1].job_id].status == JobStatus.cancelled
    with pytest.raises(ValueError, match="队列中不存在"):
        case.jobs._pending_queue.entry(second[1].job_id)
    assert case.jobs._pending_queue.entry(third[1].job_id).job_id == third[1].job_id
    snapshot = await case.jobs.list_pending(case.session_id)
    assert [entry.job_id for entry in snapshot.requests] == [third[1].job_id]
    assert case.jobs._jobs[first[1].job_id] is active and active.task is task
    assert case.executor.started.empty()

    monkeypatch.setattr(case.saver, "mark_job_terminal_status", original_writer)
    assert await case.jobs.run_session_control_operation(
        case.session_id, lambda: case.jobs.stop_session_jobs(case.session_id),
        deadline=asyncio.get_running_loop().time() + 30,
    ) == 3
    assert case.saver.execution_queue_snapshot(case.session_id, case.thread_id).records == ()
    for _, identity in [first, second, third]:
        record = case.saver.execution_queue_record(case.session_id, case.thread_id, identity.job_id)
        assert record.phase == "terminal" and record.outcome == "cancelled"
    assert case.executor.started.empty() and not case.jobs._thread_current_job
