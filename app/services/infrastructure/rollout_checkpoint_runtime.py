"""工作区级 rollout/checkpoint 组件组装。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.path_utils import get_session_path_resolver
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
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.storage.append_writer import (
    RolloutAppendWriter,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader


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
        protected_detail_key: bytes | None = None,
        node_debug_workspace_config: Callable[[], NodeDebugWorkspaceForkConfig]
        | None = None,
    ) -> None:
        self.sessions_dir = Path(sessions_dir).resolve()
        self.serde = serde or JsonPlusSerializer()
        self.storage = RolloutStorage(
            self.sessions_dir,
            serde=self.serde,
            message_codec=LangChainMessageCodec(),
        )
        self.append_writer = RolloutAppendWriter(
            self.sessions_dir,
            storage=self.storage,
        )
        self.context_reader = RolloutContextReader(self.storage)
        self.history_reader = RolloutHistoryReader(self.context_reader)
        self.context_plan_detail_store = ContextPlanDetailStore(
            self.sessions_dir,
            protected_key=protected_detail_key,
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
            node_debug_store=NodeDebugSessionStore(
                get_session_path_resolver(self.sessions_dir)
            ),
            node_debug_workspace_config=node_debug_workspace_config,
        )


__all__ = ["RolloutCheckpointRuntime"]
