"""工作区级 rollout/checkpoint 组件组装。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.path_utils import get_session_path_resolver
from app.core.session_lifecycle_gate import SessionLifecycleGate
from app.core.shielded_cleanup import run_sync_shielded
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.node_debug.session.fork import (
    NodeDebugWorkspaceForkConfig,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.checkpoint.writer import (
    RolloutAppendWriter,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader
from app.services.infrastructure.tool_output_store import ToolOutputStore


class RolloutCheckpointRuntime:
    """为一个工作区后端组装并持有唯一的 rollout 组件集合。

    该对象不是会话级对象，也不是 SQLite connection 池。它只共享工作区级
    的路径解析、storage 协调器、reader、writer 和 LangGraph saver；具体会话
    的 SQLite connection 与读快照仍由调用期间按 session_id 创建和关闭。
    """

    def __init__(
        self,
        sessions_dir: str | Path,
        *,
        serde: JsonPlusSerializer | None = None,
        node_debug_workspace_config: Callable[[], NodeDebugWorkspaceForkConfig]
        | None = None,
        attachment_store: AttachmentBlobStore | None = None,
        tool_outputs: ToolOutputStore | None = None,
    ) -> None:
        self.sessions_dir = Path(sessions_dir).resolve()
        self._path_resolver = get_session_path_resolver(self.sessions_dir)
        self._lifecycle_gate = SessionLifecycleGate(self.sessions_dir)
        self.serde = serde or JsonPlusSerializer()
        self.storage = RolloutContextStore(
            self.sessions_dir,
            serde=self.serde,
            message_codec=LangChainMessageCodec(),
            attachment_ref_projector=attachment_store.project_attachment if attachment_store is not None else None,
        )
        self.append_writer = RolloutAppendWriter(
            self.sessions_dir,
            storage=self.storage,
        )
        self.context_reader = RolloutContextReader(self.storage)
        self.history_reader = RolloutHistoryReader(self.context_reader, tool_outputs=tool_outputs)
        self.context_plan_detail_store = ContextPlanDetailStore(
            self.sessions_dir,
        )
        self.context_plan_composer = ContextPlanComposer()
        self.saver = RolloutCheckpointSaver(
            self.sessions_dir,
            serde=self.serde,
            storage=self.storage,
            writer=self.append_writer,
            context_reader=self.context_reader,
            history_reader=self.history_reader,
            detail_store=self.context_plan_detail_store,
            node_debug_store=NodeDebugSessionStore(self._path_resolver),
            node_debug_workspace_config=node_debug_workspace_config,
            attachment_store=attachment_store,
        )

    async def prune_context_plan_details(self) -> int:
        """按catalog逐会话回收；取消和删除均等待当前worker结算，不读取历史JSONL。"""
        removed = 0
        expired_before = datetime.now(UTC)
        nodes = await run_sync_shielded(self._path_resolver.list_nodes, kind="session")
        for node in nodes:
            async with self._lifecycle_gate.shared(node.node_id):
                try:
                    current = await run_sync_shielded(self._path_resolver.get_node, node.node_id)
                except KeyError:
                    # 列举后进入删除的会话由原删除owner回收整个节点。
                    continue
                if current.state != "active":
                    continue
                owners = await run_sync_shielded(
                    self.storage.checkpoints.list_checkpoint_owners, session_id=node.node_id,
                )
                for owner in owners:
                    refs = await run_sync_shielded(
                        self.saver.gc_context_plan_details, owner.session_id, owner.thread_id,
                        expired_before=expired_before, checkpoint_ns=None,
                    )
                    removed += len(refs)
        return removed


__all__ = ["RolloutCheckpointRuntime"]
