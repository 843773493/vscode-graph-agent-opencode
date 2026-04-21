from __future__ import annotations
import os
import json
import textwrap
import time
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware import ModelFallbackMiddleware
from langchain.tools.tool_node import ToolCallRequest
from langchain.messages import ToolMessage
from langgraph.types import Command

from app.core.path_utils import get_workspace_root, get_logs_dir
from app.core.event_bus import EventBus, EventType
from app.services.config_service import ConfigService


class LLMLoggingMiddleware(AgentMiddleware):
    """唯一职责：存储每个LLM调用的完整原始请求/响应"""

    def __init__(self):
        self._prepared_session_dirs: set[str] = set()
    
    def _get_session_id(self, runtime) -> str:
        """直接读取 LangChain 的 thread_id。"""
        execution_info = runtime.execution_info
        return execution_info.thread_id

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
            
            def serialize_object(obj):
                if hasattr(obj, '__dict__'):
                    result = {}
                    for key, value in obj.__dict__.items():
                        if not key.startswith('_'):
                            try:
                                json.dumps(value, default=str)
                                result[key] = value
                            except:
                                result[key] = str(value)
                    return result
                return str(obj)
            
            log_data = {
                "timestamp": timestamp,
                "session_id": session_id,
                "request": serialize_object(request),
                "response": serialize_object(response)
            }
            
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(log_data, f, ensure_ascii=False, indent=2, default=str)
                
        except Exception:
            pass

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        session_id = self._get_session_id(request.runtime)
        model_name = getattr(request.model, "model_name", str(request.model))
        
        response = handler(request)
        
        self._save_log(session_id, request, response)
        
        try:
            bus = EventBus.get_instance()
            import asyncio
            asyncio.create_task(bus.publish(
                job_id=session_id,
                event_type=EventType.LLM_REQUEST,
                payload={"model": model_name, "timestamp": int(time.time() * 1000)},
                agent_id="deep_agent"
            ))
        except Exception:
            pass

        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        session_id = self._get_session_id(request.runtime)
        model_name = getattr(request.model, "model_name", str(request.model))
        
        response = await handler(request)
        
        self._save_log(session_id, request, response)
        
        try:
            bus = EventBus.get_instance()
            await bus.publish(
                job_id=session_id,
                event_type=EventType.LLM_REQUEST,
                payload={"model": model_name, "timestamp": int(time.time() * 1000)},
                agent_id="deep_agent"
            )
        except Exception:
            pass

        return response


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
                "data": data
            }
            
            # 追加写入JSONL格式，每个事件一行
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
            "args": tool_call.get("args", {})
        })
        
        result = handler(request)
        
        self._save_trace_event(session_id, "tool_call_end", {
            "tool_name": tool_name,
            "result": str(result)
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
            "args": tool_call.get("args", {})
        })
        
        result = await handler(request)
        
        self._save_trace_event(session_id, "tool_call_end", {
            "tool_name": tool_name,
            "result": str(result)
        })
        
        return result

    def after_agent(self, state: dict[str, Any], runtime):
        session_id = self._get_session_id(runtime)
        self._save_trace_event(session_id, "agent_end", {"final_message_count": len(state.get("messages", []))})
        return None


