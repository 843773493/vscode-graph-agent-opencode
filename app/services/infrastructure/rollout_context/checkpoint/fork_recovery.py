"""Checkpoint fork 的prepared journal 与 retention 恢复 owner。"""


from __future__ import annotations

import asyncio
import json

from app.core.session_catalog_store import SessionCreationRecord
from app.core.session_catalog_store.contracts import ForkSourceCaptureRecord
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.core.session_control_store.fork_retention_release import (
    read_fork_retention_lease,
)
from app.core.session_creation import ForkCompletionProof
from app.core.session_lifecycle_gate import (
    NavigationTopologyGate,
    SessionDeletionPendingError,
    SessionLifecycleGate,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.fork_source import (
    CheckpointForkSource,
    RolloutForkResult,
)


class CheckpointForkRecovery:
    def __init__(self, *, storage, source: CheckpointForkSource) -> None:
        self._storage = storage
        self._source = source

    async def begin_pinned_fork_retention_claim(self, capture: ForkSourceCaptureRecord) -> int:
        """capture identity 已预分配；在 source 短 gate 内建立同一 preparing claim。"""
        storage = self._storage
        async with (
            NavigationTopologyGate(storage.storage.sessions_dir).shared(),
            SessionLifecycleGate(storage.storage.sessions_dir).exclusive(capture.source_session_id),
        ):
            catalog = storage.forks._catalog_store()
            if catalog.get_fork_source_capture(copy_operation_id=capture.copy_operation_id) != capture:
                raise RuntimeError("fork retention claim 与冻结 capture record 不一致")
            node = catalog.get_node(capture.source_session_id)
            if node.state != "active":
                raise SessionDeletionPendingError("session_deletion_pending: fork retention source 正在删除")
            storage.storage._resolve_thread_node(capture.source_session_id, capture.source_thread_id)
            generation = storage.forks._source_lifecycle_generation(capture.source_session_id)
            claim = storage.forks.begin_pinned_fork_retention_claim(
                claim_id=capture.fork_id, source_session_id=capture.source_session_id,
                target_session_id=capture.target_session_id,
                source_lifecycle_generation=generation,
            )
            if claim.state not in {"preparing", "active"}:
                raise RuntimeError("fork retention claim 已 released，禁止重新准入")
            # catalog blocker 先 durable；只有同 capture 的 source lease 随后
            # 提交成功才允许 capture。两库事务顺序执行，崩溃由原 target 恢复。
            control = SessionControlStore(storage.storage._resolve_session_node(capture.source_session_id) / CONTROL_DATABASE_NAME)
            try:
                lease = control.create_or_get_lease(
                    operation_kind="fork_retention", operation_identity="fork-retention:" + capture.fork_id,
                    preimage_hash=capture.preimage_hash, expected_generation=generation,
                    recovery_ref=capture.copy_operation_id,
                )
                if lease.state != "active":
                    raise RuntimeError("fork retention source lease 已进入释放，禁止新 capture")
            finally:
                control.close()
        return generation

    async def activate_pinned_fork_retention(self, capture: ForkSourceCaptureRecord) -> None:
        """先关闭 target journal reader，再单独取得 source gate 激活原 claim/ref。"""
        storage = self._storage
        checkpoint_ns = json.loads(capture.request_json)["checkpoint_ns"]
        with storage.storage._connect(capture.target_thread_id, checkpoint_ns,
                                      session_id=capture.target_session_id, read_only=True) as connection:
            row = connection.execute(
                "SELECT fork_id, source_session_id, source_thread_id, target_session_id, target_thread_id, "
                "source_checkpoint_id, source_view_id, relationship, status FROM fork_materializations "
                "WHERE materialization_id = ?", (capture.materialization_id,),
            ).fetchone()
        if row is None or row[:5] != (
            capture.fork_id, capture.source_session_id, capture.source_thread_id,
            capture.target_session_id, capture.target_thread_id,
        ) or row[7] != "pinned" or row[8] not in {"target_committed", "committed"}:
            raise RuntimeError("fork retention activation 与 target_committed journal 不一致")
        async with (
            NavigationTopologyGate(storage.storage.sessions_dir).shared(),
            SessionLifecycleGate(storage.storage.sessions_dir).exclusive(capture.source_session_id),
        ):
            node = storage.forks._catalog_store().get_node(capture.source_session_id)
            if node.state != "active":
                raise SessionDeletionPendingError("session_deletion_pending: pinned fork source 正在删除")
            claim = storage.forks._catalog_store().get_fork_retention_claim(capture.fork_id)
            control = SessionControlStore(storage.storage._resolve_session_node(capture.source_session_id) / CONTROL_DATABASE_NAME, read_only=True)
            try:
                lease = read_fork_retention_lease(control, capture, expected_generation=claim.source_lifecycle_generation)
                if lease is None or lease.state != "active":
                    raise RuntimeError("pinned fork activation 缺少原 active source lease")
            finally:
                control.close()
            storage.forks._retain_fork_source(
                source_session_id=capture.source_session_id, source_thread_id=capture.source_thread_id,
                source_checkpoint_id=row[5], source_view_id=row[6], fork_id=capture.fork_id,
                owner_session_id=capture.target_session_id, checkpoint_ns=checkpoint_ns,
            )


    async def release_prepared_fork_retentions(
        self,
        record: SessionCreationRecord,
    ) -> None:
        """在 abort/quarantine 前按 prepared target capability 释放所有引用。"""
        with self._storage.storage.fork_paths.prepared_fork_target_scope(record):
            await self._storage.forks.release_fork_retentions(record.session_id)


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
        with self._storage.storage.fork_paths.prepared_fork_target_scope(record):
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


    async def recover_materialized_fork_completion(
        self, record: SessionCreationRecord,
    ) -> ForkCompletionProof | None:
        """只按 creation capability 定点恢复；prepared 不能发布，committed 补证明。"""
        with self._storage.storage.fork_paths.prepared_fork_target_scope(record):
            target_owner = self._source.resolve_prepared_main_thread_owner(record)
            if not self._storage.storage.index_path(record.session_id,
                                            thread_id=target_owner.thread_id).is_file():
                return None
            with self._storage.storage._connect(target_owner.thread_id, session_id=record.session_id,
                                        read_only=True) as connection:
                rows = connection.execute(
                    "SELECT materialization_id, fork_id, source_session_id, source_checkpoint_id, "
                    "source_view_id, fork_mode, relationship, checkpoint_ns, source_thread_id, "
                    "target_thread_id, source_snapshot_sha256, status FROM fork_materializations "
                    "WHERE target_session_id=? AND target_thread_id=? LIMIT 2",
                    (record.session_id, target_owner.thread_id),
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
                return None
            if status not in {"target_committed", "committed"}:
                raise RuntimeError(f"prepared fork recovery journal 状态非法: {status}")
            if relationship == "pinned":
                capture = self._storage.forks._catalog_store().get_fork_source_capture(fork_id=fork_id)
                if capture is None:
                    raise RuntimeError("pinned fork recovery 缺少冻结 capture record")
                await self.activate_pinned_fork_retention(capture)
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
