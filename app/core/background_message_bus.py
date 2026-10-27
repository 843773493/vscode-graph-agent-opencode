"""持久精确 Thread 邮箱；进程内 Event 只唤醒，消息与消费回执全部读控制库。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from pydantic import JsonValue

from app.abstractions.background_message_bus import (
    BackgroundMessageBatchDTO,
    BackgroundMessageDTO,
    BackgroundMessageKind,
)
from app.core.background_task_contracts import BackgroundToolResultIdentity
from app.core.background_task_registry import BackgroundWorkStoreProtocol
from app.core.session_lifecycle_gate import OperationHolderGate
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.tool_call_identity import (
    provider_tool_call_id,
    software_tool_operation_id,
)
from app.domain.itemized.records import CanonicalItemRecord


class BackgroundToolResultReader(Protocol):
    def committed_tool_result_id(
        self, session_id: str, thread_id: str, model_call_id: str, tool_call_id: str,
    ) -> str | None: ...


class BackgroundMessageBus:
    def __init__(self, *, store: BackgroundWorkStoreProtocol) -> None:
        self._store = store
        self._consumers = OperationHolderGate(store.sessions_dir, namespace="background_consumers")
        self._waiters: dict[tuple[str, str, str], set[asyncio.Event]] = {}

    def close_session(self, session_id: str) -> None:
        active = [key for key, events in self._waiters.items() if key[0] == session_id and events]
        if active:
            raise RuntimeError(f"后台消息收集尚未排空: session_id={session_id}, owners={active}")
        # durable 邮箱随整个 Session 物理节点删除；关闭连接不丢弃冷历史。

    async def emit(
        self, session_id: str, thread_id: str, agent_id: str, content: str, *,
        message_id: str, kind: BackgroundMessageKind | str = BackgroundMessageKind.normal,
        source_id: str | None = None, payload: dict[str, JsonValue] | None = None,
    ) -> BackgroundMessageDTO:
        if not message_id or not content or not agent_id:
            raise ValueError("message_id、content 和 agent selector 必须显式提供")
        message = BackgroundMessageDTO(
            message_id=message_id, session_id=session_id, thread_id=thread_id,
            agent_id=agent_id, source_id=source_id or message_id,
            kind=BackgroundMessageKind(kind), content=content, payload=payload or {},
            timestamp=datetime.now(UTC),
        )
        async with self._store.admission(session_id):
            self._store.resolve_thread(session_id, thread_id)
            with self._store.control(session_id) as control:
                message = control.emit_background_message(message)
        for waiter in self._waiters.get((session_id, thread_id, agent_id), ()):
            waiter.set()
        return message

    async def list_messages(
        self, session_id: str, thread_id: str, agent_id: str, *,
        source_id: str | None = None, after_message_id: str | None = None, limit: int = 100,
    ) -> list[BackgroundMessageDTO]:
        async with self._store.admission(session_id):
            self._store.resolve_thread(session_id, thread_id)
            with self._store.control(session_id, read_only=True) as control:
                return control.list_background_messages(thread_id, agent_id, source_id=source_id,
                                                        after_message_id=after_message_id, limit=limit)

    async def collect(
        self, session_id: str, thread_id: str, agent_id: str, *, consumption_id: str,
        source_id: str | None = None, after_message_id: str | None = None,
        timeout_seconds: int = 300, poll_interval_seconds: float = 1.0,
        stop_on_interrupt: bool = True,
        tool_result_identity: BackgroundToolResultIdentity | None = None,
    ) -> BackgroundMessageBatchDTO:
        async with self._consumers.lock_for(f"{session_id}:{consumption_id}"):
            return await self._collect(
                session_id, thread_id, agent_id, consumption_id=consumption_id,
                source_id=source_id, after_message_id=after_message_id,
                timeout_seconds=timeout_seconds, poll_interval_seconds=poll_interval_seconds,
                stop_on_interrupt=stop_on_interrupt,
                tool_result_identity=tool_result_identity,
            )

    async def _collect(
        self, session_id: str, thread_id: str, agent_id: str, *, consumption_id: str,
        source_id: str | None, after_message_id: str | None, timeout_seconds: int,
        poll_interval_seconds: float, stop_on_interrupt: bool,
        tool_result_identity: BackgroundToolResultIdentity | None,
    ) -> BackgroundMessageBatchDTO:
        if timeout_seconds <= 0 or poll_interval_seconds <= 0 or not consumption_id or not agent_id:
            raise ValueError("消费 identity/agent selector 必须显式提供，超时和轮询间隔必须大于 0")
        preimage_hash = sha256_jcs({
            "session_id": session_id, "thread_id": thread_id, "agent_id": agent_id,
            "source_id": source_id, "after_message_id": after_message_id,
            "timeout_seconds": timeout_seconds, "poll_interval_seconds": poll_interval_seconds,
            "stop_on_interrupt": stop_on_interrupt,
            "tool_result_identity": tool_result_identity.model_dump() if tool_result_identity else None,
        })
        async with self._store.admission(session_id):
            self._store.resolve_thread(session_id, thread_id)
            with self._store.control(session_id) as control:
                batch = control.get_background_consumption(consumption_id, preimage_hash=preimage_hash)
                if batch is None:
                    batch = BackgroundMessageBatchDTO(
                        session_id=session_id, thread_id=thread_id, agent_id=agent_id,
                        consumption_id=consumption_id, source_id=source_id,
                        last_message_id=after_message_id, collected_at=datetime.now(UTC),
                        tool_result_identity=tool_result_identity,
                    )
                    control.checkpoint_background_consumption(batch, preimage_hash=preimage_hash)
                if batch.interrupted or batch.timed_out:
                    return batch
        key = (session_id, thread_id, agent_id)
        waiter = asyncio.Event()
        self._waiters.setdefault(key, set()).add(waiter)
        # 重启继续原 durable 消费窗口，不能每次重新延长超时。
        remaining_window = timeout_seconds - (datetime.now(UTC) - batch.collected_at).total_seconds()
        deadline = asyncio.get_running_loop().time() + max(0, remaining_window)
        try:
            while True:
                waiter.clear()
                messages = await self.list_messages(session_id, thread_id, agent_id, source_id=source_id,
                                                     after_message_id=batch.last_message_id, limit=1000)
                for message in messages:
                    batch.messages.append(message)
                    batch.last_message_id = message.message_id
                    if stop_on_interrupt and message.kind == BackgroundMessageKind.interrupt:
                        batch.interrupted = True
                        break
                remaining = deadline - asyncio.get_running_loop().time()
                batch.timed_out = remaining <= 0 and not batch.interrupted
                async with self._store.admission(session_id):
                    with self._store.control(session_id) as control:
                        batch = control.checkpoint_background_consumption(batch, preimage_hash=preimage_hash)
                if batch.interrupted or batch.timed_out:
                    return batch
                if len(messages) == 1000:
                    continue
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=min(remaining, poll_interval_seconds))
                except TimeoutError:
                    # Event 是提示；跨进程 producer 和合并通知都从 durable sequence 对账。
                    continue
        finally:
            self._waiters[key].discard(waiter)
            if not self._waiters[key]:
                del self._waiters[key]

    def acknowledge(self, session_id: str, consumption_id: str) -> None:
        with self._store.control(session_id) as control:
            control.acknowledge_background_consumption(consumption_id)

    def acknowledge_tool_results(
        self, session_id: str, thread_id: str, items: Sequence[CanonicalItemRecord]
    ) -> None:
        """仅供 canonical sink 在真实 append 提交后调用；不从工具返回正文取身份。"""
        consumption_ids: list[str] = []
        for item in items:
            identity = item.projection_identity
            if (
                item.semantic_kind != "tool_result"
                or item.status != "completed"
                or identity is None
                or identity.model_call_id is None
                or item.tool_call_id is None
            ):
                continue
            consumption_ids.append(
                software_tool_operation_id(
                    "background_collect",
                    session_id,
                    thread_id,
                    identity.model_call_id,
                    provider_tool_call_id(identity, item.tool_call_id),
                )
            )
        if not consumption_ids:
            return
        # 一个 canonical append 可能包含多个工具结果；共享一次连接初始化，
        # 但保留每个消费记录各自的 durable ack 事务。
        with self._store.control(session_id) as control:
            for consumption_id in consumption_ids:
                control.acknowledge_background_consumption(
                    consumption_id, if_present=True
                )

    async def reconcile(self, reader: BackgroundToolResultReader) -> int:
        """补齐 canonical 已提交、ack 未提交的重启窗口；未提交结果保持原 batch。"""
        acknowledged = 0
        for session_id in self._store.session_ids():
            with self._store.control(session_id, read_only=True) as control:
                pending = control.unacknowledged_background_consumptions()
            for consumption_id, thread_id, identity in pending:
                async with self._store.admission(session_id):
                    if reader.committed_tool_result_id(session_id, thread_id, identity.model_call_id, identity.tool_call_id) is not None:
                        self.acknowledge(session_id, consumption_id)
                        acknowledged += 1
        return acknowledged
