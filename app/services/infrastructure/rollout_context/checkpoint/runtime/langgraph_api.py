"""LangGraph checkpoint public adapter over the v2 Saver ports。"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    DeltaChannelHistory,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

from app.core.phase_observer import observe_phase_sync
from app.core.session_thread_context import (
    require_current_session_thread_owner,
    session_thread_owner_scope,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
    RolloutPruningPlan,
    RolloutTurnAnchor,
)


def _checkpoint_identity(
    config: RunnableConfig, *, for_write: bool = False,
) -> tuple[str, str]:
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        raise TypeError("checkpoint config 缺少 configurable")
    thread_id = configurable.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("checkpoint config 缺少 thread_id")
    checkpoint_ns = configurable.get("checkpoint_ns", "")
    if not isinstance(checkpoint_ns, str):
        raise TypeError("checkpoint config checkpoint_ns 必须是字符串")
    if for_write and "history_view_id" in configurable:
        raise ValueError("history-projection-read-only: 历史 selector 不能用于 checkpoint/execution 写入")
    return thread_id, checkpoint_ns


class RolloutLangGraphCheckpointMixin:
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id, checkpoint_ns = _checkpoint_identity(config)
        owner = require_current_session_thread_owner(langgraph_thread_id=thread_id)
        return self._get_tuple_for_owner(
            owner.session_id,
            thread_id,
            checkpoint_ns,
            config,
        )

    def get_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str],
    ) -> dict[str, DeltaChannelHistory]:
        if not channels:
            return {}
        thread_id, checkpoint_ns = _checkpoint_identity(config)
        owner = require_current_session_thread_owner(langgraph_thread_id=thread_id)
        try:
            snapshot = self._context_reader.open_snapshot(owner.session_id, thread_id, checkpoint_ns)
        except RolloutNotMaterializedError:
            if "history_view_id" in config["configurable"]:
                raise ValueError("history-projection-unavailable: 尚未物化的 owner 没有历史 view") from None
            return {channel: {"writes": []} for channel in channels}
        with snapshot:
            if "history_view_id" in config["configurable"]:
                self._storage.checkpoints.require_history_view(
                    snapshot, config["configurable"]["history_view_id"],
                )
            return self._storage.checkpoints.delta_channel_history(
                snapshot, get_checkpoint_id(config), channels=channels,
            )

    def _get_tuple_for_owner(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        config: RunnableConfig,
    ) -> CheckpointTuple | None:
        try:
            snapshot = self._context_reader.open_snapshot(session_id, thread_id, checkpoint_ns)
        except RolloutNotMaterializedError:
            if "history_view_id" in config["configurable"]:
                raise ValueError("history-projection-unavailable: 尚未物化的 owner 没有历史 view") from None
            return None
        with snapshot:
            history_view_id = config["configurable"].get("history_view_id")
            if "history_view_id" in config["configurable"]:
                history_view_id = self._storage.checkpoints.require_history_view(snapshot, history_view_id)
            checkpoint_index = self._context_reader.latest_checkpoint(
                snapshot,
                get_checkpoint_id(config),
            )
            if checkpoint_index is None:
                return None
            # 固定 checkpoint 只读冻结 messages；新执行还恢复已接受根输入
            # 与前序终态工具组，当前执行的未 checkpoint 输出仍保持隔离。
            checkpoint = self._storage.checkpoints.load_checkpoint(
                session_id,
                thread_id,
                checkpoint_ns,
                checkpoint_index,
                snapshot=snapshot,
                context_view_id_override=history_view_id,
                include_execution_context=get_checkpoint_id(config) is None and history_view_id is None,
            )
            return CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": checkpoint_index.checkpoint_id,
                        **({"history_view_id": history_view_id} if history_view_id is not None else {}),
                    }
                },
                checkpoint=checkpoint,
                metadata=self._decode_metadata(checkpoint_index),
                pending_writes=self._storage.checkpoints.pending_writes(
                    session_id,
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
        owners, checkpoint_ns = self._checkpoint_list_owners(config, before=before)
        yielded = 0
        for owner in owners:
            for item in self._list_owner_checkpoint_tuples(
                owner, checkpoint_ns, filter=filter, before=before, limit=limit,
            ):
                yield item
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

    def _checkpoint_list_owners(
        self, config: RunnableConfig | None, *, before: RunnableConfig | None,
    ) -> tuple[tuple[MutationIntentOwner, ...], str | None]:
        """同步与异步 list 共用真实 owner 解析和 before 约束。"""
        if config is not None:
            thread_id, checkpoint_ns = _checkpoint_identity(config)
            if before is not None and _checkpoint_identity(before) != (
                thread_id,
                checkpoint_ns,
            ):
                raise ValueError("checkpoint list before 必须属于相同 rollout owner")
            owners = (
                require_current_session_thread_owner(langgraph_thread_id=thread_id),
            )
        else:
            checkpoint_ns = None
            owners = tuple(
                MutationIntentOwner(
                    session_id=session_id,
                    thread_id=self._storage.storage.main_thread_id(session_id),
                )
                for session_id in self._storage.forks.list_thread_ids()
            )
        return owners, checkpoint_ns

    def _list_owner_checkpoint_tuples(
        self, owner: MutationIntentOwner, checkpoint_ns: str | None, *,
        filter: dict[str, Any] | None, before: RunnableConfig | None,
        limit: int | None,
    ) -> Iterator[CheckpointTuple]:
        """在调用方选定的同步或异步准入内复用同一枚举与 materialize 链路。"""
        before_id = get_checkpoint_id(before) if before is not None else None
        indexes = self._storage.checkpoints.list_checkpoints(
            owner.session_id,
            owner.thread_id,
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
            with session_thread_owner_scope(
                owner,
                langgraph_thread_id=owner.thread_id,
            ):
                item = self._tuple_from_index(
                    owner.session_id, owner.thread_id, index
                )
            yield item

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id, checkpoint_ns = _checkpoint_identity(config, for_write=True)
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
        # checkpoint worker 也会先于 stream sink 收到真实 ToolMessage。
        # 屏障必须在 storage 锁前，允许同 Thread 的 queued root 正常接受。
        for call_id in LangChainMessageCodec().tool_result_call_ids(current_messages):
            observe_phase_sync(call_id, "canonical.tool_result.before_commit")
        self._writer.append_checkpoint(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint=checkpoint,
            metadata=effective_metadata,
            parent_checkpoint_id=parent_checkpoint_id,
            current_messages=current_messages,
            consume_epoch_intent=self.consume_mutation_intent,
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }


    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, checkpoint_ns = _checkpoint_identity(config, for_write=True)
        checkpoint_id = config["configurable"].get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ValueError("put_writes 缺少 checkpoint_id")
        self._writer.append_writes(
            session_id=require_current_session_thread_owner(
                langgraph_thread_id=thread_id
            ).session_id,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=checkpoint_id,
            writes=((channel, value) for channel, value in writes),
            task_id=task_id,
            task_path=task_path,
        )


    async def release_fork_retentions(self, child_session_id: str) -> None:
        await self._storage.forks.release_fork_retentions(child_session_id)

    def rollout_id(self, session_id: str, checkpoint_ns: str = "") -> str:
        owner = self.forks.source.resolve_main_thread_owner(session_id)
        return self._storage.storage.rollout_id(
            owner.thread_id, checkpoint_ns, session_id=owner.session_id,
        )

    def pinned_fork_children(self, source_session_id: str) -> tuple[str, ...]:
        return self._storage.forks.pinned_fork_children(source_session_id)

    def plan_pruning(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = "",
        retain_checkpoint_ids: Sequence[str] = (),
        audit_before_sequence: int | None = None,
    ) -> RolloutPruningPlan:
        return self._storage.pruning.plan_pruning(
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
        return self._storage.pruning.execute_pruning(thread_id, plan, checkpoint_ns)

    def rewind(
        self,
        config: RunnableConfig,
        *,
        checkpoint_id: str,
        source_anchor: str | None = None,
        anchor_mode: str = "inclusive",
    ) -> RunnableConfig:
        thread_id, checkpoint_ns = _checkpoint_identity(config, for_write=True)
        manifest = self._storage.checkpoints.rewind_to_checkpoint(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=checkpoint_id,
            source_anchor=source_anchor,
            anchor_mode=anchor_mode,
            consume_epoch_intent=self.consume_mutation_intent,
        )
        self.reconcile_context(
            require_current_session_thread_owner(
                langgraph_thread_id=thread_id
            ).session_id,
            thread_id,
            operation="rewind",
            checkpoint_ns=checkpoint_ns,
            history_view_revision=manifest.history_view_revision,
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
        owner = require_current_session_thread_owner(langgraph_thread_id=thread_id)
        with self._context_reader.open_snapshot(
            owner.session_id,
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
        owner = require_current_session_thread_owner(langgraph_thread_id=thread_id)
        try:
            snapshot = self._context_reader.open_snapshot(owner.session_id, thread_id, checkpoint_ns)
        except RolloutNotMaterializedError:
            return None
        with snapshot:
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
        owner = require_current_session_thread_owner(langgraph_thread_id=thread_id)
        with self._context_reader.open_snapshot(
            owner.session_id,
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
        thread_id, checkpoint_ns = _checkpoint_identity(config, for_write=True)
        manifest = self._storage.checkpoints.rewind_to_turn(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            turn_id=turn_id,
            anchor_mode=anchor_mode,
            consume_epoch_intent=self.consume_mutation_intent,
        )
        self.reconcile_context(
            require_current_session_thread_owner(
                langgraph_thread_id=thread_id
            ).session_id,
            thread_id,
            operation="rewind",
            checkpoint_ns=checkpoint_ns,
            history_view_revision=manifest.history_view_revision,
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": manifest.latest_checkpoint_id,
            }
        }

    def history_replay(
        self,
        config: RunnableConfig,
        *,
        turn_id: str,
        anchor_mode: str = "inclusive",
    ) -> RunnableConfig:
        """只创建同一 owner namespace 的历史 view，不调度原 Turn。"""
        thread_id, checkpoint_ns = _checkpoint_identity(config)
        manifest, history_view_id = self._storage.checkpoints.history_replay_to_turn(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            turn_id=turn_id,
            anchor_mode=anchor_mode,
        )
        self.reconcile_context(
            require_current_session_thread_owner(
                langgraph_thread_id=thread_id
            ).session_id,
            thread_id,
            operation="history_replay",
            checkpoint_ns=checkpoint_ns,
            history_view_revision=manifest.history_view_revision,
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": manifest.latest_checkpoint_id,
                "history_view_id": history_view_id,
            }
        }

    def _tuple_from_index(
        self,
        session_id: str,
        thread_id: str,
        index: Any,
    ) -> CheckpointTuple:
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
        return self._storage.checkpoints.metadata(index)
