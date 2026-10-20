"""Checkpoint fork 编排；来源读取止于冻结快照解析，目标安装只消费原字节。"""

from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING

from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.fork_source import (
    CheckpointForkSource,
    RolloutForkResult,
)
from app.services.infrastructure.rollout_context.fork.node_debug_materialization import (
    materialize_node_debug_fork,
)
from app.services.infrastructure.rollout_context.fork.snapshot_copy import (
    installed_target_view,
    prepare_target_snapshot,
    publish_target_snapshot,
    recover_target_snapshot_publication,
    stage_captured_rollout,
)
from app.services.infrastructure.rollout_context.fork.source_snapshot import (
    ForkSourceSnapshot,
)

if TYPE_CHECKING:
    from app.core.session_catalog_store import SessionCreationRecord
    from app.core.session_catalog_store.contracts import ForkSourceCaptureRecord
    from app.core.session_control_store import ThreadCreationRecord
    from app.services.infrastructure.rollout_context.checkpoint.saver import (
        RolloutCheckpointSaver,
    )


class CheckpointForkCopy:
    def __init__(
        self, *, checkpoint: RolloutCheckpointSaver, source: CheckpointForkSource,
        storage, context_reader,
    ) -> None:
        self._checkpoint = checkpoint
        self._source = source
        self._storage = storage
        self._context_reader = context_reader

    async def _afork_from_snapshot(
        self, *, source_session_id: str, target_session_id: str,
        source_uri: str,
        source_owner: MutationIntentOwner, target_owner: MutationIntentOwner,
        source_snapshot: ForkSourceSnapshot, fork_id: str, materialization_id: str,
        mode: str, anchor_mode: str, relationship: str,
        checkpoint_ns: str, source_preflight: RolloutForkResult | None,
        source_view_id: str | None = None, source_view_revision: int | None = None,
        anchor_item_id: str | None = None,
        capture_record: ForkSourceCaptureRecord,
        creation_record: SessionCreationRecord | ThreadCreationRecord | None,
    ) -> RolloutForkResult:
        """同 Session child 也必须先退出 source scope，再解析目标 namespace。"""
        storage = self._storage.storage
        with storage.fork_source_snapshot_scope(
            session_id=source_session_id, thread_id=source_owner.thread_id,
            captured_root=source_snapshot.captured_root,
        ), session_thread_owner_scope(source_owner, langgraph_thread_id=source_owner.thread_id):
            resolved = self._source._resolve_fork_source(
                source_session_id=source_session_id, source_thread_id=source_owner.thread_id,
                mode=mode, source_view_id=source_view_id, source_view_revision=source_view_revision,
                anchor_item_id=anchor_item_id, anchor_mode=anchor_mode, checkpoint_ns=checkpoint_ns,
            )
            if source_preflight is not None and (
                resolved.source_checkpoint_id, resolved.source_view_id,
                resolved.source_view_revision, resolved.anchor_item_id,
            ) != (
                source_preflight.source_checkpoint_id, source_preflight.source_view_id,
                source_preflight.source_view_revision, source_preflight.anchor_item_id,
            ):
                raise RuntimeError("source-mismatch: captured source 与 preflight 不一致")
            source_checkpoint_id = resolved.source_checkpoint_id
            source_view_id = resolved.source_view_id
            source_view_revision = resolved.source_view_revision
            anchor_item_id = resolved.anchor_item_id
        if source_view_id is None:
            raise RuntimeError("source-mismatch: fork 没有冻结 source view")
        recover_target_snapshot_publication(
            storage, target_session_id=target_session_id, target_thread_id=target_owner.thread_id,
            checkpoint_ns=checkpoint_ns, materialization_id=materialization_id,
        )
        self._storage.forks.begin_fork_materialization(
            target_session_id=target_session_id, source_session_id=source_session_id,
            source_checkpoint_id=source_checkpoint_id, source_view_id=source_view_id,
            fork_mode=mode, relationship=relationship, checkpoint_ns=checkpoint_ns,
            source_thread_id=source_owner.thread_id, target_thread_id=target_owner.thread_id,
            source_snapshot_sha256=source_snapshot.snapshot_sha256,
            fork_id=fork_id, materialization_id=materialization_id,
        )
        view_id = installed_target_view(
            storage, target_session_id=target_session_id, target_thread_id=target_owner.thread_id,
            checkpoint_ns=checkpoint_ns, materialization_id=materialization_id,
            source_snapshot_sha256=source_snapshot.snapshot_sha256,
        )
        if view_id is None:
            stage = await asyncio.to_thread(
                stage_captured_rollout, source_snapshot,
                target_root=storage.root(target_session_id, checkpoint_ns, thread_id=target_owner.thread_id),
                materialization_id=materialization_id,
            )
            try:
                view_id = await asyncio.to_thread(
                    prepare_target_snapshot, storage, source_snapshot, stage=stage,
                    fork_store=self._storage.forks,
                    target_session_id=target_session_id, target_thread_id=target_owner.thread_id,
                    source_uri=source_uri, source_view_id=source_view_id, checkpoint_ns=checkpoint_ns,
                    materialization_id=materialization_id, prefix=mode != "full_rollout_copy",
                    anchor_item_id=anchor_item_id, anchor_mode=anchor_mode,
                    source_view_revision=source_view_revision,
                    source_checkpoint_id=source_checkpoint_id,
                )
                await asyncio.to_thread(
                    publish_target_snapshot, storage, stage=stage,
                    target_session_id=target_session_id, target_thread_id=target_owner.thread_id,
                    checkpoint_ns=checkpoint_ns, materialization_id=materialization_id,
                )
            except BaseException:
                if stage.exists():
                    shutil.rmtree(stage)
                raise
        if source_snapshot.attachments:
            if self._source.attachment_store is None:
                raise RuntimeError("fork captured 附件缺少唯一 AttachmentBlobStore")
            await self._source.attachment_store.reserve_copy_attachments(
                capture_record, creation_record, source_snapshot.attachments,
            )
        await asyncio.to_thread(
            materialize_node_debug_fork, self._storage, source_snapshot.node_debug,
            self._source.node_debug_workspace_config, materialization_id=materialization_id,
            fork_id=fork_id, target_session_id=target_session_id,
            target_thread_id=target_owner.thread_id, checkpoint_ns=checkpoint_ns,
        )
        with storage._connect(target_owner.thread_id, checkpoint_ns,
                              session_id=target_session_id, read_only=True) as connection:
            status = connection.execute(
                "SELECT status FROM fork_materializations WHERE materialization_id=? "
                "AND target_session_id=? AND target_thread_id=? AND checkpoint_ns=?",
                (materialization_id, target_session_id, target_owner.thread_id, checkpoint_ns),
            ).fetchone()
        if status == ("prepared",):
            with session_thread_owner_scope(target_owner, langgraph_thread_id=target_owner.thread_id):
                self._storage.forks.materialize_fork_checkpoint(
                    materialization_id=materialization_id, target_session_id=target_session_id,
                    target_thread_id=target_owner.thread_id, checkpoint_ns=checkpoint_ns, view_id=view_id,
                )
        self._storage.forks.commit_fork_materialization(
            materialization_id, target_session_id=target_session_id, source_session_id=source_session_id,
            source_checkpoint_id=source_checkpoint_id, source_view_id=source_view_id,
            fork_mode=mode, relationship=relationship, checkpoint_ns=checkpoint_ns,
            source_thread_id=source_owner.thread_id, target_thread_id=target_owner.thread_id,
            source_snapshot_sha256=source_snapshot.snapshot_sha256, defer_completion=True,
        )
        with self._context_reader.open_snapshot(
            target_session_id, target_owner.thread_id, checkpoint_ns,
        ) as target_snapshot:
            revision = target_snapshot.manifest.history_view_revision
        self._checkpoint.reconcile_context(
            target_session_id, target_owner.thread_id, operation="fork",
            checkpoint_ns=checkpoint_ns, history_view_revision=revision,
        )
        return RolloutForkResult(
            source_checkpoint_id=source_checkpoint_id, source_view_id=source_view_id,
            source_view_revision=source_view_revision, anchor_item_id=anchor_item_id,
            fork_id=fork_id, materialization_id=materialization_id,
            journal_state="target_committed", source_snapshot_sha256=source_snapshot.snapshot_sha256,
        )
