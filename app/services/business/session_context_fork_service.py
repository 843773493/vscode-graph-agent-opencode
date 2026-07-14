from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from uuid import uuid4

from langchain_core.messages import BaseMessage
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointMetadata

from app.core.checkpoint_config import build_checkpoint_config
from app.schemas.public_v2.session import (
    SessionCreateRequest,
    SessionDTO,
    SessionUpdateRequest,
)
from app.services.business.session_service import SessionService


class SessionContextForkService:
    """基于最新 Agent checkpoint 创建独立子会话。"""

    def __init__(
        self,
        *,
        session_service: SessionService,
        checkpointer: BaseCheckpointSaver,
    ) -> None:
        self._session_service = session_service
        self._checkpointer = checkpointer

    async def fork(self, source_session_id: str) -> SessionDTO:
        source_session = await self._session_service.get(source_session_id)
        source_checkpoint = await self._checkpointer.aget_tuple(
            build_checkpoint_config(source_session_id)
        )

        child_session = await self._session_service.create(
            SessionCreateRequest(
                title=f"{source_session.title}（上下文副本）",
                title_source="auto",
                agent_id=source_session.current_agent_id,
            )
        )
        child_session = await self._session_service.update(
            child_session.session_id,
            SessionUpdateRequest(parent_session_id=source_session_id),
        )

        if source_checkpoint is None:
            return child_session

        try:
            await self._copy_checkpoint(
                source_checkpoint.checkpoint,
                source_session_id,
                child_session.session_id,
            )
        except BaseException:
            await self._checkpointer.adelete_thread(child_session.session_id)
            await self._session_service.delete(child_session.session_id)
            raise

        return child_session

    async def _copy_checkpoint(
        self,
        source_checkpoint: dict[str, object],
        source_session_id: str,
        child_session_id: str,
    ) -> None:
        checkpoint = deepcopy(source_checkpoint)
        channel_values = checkpoint.get("channel_values")
        channel_versions = checkpoint.get("channel_versions")
        if not isinstance(channel_values, dict):
            raise TypeError(
                "源会话 checkpoint.channel_values 必须是 dict，"
                f"实际类型: {type(channel_values).__name__}"
            )
        if not isinstance(channel_versions, dict):
            raise TypeError(
                "源会话 checkpoint.channel_versions 必须是 dict，"
                f"实际类型: {type(channel_versions).__name__}"
            )

        missing_versions = set(channel_values) - set(channel_versions)
        if missing_versions:
            missing_text = ", ".join(sorted(str(name) for name in missing_versions))
            raise ValueError(f"源会话 checkpoint 状态通道缺少版本: {missing_text}")

        self._mark_forked_messages(
            channel_values=channel_values,
            source_session_id=source_session_id,
        )

        checkpoint["id"] = str(uuid4())
        checkpoint["pending_sends"] = []
        checkpoint["updated_channels"] = list(channel_values)
        metadata: CheckpointMetadata = {
            "source": "fork",
            "step": -1,
            "parents": {},
        }
        await self._checkpointer.aput(
            config=build_checkpoint_config(child_session_id),
            checkpoint=checkpoint,
            metadata=metadata,
            new_versions=channel_versions,
        )

    @staticmethod
    def _mark_forked_messages(
        *,
        channel_values: dict[object, object],
        source_session_id: str,
    ) -> None:
        raw_messages = channel_values.get("messages")
        if raw_messages is None:
            return
        if not isinstance(raw_messages, list):
            raise TypeError(
                "源会话 checkpoint messages 必须是 list，"
                f"实际类型: {type(raw_messages).__name__}"
            )

        marked_messages: list[object] = []
        for message in raw_messages:
            if isinstance(message, BaseMessage):
                response_metadata = dict(message.response_metadata or {})
                response_metadata["context_fork_source_session_id"] = (
                    source_session_id
                )
                marked_messages.append(
                    message.model_copy(
                        update={"response_metadata": response_metadata}
                    )
                )
                continue
            if isinstance(message, Mapping):
                copied_message = dict(message)
                raw_metadata = copied_message.get("response_metadata")
                if raw_metadata is not None and not isinstance(raw_metadata, Mapping):
                    raise TypeError(
                        "checkpoint message.response_metadata 必须是 mapping，"
                        f"实际类型: {type(raw_metadata).__name__}"
                    )
                response_metadata = dict(raw_metadata or {})
                response_metadata["context_fork_source_session_id"] = (
                    source_session_id
                )
                copied_message["response_metadata"] = response_metadata
                marked_messages.append(copied_message)
                continue
            raise TypeError(
                "源会话 checkpoint messages 包含不支持的元素类型: "
                f"{type(message).__name__}"
            )
        channel_values["messages"] = marked_messages
