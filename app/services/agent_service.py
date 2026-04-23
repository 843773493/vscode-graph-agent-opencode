from __future__ import annotations

from typing import Optional
from app.schemas.agent import AgentDTO
from app.services.config_service import ConfigService


class AgentService:
    _instance: Optional["AgentService"] = None

    def __init__(self):
        pass

    @classmethod
    def get_instance(cls) -> "AgentService":
        if cls._instance is None:
            cls._instance = AgentService()
        return cls._instance

    async def list(self) -> list[AgentDTO]:
        config_service = ConfigService.get_instance()
        agents_config = config_service.list_agents()
        
        if agents_config:
            return [
                AgentDTO(
                    agent_id=agent_id,
                    name=info.get("name", agent_id),
                    description=info.get("description", ""),
                    model=info.get("model", {}).get("primary_provider", "unknown"),
                    tools=info.get("tools", {}).get("allowlist", []) or list(info.get("tools", {}).get("denylist", [])) or [],
                    capabilities=list(info.get("tags", []))
                )
                for agent_id, info in agents_config.items()
            ]
        
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
