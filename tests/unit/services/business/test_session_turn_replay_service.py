from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_lifecycle_gate import OperationHolderGate
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.common import JobStatus, RunMode
from app.schemas.internal_v2.job import JobDispatchSnapshotDTO, JobDTO
from app.schemas.internal_v2.message import MessageReplayRequest, MessageRunAccepted
from app.services.business.session_turn_replay_service import SessionTurnReplayService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.support.message_service import build_message_service
from tests.support.rollout_output import commit_fixture_output

NOW = datetime(2026, 7, 16, tzinfo=UTC)


def _metadata(message_id: str) -> dict[str, object]:
    timestamp = NOW.isoformat()
    metadata = {
        "message_id": message_id,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    if message_id == "msg_1":
        metadata["turn_id"] = "turn-1"
    if message_id == "msg_2":
        metadata["turn_id"] = "turn-2"
    return metadata


def _job(message_id: str, status: JobStatus, *, job_id: str = "job_target") -> dict[str, object]:
    return {
        "job_id": job_id,
        "message_id": message_id,
        "session_id": "ses_019c205391bb7a4b81b6728df70f63e6",
        "mode": RunMode.single_agent,
        "status": status,
        "entry_agent": "default",
        "created_at": NOW,
        "updated_at": NOW,
    }


class FakeJobService:
    def __init__(self, jobs: list[JobDTO]) -> None:
        self.jobs = jobs

    async def list(self, session_id: str | None = None) -> list[JobDTO]:
        return [
            job
            for job in self.jobs
            if session_id is None or job.session_id == session_id
        ]


class FakeTraceEventStore:
    def __init__(self, events: list[object] | None = None) -> None:
        self.events = events or []

    def read_message_events(self, session_id: str) -> list[object]:
        return self.events


class RecordingDispatcher:
    def __init__(self, saver: RolloutCheckpointSaver) -> None:
        self.saver = saver
        self.pre_dispatch_message_ids: list[str] = []
        self.dispatched_message_id = ""

    async def dispatch_prepared_message(
        self,
        session_id: str,
        message,
        *,
        requested_agent_id: str | None,
    ) -> MessageRunAccepted:
        owner = self.saver.forks.source.resolve_main_thread_owner(session_id)
        with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
            checkpoint_tuple = await self.saver.aget_tuple(
                build_checkpoint_config(owner.thread_id)
            )
            assert checkpoint_tuple is not None
            messages = checkpoint_tuple.checkpoint["channel_values"]["messages"]
            self.pre_dispatch_message_ids = [
                item.response_metadata.get("message_id", "") for item in messages
            ]
            self.dispatched_message_id = message.message_id

            assert message.thread_id == owner.thread_id
            accepted = self.saver.accept_turn(
                session_id, thread_id=owner.thread_id,
                accepted_ingress_id=message.message_id,
                acceptance_idempotency_key=f"acceptance:{message.message_id}",
                job_id="job_replayed", admission_idempotency_key=f"admission:{message.message_id}",
                payload=message.content, root_item_id=f"item-{message.message_id}",
            )
            admission = self.saver.execution_admission(
                session_id, thread_id=owner.thread_id, execution_id=accepted["initial_execution_id"],
            )
            # 模拟真实 dispatcher 的 acceptance/admission，再提交同一用户根输入。
            checkpoint = deepcopy(checkpoint_tuple.checkpoint)
            values = dict(checkpoint["channel_values"])
            values["messages"] = [
                *messages,
                HumanMessage(
                    id=message.message_id,
                    content=message.content,
                    response_metadata=_metadata(message.message_id),
                ),
            ]
            versions = dict(checkpoint["channel_versions"])
            next_version = self.saver.get_next_version(versions.get("messages"), None)
            versions["messages"] = next_version
            checkpoint.update(
                id=str(uuid4()),
                channel_values=values,
                channel_versions=versions,
                updated_channels=["messages"],
            )
            with execution_admission_scope(admission, langgraph_thread_id=owner.thread_id):
                await self.saver.aput(
                    checkpoint_tuple.config,
                    checkpoint,
                    {"source": "test_dispatch", "step": -1, "writes": {}},
                    {"messages": next_version},
                )
            return MessageRunAccepted(
                message_id=message.message_id,
                job_id="job_replayed",
                turn_id=accepted["turn_id"],
                status="running",
                thread_id=owner.thread_id,
                dispatch=JobDispatchSnapshotDTO(
                    session_id=session_id,
                    thread_id=owner.thread_id,
                    job_id="job_replayed",
                    job_status="running",
                    active_job_id="job_replayed",
                    queued_jobs_ahead=0,
                    queued_job_count=0,
                    pending_job_count=1,
                ),
            )


async def _put_checkpoint(
    saver: RolloutCheckpointSaver,
    config: dict,
    *,
    owner: MutationIntentOwner,
    admission: ExecutionAdmission,
    messages: list[object],
    todos: list[str],
    summarization_event: dict[str, object] | None = None,
) -> dict:
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
        execution_admission_scope(admission, langgraph_thread_id=owner.thread_id),
    ):
        previous = await saver.aget_tuple(config)
        previous_versions = (
            dict(previous.checkpoint["channel_versions"]) if previous is not None else {}
        )
        messages_version = saver.get_next_version(previous_versions.get("messages"), None)
        todos_version = saver.get_next_version(previous_versions.get("todos"), None)
        values: dict[str, object] = {"messages": messages, "todos": todos}
        versions: dict[str, object] = {
            "messages": messages_version,
            "todos": todos_version,
        }
        new_versions: dict[str, object] = dict(versions)
        if summarization_event is not None:
            event_version = saver.get_next_version(
                previous_versions.get("_summarization_event"), None
            )
            values["_summarization_event"] = summarization_event
            versions["_summarization_event"] = event_version
            new_versions["_summarization_event"] = event_version
        checkpoint = {
            "v": 1,
            "ts": NOW.isoformat(),
            "id": str(uuid4()),
            "channel_values": values,
            "channel_versions": versions,
            "versions_seen": {},
            "updated_channels": list(new_versions),
        }
        return await saver.aput(
            config,
            checkpoint,
            {"source": "test", "step": 1, "writes": {}},
            new_versions,
        )


