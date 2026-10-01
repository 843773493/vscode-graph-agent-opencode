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


@dataclass
class QueueEntry:
    """仍在等待执行的队列项；消息正文仍由 JobState 持有。"""

    job_id: str
    enqueue_sequence: int
    delivery_policy: DeliveryPolicy
    waiting_reason: str | None = None
    last_boundary: DeliveryBoundary | None = None
    snapshot_version: int = 0


class JobPendingQueue:
    """每个会话一个严格 FIFO 队列，不提供任何改变顺序的操作。"""

    def __init__(self) -> None:
        self._waiting: dict[str, deque[str]] = {}
        self._entries: dict[str, QueueEntry] = {}
        self._next_sequence: dict[str, int] = {}
        self._snapshot_versions: dict[str, int] = {}

    def ids(self, session_id: str) -> tuple[str, ...]:
        return tuple(self._waiting.get(session_id, ()))

    def snapshot_version(self, session_id: str) -> int:
        return self._snapshot_versions.get(session_id, 0)

    def entry(self, job_id: str) -> QueueEntry:
        entry = self._entries.get(job_id)
        if entry is None:
            raise ValueError(f"队列中不存在 Job: job_id={job_id}")
        return entry

    def append(
        self,
        session_id: str,
        job_id: str,
        delivery_policy: DeliveryPolicy,
    ) -> QueueEntry:
        if job_id in self._entries:
            raise ValueError(f"Job 已在 FIFO 队列中: job_id={job_id}")
        sequence = self._next_sequence.get(session_id, 0) + 1
        self._next_sequence[session_id] = sequence
        entry = QueueEntry(
            job_id=job_id,
            enqueue_sequence=sequence,
            delivery_policy=delivery_policy,
            waiting_reason=_POSITIONAL_WAITING_REASON if self.ids(session_id) else None,
        )
        self._entries[job_id] = entry
        self._waiting.setdefault(session_id, deque()).append(job_id)
        self._bump(session_id)
        return entry

    def restore(self, session_id: str, entries: list[QueueEntry]) -> None:
        if self.ids(session_id) or any(
            entry.job_id in self._entries for entry in entries
        ):
            raise RuntimeError(f"不能覆盖已加载的待处理队列: session_id={session_id}")
        sequences = [entry.enqueue_sequence for entry in entries]
        if len(sequences) != len(set(sequences)):
            raise RuntimeError(
                f"待处理队列存在重复入队序号: session_id={session_id}, sequences={sequences}"
            )
        if sequences != sorted(sequences):
            raise RuntimeError(
                f"待处理队列入队序号未严格递增: session_id={session_id}, sequences={sequences}"
            )
        for entry in entries:
            JobPendingQueue._sync_positional_waiting_reason(
                entry,
                is_head=entry is entries[0],
            )
            self._entries[entry.job_id] = entry
        if entries:
            self._waiting[session_id] = deque(entry.job_id for entry in entries)
            self._next_sequence[session_id] = sequences[-1]
            self._snapshot_versions[session_id] = max(
                (entry.snapshot_version for entry in entries),
                default=0,
            )

    def peek_head(self, session_id: str) -> QueueEntry | None:
        waiting = self._waiting.get(session_id)
        if not waiting:
            return None
        return self.entry(waiting[0])

    def take_head(
        self,
        session_id: str,
        boundary: DeliveryBoundary,
        *,
        tool_result_available: bool = True,
    ) -> QueueEntry | None:
        entry = self.peek_head(session_id)
        if entry is None:
            return None
        if not self._policy_allows(
            entry.delivery_policy,
            boundary,
            tool_result_available=tool_result_available,
        ):
            entry.waiting_reason = self._waiting_reason(entry.delivery_policy, boundary)
            self._bump(session_id)
            return None
        entry.last_boundary = boundary
        entry.waiting_reason = None
        waiting = self._waiting[session_id]
        waiting.popleft()
        self._entries.pop(entry.job_id, None)
        if not waiting:
            self._waiting.pop(session_id, None)
        self._bump(session_id)
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
            if self.peek_head(session_id) is entry
            else "等待队首"
        )
        self._bump(session_id)
        return entry

    def remove(self, session_id: str, job_id: str) -> QueueEntry:
        entry = self.entry(job_id)
        waiting = self._waiting.get(session_id)
        if waiting is None or job_id not in waiting:
            raise RuntimeError(f"撤回消息时队列状态不一致: job_id={job_id}")
        waiting.remove(job_id)
        self._entries.pop(job_id, None)
        if not waiting:
            self._waiting.pop(session_id, None)
        self._bump(session_id)
        return entry

    def clear(self, session_id: str) -> tuple[QueueEntry, ...]:
        waiting = self._waiting.pop(session_id, deque())
        # 连同按会话持有的序号/版本计数一并释放：否则长驻进程里每个排队过的
        # 会话都会永久留下 _next_sequence/_snapshot_versions 记录，无界增长。
        # 清空后无队列项、无控制面 CAS 语义，重置不回退任何可回滚决策。
        self._next_sequence.pop(session_id, None)
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

    def touch(self, session_id: str) -> int:
        """为不改变顺序的队列内容编辑提交新的快照版本。"""
        return self._bump(session_id)

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

    def _bump(self, session_id: str) -> int:
        version = self._snapshot_versions.get(session_id, 0) + 1
        self._snapshot_versions[session_id] = version
        waiting = self._waiting.get(session_id)
        head_id = waiting[0] if waiting else None
        for job_id in waiting or ():
            entry = self._entries[job_id]
            entry.snapshot_version = version
            JobPendingQueue._sync_positional_waiting_reason(
                entry,
                is_head=job_id == head_id,
            )
        return version

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
