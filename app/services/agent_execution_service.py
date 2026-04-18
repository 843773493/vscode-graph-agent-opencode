from __future__ import annotations
import os
from pathlib import Path
from typing import Optional, Dict, Any, List

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

from app.core.path_utils import get_session_path, ensure_session_dir
from app.core.event_bus import EventBus, EventType


class AgentExecutionService:
    _instance: Optional[AgentExecutionService] = None
    
    def __init__(self):
        self.model = ChatOpenAI(
            model=os.getenv("KILO_MODEL", "bytedance-seed/dola-seed-2.0-pro:free"),
            api_key=os.getenv("KILO_API_KEY"),
            base_url=os.getenv("KILO_API_BASE", "https://api.kilo.ai/api/gateway"),
            use_responses_api=False,
            max_retries=3,
        )
        self._agent_cache = {}
    
    @classmethod
    def get_instance(cls) -> AgentExecutionService:
        if cls._instance is None:
            cls._instance = AgentExecutionService()
        return cls._instance
    
    def _get_or_create_agent(self, session_id: str):
        if session_id in self._agent_cache:
            return self._agent_cache[session_id]
        
        session_dir = ensure_session_dir(session_id)
        backend = FilesystemBackend(
            root_dir=str(session_dir),
            virtual_mode=True,
        )
        
        checkpointer = MemorySaver()
        
        agent = create_deep_agent(
            model=self.model,
            backend=backend,
            system_prompt="You are a helpful assistant.",
            checkpointer=checkpointer
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
                "thread_id": session_id
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
        
        # 兼容模式：如果动态获取失败则返回默认列表
        if not tools:
            tools = [
                {
                    "id": "write_todos",
                    "name": "write_todos",
                    "description": "管理待办事项列表",
                    "parameters": {"type": "object", "properties": {}},
                    "category": "system"
                },
                {
                    "id": "ls",
                    "name": "ls",
                    "description": "列出目录内容",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                    "category": "filesystem"
                },
                {
                    "id": "read_file",
                    "name": "read_file",
                    "description": "读取文件内容",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                    "category": "filesystem"
                },
                {
                    "id": "write_file",
                    "name": "write_file",
                    "description": "写入文件内容",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
                    "category": "filesystem"
                },
                {
                    "id": "edit_file",
                    "name": "edit_file",
                    "description": "编辑文件内容",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}},
                    "category": "filesystem"
                },
                {
                    "id": "glob",
                    "name": "glob",
                    "description": "搜索文件匹配模式",
                    "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}},
                    "category": "filesystem"
                },
                {
                    "id": "grep",
                    "name": "grep",
                    "description": "搜索文件内容",
                    "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}},
                    "category": "filesystem"
                },
                {
                    "id": "execute",
                    "name": "execute",
                    "description": "执行Shell命令",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                    "category": "system"
                },
                {
                    "id": "task",
                    "name": "task",
                    "description": "调用子代理",
                    "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "prompt": {"type": "string"}}},
                    "category": "agent"
                }
            ]
        
        return tools
