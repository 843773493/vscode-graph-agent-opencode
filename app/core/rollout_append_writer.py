"""rollout 的唯一追加写入入口。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from langchain_core.messages import BaseMessage
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata

from app.core.rollout_storage import RolloutStorage


class RolloutAppendWriter:
    """协调 canonical message、checkpoint 和 Turn finalization 的追加。

    业务层不直接组合 JSONL offset、SQLite transaction 或控制表；所有追加
    都从这里进入 ``RolloutStorage``。消息是否稳定、角色是否合法以及跨文件
    提交顺序仍由 storage 在同一把 session 锁内校验。
    """

    def __init__(
        self,
        sessions_dir: str | Path,
        *,
        storage: RolloutStorage | None = None,
    ) -> None:
        self._storage = storage or RolloutStorage(sessions_dir)

    def append_checkpoint(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        parent_checkpoint_id: str | None,
        current_messages: list[BaseMessage],
        branch_id: str | None = None,
    ) -> None:
        self._storage.append_checkpoint(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_checkpoint_id=parent_checkpoint_id,
            current_messages=current_messages,
            branch_id=branch_id,
        )

    def append_writes(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        writes: Iterable[tuple[str, object]],
        task_id: str,
        task_path: str,
    ) -> None:
        self._storage.append_writes(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=checkpoint_id,
            writes=writes,
            task_id=task_id,
            task_path=task_path,
        )

    def finalize_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        final_message_id: str,
        checkpoint_ns: str = "",
    ) -> None:
        """把最终消息 ID 解析为 sequence，再追加 SQLite finalization。"""
        final_message_sequence = self._storage.final_message_sequence(
            thread_id=session_id,
            checkpoint_ns=checkpoint_ns,
            turn_id=turn_id,
            final_message_id=final_message_id,
        )
        self._storage.append_turn_finalize(
            thread_id=session_id,
            checkpoint_ns=checkpoint_ns,
            turn_id=turn_id,
            final_message_sequence=final_message_sequence,
            final_message_id=final_message_id,
        )

    def mark_turn_terminal_status(
        self,
        *,
        session_id: str,
        turn_id: str,
        status: str,
        checkpoint_ns: str = "",
    ) -> bool:
        """持久化失败、取消或超时 Turn 的终态。"""
        return self._storage.mark_turn_terminal_status(
            thread_id=session_id,
            turn_id=turn_id,
            status=status,
            checkpoint_ns=checkpoint_ns,
        )

    def copy_turn_finalizations(
        self,
        *,
        source_session_id: str,
        source_checkpoint_id: str | None,
        target_session_id: str,
        checkpoint_ns: str = "",
    ) -> int:
        """复制 fork 源 checkpoint 中已完成 Turn 的最终消息指针。"""
        return self._storage.copy_turn_finalizations(
            source_thread_id=source_session_id,
            source_checkpoint_id=source_checkpoint_id,
            target_thread_id=target_session_id,
            checkpoint_ns=checkpoint_ns,
        )

    def cancel_unfinished_turns(
        self,
        *,
        session_id: str,
        checkpoint_ns: str = "",
    ) -> int:
        """取消 fork 目标中不会由子会话继续执行的未完成 Turn。"""
        return self._storage.cancel_unfinished_turns(
            thread_id=session_id,
            checkpoint_ns=checkpoint_ns,
        )
