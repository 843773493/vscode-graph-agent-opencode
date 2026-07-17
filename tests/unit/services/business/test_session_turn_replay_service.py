from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core.checkpoint_config import build_checkpoint_config
from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.schemas.public_v2.common import JobStatus, RunMode
from app.schemas.public_v2.job import JobDTO
from app.schemas.public_v2.message import MessageReplayRequest, MessageRunAccepted
from app.services.business.message_service import MessageService
from app.services.business.session_turn_replay_service import SessionTurnReplayService


NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)


def _metadata(message_id: str) -> dict[str, object]:
    timestamp = NOW.isoformat()
    return {
        "message_id": message_id,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _job(message_id: str, status: JobStatus, *, job_id: str = "job_target") -> JobDTO:
    return JobDTO(
        job_id=job_id,
        message_id=message_id,
        session_id="ses_replay",
        mode=RunMode.single_agent,
        status=status,
        entry_agent="default",
        created_at=NOW,
        updated_at=NOW,
    )


class FakeJobService:
    def __init__(self, jobs: list[JobDTO]) -> None:
        self.jobs = jobs

    async def list(self, session_id: str | None = None) -> list[JobDTO]:
        return [job for job in self.jobs if session_id is None or job.session_id == session_id]


class RecordingDispatcher:
    def __init__(self, saver: FileSystemCheckpointSaver) -> None:
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
        checkpoint_tuple = await self.saver.aget_tuple(build_checkpoint_config(session_id))
        assert checkpoint_tuple is not None
        messages = checkpoint_tuple.checkpoint["channel_values"]["messages"]
        self.pre_dispatch_message_ids = [
            item.response_metadata.get("message_id", "") for item in messages
        ]
        self.dispatched_message_id = message.message_id

        # 模拟 LangGraph 正常接收本次用户输入；回退服务自身不得预写同一消息。
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
        await self.saver.aput(
            checkpoint_tuple.config,
            checkpoint,
            {"source": "test_dispatch", "step": -1, "writes": {}},
            {"messages": next_version},
        )
        return MessageRunAccepted(
            message_id=message.message_id,
            job_id="job_replayed",
            status="accepted",
        )


async def _put_checkpoint(
    saver: FileSystemCheckpointSaver,
    config: dict,
    *,
    messages: list[object],
    todos: list[str],
    summarization_event: dict[str, object] | None = None,
) -> dict:
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


async def _build_service(tmp_path, jobs: list[JobDTO]):
    saver = FileSystemCheckpointSaver(sessions_dir=tmp_path)
    config = build_checkpoint_config("ses_replay")
    config = await _put_checkpoint(saver, config, messages=[], todos=["initial"])
    first_messages = [
        HumanMessage(content="第一问", response_metadata=_metadata("msg_1")),
        AIMessage(content="第一答", response_metadata=_metadata("assistant_1")),
    ]
    config = await _put_checkpoint(
        saver,
        config,
        messages=first_messages,
        todos=["after_first"],
    )
    second_messages = [
        *first_messages,
        HumanMessage(content="第二问", response_metadata=_metadata("msg_2")),
        AIMessage(
            content="",
            tool_calls=[{"id": "call_1", "name": "read_file", "args": {}}],
        ),
        ToolMessage(content="partial", tool_call_id="call_1"),
        AIMessage(content="第二答", response_metadata=_metadata("assistant_2")),
    ]
    await _put_checkpoint(
        saver,
        config,
        messages=second_messages,
        todos=["after_second"],
        summarization_event={
            "cutoff_index": 2,
            "summary_message": HumanMessage(content="第一轮摘要"),
            "file_path": "/history/ses_replay.jsonl",
        },
    )
    dispatcher = RecordingDispatcher(saver)
    session_service = SimpleNamespace(
        get=lambda session_id: None,
    )

    async def get_session(session_id: str):
        return SimpleNamespace(current_agent_id="default")

    session_service.get = get_session
    service = SessionTurnReplayService(
        checkpointer=saver,
        message_service=MessageService(checkpointer=saver),
        session_service=session_service,
        job_service=FakeJobService(jobs),
        dispatcher=dispatcher,
    )
    return service, saver, dispatcher


@pytest.mark.asyncio
async def test_edit_first_turn_removes_all_following_turns(tmp_path) -> None:
    service, saver, dispatcher = await _build_service(
        tmp_path,
        [_job("msg_1", JobStatus.completed, job_id="job_1"), _job("msg_2", JobStatus.completed)],
    )

    result = await service.replay(
        "ses_replay",
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
    latest = await saver.aget_tuple(build_checkpoint_config("ses_replay"))
    assert latest is not None
    final_messages = latest.checkpoint["channel_values"]["messages"]
    replacement_count = sum(
        item.response_metadata.get("message_id") == dispatcher.dispatched_message_id
        for item in final_messages
    )
    assert replacement_count == 1
    assert latest.checkpoint["channel_values"]["todos"] == ["initial"]
    assert "_summarization_event" not in latest.checkpoint["channel_values"]


@pytest.mark.asyncio
async def test_regenerate_last_reply_reuses_original_prompt(tmp_path) -> None:
    service, _, dispatcher = await _build_service(
        tmp_path,
        [_job("msg_2", JobStatus.completed)],
    )

    result = await service.replay(
        "ses_replay",
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
    failure_status: JobStatus,
) -> None:
    service, saver, dispatcher = await _build_service(
        tmp_path,
        [_job("msg_2", failure_status)],
    )

    result = await service.replay(
        "ses_replay",
        "msg_2",
        MessageReplayRequest(
            action="retry_failed",
            acknowledge_context_only=True,
        ),
    )

    assert result.action == "retry_failed"
    assert dispatcher.pre_dispatch_message_ids == ["msg_1", "assistant_1"]
    latest = await saver.aget_tuple(build_checkpoint_config("ses_replay"))
    assert latest is not None
    final_messages = latest.checkpoint["channel_values"]["messages"]
    assert not any(isinstance(message, ToolMessage) for message in final_messages)
    assert not any(
        isinstance(message, AIMessage) and bool(message.tool_calls)
        for message in final_messages
    )
    assert sum(
        message.response_metadata.get("message_id") == dispatcher.dispatched_message_id
        for message in final_messages
    ) == 1


@pytest.mark.asyncio
async def test_retry_requires_failed_or_timed_out_job(tmp_path) -> None:
    service, _, _ = await _build_service(
        tmp_path,
        [_job("msg_2", JobStatus.completed)],
    )

    with pytest.raises(ValueError, match="不是失败轮次"):
        await service.replay(
            "ses_replay",
            "msg_2",
            MessageReplayRequest(
                action="retry_failed",
                acknowledge_context_only=True,
            ),
        )


@pytest.mark.asyncio
async def test_replay_rejects_running_job_before_mutating_checkpoint(tmp_path) -> None:
    service, saver, dispatcher = await _build_service(
        tmp_path,
        [_job("msg_2", JobStatus.running)],
    )
    before = await saver.aget_tuple(build_checkpoint_config("ses_replay"))
    assert before is not None

    with pytest.raises(ValueError, match="仍有运行中任务"):
        await service.replay(
            "ses_replay",
            "msg_2",
            MessageReplayRequest(
                action="edit_and_continue",
                content="不能提交",
                acknowledge_context_only=True,
            ),
        )

    after = await saver.aget_tuple(build_checkpoint_config("ses_replay"))
    assert after is not None
    assert after.checkpoint["id"] == before.checkpoint["id"]
    assert dispatcher.dispatched_message_id == ""


@pytest.mark.asyncio
async def test_replay_requires_explicit_context_only_acknowledgement(tmp_path) -> None:
    service, _, _ = await _build_service(
        tmp_path,
        [_job("msg_2", JobStatus.completed)],
    )

    with pytest.raises(ValueError, match="不会撤销工作区文件修改"):
        await service.replay(
            "ses_replay",
            "msg_2",
            MessageReplayRequest(action="regenerate"),
        )