async def _build_service(
    prepare_execution,
    tmp_path,
    session_bundle_factory,
    jobs: list[dict[str, object]],
    trace_events: list[object] | None = None,
):
    session_bundle_factory(tmp_path, "ses_019c205391bb7a4b81b6728df70f63e6")
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    owner = saver.forks.source.resolve_main_thread_owner("ses_019c205391bb7a4b81b6728df70f63e6")
    config = build_checkpoint_config(owner.thread_id)
    saver.accept_turn(
        owner.session_id, thread_id=owner.thread_id, accepted_ingress_id="msg_1",
        acceptance_idempotency_key="acceptance:msg_1", job_id="job_1",
        admission_idempotency_key="admission:msg_1", payload="第一问",
        turn_id="turn-1", root_item_id="item-msg_1", initial_execution_id="execution-turn-1",
    )
    first_admission = saver.execution_admission(
        owner.session_id, thread_id=owner.thread_id, execution_id="execution-turn-1",
    )
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
        execution_admission_scope(first_admission, langgraph_thread_id=owner.thread_id),
    ):
        prepare_execution(saver, key="replay-source-create")
    first_messages = [
        HumanMessage(id="msg_1", content="第一问", response_metadata=_metadata("msg_1")),
        AIMessage(id="assistant_1", content="第一答", response_metadata=_metadata("assistant_1")),
    ]
    with (session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
          execution_admission_scope(first_admission, langgraph_thread_id=owner.thread_id)):
        commit_fixture_output(saver, messages=first_messages)
    config = await _put_checkpoint(
        saver,
        config,
        owner=owner, admission=first_admission,
        messages=first_messages,
        todos=["after_first"],
    )
    saver.accept_turn(
        owner.session_id, thread_id=owner.thread_id, accepted_ingress_id="msg_2",
        acceptance_idempotency_key="acceptance:msg_2", job_id="job_target",
        admission_idempotency_key="admission:msg_2", payload="第二问",
        turn_id="turn-2", root_item_id="item-msg_2", initial_execution_id="execution-turn-2",
    )
    second_admission = saver.execution_admission(
        owner.session_id, thread_id=owner.thread_id, execution_id="execution-turn-2",
    )
    second_messages = [
        *first_messages,
        HumanMessage(id="msg_2", content="第二问", response_metadata=_metadata("msg_2")),
        AIMessage(
            id="assistant-call-1",
            content="",
            tool_calls=[{"id": "call_1", "name": "read_file", "args": {}}],
        ),
        ToolMessage(id="tool-result-1", content="partial", tool_call_id="call_1"),
        AIMessage(id="assistant_2", content="第二答", response_metadata=_metadata("assistant_2")),
    ]
    await _put_checkpoint(
        saver,
        config,
        owner=owner, admission=second_admission,
        messages=second_messages,
        todos=["after_second"],
        summarization_event={
            "cutoff_index": 2,
            "summary_message": HumanMessage(id="summary-first-turn", content="第一轮摘要"),
            "file_path": "/history/ses_019c205391bb7a4b81b6728df70f63e6.jsonl",
        },
    )
    dispatcher = RecordingDispatcher(saver)
    session_service = SimpleNamespace(
        get=lambda session_id: None,
    )

    async def get_session(session_id: str):
        return SimpleNamespace(current_agent_id="default")

    async def resolve_main_thread(session_id: str) -> str:
        return saver.forks.source.resolve_main_thread_owner(session_id).thread_id

    session_service.get = get_session
    session_service.resolve_main_thread = resolve_main_thread
    service = SessionTurnReplayService(
        attachment_holder=OperationHolderGate(tmp_path, namespace="attachment_ingest"),
        checkpointer=saver,
        message_service=build_message_service(tmp_path, checkpointer=saver),
        session_service=session_service,
        job_service=FakeJobService([JobDTO(thread_id=owner.thread_id, **job) for job in jobs]),
        dispatcher=dispatcher,
        trace_event_store=FakeTraceEventStore(trace_events),
    )
    return service, saver, dispatcher


