from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.abstractions.background_message_bus import (
    BackgroundMessageBusProtocol,
    BackgroundMessageKind,
)
from app.core.background_task_registry import BackgroundTaskRegistry


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
                current_time = datetime.now(UTC).isoformat(timespec="seconds")
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
