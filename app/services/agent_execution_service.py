from __future__ import annotations
from typing import Optional, Dict, Any, List

from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import EventType, JobEventBus
from app.agents.agent_factory import create_runtime_deep_agent_for_session, resolve_agent_id
from app.services.config_service import ConfigService
from app.services.job_service import JobService
from app.services.message_service import MessageService
from app.services.session_service import SessionService


class AgentExecutionService:
    def __init__(self):
        self._agent_cache = {}
        self._config_service: ConfigService | None = None
        self._bus: JobEventBus | None = None
        self._background_task_registry: BackgroundTaskRegistry | None = None
        self._background_message_bus: BackgroundMessageBus | None = None
        self._job_service: JobService | None = None
        self._message_service: MessageService | None = None
        self._session_service: SessionService | None = None

    def bind_config_service(self, config_service: ConfigService) -> None:
        self._config_service = config_service

    def bind_bus(self, bus: JobEventBus) -> None:
        self._bus = bus

    def bind_dependencies(
        self,
        *,
        background_task_registry: BackgroundTaskRegistry,
        background_message_bus: BackgroundMessageBus,
        job_service: JobService,
        message_service: MessageService,
        session_service: SessionService,
    ) -> None:
        self._background_task_registry = background_task_registry
        self._background_message_bus = background_message_bus
        self._job_service = job_service
        self._message_service = message_service
        self._session_service = session_service
    
    def _get_or_create_agent(self, session_id: str, agent_id: str | None = None):
        if self._config_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 ConfigService")
        resolved_agent_id = resolve_agent_id(agent_id, self._config_service)
        cache_key = f"{session_id}::{resolved_agent_id}"
        if cache_key in self._agent_cache:
            return self._agent_cache[cache_key]

        agent = create_runtime_deep_agent_for_session(
            session_id=session_id,
            agent_id=resolved_agent_id,
            config_service=self._config_service,
            background_task_registry=self._background_task_registry,
            background_message_bus=self._background_message_bus,
            job_event_bus=self._bus,
            job_service=self._job_service,
            message_service=self._message_service,
            session_service=self._session_service,
            name=resolved_agent_id,
        )
        
        self._agent_cache[cache_key] = agent
        return agent
    
    async def run_step(self, session_id: str, message: str, agent_id: str | None = None, job_id: str | None = None) -> str:
        """
        执行单步Agent调用
        
        Args:
            session_id: 会话ID
            message: 用户输入消息
            agent_id: Agent ID
            job_id: 任务ID（用于事件发布），如果为None则使用session_id（向后兼容）
            
        Returns:
            Agent响应内容
        """
        if self._config_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 ConfigService")
        if self._background_task_registry is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundTaskRegistry")
        if self._background_message_bus is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundMessageBus")
        if self._job_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 JobService")
        if self._message_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 MessageService")
        if self._session_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 SessionService")
        resolved_agent_id = resolve_agent_id(agent_id, self._config_service)
        agent = self._get_or_create_agent(session_id, resolved_agent_id)
        if self._bus is None:
            raise RuntimeError("AgentExecutionService 未绑定 JobEventBus")
        bus = self._bus
        
        # 确定用于事件发布的job_id
        if job_id is None:
            raise ValueError(f"job_id is required for run_step. session_id={session_id}, agent_id={agent_id}. "
                           f"Do not fallback to session_id. Always pass a valid job_id. "
                           f"This is a deliberate design choice following local agent principles: "
                           f"fail fast, never hide errors, never return fake defaults.")
        effective_job_id = job_id
        
        # 发布AGENT_START事件
        await bus.publish(
            job_id=effective_job_id,
            event_type=EventType.AGENT_START,
            payload={"message": message, "agent_id": resolved_agent_id},
            agent_id=resolved_agent_id
        )
        
        config = {
            "configurable": {
                "thread_id": session_id,
                "job_id": effective_job_id,  # 将job_id注入到LangChain配置中，供middleware使用
            }
        }
        
        # 发布AGENT_STEP事件
        await bus.publish(
            job_id=effective_job_id,
            event_type=EventType.AGENT_STEP,
            payload={"phase": "invoking_agent"},
            agent_id=resolved_agent_id
        )
        
        try:
            import logging
            logger = logging.getLogger(__name__)
            logger.debug(f"About to invoke agent: session_id={session_id}, message={message[:50]}...")
            
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": message}]},
                config=config
            )
            
            logger.debug(f"Agent invoke completed, result messages count: {len(result.get('messages', []))}")
            
            response_content = result["messages"][-1].content
            
            # 发布AGENT_END事件
            await bus.publish(
                job_id=effective_job_id,
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
                job_id=effective_job_id,
                event_type=EventType.ERROR,
                payload={"error": str(e), "phase": "agent_execution"},
                agent_id=resolved_agent_id
            )
            raise

    def get_for_session(self, session_id: str, agent_id: str | None = None):
        """
        获取指定会话的Agent实例
        """
        return self._get_or_create_agent(session_id, agent_id)
    
    def get_available_tools(self) -> List[Dict[str, Any]]:
        """
        获取DeepAgent支持的所有可用工具列表
        
        本地运行原则：失败时快速崩溃，不静默降级，不隐藏问题
        """
        # 从agent实例动态获取真实工具列表
        session_id = "tools_inspection_session"
        agent = self._get_or_create_agent(session_id)
        
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
