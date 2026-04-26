from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable
from collections.abc import Awaitable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse, StateT, ContextT
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime
from langgraph.prebuilt.tool_node import ToolCallRequest as ToolCallRequestType
from langgraph.types import Command
from langchain.messages import ToolMessage

from app.core.path_utils import get_logs_dir
from app.core.job_event_bus import EventType, JobEventBus


class LLMLoggingMiddleware(AgentMiddleware[StateT, Any, Any]):
    """唯一职责：存储每个LLM调用的完整原始请求/响应"""

    def __init__(self) -> None:
        self._prepared_session_dirs: set[str] = set()

    def _get_session_id(self, runtime: Runtime[Any]) -> str:
        """直接读取 LangChain 的 thread_id。"""
        execution_info = runtime.execution_info
        return execution_info.thread_id

    def _get_job_id(self, runtime: Runtime[Any]) -> str:
        """
        从runtime中提取job_id。
        优先级：
        1. runtime.configurable.job_id
        2. runtime.execution_info.configurable.job_id
        3. runtime.execution_info.job_id
        4. 回退到 thread_id (session_id)
        """
        # 尝试 runtime.configurable
        configurable = getattr(runtime, 'configurable', None)
        if isinstance(configurable, dict):
            job_id = configurable.get('job_id')
            if job_id:
                return job_id

        # 尝试 runtime.execution_info.configurable
        execution_info = getattr(runtime, 'execution_info', None)
        if execution_info is not None:
            configurable = getattr(execution_info, 'configurable', None)
            if isinstance(configurable, dict):
                job_id = configurable.get('job_id')
                if job_id:
                    return job_id
            # 有些实现可能直接把 job_id 放在 execution_info 上
            job_id = getattr(execution_info, 'job_id', None)
            if job_id:
                return job_id

        # 回退到 thread_id (session_id)
        return self._get_session_id(runtime)

    def _ensure_session_dir(self, session_id: str) -> Path:
        logs_dir = get_logs_dir() / "llm_requests" / session_id
        if session_id not in self._prepared_session_dirs:
            logs_dir.mkdir(exist_ok=True, parents=True)
            self._prepared_session_dirs.add(session_id)
        return logs_dir

    def _save_log(self, session_id: str, request: ModelRequest, response: ModelResponse) -> None:
        try:
            logs_dir = self._ensure_session_dir(session_id)

            timestamp = int(time.time() * 1000)
            log_file = logs_dir / f"{timestamp}.json"

            def serialize_object(obj: Any) -> Any:
                if hasattr(obj, "__dict__"):
                    result: dict[str, Any] = {}
                    for key, value in obj.__dict__.items():
                        if not key.startswith("_"):
                            try:
                                json.dumps(value, default=str)
                                result[key] = value
                            except Exception:
                                result[key] = str(value)
                    return result
                return str(obj)

            log_data: dict[str, Any] = {
                "timestamp": timestamp,
                "session_id": session_id,
                "request": serialize_object(request),
                "response": serialize_object(response),
            }

            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(log_data, f, ensure_ascii=False, indent=2, default=str)

        except Exception:
            pass

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse],
    ) -> ModelResponse | AIMessage | ExtendedModelResponse:
        session_id = self._get_session_id(request.runtime)
        job_id = self._get_job_id(request.runtime)
        model_name = getattr(request.model, "model_name", str(request.model))

        response = handler(request)

        self._save_log(session_id, request, response)

        try:
            bus = JobEventBus.get_instance()
            import asyncio
            asyncio.create_task(bus.publish(
                job_id=job_id,
                event_type=EventType.LLM_REQUEST,
                payload={"model": model_name, "timestamp": int(time.time() * 1000)},
                agent_id="deep_agent",
            ))
        except Exception:
            pass

        return response

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse]],
    ) -> ModelResponse | AIMessage | ExtendedModelResponse:
        session_id = self._get_session_id(request.runtime)
        job_id = self._get_job_id(request.runtime)
        model_name = getattr(request.model, "model_name", str(request.model))

        response = await handler(request)

        self._save_log(session_id, request, response)

        try:
            bus = JobEventBus.get_instance()
            await bus.publish(
                job_id=job_id,
                event_type=EventType.LLM_REQUEST,
                payload={"model": model_name, "timestamp": int(time.time() * 1000)},
                agent_id="deep_agent",
            )
        except Exception:
            pass

        return response


