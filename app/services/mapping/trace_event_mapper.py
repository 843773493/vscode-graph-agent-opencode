from __future__ import annotations

from datetime import datetime
from typing import Any

from app.schemas.public_v2.trace import TraceEventDTO


class TraceEventMapper:
    _SUPPORTED_TYPES = {
        "agent_start",
        "agent_step",
        "llm_request",
        "tool_call_start",
        "tool_call_end",
        "agent_end",
        "error",
        "message_created",
        "job_created",
        "job_started",
        "job_completed",
        "job_cancelled",
        "job_failed",
        "status_change",
        "text_start",
        "text_delta",
        "text_end",
        "system_reminder_injected",
        "session_interrupted",
    }

    def map_many(self, events: list[dict[str, Any]], session_id: str = "") -> list[TraceEventDTO]:
        mapped: list[TraceEventDTO] = []
        for event in events:
            dto = self.map_one(event, session_id=session_id)
            if dto is not None:
                mapped.append(dto)
        mapped.sort(key=lambda item: item.timestamp)
        return mapped

    def map_one(self, event: dict[str, Any], session_id: str = "") -> TraceEventDTO | None:
        event_type = event.get("type")
        if event_type not in self._SUPPORTED_TYPES:
            return None

        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        # session_id 优先级：payload > event 顶层 > 传入参数 > 空字符串
        resolved_session_id = (
            payload.get("session_id")
            or event.get("session_id")
            or session_id
            or event.get("thread_id")
            or ""
        )
        job_id = event.get("job_id") or payload.get("job_id")
        step_id = event.get("step_id")
        timestamp = self._parse_timestamp(event.get("timestamp"), payload.get("timestamp"))

        phase, title, content, status, tool_name = self._build_view_model(event_type, payload)

        return TraceEventDTO(
            event_id=event.get("event_id") or "",
            session_id=session_id,
            job_id=job_id,
            type=event_type,
            phase=phase,
            title=title,
            content=content,
            status=status,
            tool_name=tool_name,
            step_id=step_id,
            timestamp=timestamp,
            raw=event,
        )

    def _build_view_model(self, event_type: str, payload: dict[str, Any]) -> tuple[str, str, str, str | None, str | None]:
        if event_type == "message_created":
            return "message", "用户消息已创建", payload.get("content") or "用户消息已记录", "completed", None
        if event_type == "job_created":
            return "job", "任务已创建", payload.get("message") or "任务已创建，准备执行", "running", None
        if event_type == "job_started":
            return "job", "任务已开始", payload.get("message") or "任务已开始执行", "running", None
        if event_type == "job_completed":
            return "job", "任务已完成", payload.get("result") or "任务已成功完成", "completed", None
        if event_type == "job_cancelled":
            return "job", "任务已取消", payload.get("message") or "任务已取消", "failed", None
        if event_type == "job_failed":
            return "job", "任务失败", payload.get("error") or "任务执行失败", "failed", None
        if event_type == "status_change":
            status = payload.get("status") or "状态变更"
            reason = payload.get("reason") or ""
            return "status", str(status), str(reason or "状态已更新"), "running", None
        if event_type == "agent_start":
            return "agent", "开始执行", payload.get("message") or "agent 启动，准备处理用户请求", "running", None
        if event_type == "agent_step":
            phase = payload.get("phase") or "执行步骤"
            return "agent", f"步骤：{phase}", payload.get("message") or f"agent 正在执行 {phase}", "running", None
        if event_type == "llm_request":
            model = payload.get("model") or "未知模型"
            return "llm", "模型请求", f"正在请求模型：{model}", "running", None
        if event_type == "tool_call_start":
            tool_name = payload.get("tool_name") or payload.get("name") or "未知工具"
            return "tool", "调用工具", f"正在调用 {tool_name}", "running", tool_name
        if event_type == "tool_call_end":
            tool_name = payload.get("tool_name") or payload.get("name") or "未知工具"
            return "tool", "工具返回", f"工具 {tool_name} 已返回结果", "completed", tool_name
        if event_type == "agent_end":
            return "agent", "执行结束", payload.get("final_text") or "agent 已完成本轮处理", "completed", None
        if event_type == "text_start":
            return "text", "文本开始", payload.get("text") or "助手文本开始生成", "running", None
        if event_type == "text_delta":
            return "text", "文本流", payload.get("text") or "助手正在流式输出", "running", None
        if event_type == "text_end":
            return "text", "文本结束", payload.get("text") or "助手文本生成结束", "completed", None
        if event_type == "system_reminder_injected":
            return "system", "系统提醒", payload.get("content") or "系统提醒已注入", "running", None
        if event_type == "session_interrupted":
            phase = payload.get("phase") or "text"
            tool_name = payload.get("tool_name")
            if tool_name:
                message = f"会话已打断（{phase} 阶段，工具：{tool_name}）"
            else:
                message = f"会话已打断（{phase} 阶段）"
            return "session", "会话已打断", message, "completed", tool_name
        error_text = payload.get("error") or "执行失败"
        return "error", "执行失败", error_text, "failed", None

    def _parse_timestamp(self, *values: Any) -> datetime:
        for value in values:
            if isinstance(value, datetime):
                return value
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    continue
        return datetime.now().astimezone()