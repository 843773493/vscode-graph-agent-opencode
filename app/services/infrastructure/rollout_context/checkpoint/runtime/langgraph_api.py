"""LangGraph checkpoint public adapter over the v2 Saver ports。"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any
from uuid import NAMESPACE_URL, uuid5

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
from langgraph.checkpoint.serde.types import ERROR

from app.core.failures import failure_detail
from app.core.phase_observer import observe_phase_sync
from app.core.session_thread_context import (
    require_current_session_thread_owner,
    session_thread_owner_scope,
)
from app.domain.itemized.compaction import ApplyCompactionCandidateIntent
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    checkpoint_messages,
    message_snapshot,
    message_snapshot_metadata,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.runtime.message_values import (
    prepare_message_values,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    count_uncompacted_messages,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
    RolloutPruningPlan,
    RolloutTurnAnchor,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
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
    def raw_message_count(self, session_id: str, thread_id: str, *, item_ids: Sequence[str], checkpoint_ns: str = "") -> int | None:
        """按调用方已读取的精确 canonical anchors 计数，不重新选择当前分支。"""
        with self._storage.storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            return count_uncompacted_messages(connection, item_ids, checkpoint_ns=checkpoint_ns)

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
            owners = self._storage.checkpoints.list_checkpoint_owners()
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
            metadata_filter=filter,
            limit=limit,
        )
        for index in indexes:
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
        return self._put_checkpoint(config, checkpoint, metadata, new_versions)

    def apply_compaction_candidate(
        self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata,
        new_versions: ChannelVersions, *, intent: ApplyCompactionCandidateIntent,
    ) -> RunnableConfig:
        """最终完整预算确认后，候选与 checkpoint 沿原 writer 原子发布。"""
        thread_id, checkpoint_ns = _checkpoint_identity(config, for_write=True)
        owner = require_current_session_thread_owner(langgraph_thread_id=thread_id)
        frozen = intent.frozen
        if (frozen.session_id, frozen.thread_id, frozen.checkpoint_ns) != (owner.session_id, thread_id, checkpoint_ns):
            raise ValueError("source-mismatch: 候选采用必须属于本 checkpoint owner")
        with self._storage.storage.read_connection(owner.session_id, thread_id, checkpoint_ns) as connection:
            stored, _result, _discarded = self._storage.compactions._read(
                connection, owner.session_id, thread_id, frozen.candidate_id, checkpoint_ns,
            )
            if stored != frozen:
                raise ValueError("compaction-candidate-conflict: 采用输入与冻结候选不一致")
            applied = connection.execute(
                "SELECT candidate.applied_view_id,checkpoint.checkpoint_id,view.parent_view_id FROM context_compaction_candidates candidate "
                "LEFT JOIN checkpoints checkpoint ON checkpoint.view_id=candidate.applied_view_id "
                "LEFT JOIN context_views view ON view.view_id=candidate.applied_view_id "
                "WHERE candidate.candidate_id=?", (frozen.candidate_id,),
            ).fetchone()
            if applied is not None and applied[0] is not None:
                if applied[1] is None:
                    raise RuntimeError("compaction-checkpoint-unavailable: 已采用候选缺少恢复 checkpoint")
                if applied[2] != intent.expected_view_id:
                    raise ValueError("compaction-view-conflict: 幂等采用不是同一个预算基准")
                return {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": applied[1]}}
        return self._put_checkpoint(config, checkpoint, metadata, new_versions, compaction_intent=intent)

    def adopt_candidate(self, config: RunnableConfig, intent: ApplyCompactionCandidateIntent) -> CheckpointTuple:
        """沿当前运行的冻结 checkpoint 采用候选，并读回唯一恢复基准。"""
        if not get_checkpoint_id(config):
            raise ValueError("compaction-checkpoint-required: 采用必须显式绑定运行中的 checkpoint")
        source = self.get_tuple(config)
        if source is None:
            raise RuntimeError("compaction-checkpoint-unavailable: 运行中的 checkpoint 不可读取")
        checkpoint = dict(source.checkpoint)
        # LangGraph 以 UUID 字节派生后续 task 身份；同一采用 intent 仍生成同一 checkpoint。
        checkpoint["id"] = str(uuid5(NAMESPACE_URL, canonical_json_text({
            "kind": "compaction", "candidate_id": intent.frozen.candidate_id, "view_id": intent.expected_view_id,
        })))
        checkpoint["channel_values"] = {
            **source.checkpoint["channel_values"],
            "messages": message_snapshot(checkpoint_messages(self, source)),
        }
        version = self.get_next_version(source.checkpoint["channel_versions"].get("messages"), None)
        checkpoint["channel_versions"] = {**source.checkpoint["channel_versions"], "messages": version}
        checkpoint["updated_channels"] = ["messages"]
        applied_config = self.apply_compaction_candidate(
            config, checkpoint,
            {**message_snapshot_metadata(source.metadata), "source": "compaction", "step": source.metadata.get("step", -1)},
            {"messages": version}, intent=intent,
        )
        applied = self.get_tuple(applied_config)
        if applied is None:
            raise RuntimeError("compaction-checkpoint-unavailable: 已采用候选缺少恢复 checkpoint")
        return applied._replace(checkpoint={
            **applied.checkpoint,
            "channel_values": {**applied.checkpoint["channel_values"], "messages": checkpoint_messages(self, applied)},
        })

    def _put_checkpoint(
        self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata,
        new_versions: ChannelVersions, *, compaction_intent: ApplyCompactionCandidateIntent | None = None,
    ) -> RunnableConfig:
        thread_id, checkpoint_ns = _checkpoint_identity(config, for_write=True)
        channel_values = checkpoint.get("channel_values", {})
        if not isinstance(channel_values, dict):
            raise TypeError("checkpoint.channel_values 必须是 dict")
        checkpoint = dict(checkpoint)
        owner = require_current_session_thread_owner(langgraph_thread_id=thread_id)
        current_messages, messages_state, parent_channel_versions, parent_checkpoint_id = prepare_message_values(
            self._context_reader, self._storage.checkpoints, owner=owner,
            checkpoint_ns=checkpoint_ns, parent_checkpoint_id=config["configurable"].get("checkpoint_id"),
            values=channel_values, versions=checkpoint.get("channel_versions", {}),
            new_versions=new_versions, metadata=metadata,
        )
        if messages_state == "view" and current_messages is not None and "messages" not in channel_values:
            channel_values = {**channel_values, "messages": current_messages}
            checkpoint["channel_values"] = channel_values
        channel_versions = checkpoint.get("channel_versions", {})
        if not isinstance(channel_versions, dict):
            raise TypeError("checkpoint.channel_versions 必须是 dict")
        merged_channel_versions = dict(channel_versions)
        for channel_name, version in new_versions.items():
            if channel_name in channel_versions and channel_versions[channel_name] != version:
                raise ValueError(f"checkpoint new_versions 与 channel_versions 不一致: {channel_name}")
            merged_channel_versions.setdefault(channel_name, version)
        channel_names = (*channel_values, "messages") if current_messages is None and messages_state == "view" else channel_values
        for channel_name in channel_names:
            if merged_channel_versions.get(channel_name) is None:
                version = new_versions.get(channel_name)
                if version is None and isinstance(parent_channel_versions, dict):
                    version = parent_channel_versions.get(channel_name)
                if version is None and channel_name == "messages":
                    # 某些 LangGraph 节点只提交其它 channel 的新版本，但仍会
                    # 携带完整 messages 快照；首次版本由唯一生成器分配，
                    # 后续采用和 Graph 递增沿用相同版本语法。
                    version = self.get_next_version(None, None)
                if version is not None:
                    merged_channel_versions[channel_name] = version
        checkpoint["channel_versions"] = merged_channel_versions
        effective_metadata = get_checkpoint_metadata(config, metadata)
        # checkpoint worker 也会先于 stream sink 收到真实 ToolMessage。
        # 屏障必须在 storage 锁前，允许同 Thread 的 queued root 正常接受。
        for call_id in LangChainMessageCodec().tool_result_call_ids(current_messages or ()):
            observe_phase_sync(call_id, "canonical.tool_result.before_commit")
        self._writer.append_checkpoint(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint=checkpoint,
            metadata=effective_metadata,
            parent_checkpoint_id=parent_checkpoint_id,
            current_messages=current_messages,
            messages_state=messages_state,
            consume_epoch_intent=self.consume_mutation_intent,
            new_versions=new_versions,
            compaction_intent=compaction_intent,
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
            writes=(
                (channel, failure_detail(
                    value, stage="checkpoint-task",
                    label=f"执行节点失败: task_id={task_id}, task_path={task_path}",
                ) if channel == ERROR and isinstance(value, BaseException) else value)
                for channel, value in writes
            ),
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