class ExecutionTraceMiddleware(AgentMiddleware[StateT, Any, Any]):
    """唯一职责：存储完整的执行轨迹事件"""

    def __init__(self) -> None:
        self._session_start_times: dict[str, float] = {}

    def _get_session_id(self, runtime: Runtime[Any]) -> str:
        """直接读取 LangChain 的 thread_id。"""
        execution_info = runtime.execution_info
        return execution_info.thread_id

    def _save_trace_event(self, session_id: str, event_type: str, data: dict[str, Any]) -> None:
        try:
            logs_dir = get_logs_dir() / "traces"
            logs_dir.mkdir(exist_ok=True, parents=True)

            log_file = logs_dir / f"trace_{session_id}.jsonl"

            timestamp = int(time.time() * 1000)
            log_data: dict[str, Any] = {
                "timestamp": timestamp,
                "event_type": event_type,
                "data": data,
            }

            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_data, ensure_ascii=False, default=str) + "\n")

        except Exception:
            pass

    def before_agent(
        self,
        state: dict[str, Any],
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        session_id = self._get_session_id(runtime)
        self._save_trace_event(session_id, "agent_start", {"message_count": len(state.get("messages", []))})
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequestType,
        handler: Callable[[ToolCallRequestType], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        session_id = self._get_session_id(request.runtime)
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "unknown_tool")

        self._save_trace_event(session_id, "tool_call_start", {
            "tool_name": tool_name,
            "args": tool_call.get("args", {}),
        })

        result = handler(request)

        self._save_trace_event(session_id, "tool_call_end", {
            "tool_name": tool_name,
            "result": str(result),
        })

        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequestType,
        handler: Callable[[ToolCallRequestType], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        session_id = self._get_session_id(request.runtime)
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "unknown_tool")

        self._save_trace_event(session_id, "tool_call_start", {
            "tool_name": tool_name,
            "args": tool_call.get("args", {}),
        })

        result = await handler(request)

        self._save_trace_event(session_id, "tool_call_end", {
            "tool_name": tool_name,
            "result": str(result),
        })

        return result

    def after_agent(
        self,
        state: dict[str, Any],
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        session_id = self._get_session_id(runtime)
        self._save_trace_event(session_id, "agent_end", {"final_message_count": len(state.get("messages", []))})
        return None


class ExecutionTraceMiddleware(AgentMiddleware):
    """唯一职责：存储完整的执行轨迹事件"""

    def __init__(self):
        self._session_start_times = {}

    def _get_session_id(self, runtime) -> str:
        """直接读取 LangChain 的 thread_id。"""
        execution_info = runtime.execution_info
        return execution_info.thread_id

    def _save_trace_event(self, session_id: str, event_type: str, data: dict) -> None:
        try:
            logs_dir = get_logs_dir() / "traces"
            logs_dir.mkdir(exist_ok=True, parents=True)

            log_file = logs_dir / f"trace_{session_id}.jsonl"

            timestamp = int(time.time() * 1000)
            log_data = {
                "timestamp": timestamp,
                "event_type": event_type,
                "data": data,
            }

            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_data, ensure_ascii=False, default=str) + "\n")

        except Exception:
            pass

    def before_agent(self, state: dict[str, Any], runtime):
        session_id = self._get_session_id(runtime)
        self._save_trace_event(session_id, "agent_start", {"message_count": len(state.get("messages", []))})
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        session_id = self._get_session_id(request.runtime)
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "unknown_tool")

        self._save_trace_event(session_id, "tool_call_start", {
            "tool_name": tool_name,
            "args": tool_call.get("args", {}),
        })

        result = handler(request)

        self._save_trace_event(session_id, "tool_call_end", {
            "tool_name": tool_name,
            "result": str(result),
        })

        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        session_id = self._get_session_id(request.runtime)
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "unknown_tool")

        self._save_trace_event(session_id, "tool_call_start", {
            "tool_name": tool_name,
            "args": tool_call.get("args", {}),
        })

        result = await handler(request)

        self._save_trace_event(session_id, "tool_call_end", {
            "tool_name": tool_name,
            "result": str(result),
        })

        return result

    def after_agent(self, state: dict[str, Any], runtime):
        session_id = self._get_session_id(runtime)
        self._save_trace_event(session_id, "agent_end", {"final_message_count": len(state.get("messages", []))})
        return None
