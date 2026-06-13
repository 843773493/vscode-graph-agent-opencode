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

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.core.job_event_bus import EventType
from app.schemas.event import (
    LLMRequestPayload,
)


# ========== 类型定义 ==========
class MessageDict(BaseModel):
    """BaseMessage.model_dump() 返回的字典类型"""
    type: str
    content: str | list = Field(default="")
    additional_kwargs: dict = Field(default_factory=dict)
    response_metadata: dict = Field(default_factory=dict)
    tool_calls: list = Field(default_factory=list)
    tool_call_id: str | None = None
    model_config = {"extra": "allow"}


class ToolDict(BaseModel):
    """工具序列化后的字典类型"""
    type: str = "tool"
    name: str | None = None
    description: str | None = None
    args: dict = Field(default_factory=dict)
    model_config = {"extra": "allow"}


# ========== Pydantic 日志模型定义 ==========
class LLMRequestLog(BaseModel):
    """LLM 请求日志"""
    timestamp: int
    session_id: str
    job_id: str | None = None
    model_name: str | None = None
    messages: list[MessageDict]
    tools: list[ToolDict | str] | None = None
    system_message: MessageDict | None = None


class LLMResponseLog(BaseModel):
    """LLM 响应日志"""
    result: list[MessageDict]
    structured_response: Any | None = None


class LLMFullLog(BaseModel):
    """完整的 LLM 请求-响应日志"""
    timestamp: int
    session_id: str
    job_id: str | None = None
    request: LLMRequestLog
    response: LLMResponseLog


# ========== Middleware 实现 ==========
class LLMLoggingMiddleware(AgentMiddleware[StateT, Any, Any]):
    """唯一职责：存储每个LLM调用的完整原始请求/响应"""

    def __init__(self, *, job_event_bus: JobEventBusProtocol) -> None:
        self._prepared_session_dirs: set[str] = set()
        self._job_event_bus = job_event_bus

    def _get_session_id(self, runtime: Runtime[Any]) -> str:
        """优先读取显式注入的 session_id，其次回退到 thread_id。"""
        configurable = getattr(runtime, 'configurable', None)
        if isinstance(configurable, dict):
            session_id = configurable.get('session_id')
            if session_id:
                return session_id

        execution_info = getattr(runtime, 'execution_info', None)
        if execution_info is not None:
            thread_id = getattr(execution_info, 'thread_id', None)
            if isinstance(thread_id, str) and thread_id:
                return thread_id

        return "unknown_session"

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
            if self._job_event_bus is None:
                raise RuntimeError("LLMLoggingMiddleware 未绑定 JobEventBus")
            bus = self._job_event_bus
            import asyncio
            asyncio.create_task(bus.publish(
                job_id=job_id,
                event_type=EventType.LLM_REQUEST,
                payload={"model": model_name, "timestamp": int(time.time() * 1000)},
                agent_id="deep_agent",
            ))
        except Exception as error:
            import logging
            logging.getLogger(__name__).exception(
                "[execution_trace_middleware] 保存轨迹失败: session_id=%s event_type=%s error=%s",
                session_id,
                EventType.LLM_REQUEST,
                error,
            )

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
            if self._job_event_bus is None:
                raise RuntimeError("LLMLoggingMiddleware 未绑定 JobEventBus")
            bus = self._job_event_bus
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
    """唯一职责：把执行轨迹事件发布到事件总线，由 TraceEventRecorder 持久化。"""

    def __init__(self, *, job_event_bus: JobEventBusProtocol) -> None:
        self._session_start_times: dict[str, float] = {}
        self._job_event_bus = job_event_bus

    def _get_session_id(self, runtime: Runtime[Any]) -> str:
        """优先读取显式注入的 session_id，其次回退到 thread_id。"""
        configurable = getattr(runtime, 'configurable', None)
        if isinstance(configurable, dict):
            session_id = configurable.get('session_id')
            if session_id:
                return session_id

        execution_info = runtime.execution_info
        return execution_info.thread_id

    def _get_job_id(self, runtime: Runtime[Any]) -> str:
        """从 runtime 中提取 job_id，回退到 session_id。"""
        configurable = getattr(runtime, 'configurable', None)
        if isinstance(configurable, dict):
            job_id = configurable.get('job_id')
            if job_id:
                return job_id

        execution_info = getattr(runtime, 'execution_info', None)
        if execution_info is not None:
            configurable = getattr(execution_info, 'configurable', None)
            if isinstance(configurable, dict):
                job_id = configurable.get('job_id')
                if job_id:
                    return job_id
            job_id = getattr(execution_info, 'job_id', None)
            if job_id:
                return job_id

        return self._get_session_id(runtime)

    def _publish_trace_event(self, session_id: str, job_id: str, event_type: str, data: dict[str, Any]) -> None:
        try:
            if self._job_event_bus is None:
                raise RuntimeError("ExecutionTraceMiddleware 未绑定 JobEventBus")

            agent_id = data.get("agent_id") or "unknown_agent"

            async def _publish() -> None:
                await self._job_event_bus.publish(
                    job_id=job_id,
                    event_type=event_type,
                    payload=data,
                    agent_id=agent_id,
                )

            try:
                asyncio.get_running_loop()
                asyncio.create_task(_publish())
            except RuntimeError:
                import logging
                logging.getLogger(__name__).warning(
                    "[execution_trace_middleware] 无事件循环，跳过事件发布: job_id=%s event_type=%s",
                    job_id,
                    event_type,
                )
        except Exception as exc:
            import logging
            logger = logging.getLogger(__name__)
            logger.exception(
                "[execution_trace_middleware] 发布轨迹事件失败: session_id=%s event_type=%s error=%s",
                session_id,
                event_type,
                exc,
            )
            raise

    def before_agent(
        self,
        state: dict[str, Any],
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        session_id = self._get_session_id(runtime)
        job_id = self._get_job_id(runtime)
        agent_id = getattr(runtime.execution_info, "agent_id", None) or "unknown_agent"
        self._publish_trace_event(session_id, job_id, "agent_start", {
            "message": "agent 启动，准备处理用户请求",
            "message_count": len(state.get("messages", [])),
            "agent_id": agent_id,
        })
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequestType,
        handler: Callable[[ToolCallRequestType], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        session_id = self._get_session_id(request.runtime)
        job_id = self._get_job_id(request.runtime)
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "unknown_tool")

        self._publish_trace_event(session_id, job_id, "tool_call_start", {
            "tool_name": tool_name,
            "args": tool_call.get("args", {}),
        })

        result = handler(request)

        self._publish_trace_event(session_id, job_id, "tool_call_end", {
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
        job_id = self._get_job_id(request.runtime)
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "unknown_tool")

        self._publish_trace_event(session_id, job_id, "tool_call_start", {
            "tool_name": tool_name,
            "args": tool_call.get("args", {}),
        })

        result = await handler(request)

        self._publish_trace_event(session_id, job_id, "tool_call_end", {
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
        job_id = self._get_job_id(runtime)
        agent_id = getattr(runtime.execution_info, "agent_id", None) or "unknown_agent"
        self._publish_trace_event(session_id, job_id, "agent_end", {
            "final_text": "agent 已完成本轮处理",
            "final_message_count": len(state.get("messages", [])),
            "agent_id": agent_id,
        })
        return None
