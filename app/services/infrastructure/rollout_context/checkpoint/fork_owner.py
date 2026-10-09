"""Checkpoint fork 的显式 source/copy/recovery 编排入口。"""


from __future__ import annotations

import asyncio
import tempfile
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from app.core.session_catalog_store import SessionCreationRecord
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.fork_copy import (
    CheckpointForkCopy,
)
from app.services.infrastructure.rollout_context.checkpoint.fork_recovery import (
    CheckpointForkRecovery,
)
from app.services.infrastructure.rollout_context.checkpoint.fork_source import (
    CheckpointForkSource,
    RolloutForkResult,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_text,
)


class CheckpointForkOwner:
    def __init__(
        self, *, storage, source: CheckpointForkSource,
        copy: CheckpointForkCopy, recovery: CheckpointForkRecovery,
    ) -> None:
        self._storage = storage
        self.source = source
        self.copy = copy
        self.recovery = recovery

    async def afork(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        source_owner: MutationIntentOwner | None = None,
        target_owner: MutationIntentOwner | None = None,
        creation_record: SessionCreationRecord | None = None,
        fork_id: str | None = None,
        materialization_id: str | None = None,
        mode: str = "context_fork",
        turn_id: str | None = None,
        anchor_mode: str = "inclusive",
        checkpoint_id: str | None = None,
        anchor: str | None = None,
        relationship: str = "detached",
        checkpoint_ns: str = "",
        source_preflight: RolloutForkResult | None = None,
    ) -> RolloutForkResult:
        """先冻结全部 source facts，再只从私有快照物化 target。"""
        source_owner = self.source._require_thread_owner(source_session_id, source_owner)
        if creation_record is None:
            target_owner = self.source._require_thread_owner(
                target_session_id, target_owner
            )
        else:
            if creation_record.session_id != target_session_id:
                raise RuntimeError("prepared fork creation record target 不匹配")
            prepared_owner = self.source.resolve_prepared_main_thread_owner(creation_record)
            if target_owner is not None and target_owner != prepared_owner:
                raise RuntimeError(
                    "fork prepared target owner 与 creation record 不匹配"
                )
            target_owner = prepared_owner
        fork_id = uuid4().hex if fork_id is None else strict_text(
            fork_id, field="fork_id"
        )
        materialization_id = (
            uuid4().hex
            if materialization_id is None
            else strict_text(materialization_id, field="materialization_id")
        )
        target_scope = (
            self._storage.storage.prepared_fork_target_scope(creation_record)
            if creation_record is not None else nullcontext()
        )
        with target_scope, tempfile.TemporaryDirectory(
            prefix=".fork-source-snapshot-", dir=self._storage.storage.sessions_dir.parent,
        ) as temporary:
            source_snapshot = await self.source._capture_fork_source_snapshot(
                source_session_id=source_session_id,
                source_owner=source_owner,
                mode=mode,
                checkpoint_ns=checkpoint_ns,
                snapshot_root=Path(temporary),
            )
            with self._storage.storage.fork_source_snapshot_scope(
                session_id=source_session_id,
                thread_id=source_owner.thread_id,
                session_node=source_snapshot.session_node,
            ):
                result = await self.copy._afork_from_snapshot(
                    source_session_id=source_session_id,
                    target_session_id=target_session_id,
                    source_owner=source_owner,
                    target_owner=target_owner,
                    source_snapshot=source_snapshot,
                    fork_id=fork_id,
                    materialization_id=materialization_id,
                    mode=mode,
                    turn_id=turn_id,
                    anchor_mode=anchor_mode,
                    checkpoint_id=checkpoint_id,
                    anchor=anchor,
                    relationship=relationship,
                    checkpoint_ns=checkpoint_ns,
                    source_preflight=source_preflight,
                )
            await asyncio.to_thread(
                self._storage.forks.complete_fork_materialization,
                materialization_id=materialization_id,
                target_session_id=target_session_id,
                target_thread_id=target_owner.thread_id,
                checkpoint_ns=checkpoint_ns,
            )
            return replace(result, journal_state="committed")
