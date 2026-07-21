from __future__ import annotations

from app.services.infrastructure.config_service import ConfigService
from app.schemas.public_v2.agent import AgentDTO


class AgentService:
    def __init__(self, *, config_service: ConfigService):
        self._config_service = config_service

    async def list(self) -> list[AgentDTO]:
        if self._config_service is None:
            raise RuntimeError("AgentService 未绑定 ConfigService")
        config_service = self._config_service
        agents_config = config_service.list_agents()

        if agents_config:
            agents: list[AgentDTO] = []
            for agent_id, info in agents_config.items():
                policy = config_service.resolve_agent_tool_policy(agent_id)
                agents.append(AgentDTO(
                    agent_id=agent_id,
                    name=info.get("name", agent_id),
                    description=info.get("description", ""),
                    model=info.get("model", {}).get("primary_provider", "unknown"),
                    tools=sorted(policy.enabled_names),
                    capabilities=list(info.get("tags", []))
                ))
            return agents

        raise RuntimeError(
            "Agent配置加载失败，没有找到有效的agent定义。\n"
            "请检查工作区配置文件 boxteam.json 是否存在并且包含正确的agents字段。\n"
            "这是一个故意的崩溃，遵循本地Agent设计原则：失败时快速崩溃，永远不要静默降级，永远不要返回假的默认值。"
        )

    async def get(self, agent_id: str) -> AgentDTO:
        agents = {a.agent_id: a for a in await self.list()}
        if agent_id not in agents:
            raise ValueError(f"Agent {agent_id} not found")
        return agents[agent_id]
