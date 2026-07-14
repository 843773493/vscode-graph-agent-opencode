from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from typing import Any

from cachetools import LRUCache
from langchain_core.tools import BaseTool, tool

from app.abstractions.background_message_bus import BackgroundMessageBusProtocol
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import EventType
from app.schemas.background_message import BackgroundMessageKind


def create_system_time_emitter_tool(
    session_id: str,
    agent_id: str = "default",
    *,
    background_task_registry: BackgroundTaskRegistry,
    background_message_bus: BackgroundMessageBusProtocol,
) -> BaseTool:
    """创建向后台消息总线发送系统时间的工具。"""
    @tool("emit_system_time_messages")
    async def emit_system_time_messages(
        interval_seconds: float = 1.0,
        message_count: int = 5,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """按固定间隔向后台消息总线发送当前系统时间。"""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds 必须大于 0")
        if message_count <= 0:
            raise ValueError("message_count 必须大于 0")

        resolved_source_id = source_id or f"time_{session_id}_{int(time.time() * 1000)}"
        async def _emit_background_task() -> dict[str, Any]:
            emitted_messages = []
            for index in range(message_count):
                current_time = datetime.now().isoformat(timespec="seconds")
                message = background_message_bus.emit(
                    session_id,
                    agent_id,
                    current_time,
                    kind=BackgroundMessageKind.normal,
                    source_id=resolved_source_id,
                    payload={
                        "index": index + 1,
                        "message_count": message_count,
                        "interval_seconds": interval_seconds,
                    },
                )
                emitted_messages.append(message.model_dump(mode="json"))
                if index < message_count - 1:
                    await asyncio.sleep(interval_seconds)
            return {
                "session_id": session_id,
                "agent_id": agent_id,
                "source_id": resolved_source_id,
                "interval_seconds": interval_seconds,
                "message_count": message_count,
                "messages": emitted_messages,
            }

        handle = background_task_registry.spawn(
            session_id=session_id,
            task_name="emit_system_time_messages",
            runner=_emit_background_task,
            metadata={
                "target_session_id": session_id,
                "source_id": resolved_source_id,
                "interval_seconds": interval_seconds,
                "message_count": message_count,
            },
        )
        return handle.to_dict()

    return emit_system_time_messages


def create_monitor_session_agent_end_tool(
    session_id: str,
    agent_id: str = "default",
    *,
    background_task_registry: BackgroundTaskRegistry,
    background_message_bus: BackgroundMessageBusProtocol,
    job_event_bus: JobEventBusProtocol,
    job_service: JobServiceProtocol,
) -> BaseTool:
    """创建监控 session agent 结束事件的工具。"""
    @tool("monitor_session_agent_end")
    async def monitor_session_agent_end(
        target_session_id: str,
        timeout_seconds: int | None = None,
        poll_interval_seconds: float = 1.0,
        max_events: int | None = None,
    ) -> dict[str, Any]:
        """开启后台任务，持续监控 AGENT_END；timeout_seconds/max_events 为 0 表示不限制。"""
        if not target_session_id:
            raise ValueError("target_session_id 不能为空")
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds 不能为负数")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须大于 0")
        if max_events is not None and max_events < 0:
            raise ValueError("max_events 不能为负数")

        resolved_timeout_seconds = timeout_seconds or None
        resolved_max_events = max_events or None

        submitted_at = datetime.now()
        monitor_source_id = f"monitor:{target_session_id}:{uuid.uuid4().hex[:12]}"

        async def _monitor_background_task() -> dict[str, Any]:
            deadline = (
                None
                if resolved_timeout_seconds is None
                else asyncio.get_running_loop().time() + resolved_timeout_seconds
            )
            seen_event_ids = LRUCache(maxsize=10000)
            emitted_events: list[dict[str, Any]] = []
            emitted_count = 0
            forward_tasks: dict[str, asyncio.Task] = {}
            master_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=1000)

            async def _forward_events(job_id: str, source_queue: asyncio.Queue) -> None:
                try:
                    while True:
                        event = await source_queue.get()
                        await master_queue.put((job_id, event))
                except asyncio.CancelledError:
                    await job_event_bus.unsubscribe(
                        job_id,
                        source_queue,
                        reason="background_forwarder_cancelled",
                    )
                    raise

            async def _update_subscriptions() -> None:
                while True:
                    current_jobs = await job_service.list(session_id=target_session_id)
                    current_job_ids = {job.job_id for job in current_jobs}

                    for job_id in list(forward_tasks.keys()):
                        if job_id not in current_job_ids:
                            task = forward_tasks.pop(job_id)
                            task.cancel()

                    for job in current_jobs:
                        if job.job_id not in forward_tasks:
                            queue = await job_event_bus.subscribe(
                                job.job_id,
                                subscriber_kind="agent_end_monitor",
                                metadata={
                                    "owner_session_id": session_id,
                                    "target_session_id": target_session_id,
                                    "agent_id": agent_id,
                                },
                                event_types=frozenset({EventType.AGENT_END}),
                            )
                            task = asyncio.create_task(_forward_events(job.job_id, queue))
                            forward_tasks[job.job_id] = task

                    await asyncio.sleep(poll_interval_seconds)

            manager_task = asyncio.create_task(_update_subscriptions())

            try:
                while True:
                    if manager_task.done():
                        manager_task.result()
                        raise RuntimeError("AGENT_END 监控的订阅管理任务意外结束")
                    for forward_task in forward_tasks.values():
                        if forward_task.done():
                            forward_task.result()
                            raise RuntimeError("AGENT_END 监控的事件转发任务意外结束")
                    if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                        if emitted_count == 0:
                            raise TimeoutError(f"监控 session {target_session_id} 的 AGENT_END 超时")
                        break

                    try:
                        job_id, event = await asyncio.wait_for(master_queue.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        continue

                    if event.event_id in seen_event_ids:
                        continue
                    seen_event_ids[event.event_id] = True

                    if event.type != EventType.AGENT_END:
                        continue
                    if event.timestamp <= submitted_at:
                        continue

                    final_text = event.payload.final_text
                    if not final_text:
                        continue

                    emitted_message = background_message_bus.emit(
                        session_id,
                        agent_id,
                        final_text,
                        kind=BackgroundMessageKind.interrupt,
                        source_id=monitor_source_id,
                        payload={
                            "target_session_id": target_session_id,
                            "target_job_id": job_id,
                            "target_event_id": event.event_id,
                            "target_event_timestamp": event.timestamp.isoformat(),
                            "final_text": final_text,
                            "monitor_source_id": monitor_source_id,
                            "sequence": emitted_count + 1,
                        },
                    )

                    emitted_count += 1
                    emitted_events.append({
                        "target_session_id": target_session_id,
                        "target_job_id": job_id,
                        "target_event_id": event.event_id,
                        "target_event_timestamp": event.timestamp.isoformat(),
                        "final_text": final_text,
                        "emitted_background_message": emitted_message.model_dump(mode="json"),
                    })

                    if resolved_max_events is not None and emitted_count >= resolved_max_events:
                        break

                return {
                    "target_session_id": target_session_id,
                    "monitor_source_id": monitor_source_id,
                    "emitted_count": emitted_count,
                    "timed_out": deadline is not None and asyncio.get_running_loop().time() >= deadline and emitted_count > 0,
                    "events": emitted_events,
                }
            finally:
                manager_task.cancel()
                for task in forward_tasks.values():
                    task.cancel()
                await asyncio.gather(manager_task, *forward_tasks.values(), return_exceptions=True)

        handle = background_task_registry.spawn(
            session_id=session_id,
            task_name="monitor_session_agent_end",
            runner=_monitor_background_task,
            metadata={
                "target_session_id": target_session_id,
                "timeout_seconds": resolved_timeout_seconds,
                "poll_interval_seconds": poll_interval_seconds,
                "max_events": resolved_max_events,
                "source_id": monitor_source_id,
                "submitted_at": submitted_at.isoformat(),
            },
        )

        return handle.to_dict()

    return monitor_session_agent_end


def create_background_message_collection_tool(
    session_id: str,
    agent_id: str = "default",
    *,
    background_message_bus: BackgroundMessageBusProtocol,
) -> BaseTool:
    """创建收集后台消息的工具。"""
    @tool("collect_background_messages")
    async def collect_background_messages(
        source_id: str | None = None,
        after_message_id: str | None = None,
        timeout_seconds: int = 300,
        poll_interval_seconds: float = 1.0,
        stop_on_interrupt: bool = True,
    ) -> dict[str, Any]:
        """持续收集当前 session/agent 的后台消息。"""
        batch = await background_message_bus.collect(
            session_id,
            agent_id,
            source_id=source_id,
            after_message_id=after_message_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            stop_on_interrupt=stop_on_interrupt,
        )
        return batch.model_dump(mode="json")

    return collect_background_messages