class AgentExecutionService:
    _instance: Optional[AgentExecutionService] = None
    
    def __init__(self):
        config_service = ConfigService.get_instance()
        providers = config_service.get_llm_providers()
        
        # 构建模型列表，支持fallback
        models = []
        for provider in providers:
            model = ChatOpenAI(
                model=provider["model"],
                api_key=provider["api_key"],
                base_url=provider["endpoint"],
                use_responses_api=(provider.get("interface") == "responses"),
                max_retries=3,
            )
            models.append(model)
        
        # 主模型和fallback模型
        self.model = models[0] if models else None
        self.midware_fallback_models = ModelFallbackMiddleware(*models[1:]) if len(models) > 1 else None
        
        self._agent_cache = {}
    
    @classmethod
    def get_instance(cls) -> AgentExecutionService:
        if cls._instance is None:
            cls._instance = AgentExecutionService()
        return cls._instance
    
    def _get_or_create_agent(self, session_id: str):
        if session_id in self._agent_cache:
            return self._agent_cache[session_id]
        
        workspace_root = get_workspace_root()
        backend = FilesystemBackend(
            root_dir=str(workspace_root),
            virtual_mode=True,
        )
        
        checkpointer = MemorySaver()
        
        # 构建中间件列表，添加fallback中间件
        middleware_list = [
            LLMLoggingMiddleware(),
            ExecutionTraceMiddleware(),
        ]
        
        if self.midware_fallback_models:
            middleware_list.append(self.midware_fallback_models)
        
        agent = create_deep_agent(
            model=self.model,
            backend=backend,
            system_prompt="You are a helpful assistant.",
            checkpointer=checkpointer,
            middleware=middleware_list
        )
        
        self._agent_cache[session_id] = agent
        return agent
    
    @classmethod
    async def run_step(cls, session_id: str, message: str) -> str:
        """
        执行单步Agent调用
        
        Args:
            session_id: 会话ID
            message: 用户输入消息
            
        Returns:
            Agent响应内容
        """
        instance = cls.get_instance()
        agent = instance._get_or_create_agent(session_id)
        bus = EventBus.get_instance()
        
        # 发布AGENT_START事件
        await bus.publish(
            job_id=session_id,
            event_type=EventType.AGENT_START,
            payload={"message": message},
            agent_id="deep_agent"
        )
        
        config = {
            "configurable": {
                "thread_id": session_id,
            }
        }
        
        # 发布AGENT_STEP事件
        await bus.publish(
            job_id=session_id,
            event_type=EventType.AGENT_STEP,
            payload={"phase": "invoking_agent"},
            agent_id="deep_agent"
        )
        
        try:
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": message}]},
                config=config
            )
            
            response_content = result["messages"][-1].content
            
            # 发布AGENT_END事件
            await bus.publish(
                job_id=session_id,
                event_type=EventType.AGENT_END,
                payload={"response_length": len(response_content)},
                agent_id="deep_agent"
            )
            
            return response_content
            
        except Exception as e:
            # 发布ERROR事件
            await bus.publish(
                job_id=session_id,
                event_type=EventType.ERROR,
                payload={"error": str(e), "phase": "agent_execution"},
                agent_id="deep_agent"
            )
            raise
    
    @classmethod
    def get_for_session(cls, session_id: str):
        """
        获取指定会话的Agent实例
        """
        instance = cls.get_instance()
        return instance._get_or_create_agent(session_id)
    
    @classmethod
    def get_available_tools(cls) -> List[Dict[str, Any]]:
        """
        获取DeepAgent支持的所有可用工具列表
        
        本地运行原则：失败时快速崩溃，不静默降级，不隐藏问题
        """
        # 从agent实例动态获取真实工具列表
        session_id = "tools_inspection_session"
        agent = cls.get_instance()._get_or_create_agent(session_id)
        
        # 使用正确的inspect_agent_tools实现
        tool_map = {}
        graph_view = agent.get_graph()
        nodes = getattr(graph_view, "nodes", {}) or {}
        
        for _, node in nodes.items():
            candidate = getattr(node, "data", node)
            if hasattr(candidate, "tools_by_name"):
                tool_map.update(candidate.tools_by_name)
        
        if not tool_map:
            raise RuntimeError(
                "无法从Agent实例中提取工具列表！\n"
                "Agent图中未找到包含tools_by_name属性的节点。\n"
                "这是严重错误，需要立即修复，不能静默降级。"
            )
        
        tools = []
        for tool_name, tool in tool_map.items():
            tool_def = {
                "id": tool_name,
                "name": tool_name,
                "description": getattr(tool, "description", ""),
                "parameters": tool.args_schema.schema() if hasattr(tool, 'args_schema') else {"type": "object", "properties": {}},
                "category": "general"
            }
            tools.append(tool_def)
        
        return tools