async def _read_latest(saver: RolloutCheckpointSaver):
    owner = saver.forks.source.resolve_main_thread_owner("ses_019c205391bb7a4b81b6728df70f63e6")
    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
        return await saver.aget_tuple(build_checkpoint_config(owner.thread_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_replay_holds_attachments_after_replacement_creation_until_dispatch(
    tmp_path, session_bundle_factory, frozen_provider_prepare, monkeypatch, cancel,
):
    # 历史 fixture 只提供 replay 的局部前置；本用例验证准备窗口，不代替执行验收。
    service, _saver, dispatcher = await _build_service(
        frozen_provider_prepare, tmp_path, session_bundle_factory,
        [_job("msg_1", JobStatus.completed, job_id="job_1")],
    )
    session_id = "ses_019c205391bb7a4b81b6728df70f63e6"
    paused = asyncio.Event()
    release = asyncio.Event()
    create = service._message_service.create

    async def paused_create(*args, **kwargs):
        replacement = await create(*args, **kwargs)
        paused.set()
        await release.wait()
        return replacement

    monkeypatch.setattr(service._message_service, "create", paused_create)
    replay = asyncio.create_task(service.replay(
        session_id, "msg_1",
        MessageReplayRequest(
            action="edit_and_continue", content="持锁重放", acknowledge_context_only=True,
        ),
    ))
    try:
        await asyncio.wait_for(paused.wait(), timeout=3)
        async with service._attachment_holder.lock_for(session_id, wait=False) as holder:
            assert not holder.acquired, "创建替换消息后不得提前释放附件 holder"
        if cancel:
            replay.cancel()
            with pytest.raises(asyncio.CancelledError):
                await replay
            assert dispatcher.dispatched_message_id == ""
        else:
            release.set()
            result = await replay
            assert result.message_id == dispatcher.dispatched_message_id
            assert result.removed_message_count == 4
        async with service._attachment_holder.lock_for(session_id, wait=False) as holder:
            assert holder.acquired, "重放完成或取消后必须释放 holder"
    finally:
        release.set()
        await asyncio.gather(replay, return_exceptions=True)


@pytest.mark.asyncio
async def test_edit_first_turn_removes_all_following_turns(
    tmp_path,
    session_bundle_factory,
    frozen_provider_prepare,
) -> None:
    service, saver, dispatcher = await _build_service(frozen_provider_prepare,
        tmp_path,
        session_bundle_factory,
        [
            _job("msg_1", JobStatus.completed, job_id="job_1"),
            _job("msg_2", JobStatus.completed),
        ],
    )

    result = await service.replay(
        "ses_019c205391bb7a4b81b6728df70f63e6",
        "msg_1",
        MessageReplayRequest(
            action="edit_and_continue",
            content="修改后的第一问",
            acknowledge_context_only=True,
        ),
    )

    assert result.workspace_changes_reverted is False
    assert result.removed_message_count == 4
    assert dispatcher.pre_dispatch_message_ids == []
    assert dispatcher.dispatched_message_id not in dispatcher.pre_dispatch_message_ids
    latest = await _read_latest(saver)
    assert latest is not None
    final_messages = latest.checkpoint["channel_values"]["messages"]
    replacement_count = sum(
        item.response_metadata.get("message_id") == dispatcher.dispatched_message_id
        for item in final_messages
    )
    assert replacement_count == 1
    assert latest.checkpoint["channel_values"]["todos"] == ["after_first"]
    assert "_summarization_event" not in latest.checkpoint["channel_values"]


@pytest.mark.asyncio
async def test_replay_turn_uses_turn_anchor_resolver(
    tmp_path,
    session_bundle_factory,
    frozen_provider_prepare,
) -> None:
    service, saver, dispatcher = await _build_service(frozen_provider_prepare,
        tmp_path,
        session_bundle_factory,
        [_job("msg_1", JobStatus.completed, job_id="job_1")],
    )

    result = await service.replay_turn(
        "ses_019c205391bb7a4b81b6728df70f63e6",
        "turn-1",
        MessageReplayRequest(
            action="edit_and_continue",
            content="按 Turn 重放第一问",
            acknowledge_context_only=True,
        ),
    )

    assert result.replaced_message_id == "msg_1"
    assert dispatcher.pre_dispatch_message_ids == []
    assert dispatcher.dispatched_message_id
    latest = await _read_latest(saver)
    assert latest is not None
    messages = latest.checkpoint["channel_values"]["messages"]
    assert [message.content for message in messages] == ["按 Turn 重放第一问"]


@pytest.mark.asyncio
async def test_regenerate_last_reply_reuses_original_prompt(
    tmp_path,
    session_bundle_factory,
    frozen_provider_prepare,
) -> None:
    service, _, dispatcher = await _build_service(frozen_provider_prepare,
        tmp_path,
        session_bundle_factory,
        [_job("msg_2", JobStatus.completed)],
    )

    result = await service.replay(
        "ses_019c205391bb7a4b81b6728df70f63e6",
        "msg_2",
        MessageReplayRequest(
            action="regenerate",
            acknowledge_context_only=True,
        ),
    )

    assert result.action == "regenerate"
    assert dispatcher.pre_dispatch_message_ids == ["msg_1", "assistant_1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_status", [JobStatus.failed, JobStatus.timed_out])
async def test_retry_failed_clears_partial_tool_messages(
    tmp_path,
    session_bundle_factory,
    failure_status: JobStatus,
    frozen_provider_prepare,
) -> None:
    service, saver, dispatcher = await _build_service(frozen_provider_prepare,
        tmp_path,
        session_bundle_factory,
        [_job("msg_2", failure_status)],
    )

    result = await service.replay(
        "ses_019c205391bb7a4b81b6728df70f63e6",
        "msg_2",
        MessageReplayRequest(
            action="retry_failed",
            acknowledge_context_only=True,
        ),
    )

    assert result.action == "retry_failed"
    assert dispatcher.pre_dispatch_message_ids == ["msg_1", "assistant_1"]
    latest = await _read_latest(saver)
    assert latest is not None
    final_messages = latest.checkpoint["channel_values"]["messages"]
    assert not any(isinstance(message, ToolMessage) for message in final_messages)
    assert not any(
        isinstance(message, AIMessage) and bool(message.tool_calls)
        for message in final_messages
    )
    assert (
        sum(
            message.response_metadata.get("message_id")
            == dispatcher.dispatched_message_id
            for message in final_messages
        )
        == 1
    )


@pytest.mark.asyncio
async def test_retry_failed_uses_persisted_trace_after_job_service_restart(
    tmp_path,
    session_bundle_factory,
    frozen_provider_prepare,
) -> None:
    service, _, _ = await _build_service(frozen_provider_prepare,
        tmp_path,
        session_bundle_factory,
        [],
        trace_events=[
            SimpleNamespace(
                type="job_created",
                job_id="job_persisted_failure",
                payload=SimpleNamespace(message_id="msg_2"),
            ),
            SimpleNamespace(
                type="job_failed",
                job_id="job_persisted_failure",
                payload=SimpleNamespace(error="模型失败"),
            ),
        ],
    )

    assert await service._turn_has_failed("ses_019c205391bb7a4b81b6728df70f63e6", "msg_2") is True


@pytest.mark.asyncio
async def test_retry_failed_accepts_execution_lost_session_interrupted_trace(
    tmp_path,
    session_bundle_factory,
    frozen_provider_prepare,
):
    service, _, _ = await _build_service(frozen_provider_prepare,
        tmp_path,
        session_bundle_factory,
        [],
        trace_events=[
            SimpleNamespace(
                type="job_created",
                job_id="job_lost",
                payload=SimpleNamespace(message_id="msg_2"),
            ),
            SimpleNamespace(
                type="session_interrupted",
                job_id="job_lost",
                payload=SimpleNamespace(
                    phase="process_exit",
                    code="execution_lost",
                ),
            ),
        ],
    )

    assert await service._turn_has_failed("ses_019c205391bb7a4b81b6728df70f63e6", "msg_2") is True


@pytest.mark.asyncio
async def test_retry_requires_failed_or_timed_out_job(
    tmp_path,
    session_bundle_factory,
    frozen_provider_prepare,
) -> None:
    service, _, _ = await _build_service(frozen_provider_prepare,
        tmp_path,
        session_bundle_factory,
        [_job("msg_2", JobStatus.completed)],
    )

    with pytest.raises(ValueError, match="不是失败轮次"):
        await service.replay(
            "ses_019c205391bb7a4b81b6728df70f63e6",
            "msg_2",
            MessageReplayRequest(
                action="retry_failed",
                acknowledge_context_only=True,
            ),
        )


@pytest.mark.asyncio
async def test_replay_rejects_running_job_before_mutating_checkpoint(
    tmp_path,
    session_bundle_factory,
    frozen_provider_prepare,
) -> None:
    service, saver, dispatcher = await _build_service(frozen_provider_prepare,
        tmp_path,
        session_bundle_factory,
        [_job("msg_2", JobStatus.running)],
    )
    before = await _read_latest(saver)
    assert before is not None

    with pytest.raises(ValueError, match="仍有运行中任务"):
        await service.replay(
            "ses_019c205391bb7a4b81b6728df70f63e6",
            "msg_2",
            MessageReplayRequest(
                action="edit_and_continue",
                content="不能提交",
                acknowledge_context_only=True,
            ),
        )

    after = await _read_latest(saver)
    assert after is not None
    assert after.checkpoint["id"] == before.checkpoint["id"]
    assert dispatcher.dispatched_message_id == ""


@pytest.mark.asyncio
async def test_replay_requires_explicit_context_only_acknowledgement(
    tmp_path,
    session_bundle_factory,
    frozen_provider_prepare,
) -> None:
    service, _, _ = await _build_service(frozen_provider_prepare,
        tmp_path,
        session_bundle_factory,
        [_job("msg_2", JobStatus.completed)],
    )

    with pytest.raises(ValueError, match="不会撤销工作区文件修改"):
        await service.replay(
            "ses_019c205391bb7a4b81b6728df70f63e6",
            "msg_2",
            MessageReplayRequest(action="regenerate"),
        )


@pytest.mark.asyncio
async def test_replay_lock_table_stays_bounded_across_many_sessions(
    tmp_path,
    session_bundle_factory,
    frozen_provider_prepare,
) -> None:
    """Replay 锁池对 session 数量必须有固定上界。

    旧实现为每个 session 新建一把锁，长驻进程会随历史会话数无界增长；且
    ``setdefault`` 发生在入参校验之前，连被拒绝的非法请求也会永久留下一个锁。
    """
    service, _, _ = await _build_service(frozen_provider_prepare,
        tmp_path,
        session_bundle_factory,
        [_job("msg_2", JobStatus.completed)],
    )

    for index in range(5000):
        service._session_lock(f"ses_replay_{index}")

    # 固定上界（与分片数一致的量级），不依赖内部常量即可断言有界性。
    assert len(service._session_locks) <= 64


@pytest.mark.asyncio
async def test_replay_lock_is_stable_and_serializes_same_session(
    tmp_path,
    session_bundle_factory,
    frozen_provider_prepare,
) -> None:
    """分片锁池：同一 session 恒得同一把锁，同 session 临界区仍严格互斥。"""
    service, _, _ = await _build_service(frozen_provider_prepare,
        tmp_path,
        session_bundle_factory,
        [_job("msg_2", JobStatus.completed)],
    )

    assert service._session_lock("sess_shared") is service._session_lock("sess_shared")

    lock = service._session_lock("sess_shared")
    concurrent = 0
    peak = 0

    async def worker() -> None:
        nonlocal concurrent, peak
        async with lock:
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1

    await asyncio.gather(*(worker() for _ in range(20)))
    assert peak == 1
