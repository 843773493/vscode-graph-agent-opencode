"""工作区级 rollout/checkpoint 组件组装。"""

from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.rollout_append_writer import RolloutAppendWriter
from app.core.rollout_checkpoint_saver import RolloutCheckpointSaver
from app.core.rollout_context_reader import RolloutContextReader
from app.core.rollout_storage import RolloutStorage
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
    ) -> None:
        self.sessions_dir = Path(sessions_dir).resolve()
        self.serde = serde or JsonPlusSerializer()
        self.storage = RolloutStorage(self.sessions_dir, serde=self.serde)
        self.append_writer = RolloutAppendWriter(
            self.sessions_dir,
            storage=self.storage,
        )
        self.context_reader = RolloutContextReader(self.storage)
        self.history_reader = RolloutHistoryReader(self.context_reader)
        self.saver = RolloutCheckpointSaver(
            self.sessions_dir,
            serde=self.serde,
            storage=self.storage,
            writer=self.append_writer,
            context_reader=self.context_reader,
            history_reader=self.history_reader,
        )


__all__ = ["RolloutCheckpointRuntime"]
