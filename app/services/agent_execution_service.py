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
        
        # 启用DeepAgent内置文件系统中间件和工具
        from deepagents.middleware.filesystem import FilesystemMiddleware
        
        agent = create_deep_agent(
            model=self.model,
            backend=backend,
            system_prompt="You are a helpful assistant.",
            middleware=[
                FilesystemMiddleware(
                    root_dir=str(session_dir),
                    allow_write=True,
                    allow_read=True,
                    allow_list=True
                )
            ],
            checkpointer=checkpointer,
            enable_builtin_tools=True
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
        from deepagents.tools import get_all_tools
        return get_all_tools()
