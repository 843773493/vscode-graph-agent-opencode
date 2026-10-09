"""Checkpoint fork 的source 解析、冻结和预检 owner。"""


from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.checkpoint.base import Checkpoint, CheckpointTuple

from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_catalog_store import SessionCreationRecord
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
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
from app.services.infrastructure.rollout_context.fork.source_snapshot import (
    ForkSourceSnapshot,
    capture_fork_rollout_snapshot,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutInternalForkAnchor,
    RolloutNotMaterializedError,
    RolloutTurnAnchor,
    _RolloutFileLock,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_optional_text,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.checkpoint.saver import (
        RolloutCheckpointSaver,
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


class CheckpointForkSource:
    def __init__(
        self, *, checkpoint: RolloutCheckpointSaver, storage, context_reader,
        detail_store, node_debug_store, node_debug_workspace_config,
        capture_request_only_content,
    ) -> None:
        self._checkpoint = checkpoint
        self._storage = storage
        self._context_reader = context_reader
        self._detail_store = detail_store
        self._node_debug_store = node_debug_store
        self._node_debug_workspace_config = node_debug_workspace_config
        self._capture_request_only_content_for_fork = capture_request_only_content

    def resolve_thread_owner(self, session_id: str, thread_id: str) -> MutationIntentOwner:
        """从两个权威 catalog 解析精确 Session/thread owner。"""
        resolver = self._storage.storage._path_resolver
        resolver.resolve_thread_node(session_id, thread_id)
        return MutationIntentOwner(session_id=session_id, thread_id=thread_id)


    def resolve_main_thread_owner(self, session_id: str) -> MutationIntentOwner:
        resolver = self._storage.storage._path_resolver
        return self.resolve_thread_owner(session_id, resolver.main_thread_id(session_id))


    def resolve_prepared_main_thread_owner(
        self,
        record: SessionCreationRecord,
    ) -> MutationIntentOwner:
        """仅凭 fork 创建 record capability 解析未发布 target main owner。"""
        resolver = self._storage.storage._path_resolver
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
        gate = SessionLifecycleGate(self._storage.storage.sessions_dir)
        async with gate.shared(source_session_id):
            source_rollout = self._storage.storage.root(
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
            source_view_id = self._storage.forks._fork_source_view_id(
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
            self._storage.checkpoints.validate_fork_source_closure(
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
        self._storage.checkpoints.validate_fork_source_closure(
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
            turn_anchor = await self._checkpoint.aresolve_latest_completed_turn_anchor(
                build_checkpoint_config(source_thread_id, checkpoint_ns=checkpoint_ns),
                anchor_mode=anchor_mode,
            )
            if turn_anchor is None and mode in {"context_fork", "history_prefix_fork"}:
                if mode != "context_fork" or anchor_mode != "inclusive":
                    raise ValueError("fork_source_not_completed: source 没有可复制的已完成 Turn")
                try:
                    with self._context_reader.open_snapshot(source_session_id, source_thread_id, checkpoint_ns) as snapshot:
                        turn_anchor = self._storage.forks.resolve_completed_internal_fork_anchor(snapshot)
                except RolloutNotMaterializedError as error:
                    raise ValueError(
                        "fork_source_not_completed: source 没有可复制的已完成 Turn 或内部 execution"
                    ) from error
            if isinstance(turn_anchor, RolloutTurnAnchor):
                turn_id = turn_anchor.turn_id
        if turn_anchor is None and turn_id is not None:
            turn_anchor = await self._checkpoint.aresolve_turn_anchor(
                build_checkpoint_config(source_thread_id, checkpoint_ns=checkpoint_ns),
                turn_id=turn_id,
                anchor_mode=anchor_mode,
                require_completed=True,
            )
        if turn_anchor is not None:
            source_checkpoint = await self._checkpoint.aget_tuple(
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
                return await self._checkpoint.aget_tuple(
                    build_checkpoint_config(
                        thread_id,
                        checkpoint_ns=checkpoint_ns,
                        checkpoint_id=checkpoint_id,
                    )
                )

        with session_thread_owner_scope(owner, langgraph_thread_id=thread_id):
            checkpoints = [
                item
                async for item in self._checkpoint.alist(
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
