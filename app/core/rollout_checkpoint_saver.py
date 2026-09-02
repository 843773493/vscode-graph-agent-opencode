"""基于 rollout 增量日志的 LangGraph CheckpointSaver。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self
from uuid import uuid4

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.checkpoint_config import build_checkpoint_config
from app.core.history_loading import HistoryLoadingConfig
from app.core.rollout_append_writer import RolloutAppendWriter
from app.core.rollout_context_reader import RolloutContextReader
from app.core.rollout_storage import (
    RolloutCompactionResult,
    RolloutPruningPlan,
    RolloutStorage,
    RolloutTurnAnchor,
)
from app.schemas.internal_v2.turn import (
    TurnHistoryLoadRequest,
    TurnHistoryPageDTO,
    TurnSummaryDTO,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader


@dataclass(frozen=True, slots=True)
class RolloutForkResult:
    """Saver 完成一次 fork 数据物化后返回的来源定位。"""

    source_checkpoint_id: str | None
    source_view_id: str | None


class RolloutCheckpointSaver(
    BaseCheckpointSaver[str],
    AbstractContextManager,
    AbstractAsyncContextManager,
):
    """将 messages channel 作为 rollout 增量记录，其它 channel 随 checkpoint 保存。"""

    def __init__(
        self,
        sessions_dir: str | Path,
        *,
        serde: JsonPlusSerializer | None = None,
        storage: RolloutStorage | None = None,
        writer: RolloutAppendWriter | None = None,
        context_reader: RolloutContextReader | None = None,
        history_reader: RolloutHistoryReader | None = None,
    ) -> None:
        super().__init__(serde=serde)
        self._serde = serde or JsonPlusSerializer()
        self._storage = storage or RolloutStorage(sessions_dir, serde=self._serde)
        self._writer = writer or RolloutAppendWriter(
            sessions_dir,
            storage=self._storage,
        )
        self._context_reader = context_reader or RolloutContextReader(self._storage)
        # 历史 DTO 读取器属于 Saver 内部实现，业务层只依赖本类。
        if history_reader is None:
            from app.services.infrastructure.rollout_history_reader import (
                RolloutHistoryReader,
            )

            history_reader = RolloutHistoryReader(self._context_reader)
        self._history_reader = history_reader
        self._lock = threading.RLock()

    def bootstrap_history(
        self,
        session_id: str,
        *,
        policy: HistoryLoadingConfig | None = None,
    ) -> tuple[TurnSummaryDTO | None, str | None, int]:
        """通过内部历史 reader 生成会话 bootstrap。"""
        latest, older_cursor, projection_epoch = self._history_reader.bootstrap(
            session_id,
            policy=policy,
        )
        return latest, older_cursor, projection_epoch

    def mark_turn_terminal_status(
        self,
        *,
        session_id: str,
        turn_id: str,
        status: str,
    ) -> bool:
        """通过 Saver 统一持久化失败、取消和超时等 Turn 终态。"""
        return self._writer.mark_turn_terminal_status(
            session_id=session_id,
            turn_id=turn_id,
            status=status,
        )

    def load_history(
        self,
        session_id: str,
        request: TurnHistoryLoadRequest,
        *,
        policy: HistoryLoadingConfig | None = None,
    ) -> TurnHistoryPageDTO:
        """通过内部历史 reader 读取 projection/detail 历史页面。"""
        return self._history_reader.load(session_id, request, policy=policy)

    async def afork(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        mode: str = "context_fork",
        turn_id: str | None = None,
        anchor_mode: str = "inclusive",
        checkpoint_id: str | None = None,
        anchor: str | None = None,
        relationship: str = "detached",
        checkpoint_ns: str = "",
    ) -> RolloutForkResult:
        """统一物化三种 fork 的 rollout、checkpoint 和 SQLite 状态。"""
        if mode not in {"context_fork", "history_prefix_fork", "full_rollout_copy"}:
            raise ValueError(f"不支持的 fork mode: {mode}")
        if turn_id is not None and (checkpoint_id is not None or anchor is not None):
            raise ValueError("fork 只能传 turn_id，不能同时传 checkpoint_id 或 anchor")

        turn_anchor = None
        if turn_id is None and checkpoint_id is None and anchor is None:
            turn_anchor = await self.aresolve_latest_completed_turn_anchor(
                build_checkpoint_config(
                    source_session_id, checkpoint_ns=checkpoint_ns
                ),
                anchor_mode=anchor_mode,
            )
            if turn_anchor is not None:
                turn_id = turn_anchor.turn_id
        if turn_anchor is None and turn_id is not None:
            turn_anchor = await self.aresolve_turn_anchor(
                build_checkpoint_config(
                    source_session_id, checkpoint_ns=checkpoint_ns
                ),
                turn_id=turn_id,
                anchor_mode=anchor_mode,
                require_completed=True,
            )
        if turn_anchor is not None:
            source_checkpoint = await self.aget_tuple(
                build_checkpoint_config(
                    source_session_id,
                    checkpoint_ns=checkpoint_ns,
                    checkpoint_id=turn_anchor.checkpoint_id,
                )
            )
        else:
            source_checkpoint = await self._resolve_fork_checkpoint(
                source_session_id,
                checkpoint_id=checkpoint_id,
                anchor=anchor,
                checkpoint_ns=checkpoint_ns,
            )

        source_checkpoint_id = (
            str(source_checkpoint.checkpoint.get("id"))
            if source_checkpoint is not None
            and isinstance(source_checkpoint.checkpoint.get("id"), str)
            else checkpoint_id
        )
        source_view_id: str | None = (
            turn_anchor.view_id if turn_anchor is not None else None
        )

        if mode == "full_rollout_copy":
            source_view_id = self.clone_rollout(
                source_thread_id=source_session_id,
                target_thread_id=target_session_id,
                checkpoint_ns=checkpoint_ns,
                source_checkpoint_id=source_checkpoint_id,
            )

        materialization_id, _fork_id = self._storage.begin_fork_materialization(
            target_session_id=target_session_id,
            source_session_id=source_session_id,
            source_checkpoint_id=source_checkpoint_id,
            source_view_id=source_view_id,
            fork_mode=mode,
            relationship=relationship,
            checkpoint_ns=checkpoint_ns,
        )

        if mode == "full_rollout_copy":
            if turn_anchor is not None:
                self.rewind_to_turn(
                    build_checkpoint_config(
                        target_session_id, checkpoint_ns=checkpoint_ns
                    ),
                    turn_id=turn_anchor.turn_id,
                    anchor_mode=anchor_mode,
                )
        elif source_checkpoint is not None:
            if mode == "history_prefix_fork" and turn_anchor is None:
                await self._copy_history_prefix(
                    source_session_id,
                    target_session_id,
                    source_checkpoint_id=source_checkpoint_id,
                    checkpoint_ns=checkpoint_ns,
                )
            else:
                messages_override = None
                if turn_anchor is not None:
                    _resolved, messages_override = await self.amaterialize_turn_anchor(
                        build_checkpoint_config(
                            source_session_id, checkpoint_ns=checkpoint_ns
                        ),
                        turn_id=turn_anchor.turn_id,
                        anchor_mode=anchor_mode,
                    )
                await self._copy_fork_checkpoint(
                    source_checkpoint.checkpoint,
                    source_session_id,
                    target_session_id,
                    fork_mode=mode,
                    anchor=turn_id or anchor,
                    messages_override=messages_override,
                    checkpoint_ns=checkpoint_ns,
                )

        self._storage.commit_fork_materialization(
            materialization_id,
            target_session_id=target_session_id,
            source_session_id=source_session_id,
            source_checkpoint_id=source_checkpoint_id,
            source_view_id=source_view_id,
            fork_mode=mode,
            relationship=relationship,
            checkpoint_ns=checkpoint_ns,
        )
        return RolloutForkResult(
            source_checkpoint_id=source_checkpoint_id,
            source_view_id=source_view_id,
        )

    async def _resolve_fork_checkpoint(
        self,
        source_session_id: str,
        *,
        checkpoint_id: str | None,
        anchor: str | None,
        checkpoint_ns: str = "",
    ) -> CheckpointTuple | None:
        if checkpoint_id is not None or anchor is None:
            return await self.aget_tuple(
                build_checkpoint_config(
                    source_session_id,
                    checkpoint_ns=checkpoint_ns,
                    checkpoint_id=checkpoint_id,
                )
            )

        checkpoints = [
            item
            async for item in self.alist(
                build_checkpoint_config(
                    source_session_id, checkpoint_ns=checkpoint_ns
                )
            )
        ]
        candidate: CheckpointTuple | None = None
        for item in reversed(checkpoints):
            messages = self._checkpoint_messages(item.checkpoint)
            anchor_index = next(
                (
                    index
                    for index, message in enumerate(messages)
                    if self._message_matches_anchor(message, anchor)
                ),
                None,
            )
            if anchor_index is None:
                continue
            if any(
                isinstance(message, HumanMessage)
                and not self._message_is_internal(message)
                for message in messages[anchor_index + 1 :]
            ):
                if candidate is not None:
                    return candidate
                continue
            candidate = item
        if candidate is None:
            raise KeyError(f"源 rollout 不存在 anchor: {anchor}")
        return candidate

    async def _copy_fork_checkpoint(
        self,
        source_checkpoint: Checkpoint,
        source_session_id: str,
        target_session_id: str,
        *,
        fork_mode: str,
        anchor: str | None,
        messages_override: list[BaseMessage] | None,
        checkpoint_ns: str = "",
    ) -> None:
        checkpoint = deepcopy(source_checkpoint)
        channel_values = checkpoint.get("channel_values")
        channel_versions = checkpoint.get("channel_versions")
        if not isinstance(channel_values, dict) or not isinstance(channel_versions, dict):
            raise TypeError("源会话 checkpoint channel 状态结构非法")
        missing_versions = set(channel_values) - set(channel_versions)
        if missing_versions:
            missing_text = ", ".join(sorted(str(name) for name in missing_versions))
            raise ValueError(f"源会话 checkpoint 状态通道缺少版本: {missing_text}")
        if messages_override is not None:
            channel_values["messages"] = messages_override
        self._mark_forked_messages(
            channel_values=channel_values,
            source_session_id=source_session_id,
        )
        source_checkpoint_id = checkpoint.get("id")
        if not isinstance(source_checkpoint_id, str) or not source_checkpoint_id:
            raise ValueError("源 checkpoint 缺少字符串 id")
        checkpoint["id"] = str(uuid4())
        checkpoint["updated_channels"] = list(channel_values)
        child_config = await self.aput(
            config=build_checkpoint_config(
                target_session_id, checkpoint_ns=checkpoint_ns
            ),
            checkpoint=checkpoint,
            metadata={
                "source": "fork",
                "step": -1,
                "parents": {},
                "fork_mode": fork_mode,
                "source_session_id": source_session_id,
                "source_anchor": anchor,
            },
            new_versions=channel_versions,
        )
        self.copy_pending_writes(
            source_thread_id=source_session_id,
            source_checkpoint_id=source_checkpoint_id,
            target_thread_id=target_session_id,
            target_checkpoint_id=str(child_config["configurable"]["checkpoint_id"]),
            checkpoint_ns=checkpoint_ns,
        )

    async def _copy_history_prefix(
        self,
        source_session_id: str,
        target_session_id: str,
        *,
        source_checkpoint_id: str | None,
        checkpoint_ns: str = "",
    ) -> None:
        if not isinstance(source_checkpoint_id, str):
            raise TypeError("history_prefix_fork 缺少源 checkpoint")
        tuples = [
            item
            async for item in self.alist(
                build_checkpoint_config(
                    source_session_id, checkpoint_ns=checkpoint_ns
                )
            )
        ]
        anchor_index = next(
            (
                index
                for index, item in enumerate(tuples)
                if item.checkpoint.get("id") == source_checkpoint_id
            ),
            None,
        )
        if anchor_index is None:
            raise KeyError(f"源 checkpoint 不存在: {source_checkpoint_id}")
        for item in reversed(tuples[anchor_index:]):
            await self._copy_fork_checkpoint(
                item.checkpoint,
                source_session_id,
                target_session_id,
                fork_mode="history_prefix_fork",
                anchor=source_checkpoint_id,
                messages_override=None,
                checkpoint_ns=checkpoint_ns,
            )

    @staticmethod
    def _checkpoint_messages(checkpoint: Checkpoint) -> list[object]:
        channel_values = checkpoint.get("channel_values")
        if not isinstance(channel_values, dict):
            raise TypeError("源会话 checkpoint.channel_values 必须是 dict")
        messages = channel_values.get("messages", [])
        if not isinstance(messages, list):
            raise TypeError("源会话 checkpoint messages 必须是 list")
        return messages

    @staticmethod
    def _message_matches_anchor(message: object, anchor: str) -> bool:
        if not isinstance(message, BaseMessage):
            return False
        if message.id == anchor:
            return True
        response_metadata = message.response_metadata or {}
        if response_metadata.get("message_id") == anchor:
            return True
        message_metadata = response_metadata.get("message_metadata")
        return isinstance(message_metadata, Mapping) and anchor in {
            message_metadata.get("turn_id"),
            message_metadata.get("job_id"),
        }

    @staticmethod
    def _message_is_internal(message: BaseMessage) -> bool:
        response_metadata = message.response_metadata or {}
        if response_metadata.get("internal") is True:
            return True
        message_metadata = response_metadata.get("message_metadata")
        return (
            isinstance(message_metadata, Mapping)
            and message_metadata.get("internal") is True
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
            raise TypeError("源会话 checkpoint messages 必须是 list")
        marked_messages: list[object] = []
        for message in raw_messages:
            if isinstance(message, BaseMessage):
                response_metadata = dict(message.response_metadata or {})
                response_metadata["context_fork_source_session_id"] = source_session_id
                marked_messages.append(
                    message.model_copy(update={"response_metadata": response_metadata})
                )
                continue
            if isinstance(message, Mapping):
                copied_message = dict(message)
                raw_metadata = copied_message.get("response_metadata")
                if raw_metadata is not None and not isinstance(raw_metadata, Mapping):
                    raise TypeError("checkpoint message.response_metadata 必须是 mapping")
                response_metadata = dict(raw_metadata or {})
                response_metadata["context_fork_source_session_id"] = source_session_id
                copied_message["response_metadata"] = response_metadata
                marked_messages.append(copied_message)
                continue
            raise TypeError(
                "源会话 checkpoint 包含不支持的消息类型: "
                f"{type(message).__name__}"
            )
        channel_values["messages"] = marked_messages

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id, checkpoint_ns = _checkpoint_identity(config)
        with self._context_reader.open_snapshot(
            thread_id,
            checkpoint_ns,
        ) as snapshot:
            checkpoint_index = self._context_reader.latest_checkpoint(
                snapshot,
                get_checkpoint_id(config),
            )
            if checkpoint_index is None:
                return None
            active_view_id = (
                self._storage.active_view_id(snapshot)
                if get_checkpoint_id(config) is None
                else None
            )
            checkpoint = self._storage.load_checkpoint(
                thread_id,
                checkpoint_ns,
                checkpoint_index,
                snapshot=snapshot,
                context_view_id_override=active_view_id,
            )
            return CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": checkpoint_index.checkpoint_id,
                    }
                },
                checkpoint=checkpoint,
                metadata=self._decode_metadata(checkpoint_index),
                pending_writes=self._storage.pending_writes(
                    thread_id,
                    checkpoint_ns,
                    checkpoint_index.checkpoint_id,
                    snapshot=snapshot,
                ),
                parent_config=(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": checkpoint_index.parent_checkpoint_id,
                        }
                    }
                    if checkpoint_index.parent_checkpoint_id
                    else None
                ),
            )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        thread_ids = (
            (config["configurable"]["thread_id"],)
            if config is not None
            else tuple(self._storage.list_thread_ids())
        )
        checkpoint_ns = (
            config["configurable"].get("checkpoint_ns") if config is not None else None
        )
        before_id = get_checkpoint_id(before) if before is not None else None
        yielded = 0
        for thread_id in thread_ids:
            indexes = self._storage.list_checkpoints(
                thread_id,
                checkpoint_ns,
                before_checkpoint_id=before_id,
                limit=limit,
            )
            for index in indexes:
                metadata = self._decode_metadata(index)
                if filter and not all(
                    metadata.get(key) == value for key, value in filter.items()
                ):
                    continue
                yield self._tuple_from_index(thread_id, index)
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id, checkpoint_ns = _checkpoint_identity(config)
        channel_values = checkpoint.get("channel_values", {})
        if not isinstance(channel_values, dict):
            raise TypeError("checkpoint.channel_values 必须是 dict")
        checkpoint = dict(checkpoint)
        parent_checkpoint = None
        parent_checkpoint_id = config["configurable"].get("checkpoint_id")
        if isinstance(parent_checkpoint_id, str) and parent_checkpoint_id:
            parent_checkpoint = self.get_tuple(config)
        current_messages = channel_values.get("messages", [])
        if not isinstance(current_messages, list) or not all(
            isinstance(message, BaseMessage) for message in current_messages
        ):
            raise TypeError("checkpoint messages 必须是 LangChain BaseMessage 列表")
        if "messages" not in channel_values:
            if (
                parent_checkpoint is None
                and isinstance(parent_checkpoint_id, str)
                and parent_checkpoint_id
            ):
                raise RuntimeError(
                    f"父 checkpoint 不可读取且当前 checkpoint 未携带 messages: {parent_checkpoint_id}"
                )
            if parent_checkpoint is not None:
                parent_values = parent_checkpoint.checkpoint.get("channel_values", {})
                inherited_messages = (
                    parent_values.get("messages")
                    if isinstance(parent_values, dict)
                    else None
                )
                if not isinstance(inherited_messages, list) or not all(
                    isinstance(message, BaseMessage) for message in inherited_messages
                ):
                    raise RuntimeError(
                        f"父 checkpoint 的 messages channel 不可恢复: {parent_checkpoint_id}"
                    )
            else:
                inherited_messages = []
            channel_values = dict(channel_values)
            channel_values["messages"] = inherited_messages
            checkpoint["channel_values"] = channel_values
            current_messages = inherited_messages
        elif parent_checkpoint is None and isinstance(parent_checkpoint_id, str):
            # LangGraph 的首个实际写入可能带着尚未持久化的内存父 ID。
            # 当前 checkpoint 已携带完整 messages 快照，按根 checkpoint 落盘，
            # 避免新 rollout 从一开始就生成不可解析的孤儿父引用。
            parent_checkpoint_id = None
        channel_versions = checkpoint.get("channel_versions", {})
        if not isinstance(channel_versions, dict):
            raise TypeError("checkpoint.channel_versions 必须是 dict")
        merged_channel_versions = dict(channel_versions)
        for channel_name, version in new_versions.items():
            merged_channel_versions.setdefault(channel_name, version)
        parent_channel_versions = (
            parent_checkpoint.checkpoint.get("channel_versions", {})
            if parent_checkpoint is not None
            else {}
        )
        for channel_name in channel_values:
            if merged_channel_versions.get(channel_name) is None:
                version = new_versions.get(channel_name)
                if version is None and isinstance(parent_channel_versions, dict):
                    version = parent_channel_versions.get(channel_name)
                if version is None and channel_name == "messages":
                    # 某些 LangGraph 节点只提交其它 channel 的新版本，但仍会
                    # 携带完整 messages 快照；此时用 checkpoint ID 标记未变更的
                    # messages，避免因索引缺版本而丢失整个 checkpoint。
                    version = f"checkpoint:{checkpoint['id']}"
                if version is not None:
                    merged_channel_versions[channel_name] = version
        checkpoint["channel_versions"] = merged_channel_versions
        effective_metadata = get_checkpoint_metadata(config, metadata)
        self._writer.append_checkpoint(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint=checkpoint,
            metadata=effective_metadata,
            parent_checkpoint_id=parent_checkpoint_id,
            current_messages=current_messages,
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def finalize_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        final_message_id: str,
    ) -> None:
        """根据最终消息 ID 写入 Turn 完成指针，不复制消息正文。"""
        self._writer.finalize_turn(
            session_id=session_id,
            turn_id=turn_id,
            final_message_id=final_message_id,
        )

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, checkpoint_ns = _checkpoint_identity(config)
        checkpoint_id = config["configurable"].get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ValueError("put_writes 缺少 checkpoint_id")
        self._writer.append_writes(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=checkpoint_id,
            writes=((channel, value) for channel, value in writes),
            task_id=task_id,
            task_path=task_path,
        )

    def delete_thread(self, thread_id: str) -> None:
        self._storage.delete_thread(thread_id)

    def clone_rollout(
        self,
        *,
        source_thread_id: str,
        target_thread_id: str,
        checkpoint_ns: str = "",
        source_checkpoint_id: str | None,
    ) -> str | None:
        return self._storage.clone_rollout(
            source_thread_id=source_thread_id,
            target_thread_id=target_thread_id,
            checkpoint_ns=checkpoint_ns,
            source_checkpoint_id=source_checkpoint_id,
        )

    def copy_turn_finalizations(
        self,
        *,
        source_session_id: str,
        source_checkpoint_id: str | None,
        target_session_id: str,
        checkpoint_ns: str = "",
    ) -> int:
        """复制 fork 源 checkpoint 中已完成 Turn 的最终消息指针。"""
        return self._writer.copy_turn_finalizations(
            source_session_id=source_session_id,
            source_checkpoint_id=source_checkpoint_id,
            target_session_id=target_session_id,
            checkpoint_ns=checkpoint_ns,
        )

    def cancel_unfinished_turns(
        self,
        *,
        session_id: str,
        checkpoint_ns: str = "",
    ) -> int:
        """取消 fork 目标中不会由子会话继续执行的未完成 Turn。"""
        return self._writer.cancel_unfinished_turns(
            session_id=session_id,
            checkpoint_ns=checkpoint_ns,
        )

    def record_fork_origin(
        self,
        *,
        target_thread_id: str,
        source_session_id: str,
        source_checkpoint_id: str | None,
        source_view_id: str | None,
        fork_mode: str,
        relationship: str = "detached",
        checkpoint_ns: str = "",
    ) -> str:
        return self._storage.record_fork_origin(
            target_thread_id=target_thread_id,
            source_session_id=source_session_id,
            source_checkpoint_id=source_checkpoint_id,
            source_view_id=source_view_id,
            fork_mode=fork_mode,
            relationship=relationship,
            checkpoint_ns=checkpoint_ns,
        )

    def copy_pending_writes(
        self,
        *,
        source_thread_id: str,
        source_checkpoint_id: str,
        target_thread_id: str,
        target_checkpoint_id: str,
        checkpoint_ns: str = "",
    ) -> None:
        self._storage.copy_pending_writes(
            source_thread_id=source_thread_id,
            source_checkpoint_id=source_checkpoint_id,
            target_thread_id=target_thread_id,
            target_checkpoint_id=target_checkpoint_id,
            checkpoint_ns=checkpoint_ns,
        )

    def release_fork_retentions(self, child_session_id: str) -> None:
        self._storage.release_fork_retentions(child_session_id)

    def rollout_id(self, thread_id: str, checkpoint_ns: str = "") -> str:
        return self._storage.rollout_id(thread_id, checkpoint_ns)

    def pinned_fork_children(self, source_thread_id: str) -> tuple[str, ...]:
        return self._storage.pinned_fork_children(source_thread_id)

    def plan_pruning(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = "",
        retain_checkpoint_ids: Sequence[str] = (),
        audit_before_sequence: int | None = None,
    ) -> RolloutPruningPlan:
        return self._storage.plan_pruning(
            thread_id,
            checkpoint_ns,
            retain_checkpoint_ids=retain_checkpoint_ids,
            audit_before_sequence=audit_before_sequence,
        )

    def execute_pruning(
        self,
        thread_id: str,
        plan: RolloutPruningPlan,
        *,
        checkpoint_ns: str = "",
    ) -> tuple[str, ...]:
        return self._storage.execute_pruning(thread_id, plan, checkpoint_ns)

    def compact_jsonl_offline(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> RolloutCompactionResult:
        """显式执行单文件 JSONL 回收；普通读取和 pruning 不会触发。"""
        return self._storage.compact_jsonl_offline(thread_id, checkpoint_ns)

    def rewind(
        self,
        config: RunnableConfig,
        *,
        checkpoint_id: str,
        source_anchor: str | None = None,
        anchor_mode: str = "inclusive",
    ) -> RunnableConfig:
        thread_id, checkpoint_ns = _checkpoint_identity(config)
        self._storage.rewind_to_checkpoint(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=checkpoint_id,
            source_anchor=source_anchor,
            anchor_mode=anchor_mode,
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
            }
        }

    def resolve_turn_anchor(
        self,
        config: RunnableConfig,
        *,
        turn_id: str,
        anchor_mode: str = "inclusive",
        require_completed: bool = False,
    ) -> RolloutTurnAnchor:
        thread_id, checkpoint_ns = _checkpoint_identity(config)
        with self._context_reader.open_snapshot(
            thread_id,
            checkpoint_ns,
        ) as snapshot:
            return self._context_reader.resolve_turn_anchor(
                snapshot,
                turn_id,
                anchor_mode=anchor_mode,
                require_completed=require_completed,
            )

    def resolve_latest_completed_turn_anchor(
        self,
        config: RunnableConfig,
        *,
        anchor_mode: str = "inclusive",
    ) -> RolloutTurnAnchor | None:
        thread_id, checkpoint_ns = _checkpoint_identity(config)
        with self._context_reader.open_snapshot(
            thread_id,
            checkpoint_ns,
        ) as snapshot:
            return self._context_reader.resolve_latest_completed_turn_anchor(
                snapshot,
                anchor_mode=anchor_mode,
            )

    def materialize_turn_anchor(
        self,
        config: RunnableConfig,
        *,
        turn_id: str,
        anchor_mode: str = "inclusive",
    ) -> tuple[RolloutTurnAnchor, list[BaseMessage]]:
        thread_id, checkpoint_ns = _checkpoint_identity(config)
        with self._context_reader.open_snapshot(
            thread_id,
            checkpoint_ns,
        ) as snapshot:
            anchor = self._context_reader.resolve_turn_anchor(
                snapshot,
                turn_id,
                anchor_mode=anchor_mode,
            )
            return anchor, self._context_reader.read_turn_anchor_messages(
                snapshot,
                anchor,
            )

    def rewind_to_turn(
        self,
        config: RunnableConfig,
        *,
        turn_id: str,
        anchor_mode: str = "inclusive",
    ) -> RunnableConfig:
        thread_id, checkpoint_ns = _checkpoint_identity(config)
        manifest = self._storage.rewind_to_turn(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            turn_id=turn_id,
            anchor_mode=anchor_mode,
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": manifest.latest_checkpoint_id,
            }
        }

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(
            self.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)

    async def arewind(
        self,
        config: RunnableConfig,
        *,
        checkpoint_id: str,
        source_anchor: str | None = None,
        anchor_mode: str = "inclusive",
    ) -> RunnableConfig:
        return await asyncio.to_thread(
            self.rewind,
            config,
            checkpoint_id=checkpoint_id,
            source_anchor=source_anchor,
            anchor_mode=anchor_mode,
        )

    async def aresolve_turn_anchor(
        self,
        config: RunnableConfig,
        *,
        turn_id: str,
        anchor_mode: str = "inclusive",
        require_completed: bool = False,
    ) -> RolloutTurnAnchor:
        return await asyncio.to_thread(
            self.resolve_turn_anchor,
            config,
            turn_id=turn_id,
            anchor_mode=anchor_mode,
            require_completed=require_completed,
        )

    async def aresolve_latest_completed_turn_anchor(
        self,
        config: RunnableConfig,
        *,
        anchor_mode: str = "inclusive",
    ) -> RolloutTurnAnchor | None:
        return await asyncio.to_thread(
            self.resolve_latest_completed_turn_anchor,
            config,
            anchor_mode=anchor_mode,
        )

    async def amaterialize_turn_anchor(
        self,
        config: RunnableConfig,
        *,
        turn_id: str,
        anchor_mode: str = "inclusive",
    ) -> tuple[RolloutTurnAnchor, list[BaseMessage]]:
        return await asyncio.to_thread(
            self.materialize_turn_anchor,
            config,
            turn_id=turn_id,
            anchor_mode=anchor_mode,
        )

    async def arewind_to_turn(
        self,
        config: RunnableConfig,
        *,
        turn_id: str,
        anchor_mode: str = "inclusive",
    ) -> RunnableConfig:
        return await asyncio.to_thread(
            self.rewind_to_turn,
            config,
            turn_id=turn_id,
            anchor_mode=anchor_mode,
        )

    def get_next_version(self, current: str | None, channel: None) -> str:
        if current is None:
            current_version = 0
        elif isinstance(current, int):
            current_version = current
        else:
            current_version = int(current.split(".", 1)[0])
        return f"{current_version + 1:032}.{__import__('os').urandom(4).hex()}"

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def _tuple_from_index(self, thread_id: str, index: Any) -> CheckpointTuple:
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": index.checkpoint_ns,
                "checkpoint_id": index.checkpoint_id,
            }
        }
        value = self.get_tuple(config)
        if value is None:
            raise RuntimeError(
                f"checkpoint 索引无法 materialize: {index.checkpoint_id}"
            )
        return value

    def _decode_metadata(self, index: Any) -> CheckpointMetadata:
        return self._storage.metadata(index)


def _checkpoint_identity(config: RunnableConfig) -> tuple[str, str]:
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        raise TypeError("checkpoint config 缺少 configurable")
    thread_id = configurable.get("thread_id")
    checkpoint_ns = configurable.get("checkpoint_ns", "")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("checkpoint config 缺少 thread_id")
    if not isinstance(checkpoint_ns, str):
        raise TypeError("checkpoint config checkpoint_ns 必须是字符串")
    return thread_id, checkpoint_ns


def _messages_from_checkpoint(value: CheckpointTuple | None) -> list[BaseMessage]:
    if value is None:
        return []
    channel_values = value.checkpoint.get("channel_values", {})
    if not isinstance(channel_values, dict):
        raise TypeError("checkpoint channel_values 必须是 dict")
    messages = channel_values.get("messages", [])
    if not isinstance(messages, list) or not all(
        isinstance(message, BaseMessage) for message in messages
    ):
        raise TypeError("checkpoint messages 必须是 BaseMessage 列表")
    return messages
