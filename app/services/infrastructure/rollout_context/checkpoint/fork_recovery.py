"""Checkpoint fork 的prepared journal 与 retention 恢复 owner。"""


from __future__ import annotations

import asyncio
import json

from app.core.session_catalog_store import SessionCreationRecord
from app.core.session_creation import ForkCompletionProof
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.fork_source import (
    CheckpointForkSource,
    RolloutForkResult,
)


class CheckpointForkRecovery:
    def __init__(self, *, storage, source: CheckpointForkSource) -> None:
        self._storage = storage
        self._source = source

    def begin_pinned_fork_retention_claim(
        self,
        *,
        fork_id: str,
        source_session_id: str,
        target_session_id: str,
    ) -> int:
        """用 source 的真实 lifecycle generation 建立 capture 前 claim。"""
        storage = self._storage
        generation = storage.forks._source_lifecycle_generation(source_session_id)
        storage.forks.begin_pinned_fork_retention_claim(
            claim_id=fork_id,
            source_session_id=source_session_id,
            target_session_id=target_session_id,
            source_lifecycle_generation=generation,
        )
        return generation


    def release_prepared_fork_retentions(
        self,
        record: SessionCreationRecord,
    ) -> None:
        """在 abort/quarantine 前按 prepared target capability 释放所有引用。"""
        with self._storage.storage.prepared_fork_target_scope(record):
            self._storage.forks.release_fork_retentions(record.session_id)


    def read_committed_fork_completion_proof(
        self,
        *,
        record: SessionCreationRecord,
        source_session_id: str,
        source_owner: MutationIntentOwner,
        result: RolloutForkResult,
        mode: str,
        relationship: str,
        checkpoint_ns: str,
    ) -> ForkCompletionProof:
        """只从 target 的 committed journal 构造 provider 可接受的窄证明。"""
        if (
            result.fork_id is None
            or result.materialization_id is None
            or result.source_snapshot_sha256 is None
        ):
            raise RuntimeError("fork 物化结果缺少 committed journal identity/hash")
        with self._storage.storage.prepared_fork_target_scope(record):
            target_owner = self._source.resolve_prepared_main_thread_owner(record)
            materialization_id, fork_id = (
                self._storage.forks.read_committed_fork_materialization(
                    target_session_id=record.session_id,
                    source_session_id=source_session_id,
                    source_thread_id=source_owner.thread_id,
                    target_thread_id=target_owner.thread_id,
                    source_checkpoint_id=result.source_checkpoint_id,
                    source_view_id=result.source_view_id,
                    fork_mode=mode,
                    relationship=relationship,
                    checkpoint_ns=checkpoint_ns,
                    fork_id=result.fork_id,
                    materialization_id=result.materialization_id,
                    source_snapshot_sha256=result.source_snapshot_sha256,
                )
            )
        return ForkCompletionProof(
            target_session_id=record.session_id,
            fork_id=fork_id,
            materialization_id=materialization_id,
        )


    async def recover_prepared_fork_completion(
        self, record: SessionCreationRecord,
    ) -> ForkCompletionProof | None:
        """只按 creation capability 定点恢复；prepared 不能发布，committed 补证明。"""
        with self._storage.storage.prepared_fork_target_scope(record):
            target_owner = self._source.resolve_prepared_main_thread_owner(record)
            if not self._storage.storage.index_path(record.session_id,
                                            thread_id=target_owner.thread_id).is_file():
                return None
            with self._storage.storage._connect(target_owner.thread_id, session_id=record.session_id,
                                        read_only=True) as connection:
                rows = connection.execute(
                    "SELECT materialization_id, fork_id, source_session_id, source_checkpoint_id, "
                    "source_view_id, fork_mode, relationship, checkpoint_ns, source_thread_id, "
                    "target_thread_id, source_snapshot_sha256, status FROM fork_materializations"
                ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise RuntimeError("prepared fork 必须只有一条 materialization journal")
            (materialization_id, fork_id, source_session_id, source_checkpoint_id,
             source_view_id, mode, relationship, checkpoint_ns, source_thread_id,
             target_thread_id, snapshot_sha256, status) = rows[0]
            metadata = json.loads(record.session_metadata_json)
            if metadata["context_source_session_id"] != source_session_id:
                raise RuntimeError("prepared fork recovery source 与冻结 creation record 不一致")
            if target_thread_id != target_owner.thread_id:
                raise RuntimeError("prepared fork recovery target owner 不一致")
            if status in {"prepared", "aborted"}:
                self._storage.forks.abort_fork_materialization(
                    materialization_id, target_session_id=record.session_id,
                    checkpoint_ns=checkpoint_ns,
                )
                return None
            if status not in {"target_committed", "committed"}:
                raise RuntimeError(f"prepared fork recovery journal 状态非法: {status}")
            self._storage.storage._active_fork_materializations.discard(
                (record.session_id, target_owner.thread_id, checkpoint_ns)
            )
            await asyncio.to_thread(self._storage.storage.initialize, record.session_id,
                                    checkpoint_ns, thread_id=target_owner.thread_id)
            return self.read_committed_fork_completion_proof(
                record=record, source_session_id=source_session_id,
                source_owner=MutationIntentOwner(session_id=source_session_id,
                                                 thread_id=source_thread_id),
                result=RolloutForkResult(source_checkpoint_id=source_checkpoint_id,
                                        source_view_id=source_view_id, fork_id=fork_id,
                                        materialization_id=materialization_id,
                                        source_snapshot_sha256=snapshot_sha256),
                mode=mode, relationship=relationship, checkpoint_ns=checkpoint_ns,
            )
