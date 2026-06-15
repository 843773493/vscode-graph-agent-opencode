"""SystemReminderMiddleware：在模型调用前注入 <system_reminder>。"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable
from collections.abc import Awaitable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse, StateT
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.core.checkpoint_saver import next_channel_version
from app.core.job_context import get_current_agent_id, get_current_job_id
from app.core.job_event_bus import EventType
from app.schemas.event import SystemReminderInjectedPayload
from app.schemas.system_reminder import (
    ReminderTriggerContext,
    SystemReminder,
    SystemReminderPosition,
    ToolResultSnapshot,
)
from app.services.infrastructure.system_reminder_triggers import (
    SystemReminderTriggerRegistry,
)


def _checkpoint_config(session_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}}


class SystemReminderMiddleware(AgentMiddleware[StateT, Any, Any]):
    """在消息列表的断点处注入 <system_reminder> 上下文提醒。"""

    def __init__(
        self,
        *,
        trigger_registry: SystemReminderTriggerRegistry,
        job_event_bus: Any | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> None:
        self._trigger_registry = trigger_registry
        self._job_event_bus = job_event_bus
        self._checkpointer = checkpointer

    def _get_session_id(self, runtime: Any) -> str:
        configurable = getattr(runtime, "configurable", None)
        if isinstance(configurable, dict):
            session_id = configurable.get("session_id")
            if session_id:
                return session_id
        execution_info = getattr(runtime, "execution_info", None)
        if execution_info is not None:
            thread_id = getattr(execution_info, "thread_id", None)
            if isinstance(thread_id, str) and thread_id:
                return thread_id
        return "unknown_session"

    def _get_job_id(self, runtime: Any) -> str:
        context_job_id = get_current_job_id()
        if context_job_id:
            return context_job_id

        configurable = getattr(runtime, "configurable", None)
        if isinstance(configurable, dict):
            job_id = configurable.get("job_id")
            if job_id:
                return job_id

        execution_info = getattr(runtime, "execution_info", None)
        if execution_info is not None:
            configurable = getattr(execution_info, "configurable", None)
            if isinstance(configurable, dict):
                job_id = configurable.get("job_id")
                if job_id:
                    return job_id
            job_id = getattr(execution_info, "job_id", None)
            if job_id:
                return job_id

        return self._get_session_id(runtime)

    def _get_agent_id(self, runtime: Any) -> str:
        agent_id = get_current_agent_id()
        if agent_id:
            return agent_id
        return "unknown_agent"

    def _build_trigger_context(
        self,
        request: ModelRequest[Any],
    ) -> ReminderTriggerContext:
        from app.core.job_context import get_last_turn_status, get_recent_tool_results

        messages = list(getattr(request, "messages", []))
        recent_tool_results = get_recent_tool_results() or []

        return ReminderTriggerContext(
            session_id=self._get_session_id(request.runtime),
            job_id=self._get_job_id(request.runtime),
            agent_id=self._get_agent_id(request.runtime),
            messages=messages,
            last_turn_status=get_last_turn_status() or "ok",
            recent_tool_results=[
                raw if isinstance(raw, ToolResultSnapshot) else ToolResultSnapshot(**raw)
                for raw in recent_tool_results
            ],
        )

    def _extract_interrupt_reminder(
        self,
        session_id: str,
    ) -> tuple[SystemReminder | None, Any]:
        """从 checkpoint 读取用户打断标记，转换为 system_reminder。

        返回 (reminder, checkpoint_tuple)；调用方负责在注入后持久化并清除标记。
        """
        if self._checkpointer is None:
            return None, None

        config = _checkpoint_config(session_id)
        try:
            tup = self._checkpointer.get_tuple(config)
        except Exception:
            return None, None

        if tup is None:
            return None, None

        channel_values = tup.checkpoint.get("channel_values", {})
        interrupt_marker = channel_values.get("__boxteam_interrupt__")
        if not interrupt_marker:
            return None, None

        phase = interrupt_marker.get("phase", "text")
        tool_name = interrupt_marker.get("tool_name")
        interrupted_at = interrupt_marker.get("interrupted_at", "")

        if phase == "tool" and tool_name:
            content = (
                f"用户在你调用工具（{tool_name}）的过程中于 {interrupted_at} 打断。"
                f"当前工具调用已被取消，请停止当前操作，根据已有信息回应用户最新请求。"
            )
        else:
            content = (
                f"用户在文本生成过程中于 {interrupted_at} 打断。"
                f"请停止当前输出，根据已有信息回应用户最新请求。"
            )

        reminder = SystemReminder(
            content=content,
            position=SystemReminderPosition.AFTER_LAST_ASSISTANT,
            dedup_key=f"interrupt:{session_id}:{interrupted_at}",
            metadata={"phase": phase, "tool_name": tool_name, "source": "interrupt"},
        )
        return reminder, tup

    def _persist_messages_and_clear_marker(
        self,
        session_id: str,
        messages: list[BaseMessage],
        tup: Any,
    ) -> None:
        """将注入 system_reminder 后的消息写回 checkpoint，并清除打断标记。"""
        if self._checkpointer is None or tup is None:
            return

        checkpoint = tup.checkpoint.copy()
        channel_values = dict(checkpoint.get("channel_values", {}))
        channel_values["messages"] = messages
        channel_values.pop("__boxteam_interrupt__", None)
        checkpoint["channel_values"] = channel_values

        checkpoint["id"] = str(uuid.uuid4())

        channel_versions = dict(checkpoint.get("channel_versions", {}))
        channel_versions["messages"] = next_channel_version(channel_versions.get("messages"))
        channel_versions.pop("__boxteam_interrupt__", None)
        checkpoint["channel_versions"] = channel_versions

        updated_channels = list(checkpoint.get("updated_channels", []))
        if "messages" not in updated_channels:
            updated_channels.append("messages")
        checkpoint["updated_channels"] = updated_channels

        metadata = {"source": "interrupt_reminder_persisted", "step": -1, "writes": {}}
        try:
            self._checkpointer.put(
                config=tup.config,
                checkpoint=checkpoint,
                metadata=metadata,
                new_versions={"messages": channel_versions["messages"]},
            )
        except Exception:
            return

    def _inject_reminders(
        self,
        messages: list[BaseMessage],
        reminders: list[SystemReminder],
    ) -> list[BaseMessage]:
        if not reminders:
            return messages

        by_position: dict[SystemReminderPosition, list[SystemReminder]] = {}
        for r in reminders:
            by_position.setdefault(r.position, []).append(r)

        for pos in by_position:
            by_position[pos].sort(key=lambda r: (r.priority, r.content))

        result = list(messages)

        def find_last_indices(msgs: list[BaseMessage]) -> tuple[int, int, int]:
            last_assistant_idx = -1
            last_tool_idx = -1
            last_user_idx = -1
            for i, msg in enumerate(msgs):
                if isinstance(msg, AIMessage):
                    last_assistant_idx = i
                elif isinstance(msg, ToolMessage):
                    last_tool_idx = i
                elif isinstance(msg, HumanMessage):
                    last_user_idx = i
                elif isinstance(msg, BaseMessage):
                    role = getattr(msg, "type", "") or getattr(msg, "role", "")
                    if role in {"human", "user"}:
                        last_user_idx = i
                    elif role in {"ai", "assistant"}:
                        last_assistant_idx = i
                    elif role == "tool":
                        last_tool_idx = i
            return last_assistant_idx, last_tool_idx, last_user_idx

        def _append_to_assistant(index: int, reminders_for_pos: list[SystemReminder]) -> None:
            msg = result[index]
            if not isinstance(msg, AIMessage):
                return
            parts: list[str] = []
            meta: dict[str, Any] = {}
            for r in reminders_for_pos:
                parts.append(f"<system_reminder>\n{r.content}\n</system_reminder>")
                meta.update(r.metadata)
            appended = "\n".join(parts)

            # 直接修改原 AIMessage 对象，确保 LangGraph state channel 同步
            if isinstance(msg.content, str):
                msg.content = msg.content + "\n" + appended
            elif isinstance(msg.content, list):
                msg.content.append({"type": "text", "text": "\n" + appended})
            else:
                msg.content = str(msg.content) + "\n" + appended

            msg.response_metadata = dict(msg.response_metadata or {})
            msg.response_metadata.update(meta)

        def insert_after(index: int, reminders_for_pos: list[SystemReminder]) -> None:
            nonlocal result
            wrapped = [
                HumanMessage(
                    content=f"<system_reminder>\n{r.content}\n</system_reminder>"
                )
                for r in reminders_for_pos
            ]
            result = result[: index + 1] + wrapped + result[index + 1 :]

        def fallback_to_append(position: SystemReminderPosition) -> None:
            by_position[SystemReminderPosition.APPEND] = (
                by_position.get(SystemReminderPosition.APPEND, [])
                + by_position[position]
            )
            del by_position[position]

        if SystemReminderPosition.AFTER_LAST_ASSISTANT in by_position:
            last_assistant_idx, _, _ = find_last_indices(result)
            if last_assistant_idx >= 0:
                _append_to_assistant(last_assistant_idx, by_position[SystemReminderPosition.AFTER_LAST_ASSISTANT])
                del by_position[SystemReminderPosition.AFTER_LAST_ASSISTANT]
            else:
                fallback_to_append(SystemReminderPosition.AFTER_LAST_ASSISTANT)

        if SystemReminderPosition.AFTER_TOOL_CALLS in by_position:
            _, last_tool_idx, _ = find_last_indices(result)
            if last_tool_idx >= 0:
                insert_after(last_tool_idx, by_position[SystemReminderPosition.AFTER_TOOL_CALLS])
                del by_position[SystemReminderPosition.AFTER_TOOL_CALLS]
            else:
                fallback_to_append(SystemReminderPosition.AFTER_TOOL_CALLS)

        if SystemReminderPosition.AFTER_LAST_USER in by_position:
            _, _, last_user_idx = find_last_indices(result)
            if last_user_idx >= 0:
                insert_after(last_user_idx, by_position[SystemReminderPosition.AFTER_LAST_USER])
                del by_position[SystemReminderPosition.AFTER_LAST_USER]
            else:
                fallback_to_append(SystemReminderPosition.AFTER_LAST_USER)

        if SystemReminderPosition.APPEND in by_position:
            result.extend(
                HumanMessage(
                    content=f"<system_reminder>\n{r.content}\n</system_reminder>"
                )
                for r in by_position[SystemReminderPosition.APPEND]
            )

        return result

    async def _publish_injected_event(
        self,
        job_id: str,
        agent_id: str,
        reminder: SystemReminder,
    ) -> None:
        if self._job_event_bus is None:
            return
        await self._job_event_bus.publish(
            job_id=job_id,
            event_type=EventType.SYSTEM_REMINDER_INJECTED,
            payload=SystemReminderInjectedPayload(
                position=reminder.position.value,
                content=reminder.content,
                dedup_key=reminder.dedup_key,
            ).model_dump(),
            agent_id=agent_id,
        )

    def _publish_injected_event_sync(
        self,
        job_id: str,
        agent_id: str,
        reminder: SystemReminder,
    ) -> None:
        if self._job_event_bus is None:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self._publish_injected_event(job_id, agent_id, reminder)
            )
        except RuntimeError:
            pass

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse],
    ) -> ModelResponse | AIMessage | ExtendedModelResponse:
        ctx = self._build_trigger_context(request)
        reminders = self._trigger_registry.collect(ctx)

        # 从 checkpoint 读取用户打断标记；无标记时不产生副作用
        interrupt_reminder, interrupt_tuple = self._extract_interrupt_reminder(ctx.session_id)
        if interrupt_reminder is not None:
            reminders.append(interrupt_reminder)

        if reminders:
            request.messages = self._inject_reminders(list(request.messages), reminders)
            for reminder in reminders:
                self._publish_injected_event_sync(ctx.job_id, ctx.agent_id, reminder)

        if interrupt_reminder is not None:
            self._persist_messages_and_clear_marker(
                ctx.session_id, list(request.messages), interrupt_tuple
            )

        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse]],
    ) -> ModelResponse | AIMessage | ExtendedModelResponse:
        ctx = self._build_trigger_context(request)
        reminders = self._trigger_registry.collect(ctx)

        # 从 checkpoint 读取用户打断标记；无标记时不产生副作用
        interrupt_reminder, interrupt_tuple = self._extract_interrupt_reminder(ctx.session_id)
        if interrupt_reminder is not None:
            reminders.append(interrupt_reminder)

        if reminders:
            request.messages = self._inject_reminders(list(request.messages), reminders)
            for reminder in reminders:
                await self._publish_injected_event(ctx.job_id, ctx.agent_id, reminder)

        if interrupt_reminder is not None:
            self._persist_messages_and_clear_marker(
                ctx.session_id, list(request.messages), interrupt_tuple
            )

        return await handler(request)
