from __future__ import annotations
from typing import Optional, Dict, Any, List

from app.core.job_event_bus import EventType, JobEventBus
from app.agents.agent_factory import create_runtime_deep_agent_for_session, resolve_agent_id


class AgentExecutionService:
    _instance: Optional[AgentExecutionService] = None
    
    def __init__(self):
        self._agent_cache = {}
    
    @classmethod
    def get_instance(cls) -> AgentExecutionService:
        if cls._instance is None:
            cls._instance = AgentExecutionService()
        return cls._instance
    
    def _get_or_create_agent(self, session_id: str, agent_id: str | None = None):
        resolved_agent_id = resolve_agent_id(agent_id)
        cache_key = f"{session_id}::{resolved_agent_id}"
        if cache_key in self._agent_cache:
            return self._agent_cache[cache_key]

        agent = create_runtime_deep_agent_for_session(
            session_id=session_id,
            agent_id=resolved_agent_id,
            name=resolved_agent_id,
        )
        
        self._agent_cache[cache_key] = agent
        return agent
    
    @classmethod
    async def run_step(cls, session_id: str, message: str, agent_id: str | None = None) -> str:
        """
        执行单步Agent调用
        
        Args:
            session_id: 会话ID
            message: 用户输入消息
            
        Returns:
            Agent响应内容
        """
        instance = cls.get_instance()
        resolved_agent_id = resolve_agent_id(agent_id)
        agent = instance._get_or_create_agent(session_id, resolved_agent_id)
        bus = JobEventBus.get_instance()
        
        # 发布AGENT_START事件
        await bus.publish(
            job_id=session_id,
            event_type=EventType.AGENT_START,
            payload={"message": message, "agent_id": resolved_agent_id},
            agent_id=resolved_agent_id
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
            agent_id=resolved_agent_id
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
                payload={
                    "response_length": len(response_content),
                    "final_text": response_content,
                    "agent_id": resolved_agent_id,
                },
                agent_id=resolved_agent_id
            )
            
            return response_content

        except Exception as e:
            # 发布ERROR事件
            await bus.publish(
                job_id=session_id,
                event_type=EventType.ERROR,
                payload={"error": str(e), "phase": "agent_execution"},
                agent_id=resolved_agent_id
            )
            raise

    @classmethod
    def get_for_session(cls, session_id: str, agent_id: str | None = None):
        """
        获取指定会话的Agent实例
        """
        instance = cls.get_instance()
        return instance._get_or_create_agent(session_id, agent_id)
    
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
