"""Checkpoint fork 的target 物化与 checkpoint/history copy owner。"""


from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import TYPE_CHECKING
from uuid import uuid4

from langchain_core.messages import BaseMessage
from langgraph.checkpoint.base import Checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.fork.node_debug_materialization import (
    prepare_node_debug_fork,
    publish_node_debug_fork,
)
from app.services.infrastructure.rollout_context.fork.source_snapshot import (
    ForkSourceSnapshot,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutTurnAnchor,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.checkpoint.saver import (
        RolloutCheckpointSaver,
    )

from app.services.infrastructure.rollout_context.checkpoint.fork_source import (
    CheckpointForkSource,
    RolloutForkResult,
)


class CheckpointForkCopy:
    def __init__(
        self, *, checkpoint: RolloutCheckpointSaver, source: CheckpointForkSource,
        storage, context_reader, detail_store, node_debug_workspace_config,
        copy_request_only_content,
    ) -> None:
        self._checkpoint = checkpoint
        self._source = source
        self._storage = storage
        self._context_reader = context_reader
        self._detail_store = detail_store
        self._node_debug_workspace_config = node_debug_workspace_config
        self._copy_request_only_context_for_fork = copy_request_only_content

    async def _afork_from_snapshot(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        source_owner: MutationIntentOwner,
        target_owner: MutationIntentOwner,
        source_snapshot: ForkSourceSnapshot,
        fork_id: str,
        materialization_id: str,
        mode: str,
        turn_id: str | None,
        anchor_mode: str,
        checkpoint_id: str | None,
        anchor: str | None,
        relationship: str,
        checkpoint_ns: str,
        source_preflight: RolloutForkResult | None,
    ) -> RolloutForkResult:
        """所有 source 读取都经由已冻结的 rollout snapshot scope。"""
        if relationship == "pinned":
            claim = self._storage.forks._catalog_store().get_fork_retention_claim(fork_id)
            if (claim.source_session_id, claim.target_session_id,
                claim.source_lifecycle_generation, claim.state) != (
                source_session_id, target_session_id,
                source_snapshot.rollout.source_lifecycle_generation, "preparing",
            ):
                raise RuntimeError("fork source snapshot 与 capture 前 pinned claim 不一致")
        with session_thread_owner_scope(
            source_owner,
            langgraph_thread_id=source_owner.thread_id,
        ):
            (
                source_checkpoint,
                source_checkpoint_id,
                source_view_id,
                turn_anchor,
            ) = await self._source._resolve_fork_source(
                source_session_id=source_session_id,
                source_thread_id=source_owner.thread_id,
                mode=mode,
                turn_id=turn_id,
                anchor_mode=anchor_mode,
                checkpoint_id=checkpoint_id,
                anchor=anchor,
                checkpoint_ns=checkpoint_ns,
            )
        if turn_anchor is None and turn_id is not None:
            with session_thread_owner_scope(
                source_owner,
                langgraph_thread_id=source_owner.thread_id,
            ):
                turn_anchor = await self._checkpoint.aresolve_turn_anchor(
                    build_checkpoint_config(
                        source_owner.thread_id,
                        checkpoint_ns=checkpoint_ns,
                    ),
                    turn_id=turn_id,
                    anchor_mode=anchor_mode,
                    require_completed=True,
                )

        if mode in {"context_fork", "history_prefix_fork"}:
            # 冲突必须发生在 begin_fork_materialization（target staging）
            # 与任何 target durable mutation 之前。
            self._source._validate_fork_boundary(
                source_session_id=source_session_id,
                source_thread_id=source_owner.thread_id,
                turn_anchor=turn_anchor,
                source_checkpoint=source_checkpoint,
                checkpoint_ns=checkpoint_ns,
            )

        if source_view_id is None:
            source_view_id = self._storage.forks._fork_source_view_id(
                source_session_id,
                source_checkpoint_id,
                checkpoint_ns,
                source_thread_id=source_owner.thread_id,
            )
        if source_preflight is not None and (
            source_checkpoint_id, source_view_id, turn_anchor
        ) != (
            source_preflight.source_checkpoint_id, source_preflight.source_view_id, source_preflight.source_anchor
        ):
            raise RuntimeError("source-mismatch: fork 冻结 snapshot 与 preflight owner/anchor/revision 不一致")

        debug_snapshot = source_snapshot.node_debug_snapshot

        if mode == "full_rollout_copy":
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
                detail_capability=self._detail_store.fork_detail_capability(
                    source_snapshots=source_snapshot.rollout.detail_snapshots
                ),
                debug_snapshot=debug_snapshot,
                debug_workspace_config=self._node_debug_workspace_config,
                fork_id=fork_id,
                materialization_id=materialization_id,
                source_snapshot_sha256=source_snapshot.snapshot_sha256,
                defer_completion=True,
            )
            if isinstance(turn_anchor, RolloutTurnAnchor):
                # full copy 先完成 target-local 安装；cutoff 必须随后使用
                # identity mapping 得到的 target Turn，不能把 source ID
                # 直接交给 rewind/dispatch。
                target_turn_id = self._storage.forks.fork_target_identity(
                    target_session_id,
                    fork_id=_fork_id,
                    source_session_id=source_session_id,
                    entity_type="turn",
                    source_local_id=turn_anchor.turn_id,
                    checkpoint_ns=checkpoint_ns,
                )
                if target_turn_id is None:
                    raise RuntimeError("full copy cutoff 缺少真实 target Turn mapping")
                with session_thread_owner_scope(
                    target_owner,
                    langgraph_thread_id=target_owner.thread_id,
                ):
                    self._checkpoint.rewind_to_turn(
                        build_checkpoint_config(
                            target_owner.thread_id,
                            checkpoint_ns=checkpoint_ns,
                        ),
                        turn_id=target_turn_id,
                        anchor_mode=anchor_mode,
                    )
            return RolloutForkResult(
                source_checkpoint_id=source_checkpoint_id,
                source_view_id=source_view_id,
                fork_id=fork_id,
                materialization_id=materialization_id,
                journal_state="target_committed",
                source_snapshot_sha256=source_snapshot.snapshot_sha256,
            )

        materialization_id, fork_id = self._storage.forks.begin_fork_materialization(
            target_session_id=target_session_id,
            source_session_id=source_session_id,
            source_checkpoint_id=source_checkpoint_id,
            source_view_id=source_view_id,
            fork_mode=mode,
            relationship=relationship,
            checkpoint_ns=checkpoint_ns,
            source_thread_id=source_owner.thread_id,
            target_thread_id=target_owner.thread_id,
            source_snapshot_sha256=source_snapshot.snapshot_sha256,
            fork_id=fork_id,
            materialization_id=materialization_id,
        )
        debug_staged = prepare_node_debug_fork(
            self._storage,
            debug_snapshot,
            self._node_debug_workspace_config,
            materialization_id=materialization_id,
            fork_id=fork_id,
            target_session_id=target_session_id,
            checkpoint_ns=checkpoint_ns,
        )

        if source_checkpoint is not None:
            source_messages = source_checkpoint.checkpoint.get("channel_values", {}).get("messages", [])
            if isinstance(turn_anchor, RolloutTurnAnchor):
                with session_thread_owner_scope(source_owner, langgraph_thread_id=source_owner.thread_id):
                    _resolved, source_messages = await self._checkpoint.amaterialize_turn_anchor(
                        build_checkpoint_config(source_owner.thread_id, checkpoint_ns=checkpoint_ns),
                        turn_id=turn_anchor.turn_id, anchor_mode=anchor_mode,
                    )
            await asyncio.to_thread(
                self._storage.forks.copy_v2_items_for_fork,
                source_session_id=source_session_id, source_thread_id=source_owner.thread_id,
                target_session_id=target_session_id, target_thread_id=target_owner.thread_id,
                checkpoint_ns=checkpoint_ns, fork_id=fork_id,
                materialization_id=materialization_id,
                source_message_ids=tuple(self._storage.storage._codec().message_id(message, index)
                                         for index, message in enumerate(source_messages)),
            )

        if mode != "full_rollout_copy" and source_checkpoint is not None:
            if mode == "history_prefix_fork" and turn_anchor is None:
                await self._copy_history_prefix(
                    source_session_id,
                    target_session_id,
                    source_owner=source_owner,
                    target_owner=target_owner,
                    source_checkpoint_id=source_checkpoint_id,
                    checkpoint_ns=checkpoint_ns,
                    materialization_id=materialization_id,
                )
            else:
                messages_override = None
                if isinstance(turn_anchor, RolloutTurnAnchor):
                    with session_thread_owner_scope(
                        source_owner,
                        langgraph_thread_id=source_owner.thread_id,
                    ):
                        _resolved, messages_override = (
                            await self._checkpoint.amaterialize_turn_anchor(
                                build_checkpoint_config(
                                    source_owner.thread_id,
                                    checkpoint_ns=checkpoint_ns,
                                ),
                                turn_id=turn_anchor.turn_id,
                                anchor_mode=anchor_mode,
                            )
                        )
                await self._copy_fork_checkpoint(
                    source_checkpoint.checkpoint,
                    source_session_id,
                    target_session_id,
                    source_owner=source_owner,
                    target_owner=target_owner,
                    fork_mode=mode,
                    anchor=turn_id or anchor,
                    messages_override=messages_override,
                    checkpoint_ns=checkpoint_ns,
                    materialization_id=materialization_id,
                )
        if debug_snapshot is not None and debug_staged is not None:
            publish_node_debug_fork(
                self._storage,
                debug_snapshot,
                debug_staged,
                self._node_debug_workspace_config,
                materialization_id=materialization_id,
                target_session_id=target_session_id,
                checkpoint_ns=checkpoint_ns,
            )

        self._storage.forks.commit_fork_materialization(
            materialization_id,
            target_session_id=target_session_id,
            source_session_id=source_session_id,
            source_checkpoint_id=source_checkpoint_id,
            source_view_id=source_view_id,
            fork_mode=mode,
            relationship=relationship,
            checkpoint_ns=checkpoint_ns,
            source_thread_id=source_owner.thread_id,
            target_thread_id=target_owner.thread_id,
            source_snapshot_sha256=source_snapshot.snapshot_sha256,
            defer_completion=True,
            detail_capability=self._detail_store.fork_detail_capability(
                source_snapshots=source_snapshot.rollout.detail_snapshots,
            ),
            detail_store=self._detail_store,
        )
        self._copy_request_only_context_for_fork(
            source_session_id=source_session_id,
            source_thread_id=source_owner.thread_id,
            target_session_id=target_session_id,
            target_thread_id=target_owner.thread_id,
            fork_id=fork_id,
            checkpoint_ns=checkpoint_ns,
            source_content_snapshot=source_snapshot.request_only_content,
        )
        target_thread_id = target_owner.thread_id
        with self._context_reader.open_snapshot(
            target_session_id,
            target_thread_id,
            checkpoint_ns,
        ) as target_snapshot:
            target_history_revision = target_snapshot.manifest.history_view_revision
        self._checkpoint.reconcile_context(
            target_session_id,
            target_thread_id,
            operation="fork",
            checkpoint_ns=checkpoint_ns,
            history_view_revision=target_history_revision,
        )
        return RolloutForkResult(
            source_checkpoint_id=source_checkpoint_id,
            source_view_id=source_view_id,
            fork_id=fork_id,
            materialization_id=materialization_id,
            journal_state="target_committed",
            source_snapshot_sha256=source_snapshot.snapshot_sha256,
        )


    def _fork_id_for_materialization(
        self, target_session_id: str, target_thread_id: str,
        materialization_id: str, checkpoint_ns: str,
    ) -> str:
        with self._storage.storage._connect(target_thread_id, checkpoint_ns,
                                    session_id=target_session_id, read_only=True) as connection:
            row = connection.execute(
                "SELECT fork_id FROM fork_materializations WHERE materialization_id = ? AND status = 'prepared'",
                (materialization_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("fork checkpoint 缺少 prepared materialization journal")
        return strict_text(row[0], field="fork.fork_id")


    async def _copy_fork_checkpoint(
        self,
        source_checkpoint: Checkpoint,
        source_session_id: str,
        target_session_id: str,
        *,
        source_owner: MutationIntentOwner,
        target_owner: MutationIntentOwner,
        fork_mode: str,
        anchor: str | None,
        messages_override: list[BaseMessage] | None,
        checkpoint_ns: str = "",
        materialization_id: str,
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
        source_checkpoint_id = strict_text(
            checkpoint.get("id"),
            field="fork.source_checkpoint.id",
        )
        checkpoint["id"] = str(uuid4())
        checkpoint["updated_channels"] = list(channel_values)
        target_items = self._storage.storage.read_items(
            target_session_id, target_owner.thread_id, checkpoint_ns=checkpoint_ns,
        )
        fork_id = self._fork_id_for_materialization(
            target_session_id, target_owner.thread_id, materialization_id, checkpoint_ns,
        )
        target_messages = []
        for index, message in enumerate(channel_values.get("messages", [])):
            source_message_id = self._storage.storage._codec().message_id(message, index)
            target_message_id = self._storage.forks.fork_target_identity(
                target_session_id, source_session_id=source_session_id,
                fork_id=fork_id,
                entity_type="message", source_local_id=source_message_id, checkpoint_ns=checkpoint_ns,
            )
            if target_message_id is None:
                raise RuntimeError(f"fork checkpoint 缺少 target message mapping: {source_message_id}")
            group = tuple(item for item in target_items if item.message_group_id == f"message-{target_message_id}")
            if not group:
                raise RuntimeError(f"fork checkpoint 缺少 target canonical message group: {target_message_id}")
            target_messages.append(self._storage.storage._codec().from_dict(self._storage.storage._codec().project_message(group)))
        channel_values["messages"] = target_messages
        metadata = {"source": "fork", "step": -1, "parents": {}, "fork_mode": fork_mode,
                    "source_session_id": source_session_id, "source_anchor": anchor}
        with self._storage.storage._connect(
            source_owner.thread_id, checkpoint_ns, session_id=source_session_id, read_only=True,
        ) as connection:
            source_metadata = connection.execute(
                "SELECT metadata_json FROM checkpoints WHERE checkpoint_id=? AND checkpoint_ns=?",
                (source_checkpoint_id, checkpoint_ns),
            ).fetchone()
        if source_metadata is None:
            raise RuntimeError("source-mismatch: fork checkpoint 缺少冻结 metadata")
        binding = json.loads(source_metadata[0]).get("boxteam_execution_binding")
        if binding is not None:
            if not isinstance(binding, dict):
                raise TypeError("source-mismatch: internal fork checkpoint binding 必须是 object")
            target_execution = self._storage.forks.fork_target_identity(
                target_session_id, source_session_id=source_session_id, fork_id=fork_id,
                entity_type="execution", source_local_id=binding["execution_id"], checkpoint_ns=checkpoint_ns,
            )
            with self._storage.storage._connect(
                target_owner.thread_id, checkpoint_ns, session_id=target_session_id, read_only=True,
            ) as connection:
                target_binding = connection.execute(
                    "SELECT execution_id,job_id,admission_idempotency_key,admission_payload_hash "
                    "FROM executions WHERE session_id=? AND thread_id=? AND execution_id=? "
                    "AND execution_origin='internal' AND turn_id IS NULL",
                    (target_session_id, target_owner.thread_id, target_execution),
                ).fetchone()
            if target_binding is None:
                raise RuntimeError("source-mismatch: fork checkpoint 缺少本地 internal admission mapping")
            metadata["boxteam_execution_binding"] = dict(zip(
                ("execution_id", "job_id", "admission_idempotency_key", "admission_payload_hash"),
                target_binding, strict=True,
            ))
        with session_thread_owner_scope(target_owner, langgraph_thread_id=target_owner.thread_id):
            self._storage.forks.materialize_fork_checkpoint(
                materialization_id=materialization_id, target_session_id=target_session_id,
                target_thread_id=target_owner.thread_id, checkpoint_ns=checkpoint_ns,
                checkpoint=checkpoint,
                metadata=metadata,
                current_messages=target_messages,
            )
        target_checkpoint_id = checkpoint["id"]
        self._checkpoint.copy_pending_writes(
            source_session_id=source_session_id,
            target_session_id=target_session_id,
            source_thread_id=source_owner.thread_id,
            source_checkpoint_id=source_checkpoint_id,
            target_thread_id=target_owner.thread_id,
            target_checkpoint_id=target_checkpoint_id,
            checkpoint_ns=checkpoint_ns,
        )


    async def _copy_history_prefix(
        self,
        source_session_id: str,
        target_session_id: str,
        *,
        source_owner: MutationIntentOwner,
        target_owner: MutationIntentOwner,
        source_checkpoint_id: str | None,
        checkpoint_ns: str = "",
        materialization_id: str,
    ) -> None:
        source_checkpoint_id = strict_text(
            source_checkpoint_id,
            field="history_prefix_fork.source_checkpoint_id",
        )
        with session_thread_owner_scope(
            source_owner,
            langgraph_thread_id=source_owner.thread_id,
        ):
            tuples = [
                item
                async for item in self._checkpoint.alist(
                    build_checkpoint_config(
                        source_owner.thread_id,
                        checkpoint_ns=checkpoint_ns,
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
                source_owner=source_owner,
                target_owner=target_owner,
                fork_mode="history_prefix_fork",
                anchor=source_checkpoint_id,
                messages_override=None,
                checkpoint_ns=checkpoint_ns,
                materialization_id=materialization_id,
            )
