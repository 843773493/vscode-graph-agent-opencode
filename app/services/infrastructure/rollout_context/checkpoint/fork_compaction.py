"""Checkpoint fork/history-prefix/full-copy 的专责实现。

公开 CheckpointSaver 只保留协议入口；本 mixin 依赖 Saver 提供的
context owner、checkpoint persistence 与 storage ports。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.checkpoint.base import Checkpoint, CheckpointTuple

from app.core.checkpoint_config import build_checkpoint_config
from app.services.infrastructure.rollout_context.fork.full_copy.preflight import (
    read_source_format,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutTurnAnchor,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_optional_text,
    strict_text,
)


@dataclass(frozen=True, slots=True)
class RolloutForkResult:
    """Saver 完成一次 fork 数据物化后返回的来源定位。"""

    source_checkpoint_id: str | None
    source_view_id: str | None


class ForkCompactionMixin:
    async def preflight_fork(
        self,
        *,
        source_session_id: str,
        mode: str = "context_fork",
        turn_id: str | None = None,
        anchor_mode: str = "inclusive",
        checkpoint_id: str | None = None,
        anchor: str | None = None,
        checkpoint_ns: str = "",
    ) -> RolloutForkResult:
        """在创建 target session 前验证 fork source。

        ``context_fork`` 和 ``history_prefix_fork`` 的拒绝必须发生在
        session catalog/物理节点创建之前；该入口只读取 source，不建立
        target journal，也不产生 target-local identity。
        """
        (
            source_checkpoint,
            source_checkpoint_id,
            source_view_id,
            turn_anchor,
        ) = await self._resolve_fork_source(
            source_session_id=source_session_id,
            mode=mode,
            turn_id=turn_id,
            anchor_mode=anchor_mode,
            checkpoint_id=checkpoint_id,
            anchor=anchor,
            checkpoint_ns=checkpoint_ns,
        )
        if mode != "full_rollout_copy" and (
            source_checkpoint is None or source_checkpoint_id is None
        ):
            raise KeyError("fork source checkpoint 不存在")
        if mode in {"context_fork", "history_prefix_fork"}:
            self._validate_fork_boundary(
                source_session_id=source_session_id,
                turn_anchor=turn_anchor,
                source_checkpoint=source_checkpoint,
                checkpoint_ns=checkpoint_ns,
            )
        return RolloutForkResult(
            source_checkpoint_id=source_checkpoint_id,
            source_view_id=source_view_id,
        )

    def _validate_fork_boundary(
        self,
        *,
        source_session_id: str,
        turn_anchor: RolloutTurnAnchor | None,
        source_checkpoint: CheckpointTuple | None,
        checkpoint_ns: str,
    ) -> None:
        """target staging/durable mutation 前验证 source view 闭合。"""
        if source_checkpoint is None:
            return
        if turn_anchor is not None:
            self._storage.validate_fork_source_closure(
                source_session_id,
                checkpoint_ns=checkpoint_ns,
                source_checkpoint_id=turn_anchor.checkpoint_id,
                cutoff_message_sequence=turn_anchor.cutoff_message_sequence,
            )
            return
        source_checkpoint_id = strict_text(
            source_checkpoint.checkpoint.get("id"),
            field="fork.source_checkpoint.id",
        )
        self._storage.validate_fork_source_closure(
            source_session_id,
            checkpoint_ns=checkpoint_ns,
            source_checkpoint_id=source_checkpoint_id,
            cutoff_message_sequence=None,
        )

    async def _resolve_fork_source(
        self,
        *,
        source_session_id: str,
        mode: str,
        turn_id: str | None,
        anchor_mode: str,
        checkpoint_id: str | None,
        anchor: str | None,
        checkpoint_ns: str,
    ) -> tuple[CheckpointTuple | None, str | None, str | None, object | None]:
        source_session_id = strict_text(
            source_session_id,
            field="fork.source_session_id",
        )
        mode = strict_text(mode, field="fork.mode")
        if mode not in {"context_fork", "history_prefix_fork", "full_rollout_copy"}:
            raise ValueError(f"不支持的 fork mode: {mode}")
        turn_id = strict_optional_text(turn_id, field="fork.turn_id")
        checkpoint_id = strict_optional_text(
            checkpoint_id,
            field="fork.checkpoint_id",
        )
        anchor = strict_optional_text(anchor, field="fork.anchor")
        checkpoint_ns = strict_text(
            checkpoint_ns,
            field="fork.checkpoint_ns",
            allow_empty=True,
        )
        anchor_mode = strict_text(anchor_mode, field="fork.anchor_mode")
        if anchor_mode not in {"inclusive", "before"}:
            raise ValueError("fork anchor_mode 必须是 inclusive 或 before")
        if turn_id is not None and (checkpoint_id is not None or anchor is not None):
            raise ValueError("fork 只能传 turn_id，不能同时传 checkpoint_id 或 anchor")

        # full_rollout_copy 对 v1 source 的唯一合法入口是显式 migration
        # staging。这里不能让正常 v2 anchor/history reader 先打开 v1；完整
        # copy 会在 clone_rollout 内把整个 source 导入 target v2，再继续
        # target-local materialization。v1 没有可靠的 v2 checkpoint/view
        # 边界，因此不接受 selector，避免把 legacy message_sequence 冒充
        # 新 runtime 的 checkpoint identity。
        if mode == "full_rollout_copy":
            source_format_version = read_source_format(
                self._storage, source_session_id, checkpoint_ns
            )
            if source_format_version == 1:
                if (
                    turn_id is not None
                    or checkpoint_id is not None
                    or anchor is not None
                ):
                    raise ValueError(
                        "v1 full_rollout_copy 只能迁移完整 source，不能使用 v2 selector"
                    )
                return None, None, None, None

            if turn_id is None and checkpoint_id is None and anchor is None:
                # full 描述全部历史，不能用最近 completed Turn 把运行态尾部裁掉。
                # 具体 active view 由 clone 的只读 source snapshot 决定。
                return None, None, None, None

        turn_anchor = None
        implicit_latest_turn = (
            turn_id is None and checkpoint_id is None and anchor is None
        )
        if implicit_latest_turn:
            turn_anchor = await self.aresolve_latest_completed_turn_anchor(
                build_checkpoint_config(source_session_id, checkpoint_ns=checkpoint_ns),
                anchor_mode=anchor_mode,
            )
            if turn_anchor is None and mode in {"context_fork", "history_prefix_fork"}:
                raise ValueError(
                    "fork_source_not_completed: source 没有可复制的已完成 Turn"
                )
            if turn_anchor is not None:
                turn_id = turn_anchor.turn_id
        if turn_anchor is None and turn_id is not None:
            turn_anchor = await self.aresolve_turn_anchor(
                build_checkpoint_config(source_session_id, checkpoint_ns=checkpoint_ns),
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
        if source_checkpoint is not None:
            source_checkpoint_id = strict_text(
                source_checkpoint.checkpoint.get("id"),
                field="fork.source_checkpoint.id",
            )
        else:
            source_checkpoint_id = checkpoint_id
        source_view_id = turn_anchor.view_id if turn_anchor is not None else None
        return source_checkpoint, source_checkpoint_id, source_view_id, turn_anchor

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
        (
            source_checkpoint,
            source_checkpoint_id,
            source_view_id,
            turn_anchor,
        ) = await self._resolve_fork_source(
            source_session_id=source_session_id,
            mode=mode,
            turn_id=turn_id,
            anchor_mode=anchor_mode,
            checkpoint_id=checkpoint_id,
            anchor=anchor,
            checkpoint_ns=checkpoint_ns,
        )
        if turn_anchor is None and turn_id is not None:
            turn_anchor = await self.aresolve_turn_anchor(
                build_checkpoint_config(source_session_id, checkpoint_ns=checkpoint_ns),
                turn_id=turn_id,
                anchor_mode=anchor_mode,
                require_completed=True,
            )

        if mode in {"context_fork", "history_prefix_fork"}:
            # 冲突必须发生在 begin_fork_materialization（target staging）
            # 与任何 target durable mutation 之前。
            self._validate_fork_boundary(
                source_session_id=source_session_id,
                turn_anchor=turn_anchor,
                source_checkpoint=source_checkpoint,
                checkpoint_ns=checkpoint_ns,
            )

        if mode == "full_rollout_copy":
            source_format = read_source_format(
                self._storage, source_session_id, checkpoint_ns
            )
            if source_format == 1:
                # 保留已有显式 v1 import 入口；typed/protected staging 不解析旧格式。
                source_view_id = self.clone_rollout(
                    source_thread_id=source_session_id,
                    target_thread_id=target_session_id,
                    checkpoint_ns=checkpoint_ns,
                    source_checkpoint_id=source_checkpoint_id,
                )
            else:
                from app.services.infrastructure.rollout_context.fork.full_copy.operation import (
                    full_rollout_copy,
                )

                source_view_id, _fork_id = full_rollout_copy(
                    self._storage,
                    source_session_id=source_session_id,
                    target_session_id=target_session_id,
                    checkpoint_ns=checkpoint_ns,
                    source_checkpoint_id=source_checkpoint_id,
                    relationship=relationship,
                    detail_capability=self._detail_store.fork_detail_capability(),
                )
                if turn_anchor is not None:
                    # full copy 先完成 target-local 安装；cutoff 必须随后使用
                    # identity mapping 得到的 target Turn，不能把 source ID
                    # 直接交给 rewind/dispatch。
                    target_turn_id = (
                        self._storage.fork_target_identity(
                            target_session_id,
                            fork_id=_fork_id,
                            source_session_id=source_session_id,
                            entity_type="turn",
                            source_local_id=turn_anchor.turn_id,
                            checkpoint_ns=checkpoint_ns,
                        )
                        or turn_anchor.turn_id
                    )
                    self.rewind_to_turn(
                        build_checkpoint_config(
                            target_session_id, checkpoint_ns=checkpoint_ns
                        ),
                        turn_id=target_turn_id,
                        anchor_mode=anchor_mode,
                    )
                return RolloutForkResult(
                    source_checkpoint_id=source_checkpoint_id,
                    source_view_id=source_view_id,
                )

        materialization_id, fork_id = self._storage.begin_fork_materialization(
            target_session_id=target_session_id,
            source_session_id=source_session_id,
            source_checkpoint_id=source_checkpoint_id,
            source_view_id=source_view_id,
            fork_mode=mode,
            relationship=relationship,
            checkpoint_ns=checkpoint_ns,
        )

        if mode != "full_rollout_copy" and source_checkpoint is not None:
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
        if (
            mode in {"context_fork", "history_prefix_fork"}
            and source_checkpoint is not None
        ):
            # checkpoint message channel 不覆盖没有 LangChain message 的
            # provider block/tool/reasoning item；先完成 message view，再由
            # storage 按 target 已物化 Turn 补齐 canonical item，避免 target
            # 运行时回读 source JSONL。history_prefix_fork 会复制多个 checkpoint，
            # 也必须在所有 channel 写完后执行一次。
            await asyncio.to_thread(
                self._storage.copy_v2_items_for_fork,
                source_thread_id=source_session_id,
                target_thread_id=target_session_id,
                checkpoint_ns=checkpoint_ns,
                fork_id=fork_id,
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
        if mode == "full_rollout_copy" and turn_anchor is not None:
            # full copy 的 v2 identity 在 commit 事务中才会映射到 target；
            # rewind 必须使用 mapping 之后的 target Turn，不能把 source
            # turn_id 当作 target-local dispatch identity。
            target_turn_id = (
                self._storage.fork_target_identity(
                    target_session_id,
                    fork_id=fork_id,
                    source_session_id=source_session_id,
                    entity_type="turn",
                    source_local_id=turn_anchor.turn_id,
                    checkpoint_ns=checkpoint_ns,
                )
                or turn_anchor.turn_id
            )
            self.rewind_to_turn(
                build_checkpoint_config(target_session_id, checkpoint_ns=checkpoint_ns),
                turn_id=target_turn_id,
                anchor_mode=anchor_mode,
            )
        self._copy_request_only_context_for_fork(
            source_session_id=source_session_id,
            target_session_id=target_session_id,
            fork_id=fork_id,
            checkpoint_ns=checkpoint_ns,
        )
        with self._context_reader.open_snapshot(
            target_session_id,
            checkpoint_ns,
        ) as target_snapshot:
            target_history_revision = target_snapshot.manifest.history_view_revision
        self.reconcile_context(
            target_session_id,
            operation="fork",
            checkpoint_ns=checkpoint_ns,
            history_view_revision=target_history_revision,
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
                build_checkpoint_config(source_session_id, checkpoint_ns=checkpoint_ns)
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
        if not isinstance(channel_values, dict) or not isinstance(
            channel_versions, dict
        ):
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
        source_checkpoint_id = strict_text(
            checkpoint.get("id"),
            field="fork.source_checkpoint.id",
        )
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
        target_configurable = child_config.get("configurable")
        if not isinstance(target_configurable, Mapping):
            raise TypeError("fork target checkpoint config 缺少 configurable")
        target_checkpoint_id = strict_text(
            target_configurable.get("checkpoint_id"),
            field="fork.target_checkpoint_id",
        )
        self.copy_pending_writes(
            source_thread_id=source_session_id,
            source_checkpoint_id=source_checkpoint_id,
            target_thread_id=target_session_id,
            target_checkpoint_id=target_checkpoint_id,
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
        source_checkpoint_id = strict_text(
            source_checkpoint_id,
            field="history_prefix_fork.source_checkpoint_id",
        )
        tuples = [
            item
            async for item in self.alist(
                build_checkpoint_config(source_session_id, checkpoint_ns=checkpoint_ns)
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
                    raise TypeError(
                        "checkpoint message.response_metadata 必须是 mapping"
                    )
                response_metadata = dict(raw_metadata or {})
                response_metadata["context_fork_source_session_id"] = source_session_id
                copied_message["response_metadata"] = response_metadata
                marked_messages.append(copied_message)
                continue
            raise TypeError(
                f"源会话 checkpoint 包含不支持的消息类型: {type(message).__name__}"
            )
        channel_values["messages"] = marked_messages
