from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from app.schemas.internal_v2.pending_request import (
    DeliveryBoundary,
    DeliveryPolicy,
)

# 「等待队首」是纯位置理由：只有非队首项才等待队首。它必须随位置变化被
# 清除，否则原队长被取走后，接替者会成为新队首却仍对外宣称「等待队首」。
_POSITIONAL_WAITING_REASON = "等待队首"


@dataclass(slots=True)
class QueueEntry:
    """仍在等待执行的队列项；消息正文仍由 JobState 持有。"""

    job_id: str
    thread_id: str
    enqueue_sequence: int
    delivery_policy: DeliveryPolicy
    waiting_reason: str | None = None
    last_boundary: DeliveryBoundary | None = None


class JobPendingQueue:
    """同一 thread 严格 FIFO；会话聚合保持原入队顺序。"""

    def __init__(self) -> None:
        self._waiting: dict[str, deque[str]] = {}
        self._entries: dict[str, QueueEntry] = {}
        self._snapshot_versions: dict[str, int] = {}

    def queued_job_ids(
        self,
        session_id: str,
        thread_id: str | None = None,
    ) -> tuple[str, ...]:
        return tuple(
            job_id
            for job_id in self._waiting.get(session_id, ())
            if thread_id is None or self._entries[job_id].thread_id == thread_id
        )

    def queued_job_position_and_count(
        self,
        session_id: str,
        thread_id: str,
        job_id: str,
    ) -> tuple[int | None, int]:
        """返回目标 Job 的零基队列位置与该 Thread 的排队数。"""
        position: int | None = None
        count = 0
        for queued_job_id in self._waiting.get(session_id, ()):
            if self._entries[queued_job_id].thread_id != thread_id:
                continue
            if queued_job_id == job_id and position is None:
                position = count
            count += 1
        return position, count

    def snapshot_version(self, session_id: str) -> int:
        return self._snapshot_versions.get(session_id, 0)

    def entry(self, job_id: str) -> QueueEntry:
        entry = self._entries.get(job_id)
        if entry is None:
            raise ValueError(f"队列中不存在 Job: job_id={job_id}")
        return entry

    def _thread_head_entry(
        self,
        session_id: str,
        thread_id: str,
    ) -> QueueEntry | None:
        for job_id in self._waiting.get(session_id, ()):
            entry = self._entries[job_id]
            if entry.thread_id == thread_id:
                return entry
        return None

    def append(
        self,
        session_id: str,
        job_id: str,
        delivery_policy: DeliveryPolicy,
        *,
        thread_id: str,
        enqueue_sequence: int,
    ) -> QueueEntry:
        if not thread_id:
            raise ValueError("队列入队必须携带精确 thread_id")
        if job_id in self._entries:
            raise ValueError(f"Job 已在 FIFO 队列中: job_id={job_id}")
        if (
            not isinstance(enqueue_sequence, int)
            or isinstance(enqueue_sequence, bool)
            or enqueue_sequence < 1
        ):
            raise ValueError("入队顺序必须来自原 admission commit cursor")
        has_thread_waiting = False
        last_thread_sequence: int | None = None
        for queued_job_id in self._waiting.get(session_id, ()):
            queued_entry = self._entries[queued_job_id]
            if queued_entry.thread_id != thread_id:
                continue
            has_thread_waiting = True
            last_thread_sequence = queued_entry.enqueue_sequence
        if (
            last_thread_sequence is not None
            and enqueue_sequence <= last_thread_sequence
        ):
            raise RuntimeError("FIFO 镜像不能改变同 Thread 的准入顺序")
        entry = QueueEntry(
            job_id=job_id,
            thread_id=thread_id,
            enqueue_sequence=enqueue_sequence,
            delivery_policy=delivery_policy,
            waiting_reason=_POSITIONAL_WAITING_REASON if has_thread_waiting else None,
        )
        self._entries[job_id] = entry
        self._waiting.setdefault(session_id, deque()).append(job_id)
        return entry

    def restore(
        self,
        session_id: str,
        entries: list[QueueEntry],
        *,
        queue_snapshot_version: int = 0,
    ) -> None:
        """恢复队列内容与所属 Session 的聚合快照版本。"""
        if self._waiting.get(session_id) or any(
            entry.job_id in self._entries for entry in entries
        ):
            raise RuntimeError(f"不能覆盖已加载的待处理队列: session_id={session_id}")
        previous_by_thread: dict[str, int] = {}
        for entry in entries:
            if entry.enqueue_sequence <= previous_by_thread.get(entry.thread_id, 0):
                raise RuntimeError("FIFO 镜像恢复时同 Thread 准入顺序错误")
            previous_by_thread[entry.thread_id] = entry.enqueue_sequence
        seen_threads: set[str] = set()
        for entry in entries:
            JobPendingQueue._sync_positional_waiting_reason(
                entry,
                is_head=entry.thread_id not in seen_threads,
            )
            seen_threads.add(entry.thread_id)
            self._entries[entry.job_id] = entry
        if entries:
            self._waiting[session_id] = deque(entry.job_id for entry in entries)
        self._snapshot_versions[session_id] = queue_snapshot_version

    def peek_head(self, session_id: str, thread_id: str) -> QueueEntry | None:
        return self._thread_head_entry(session_id, thread_id)

    def take_head(
        self,
        session_id: str,
        boundary: DeliveryBoundary,
        *,
        thread_id: str,
        tool_result_available: bool = True,
    ) -> QueueEntry | None:
        entry = self.peek_head(session_id, thread_id)
        if entry is None:
            return None
        if not self._policy_allows(
            entry.delivery_policy,
            boundary,
            tool_result_available=tool_result_available,
        ):
            entry.waiting_reason = self._waiting_reason(entry.delivery_policy, boundary)
            return None
        entry.last_boundary = boundary
        entry.waiting_reason = None
        waiting = self._waiting[session_id]
        waiting.remove(entry.job_id)
        self._entries.pop(entry.job_id, None)
        if not waiting:
            self._waiting.pop(session_id, None)
        else:
            promoted_head = self._thread_head_entry(session_id, thread_id)
            if promoted_head is not None:
                self._sync_positional_waiting_reason(promoted_head, is_head=True)
        return entry

    def update_policy(
        self,
        session_id: str,
        job_id: str,
        delivery_policy: DeliveryPolicy,
    ) -> QueueEntry:
        entry = self.entry(job_id)
        # 非队首允许修改策略，但不得触发投递或改变位置。
        entry.delivery_policy = delivery_policy
        entry.waiting_reason = (
            None
            if self.peek_head(session_id, entry.thread_id) is entry
            else _POSITIONAL_WAITING_REASON
        )
        return entry

    def remove(self, session_id: str, job_id: str) -> QueueEntry:
        entry = self.entry(job_id)
        waiting = self._waiting.get(session_id)
        if waiting is None:
            raise RuntimeError(f"撤回消息时队列状态不一致: job_id={job_id}")
        was_thread_head = self._thread_head_entry(session_id, entry.thread_id) is entry
        try:
            waiting.remove(job_id)
        except ValueError:
            raise RuntimeError(f"撤回消息时队列状态不一致: job_id={job_id}") from None
        self._entries.pop(job_id, None)
        if not waiting:
            self._waiting.pop(session_id, None)
        elif was_thread_head:
            promoted_head = self._thread_head_entry(session_id, entry.thread_id)
            if promoted_head is not None:
                self._sync_positional_waiting_reason(promoted_head, is_head=True)
        return entry

    def clear(self, session_id: str) -> tuple[QueueEntry, ...]:
        waiting = self._waiting.pop(session_id, deque())
        self._snapshot_versions.pop(session_id, None)
        removed: list[QueueEntry] = []
        for job_id in waiting:
            entry = self.entry(job_id)
            self._entries.pop(job_id, None)
            removed.append(entry)
        return tuple(removed)

    def reject_reorder(self, session_id: str) -> None:
        raise ValueError(
            f"会话 {session_id} 的 FIFO 队列不支持重排、提升队首或立即发送"
        )

    def set_snapshot_version(self, session_id: str, version: int) -> None:
        """只缓存已发布 SQLite revision 的 Session 聚合值。"""
        self._snapshot_versions[session_id] = version

    @staticmethod
    def _policy_allows(
        policy: DeliveryPolicy,
        boundary: DeliveryBoundary,
        *,
        tool_result_available: bool,
    ) -> bool:
        if boundary == "idle":
            return True
        # interrupt 边界只在当前 turn 已提交终止后到达，因此它同时满足
        # "当前 turn 到达终止边界" 的 after_turn 语义；否则一条 after_turn
        # 队首会在用户中断后永久阻塞整个 FIFO 队列。
        if boundary == "after_interrupt":
            return True
        if policy == "after_turn":
            return boundary == "after_turn"
        if policy == "after_tool_result":
            return boundary == "after_tool_result" or (
                boundary == "after_turn" and not tool_result_available
            )
        return False

    @staticmethod
    def _waiting_reason(policy: DeliveryPolicy, boundary: DeliveryBoundary) -> str:
        if policy == "after_interrupt":
            return "等待已提交的 interrupt 边界"
        if boundary == "after_tool_result":
            return "等待完整 tool-result 边界"
        if policy == "after_tool_result":
            return "等待 tool-result；无 tool-result 时回退到 turn 结束"
        return "等待当前 turn 结束"

    @staticmethod
    def _sync_positional_waiting_reason(entry: QueueEntry, *, is_head: bool) -> None:
        """按当前位置刷新位置理由，保留边界理由。

        边界理由（如「等待已提交的 interrupt 边界」）由 ``take_head`` 写入，
        与位置无关；位置理由只对非队首成立，成为队首后必须清除。改前用
        ``已有理由 or 新理由`` 计算，一旦写入「等待队首」就再也不会被清掉。
        """
        if is_head:
            if entry.waiting_reason == _POSITIONAL_WAITING_REASON:
                entry.waiting_reason = None
            return
        if entry.waiting_reason is None:
            entry.waiting_reason = _POSITIONAL_WAITING_REASON
