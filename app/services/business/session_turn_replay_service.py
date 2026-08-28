"""会话轮次重试、重新生成和编辑后继续的权威上下文回退。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import ClassVar, Protocol
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple

from app.abstractions.job_service import JobServiceProtocol
from app.core.checkpoint_config import build_checkpoint_config
from app.schemas.internal_v2.common import JobStatus, MessageRole
from app.schemas.internal_v2.message import (
    MessageCreateRequest,
    MessageDTO,
    MessageReplayAccepted,
    MessageReplayRequest,
    MessageRunAccepted,
)
from app.services.business.message_service import MessageService
from app.services.business.session_service import SessionService

CONTEXT_ONLY_NOTICE = "已移除目标消息及其后的会话上下文；工作区文件修改不会被撤销。"


class PreparedMessageDispatcher(Protocol):
    async def dispatch_prepared_message(
        self,
        session_id: str,
        message: MessageDTO,
        *,
        requested_agent_id: str | None,
    ) -> MessageRunAccepted: ...


class FailedJobTraceReader(Protocol):
    def read_message_events(self, session_id: str) -> Iterable[object]: ...


class SessionTurnReplayService:
    """在当前会话追加截断 checkpoint，并用稳定 message_id 重新执行。"""

    _TERMINAL_JOB_STATUSES: ClassVar[set[JobStatus]] = {
        JobStatus.completed,
        JobStatus.succeeded,
        JobStatus.failed,
        JobStatus.cancelled,
        JobStatus.timed_out,
    }

    def __init__(
        self,
        *,
        checkpointer: BaseCheckpointSaver,
        message_service: MessageService,
        session_service: SessionService,
        job_service: JobServiceProtocol,
        dispatcher: PreparedMessageDispatcher,
        trace_event_store: FailedJobTraceReader,
    ) -> None:
        self._checkpointer = checkpointer
        self._message_service = message_service
        self._session_service = session_service
        self._job_service = job_service
        self._dispatcher = dispatcher
        self._trace_event_store = trace_event_store
        self._session_locks: dict[str, asyncio.Lock] = {}

    async def replay(
        self,
        session_id: str,
        target_message_id: str,
        request: MessageReplayRequest,
        *,
        target_turn_id: str | None = None,
    ) -> MessageReplayAccepted:
        if not request.acknowledge_context_only:
            raise ValueError(
                "必须确认：操作会移除目标消息及其后的会话上下文，但不会撤销工作区文件修改"
            )

        session_lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with session_lock:
            await self._assert_session_idle(session_id)
            session = await self._session_service.get(session_id)
            visible_messages = (
                await self._message_service.list(session_id=session_id, limit=100_000)
            ).items
            target_index = next(
                (
                    index
                    for index, message in enumerate(visible_messages)
                    if message.message_id == target_message_id
                ),
                -1,
            )
            if target_index < 0:
                raise ValueError(
                    f"目标用户消息不在当前有效上下文中: message_id={target_message_id}"
                )
            target = visible_messages[target_index]
            if target.role != MessageRole.user:
                raise ValueError("轮次操作只能以用户消息为目标")

            await self._validate_action(
                session_id=session_id,
                request=request,
                target=target,
                target_index=target_index,
                visible_messages=visible_messages,
            )
            content = self._resolved_content(request, target)
            replacement = await self._message_service.create(
                session_id,
                MessageCreateRequest(
                    role=MessageRole.user,
                    content=content,
                    attachments=target.attachments,
                    metadata={
                        "replay_action": request.action,
                        "replaced_message_id": target_message_id,
                    },
                ),
            )
            removed_message_count = await self._append_replay_checkpoint(
                session_id=session_id,
                target_message_id=target_message_id,
                target_turn_id=target_turn_id,
                action=request.action,
            )
            accepted = await self._dispatcher.dispatch_prepared_message(
                session_id,
                replacement,
                requested_agent_id=session.current_agent_id,
            )
            return MessageReplayAccepted(
                **accepted.model_dump(),
                session_id=session_id,
                action=request.action,
                replaced_message_id=target_message_id,
                removed_message_count=removed_message_count,
                workspace_changes_reverted=False,
                notice=CONTEXT_ONLY_NOTICE,
            )

    async def replay_turn(
        self,
        session_id: str,
        turn_id: str,
        request: MessageReplayRequest,
    ) -> MessageReplayAccepted:
        """以用户可见的 Turn ID 解析目标，内部再定位用户消息。"""
        visible_messages = (
            await self._message_service.list(session_id=session_id, limit=100_000)
        ).items
        target = next(
            (
                message
                for message in visible_messages
                if message.role == MessageRole.user
                and message.metadata.get("turn_id") == turn_id
            ),
            None,
        )
        if target is None:
            raise ValueError(f"当前有效上下文中不存在目标 Turn: turn_id={turn_id}")
        return await self.replay(
            session_id,
            target.message_id,
            request,
            target_turn_id=turn_id,
        )

    async def _assert_session_idle(self, session_id: str) -> None:
        active = [
            job
            for job in await self._job_service.list(session_id=session_id)
            if job.status not in self._TERMINAL_JOB_STATUSES
        ]
        if active:
            details = ", ".join(f"{job.job_id}:{job.status.value}" for job in active)
            raise ValueError(f"会话仍有运行中任务，不能回退上下文: {details}")

    async def _validate_action(
        self,
        *,
        session_id: str,
        request: MessageReplayRequest,
        target: MessageDTO,
        target_index: int,
        visible_messages: list[MessageDTO],
    ) -> None:
        if request.action == "edit_and_continue":
            if request.content is None or not request.content.strip():
                raise ValueError("编辑并从此处继续需要非空消息内容")
            return

        user_messages = [
            message for message in visible_messages if message.role == MessageRole.user
        ]
        if not user_messages or user_messages[-1].message_id != target.message_id:
            raise ValueError("重试或重新生成只能作用于当前最后一个用户轮次")
        if request.content is not None:
            raise ValueError("重试或重新生成不接受替换内容")

        following = visible_messages[target_index + 1 :]
        if request.action == "regenerate":
            if not any(message.role == MessageRole.assistant for message in following):
                raise ValueError("目标轮次没有可重新生成的 Assistant 回复")
            return

        if request.action == "retry_failed":
            if not await self._turn_has_failed(session_id, target.message_id):
                raise ValueError("目标轮次不是失败轮次")
            return

        raise ValueError(f"未知轮次操作: {request.action}")

    async def _turn_has_failed(self, session_id: str, message_id: str) -> bool:
        matching_jobs = [
            job
            for job in await self._job_service.list(session_id=session_id)
            if job.message_id == message_id
        ]
        if not matching_jobs:
            if self._trace_has_failed_job(session_id, message_id):
                return True
            raise ValueError(f"目标轮次缺少对应 Job: message_id={message_id}")
        matching_jobs.sort(key=lambda job: job.created_at)
        return matching_jobs[-1].status in {JobStatus.failed, JobStatus.timed_out}

    def _trace_has_failed_job(self, session_id: str, message_id: str) -> bool:
        job_message_ids: dict[str, str] = {}
        terminal_statuses: dict[str, str] = {}
        for event in self._trace_event_store.read_message_events(session_id):
            event_type = getattr(event, "type", None)
            job_id = getattr(event, "job_id", None)
            payload = getattr(event, "payload", None)
            if not isinstance(job_id, str) or payload is None:
                continue
            if event_type == "job_created":
                created_message_id = getattr(payload, "message_id", None)
                if isinstance(created_message_id, str):
                    job_message_ids[job_id] = created_message_id
            elif event_type in {"job_completed", "job_cancelled", "job_failed"}:
                terminal_statuses[job_id] = event_type

        return any(
            job_message_id == message_id
            and terminal_statuses.get(job_id) == "job_failed"
            for job_id, job_message_id in job_message_ids.items()
        )

    @staticmethod
    def _resolved_content(request: MessageReplayRequest, target: MessageDTO) -> str:
        if request.action == "edit_and_continue":
            assert request.content is not None
            return request.content.strip()
        return target.content

    async def _append_replay_checkpoint(
        self,
        *,
        session_id: str,
        target_message_id: str,
        target_turn_id: str | None,
        action: str,
    ) -> int:
        latest = await self._checkpointer.aget_tuple(
            build_checkpoint_config(session_id)
        )
        if latest is None:
            raise ValueError("当前会话没有可回退的 checkpoint")
        latest_messages = self._checkpoint_messages(latest)
        target_raw_index = self._message_index(latest_messages, target_message_id)
        if target_raw_index < 0:
            raise ValueError(
                f"目标用户消息不在最新 checkpoint 中: message_id={target_message_id}"
            )

        latest_versions = latest.checkpoint.get("channel_versions")
        if not isinstance(latest_versions, dict):
            raise TypeError("最新 checkpoint.channel_versions 必须是 dict")
        # 先解析所有 replay 必需的下一版本。这个校验必须发生在 rewind
        # 之前，否则损坏的 channel version 会留下只有回退、没有新 checkpoint
        # 的半完成状态。
        next_messages_version = self._checkpointer.get_next_version(
            latest_versions.get("messages"),
            None,
        )
        next_summarization_version = self._checkpointer.get_next_version(
            latest_versions.get("_summarization_event"),
            None,
        )

        if target_turn_id is not None:
            materialize = getattr(
                self._checkpointer,
                "amaterialize_turn_anchor",
                None,
            )
            rewind_to_turn = getattr(self._checkpointer, "arewind_to_turn", None)
            if not callable(materialize) or not callable(rewind_to_turn):
                raise RuntimeError("当前 checkpoint saver 不支持 Turn replay")
            _anchor, base_messages = await materialize(
                latest.config,
                turn_id=target_turn_id,
                anchor_mode="before",
            )
            active_config = await rewind_to_turn(
                latest.config,
                turn_id=target_turn_id,
                anchor_mode="before",
            )
            base = await self._checkpointer.aget_tuple(
                build_checkpoint_config(session_id)
            )
            if base is None:
                raise RuntimeError("Turn rewind 后无法读取 active checkpoint")
        else:
            base, base_messages = await self._checkpoint_before_message(
                latest=latest,
                target_message_id=target_message_id,
            )

            active_config = latest.config
            rewind = getattr(self._checkpointer, "arewind", None)
            base_checkpoint_id = base.checkpoint.get("id")
            if callable(rewind) and isinstance(base_checkpoint_id, str):
                source_anchor = (
                    target_message_id
                    if self._message_index(
                        self._checkpoint_messages(base),
                        target_message_id,
                    )
                    >= 0
                    else None
                )
                active_config = await rewind(
                    latest.config,
                    checkpoint_id=base_checkpoint_id,
                    source_anchor=source_anchor,
                    anchor_mode="before",
                )

        checkpoint = deepcopy(base.checkpoint)
        channel_values = checkpoint.get("channel_values")
        channel_versions = checkpoint.get("channel_versions")
        if not isinstance(channel_values, dict):
            raise TypeError("回退 checkpoint.channel_values 必须是 dict")
        if not isinstance(channel_versions, dict) or not isinstance(
            latest_versions, dict
        ):
            raise TypeError("回退 checkpoint.channel_versions 必须是 dict")

        channel_values["messages"] = base_messages
        new_versions: dict[str, str] = {}
        summarization_event = channel_values.get("_summarization_event")
        if isinstance(summarization_event, Mapping):
            cutoff_index = summarization_event.get("cutoff_index")
            if isinstance(cutoff_index, int) and cutoff_index > len(base_messages):
                channel_values.pop("_summarization_event")
                channel_versions["_summarization_event"] = next_summarization_version
                new_versions["_summarization_event"] = next_summarization_version

        channel_versions["messages"] = next_messages_version
        new_versions["messages"] = next_messages_version
        checkpoint["channel_values"] = channel_values
        checkpoint["channel_versions"] = channel_versions
        checkpoint["id"] = str(uuid4())
        checkpoint["pending_sends"] = []
        checkpoint["updated_channels"] = list(new_versions)
        await self._checkpointer.aput(
            config=active_config,
            checkpoint=checkpoint,
            metadata={
                "source": "session_turn_replay",
                "step": -1,
                "writes": {},
                "replay_action": action,
                "replaced_message_id": target_message_id,
                "workspace_changes_reverted": False,
            },
            new_versions=new_versions,
        )
        return sum(
            1
            for message in latest_messages[target_raw_index:]
            if self._is_visible_message(message)
        )

    async def _checkpoint_before_message(
        self,
        *,
        latest: CheckpointTuple,
        target_message_id: str,
    ) -> tuple[CheckpointTuple, list[object]]:
        checkpoint = latest
        while True:
            checkpoint_messages = self._checkpoint_messages(checkpoint)
            target_index = self._message_index(checkpoint_messages, target_message_id)
            if target_index < 0:
                raise ValueError(
                    "当前 checkpoint 父链在定位目标消息前已失去该消息: "
                    f"message_id={target_message_id}"
                )
            if checkpoint.parent_config is None:
                return checkpoint, checkpoint_messages[:target_index]
            parent = await self._checkpointer.aget_tuple(checkpoint.parent_config)
            if parent is None:
                # 首个真实 checkpoint 可能继承 LangGraph 尚未落盘的内存
                # 初始 checkpoint。当前 checkpoint 已经包含目标消息前的完整
                # 快照，此时直接使用它的前缀即可，不应让 replay 变成 500。
                return checkpoint, checkpoint_messages[:target_index]
            parent_messages = self._checkpoint_messages(parent)
            if self._message_index(parent_messages, target_message_id) < 0:
                return parent, parent_messages
            checkpoint = parent

    @staticmethod
    def _checkpoint_messages(checkpoint: CheckpointTuple) -> list[object]:
        channel_values = checkpoint.checkpoint.get("channel_values")
        if not isinstance(channel_values, Mapping):
            raise TypeError("checkpoint.channel_values 必须是 mapping")
        messages = channel_values.get("messages", [])
        if not isinstance(messages, list):
            raise TypeError("checkpoint messages 必须是 list")
        return list(messages)

    @staticmethod
    def _message_index(messages: list[object], message_id: str) -> int:
        for index, message in enumerate(messages):
            if isinstance(message, BaseMessage):
                metadata = message.response_metadata or {}
            elif isinstance(message, Mapping):
                metadata = message.get("response_metadata") or {}
            else:
                continue
            if (
                isinstance(metadata, Mapping)
                and metadata.get("message_id") == message_id
            ):
                return index
        return -1

    @staticmethod
    def _is_visible_message(message: object) -> bool:
        if isinstance(message, HumanMessage):
            return True
        if isinstance(message, AIMessage):
            return not bool(message.tool_calls) and bool(str(message.content).strip())
        return False
