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
    get_checkpoint_id,
    get_checkpoint_metadata,
)

from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutPruningPlan,
    RolloutTurnAnchor,
)


def _checkpoint_identity(config: RunnableConfig) -> tuple[str, str]:
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        raise TypeError("checkpoint config 缺少 configurable")
    thread_id = configurable.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("checkpoint config 缺少 thread_id")
    checkpoint_ns = configurable.get("checkpoint_ns", "")
    if not isinstance(checkpoint_ns, str):
        raise TypeError("checkpoint config checkpoint_ns 必须是字符串")
    return thread_id, checkpoint_ns


class RolloutLangGraphCheckpointMixin:
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

    def list_fork_identity_mappings(
        self,
        target_thread_id: str,
        *,
        fork_id: str | None = None,
        entity_type: str | None = None,
        checkpoint_ns: str = "",
    ) -> list[dict[str, object]]:
        return self._storage.list_fork_identity_mappings(
            target_thread_id,
            fork_id=fork_id,
            entity_type=entity_type,
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


    def rewind(
        self,
        config: RunnableConfig,
        *,
        checkpoint_id: str,
        source_anchor: str | None = None,
        anchor_mode: str = "inclusive",
    ) -> RunnableConfig:
        thread_id, checkpoint_ns = _checkpoint_identity(config)
        manifest = self._storage.rewind_to_checkpoint(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=checkpoint_id,
            source_anchor=source_anchor,
            anchor_mode=anchor_mode,
        )
        self.reconcile_context(
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
        self.reconcile_context(
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
        manifest = self._storage.history_replay_to_turn(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            turn_id=turn_id,
            anchor_mode=anchor_mode,
        )
        self.reconcile_context(
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
            }
        }

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
