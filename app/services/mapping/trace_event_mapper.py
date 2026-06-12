from __future__ import annotations

from datetime import datetime
from typing import Any

from app.schemas.public_v2.trace import TraceEventDTO


class TraceEventMapper:
    _SUPPORTED_TYPES = {
        "agent_start",
        "llm_request",
        "tool_call_start",
        "tool_call_end",
        "agent_end",
        "error",
    }

    def map_many(self, events: list[dict[str, Any]]) -> list[TraceEventDTO]:
        mapped: list[TraceEventDTO] = []
        for event in events:
            dto = self.map_one(event)
            if dto is not None:
                mapped.append(dto)
        mapped.sort(key=lambda item: item.timestamp)
        return mapped

    def map_one(self, event: dict[str, Any]) -> TraceEventDTO | None:
        event_type = event.get("type")
        if event_type not in self._SUPPORTED_TYPES:
            return None

        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        session_id = payload.get("session_id") or event.get("session_id") or event.get("thread_id") or ""
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
        if event_type == "agent_start":
            return "agent", "开始执行", payload.get("message") or "agent 启动，准备处理用户请求", "running", None
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