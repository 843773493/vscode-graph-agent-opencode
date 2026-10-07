"""Checkpoint fork/history-prefix/full-copy 的专责实现。

公开 CheckpointSaver 只保留协议入口；本 mixin 依赖 Saver 提供的
context owner、checkpoint persistence 与 storage ports。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Mapping
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.checkpoint.base import Checkpoint, CheckpointTuple

from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_catalog_store import SessionCreationRecord
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.core.session_creation import ForkCompletionProof
from app.core.session_lifecycle_gate import SessionLifecycleGate
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.node_debug.session.fork import (
    NodeDebugSourceCopySnapshot,
    capture_source_copy_snapshot,
)
from app.services.infrastructure.rollout_context.fork.full_copy.preflight import (
    read_source_format,
)
from app.services.infrastructure.rollout_context.fork.node_debug_materialization import (
    prepare_node_debug_fork,
    publish_node_debug_fork,
)
from app.services.infrastructure.rollout_context.fork.source_snapshot import (
    ForkSourceSnapshot,
    capture_fork_rollout_snapshot,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutInternalForkAnchor,
    RolloutTurnAnchor,
    _RolloutFileLock,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_optional_text,
    strict_text,
)


@dataclass(frozen=True, slots=True)
class RolloutForkResult:
    """Saver 完成并收敛 target fork journal 后返回的 completion facts。"""

    source_checkpoint_id: str | None
    source_view_id: str | None
    fork_id: str | None = None
    materialization_id: str | None = None
    journal_state: str | None = None
    source_snapshot_sha256: str | None = None
    source_anchor: RolloutTurnAnchor | RolloutInternalForkAnchor | None = None


class ForkCompactionMixin:
    def resolve_thread_owner(self, session_id: str, thread_id: str) -> MutationIntentOwner:
        """从两个权威 catalog 解析精确 Session/thread owner。"""
        resolver = self._storage._path_resolver
        resolver.resolve_thread_node(session_id, thread_id)
        return MutationIntentOwner(session_id=session_id, thread_id=thread_id)

    def resolve_main_thread_owner(self, session_id: str) -> MutationIntentOwner:
        resolver = self._storage._path_resolver
        return self.resolve_thread_owner(session_id, resolver.main_thread_id(session_id))

    def resolve_prepared_main_thread_owner(
        self,
        record: SessionCreationRecord,
    ) -> MutationIntentOwner:
        """仅凭 fork 创建 record capability 解析未发布 target main owner。"""
        resolver = self._storage._path_resolver
        session_path = resolver.resolve_prepared_session_node(record)
        thread_id = record.main_thread_id
        thread_path = resolver.resolve_prepared_thread_node(record, thread_id)
        if thread_path != session_path:
            raise RuntimeError(
                "prepared fork main thread 必须折叠到 Session 根目录: "
                f"session_id={record.session_id}, thread_id={thread_id}, "
                f"path={thread_path}"
            )
        control_path = session_path / CONTROL_DATABASE_NAME
        if not control_path.is_file() or control_path.is_symlink():
            raise RuntimeError(
                "prepared fork target 缺少普通文件形式的 per-session thread "
                f"catalog: session_id={record.session_id}, path={control_path}"
            )
        control_store = SessionControlStore(control_path)
        try:
            control_store.verify_matches_catalog_main_thread(thread_id)
        finally:
            control_store.close()
        return MutationIntentOwner(session_id=record.session_id, thread_id=thread_id)

    def begin_pinned_fork_retention_claim(
        self,
        *,
        fork_id: str,
        source_session_id: str,
        target_session_id: str,
    ) -> int:
        """用 source 的真实 lifecycle generation 建立 capture 前 claim。"""
        storage = self._storage
        generation = storage._source_lifecycle_generation(source_session_id)
        storage.begin_pinned_fork_retention_claim(
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
        with self._storage.prepared_fork_target_scope(record):
            self._storage.release_fork_retentions(record.session_id)

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
        with self._storage.prepared_fork_target_scope(record):
            target_owner = self.resolve_prepared_main_thread_owner(record)
            materialization_id, fork_id = (
                self._storage.read_committed_fork_materialization(
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
        with self._storage.prepared_fork_target_scope(record):
            target_owner = self.resolve_prepared_main_thread_owner(record)
            if not self._storage.index_path(record.session_id,
                                            thread_id=target_owner.thread_id).is_file():
                return None
            with self._storage._connect(target_owner.thread_id, session_id=record.session_id,
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
                self._storage.abort_fork_materialization(
                    materialization_id, target_session_id=record.session_id,
                    checkpoint_ns=checkpoint_ns,
                )
                return None
            if status not in {"target_committed", "committed"}:
                raise RuntimeError(f"prepared fork recovery journal 状态非法: {status}")
            self._storage._active_fork_materializations.discard(
                (record.session_id, target_owner.thread_id, checkpoint_ns)
            )
            await asyncio.to_thread(self._storage.initialize, record.session_id,
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

    def _require_thread_owner(
        self,
        session_id: str,
        owner: MutationIntentOwner | None,
    ) -> MutationIntentOwner:
        if owner is None:
            return self.resolve_main_thread_owner(session_id)
        if owner.session_id != session_id:
            raise RuntimeError("fork source owner 与 Session 不匹配")
        return self.resolve_thread_owner(session_id, owner.thread_id)

    def _capture_fork_debug_snapshot(
        self, source_session_id: str, source_thread_id: str, mode: str
    ) -> NodeDebugSourceCopySnapshot | None:
        if mode == "history_prefix_fork":
            return None
        _payload, manifest = self._node_debug_store.read_manifest_payload(
            source_session_id, source_thread_id
        )
        if manifest is None:
            return None
        if self._node_debug_workspace_config is None:
            raise RuntimeError(
                "source 存在调试方案，但 RolloutCheckpointSaver 未注入 Workspace debug 配置"
            )
        workspace_config = self._node_debug_workspace_config()
        return capture_source_copy_snapshot(
            self._node_debug_store,
            session_id=source_session_id,
            thread_id=source_thread_id,
            capture_mode=mode,
            workspace_config_revision=workspace_config.revision,
            workspace_config_hash=workspace_config.content_hash,
            workspace_id=workspace_config.workspace_id,
        )

    async def _capture_fork_source_snapshot(
        self,
        *,
        source_session_id: str,
        source_owner: MutationIntentOwner,
        mode: str,
        checkpoint_ns: str,
        snapshot_root: Path,
    ) -> ForkSourceSnapshot:
        """在 source lifecycle guard 与 rollout shared lock 内冻结全部 source facts。"""
        gate = SessionLifecycleGate(self._storage.sessions_dir)
        async with gate.shared(source_session_id):
            source_rollout = self._storage.root(
                source_session_id,
                checkpoint_ns,
                thread_id=source_owner.thread_id,
            )
            rollout_lock = _RolloutFileLock(
                source_rollout.parent / ".rollout.write.lock",
                exclusive=False,
            )
            rollout_lock.acquire()
            try:
                debug_snapshot = self._capture_fork_debug_snapshot(
                    source_session_id, source_owner.thread_id, mode
                )
                request_only_content = self._capture_request_only_content_for_fork(
                    source_session_id=source_session_id,
                    source_thread_id=source_owner.thread_id,
                    checkpoint_ns=checkpoint_ns,
                )
                rollout_snapshot = await asyncio.to_thread(
                    capture_fork_rollout_snapshot,
                    self._storage,
                    source_session_id=source_session_id,
                    source_thread_id=source_owner.thread_id,
                    snapshot_session_node=snapshot_root / source_session_id,
                    snapshot_id=uuid4().hex,
                    detail_capability=self._detail_store.fork_detail_capability(),
                    rollout_lock_held=True,
                )
            finally:
                rollout_lock.release()
        return ForkSourceSnapshot(
            rollout=rollout_snapshot,
            request_only_content=request_only_content,
            node_debug_snapshot=debug_snapshot,
        )

    async def preflight_fork(
        self,
        *,
        source_session_id: str,
        source_owner: MutationIntentOwner | None = None,
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
        source_owner = self._require_thread_owner(source_session_id, source_owner)
        with session_thread_owner_scope(
            source_owner,
            langgraph_thread_id=source_owner.thread_id,
        ):
            (
                source_checkpoint,
                source_checkpoint_id,
                source_view_id,
                turn_anchor,
            ) = await self._resolve_fork_source(
                source_session_id=source_session_id,
                source_thread_id=source_owner.thread_id,
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
                source_thread_id=source_owner.thread_id,
                turn_anchor=turn_anchor,
                source_checkpoint=source_checkpoint,
                checkpoint_ns=checkpoint_ns,
            )

        if source_view_id is None:
            source_view_id = self._storage._fork_source_view_id(
                source_session_id,
                source_checkpoint_id,
                checkpoint_ns,
                source_thread_id=source_owner.thread_id,
            )

        return RolloutForkResult(
            source_checkpoint_id=source_checkpoint_id,
            source_view_id=source_view_id,
            source_anchor=turn_anchor,
        )

    def _validate_fork_boundary(
        self,
        *,
        source_session_id: str,
        source_thread_id: str,
        turn_anchor: RolloutTurnAnchor | RolloutInternalForkAnchor | None,
        source_checkpoint: CheckpointTuple | None,
        checkpoint_ns: str,
    ) -> None:
        """target staging/durable mutation 前验证 source view 闭合。"""
        if source_checkpoint is None:
            return
        if isinstance(turn_anchor, RolloutTurnAnchor):
            self._storage.validate_fork_source_closure(
                source_session_id,
                thread_id=source_thread_id,
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
            thread_id=source_thread_id,
            checkpoint_ns=checkpoint_ns,
            source_checkpoint_id=source_checkpoint_id,
            cutoff_message_sequence=None,
        )

    async def _resolve_fork_source(
        self,
        *,
        source_session_id: str,
        source_thread_id: str,
        mode: str,
        turn_id: str | None,
        anchor_mode: str,
        checkpoint_id: str | None,
        anchor: str | None,
        checkpoint_ns: str,
    ) -> tuple[CheckpointTuple | None, str | None, str | None, RolloutTurnAnchor | RolloutInternalForkAnchor | None]:
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

        if mode == "full_rollout_copy":
            if read_source_format(self._storage, source_session_id, checkpoint_ns) != 2:
                raise ValueError("full_rollout_copy 只接受真实 v2 source，不提供旧格式 runtime")
            if turn_id is None and checkpoint_id is None and anchor is None:
                return None, None, None, None

        turn_anchor = None
        implicit_latest_turn = (
            turn_id is None and checkpoint_id is None and anchor is None
        )
        if implicit_latest_turn:
            turn_anchor = await self.aresolve_latest_completed_turn_anchor(
                build_checkpoint_config(source_thread_id, checkpoint_ns=checkpoint_ns),
                anchor_mode=anchor_mode,
            )
            if turn_anchor is None and mode in {"context_fork", "history_prefix_fork"}:
                if mode != "context_fork" or anchor_mode != "inclusive":
                    raise ValueError("fork_source_not_completed: source 没有可复制的已完成 Turn")
                with self._context_reader.open_snapshot(source_session_id, source_thread_id, checkpoint_ns) as snapshot:
                    turn_anchor = self._storage.resolve_completed_internal_fork_anchor(snapshot)
            if isinstance(turn_anchor, RolloutTurnAnchor):
                turn_id = turn_anchor.turn_id
        if turn_anchor is None and turn_id is not None:
            turn_anchor = await self.aresolve_turn_anchor(
                build_checkpoint_config(source_thread_id, checkpoint_ns=checkpoint_ns),
                turn_id=turn_id,
                anchor_mode=anchor_mode,
                require_completed=True,
            )
        if turn_anchor is not None:
            source_checkpoint = await self.aget_tuple(
                build_checkpoint_config(
                    source_thread_id,
                    checkpoint_ns=checkpoint_ns,
                    checkpoint_id=turn_anchor.checkpoint_id,
                )
            )
        else:
            source_checkpoint = await self._resolve_fork_checkpoint(
                source_session_id,
                source_thread_id=source_thread_id,
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
        source_owner = self._require_thread_owner(source_session_id, source_owner)
        if creation_record is None:
            target_owner = self._require_thread_owner(
                target_session_id, target_owner
            )
        else:
            if creation_record.session_id != target_session_id:
                raise RuntimeError("prepared fork creation record target 不匹配")
            prepared_owner = self.resolve_prepared_main_thread_owner(creation_record)
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
            self._storage.prepared_fork_target_scope(creation_record)
            if creation_record is not None else nullcontext()
        )
        with target_scope, tempfile.TemporaryDirectory(
            prefix=".fork-source-snapshot-", dir=self._storage.sessions_dir.parent,
        ) as temporary:
            source_snapshot = await self._capture_fork_source_snapshot(
                source_session_id=source_session_id,
                source_owner=source_owner,
                mode=mode,
                checkpoint_ns=checkpoint_ns,
                snapshot_root=Path(temporary),
            )
            with self._storage.fork_source_snapshot_scope(
                session_id=source_session_id,
                thread_id=source_owner.thread_id,
                session_node=source_snapshot.session_node,
            ):
                result = await self._afork_from_snapshot(
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
                self._storage.complete_fork_materialization,
                materialization_id=materialization_id,
                target_session_id=target_session_id,
                target_thread_id=target_owner.thread_id,
                checkpoint_ns=checkpoint_ns,
            )
            return replace(result, journal_state="committed")

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
            claim = self._storage._catalog_store().get_fork_retention_claim(fork_id)
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
            ) = await self._resolve_fork_source(
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
                turn_anchor = await self.aresolve_turn_anchor(
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
            self._validate_fork_boundary(
                source_session_id=source_session_id,
                source_thread_id=source_owner.thread_id,
                turn_anchor=turn_anchor,
                source_checkpoint=source_checkpoint,
                checkpoint_ns=checkpoint_ns,
            )

        if source_view_id is None:
            source_view_id = self._storage._fork_source_view_id(
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
                target_turn_id = self._storage.fork_target_identity(
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
                    self.rewind_to_turn(
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

        materialization_id, fork_id = self._storage.begin_fork_materialization(
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
                    _resolved, source_messages = await self.amaterialize_turn_anchor(
                        build_checkpoint_config(source_owner.thread_id, checkpoint_ns=checkpoint_ns),
                        turn_id=turn_anchor.turn_id, anchor_mode=anchor_mode,
                    )
            await asyncio.to_thread(
                self._storage.copy_v2_items_for_fork,
                source_session_id=source_session_id, source_thread_id=source_owner.thread_id,
                target_session_id=target_session_id, target_thread_id=target_owner.thread_id,
                checkpoint_ns=checkpoint_ns, fork_id=fork_id,
                materialization_id=materialization_id,
                source_message_ids=tuple(self._storage._codec().message_id(message, index)
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
                            await self.amaterialize_turn_anchor(
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

        self._storage.commit_fork_materialization(
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
        self.reconcile_context(
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

    async def _resolve_fork_checkpoint(
        self,
        source_session_id: str,
        *,
        source_thread_id: str,
        checkpoint_id: str | None,
        anchor: str | None,
        checkpoint_ns: str = "",
    ) -> CheckpointTuple | None:
        thread_id = source_thread_id
        owner = MutationIntentOwner(
            session_id=source_session_id,
            thread_id=source_thread_id,
        )
        if checkpoint_id is not None or anchor is None:
            with session_thread_owner_scope(owner, langgraph_thread_id=thread_id):
                return await self.aget_tuple(
                    build_checkpoint_config(
                        thread_id,
                        checkpoint_ns=checkpoint_ns,
                        checkpoint_id=checkpoint_id,
                    )
                )

        with session_thread_owner_scope(owner, langgraph_thread_id=thread_id):
            checkpoints = [
                item
                async for item in self.alist(
                    build_checkpoint_config(thread_id, checkpoint_ns=checkpoint_ns)
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

    def _fork_id_for_materialization(
        self, target_session_id: str, target_thread_id: str,
        materialization_id: str, checkpoint_ns: str,
    ) -> str:
        with self._storage._connect(target_thread_id, checkpoint_ns,
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
        target_items = self._storage.read_items(
            target_session_id, target_owner.thread_id, checkpoint_ns=checkpoint_ns,
        )
        fork_id = self._fork_id_for_materialization(
            target_session_id, target_owner.thread_id, materialization_id, checkpoint_ns,
        )
        target_messages = []
        for index, message in enumerate(channel_values.get("messages", [])):
            source_message_id = self._storage._codec().message_id(message, index)
            target_message_id = self._storage.fork_target_identity(
                target_session_id, source_session_id=source_session_id,
                fork_id=fork_id,
                entity_type="message", source_local_id=source_message_id, checkpoint_ns=checkpoint_ns,
            )
            if target_message_id is None:
                raise RuntimeError(f"fork checkpoint 缺少 target message mapping: {source_message_id}")
            group = tuple(item for item in target_items if item.message_group_id == f"message-{target_message_id}")
            if not group:
                raise RuntimeError(f"fork checkpoint 缺少 target canonical message group: {target_message_id}")
            target_messages.append(self._storage._codec().from_dict(self._storage._codec().project_message(group)))
        channel_values["messages"] = target_messages
        metadata = {"source": "fork", "step": -1, "parents": {}, "fork_mode": fork_mode,
                    "source_session_id": source_session_id, "source_anchor": anchor}
        with self._storage._connect(
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
            target_execution = self._storage.fork_target_identity(
                target_session_id, source_session_id=source_session_id, fork_id=fork_id,
                entity_type="execution", source_local_id=binding["execution_id"], checkpoint_ns=checkpoint_ns,
            )
            with self._storage._connect(
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
            self._storage.materialize_fork_checkpoint(
                materialization_id=materialization_id, target_session_id=target_session_id,
                target_thread_id=target_owner.thread_id, checkpoint_ns=checkpoint_ns,
                checkpoint=checkpoint,
                metadata=metadata,
                current_messages=target_messages,
            )
        target_checkpoint_id = checkpoint["id"]
        self.copy_pending_writes(
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
                async for item in self.alist(
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
        return (
            isinstance(message_metadata, Mapping)
            and message_metadata.get("turn_id") == anchor
        )

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
