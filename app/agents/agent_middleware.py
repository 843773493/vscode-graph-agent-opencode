from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable
from collections.abc import Awaitable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse, StateT, ContextT
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.runtime import Runtime
from langgraph.prebuilt.tool_node import ToolCallRequest as ToolCallRequestType
from langgraph.types import Command
from langchain.messages import ToolMessage
from pydantic import BaseModel, Field

from app.core.path_utils import get_logs_dir
from app.core.job_event_bus import EventType, JobEventBus


# ========== Pydantic 日志模型定义 ==========
class _BaseLLMLog(BaseModel):
    """LLM 日志基类"""
    model_config = {"arbitrary_types_allowed": True}


class LLMRequestLog(_BaseLLMLog):
    """LLM 请求日志"""
    timestamp: int
    session_id: str
    job_id: str | None = None
    model_name: str | None = None
    messages: list[dict[str, Any]]
    tools: list[Any] | None = None
    system_message: dict[str, Any] | None = None
    # 可以添加其他需要的字段


class LLMResponseLog(_BaseLLMLog):
    """LLM 响应日志"""
    result: list[dict[str, Any]]
    structured_response: Any | None = None


class LLMFullLog(_BaseLLMLog):
    """完整的 LLM 请求-响应日志"""
    timestamp: int
    session_id: str
    job_id: str | None = None
    request: LLMRequestLog
    response: LLMResponseLog


# ========== Middleware 实现 ==========
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

    def _save_log(self, session_id: str, request: ModelRequest[Any], response: ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]) -> None:
        try:
            logs_dir = self._ensure_session_dir(session_id)
            timestamp = int(time.time() * 1000)
            log_file = logs_dir / f"{timestamp}.json"

            # 提取请求信息
            model_name = getattr(request.model, "model_name", None)
            
            # 序列化消息列表
            messages_list = []
            for msg in request.messages:
                if isinstance(msg, BaseMessage):
                    messages_list.append(msg.model_dump())
                else:
                    messages_list.append(str(msg))
            
            # 序列化工具列表（templates 或 tools 字段，取决于 ModelRequest 版本）
            tools_list = None
            tools_attr = getattr(request, 'tools', None) or getattr(request, 'templates', None)
            if tools_attr is not None:
                tools_list = []
                for tool in tools_attr:
                    if isinstance(tool, BaseMessage):
                        tools_list.append(tool.model_dump())
                    elif hasattr(tool, 'model_dump'):
                        try:
                            tools_list.append(tool.model_dump())
                        except Exception:
                            tools_list.append(str(tool))
                    else:
                        tools_list.append(str(tool))
            
            # 序列化 system_message
            system_msg = None
            if hasattr(request, 'system_message') and request.system_message is not None:
                if isinstance(request.system_message, BaseMessage):
                    system_msg = request.system_message.model_dump()
                else:
                    system_msg = str(request.system_message)
            
            # 构建请求日志对象（使用 Pydantic 自动序列化）
            req_log = LLMRequestLog(
                timestamp=timestamp,
                session_id=session_id,
                job_id=self._get_job_id(request.runtime) if hasattr(request, 'runtime') else None,
                model_name=model_name,
                messages=messages_list,
                tools=tools_list,
                system_message=system_msg,
            )
            
            # 提取响应信息
            if isinstance(response, AIMessage):
                result_list = [response.model_dump()]
            elif isinstance(response, ModelResponse):
                result_list = []
                for item in response.result:
                    if isinstance(item, BaseMessage):
                        result_list.append(item.model_dump())
                    else:
                        result_list.append(str(item))
            elif isinstance(response, ExtendedModelResponse):
                # ExtendedModelResponse 包含 model_response 字段
                mr = response.model_response
                result_list = []
                if isinstance(mr, ModelResponse):
                    for item in mr.result:
                        if isinstance(item, BaseMessage):
                            result_list.append(item.model_dump())
                        else:
                            result_list.append(str(item))
                else:
                    result_list = [str(mr)]
            else:
                result_list = [str(response)]
            
            resp_log = LLMResponseLog(
                result=result_list,
                structured_response=getattr(response, 'structured_response', None),
            )
            
            # 构建完整日志
            full_log = LLMFullLog(
                timestamp=timestamp,
                session_id=session_id,
                job_id=req_log.job_id,
                request=req_log,
                response=resp_log,
            )
            
            # 写入文件（Pydantic 的 model_dump 可指定为 dict）
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(full_log.model_dump(), f, ensure_ascii=False, indent=2, default=str)

        except Exception:
            # 日志保存失败不应影响主流程
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
            # asyncio 已在函数外通过 runtime 懒加载
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
