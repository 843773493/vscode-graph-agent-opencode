from __future__ import annotations

from collections import deque

from app.schemas.public_v2.pending_request import (
    PendingRequestKind,
    PendingRequestOrderItem,
)


class JobPendingQueue:
    """维护各会话尚未执行 Job 的顺序和 Steering yield 信号。"""

    def __init__(self) -> None:
        self._waiting: dict[str, deque[str]] = {}
        self._kinds: dict[str, PendingRequestKind] = {}
        self._yield_requested_sessions: set[str] = set()

    def ids(self, session_id: str) -> tuple[str, ...]:
        return tuple(self._waiting.get(session_id, ()))

    def kind(self, job_id: str) -> PendingRequestKind:
        kind = self._kinds.get(job_id)
        if kind is None:
            raise RuntimeError(f"排队 Job 缺少 pending kind: job_id={job_id}")
        return kind

    def append(
        self,
        session_id: str,
        job_id: str,
        kind: PendingRequestKind,
    ) -> int:
        waiting = self._waiting.setdefault(session_id, deque())
        if kind == "steering":
            insert_at = 0
            while (
                insert_at < len(waiting)
                and self._kinds.get(waiting[insert_at]) == "steering"
            ):
                insert_at += 1
            waiting.insert(insert_at, job_id)
            self._yield_requested_sessions.add(session_id)
        else:
            waiting.append(job_id)
        self._kinds[job_id] = kind
        return list(waiting).index(job_id)

    def restore(
        self,
        session_id: str,
        requests: list[tuple[str, PendingRequestKind]],
    ) -> None:
        if self.ids(session_id):
            raise RuntimeError(f"不能覆盖已加载的待处理队列: session_id={session_id}")
        if not requests:
            return
        self._waiting[session_id] = deque(job_id for job_id, _kind in requests)
        for job_id, kind in requests:
            self._kinds[job_id] = kind
        self._refresh_yield(session_id)

    def popleft(self, session_id: str) -> str | None:
        waiting = self._waiting.get(session_id)
        if not waiting:
            return None
        job_id = waiting.popleft()
        self._kinds.pop(job_id, None)
        if not waiting:
            self._waiting.pop(session_id, None)
        self._refresh_yield(session_id)
        return job_id

    def pop_next_group(self, session_id: str) -> tuple[str, ...]:
        """连续 Steering 合并为一组；普通排队消息一次只取一条。"""
        waiting = self._waiting.get(session_id)
        if not waiting:
            return ()
        first_id = waiting[0]
        first_kind = self.kind(first_id)
        count = 1
        if first_kind == "steering":
            count = 0
            for job_id in waiting:
                if self.kind(job_id) != "steering":
                    break
                count += 1
        result = tuple(waiting.popleft() for _ in range(count))
        for job_id in result:
            self._kinds.pop(job_id, None)
        if not waiting:
            self._waiting.pop(session_id, None)
        self._refresh_yield(session_id)
        return result

    def remove(self, session_id: str, job_id: str) -> bool:
        waiting = self._waiting.get(session_id)
        if waiting is None or job_id not in waiting:
            return False
        waiting.remove(job_id)
        self._kinds.pop(job_id, None)
        if not waiting:
            self._waiting.pop(session_id, None)
        self._refresh_yield(session_id)
        return True

    def clear(self, session_id: str) -> tuple[str, ...]:
        removed = tuple(self._waiting.pop(session_id, ()))
        for job_id in removed:
            self._kinds.pop(job_id, None)
        self._yield_requested_sessions.discard(session_id)
        return removed

    def reorder(
        self,
        session_id: str,
        requests: list[PendingRequestOrderItem],
        *,
        job_id_by_message_id: dict[str, str],
    ) -> None:
        current = self.ids(session_id)
        requested_ids = tuple(
            job_id_by_message_id[item.message_id] for item in requests
        )
        if set(requested_ids) != set(current) or len(requested_ids) != len(current):
            raise ValueError(
                "重排请求必须完整且仅包含当前待处理消息: "
                f"current={list(current)} requested={list(requested_ids)}"
            )
        steering = [
            job_id
            for job_id, item in zip(requested_ids, requests, strict=True)
            if item.kind == "steering"
        ]
        queued = [
            job_id
            for job_id, item in zip(requested_ids, requests, strict=True)
            if item.kind == "queued"
        ]
        self._waiting[session_id] = deque([*steering, *queued])
        for job_id, item in zip(requested_ids, requests, strict=True):
            self._kinds[job_id] = item.kind
        self._refresh_yield(session_id)

    def promote(self, session_id: str, job_id: str) -> None:
        waiting = self._waiting.get(session_id)
        if waiting is None or job_id not in waiting:
            raise ValueError(f"待立即发送的消息不在队列中: job_id={job_id}")
        waiting.remove(job_id)
        waiting.appendleft(job_id)

    def yield_requested(self, session_id: str) -> bool:
        return session_id in self._yield_requested_sessions

    def _refresh_yield(self, session_id: str) -> None:
        waiting = self._waiting.get(session_id, ())
        has_steering = any(
            self._kinds.get(job_id) == "steering" for job_id in waiting
        )
        if has_steering:
            self._yield_requested_sessions.add(session_id)
        else:
            self._yield_requested_sessions.discard(session_id)
