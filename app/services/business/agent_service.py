from __future__ import annotations

from app.schemas.public_v2.agent import AgentDTO, AgentProviderDTO
from app.services.infrastructure.config_service import ConfigService


class AgentService:
    def __init__(self, *, config_service: ConfigService):
        self._config_service = config_service

    async def list(self) -> list[AgentDTO]:
        if self._config_service is None:
            raise RuntimeError("AgentService 未绑定 ConfigService")
        config_service = self._config_service
        agents_config = config_service.list_agents()

        if agents_config:
            workspace_default_agent_id = config_service.get_workspace_default_agent_id()
            agents: list[AgentDTO] = []
            for agent_id, info in agents_config.items():
                policy = config_service.resolve_agent_tool_policy(agent_id)
                runtime = config_service.get_agent_runtime_config(agent_id)
                workspace_default_provider_id = (
                    config_service.get_workspace_default_provider_id(agent_id)
                )
                providers = [
                    AgentProviderDTO(
                        provider_id=str(provider["id"]),
                        model=str(provider["model"]),
                        custom_llm_provider=str(provider["custom_llm_provider"]),
                        workspace_default=(
                            provider["id"] == workspace_default_provider_id
                        ),
                    )
                    for provider in runtime["providers"]
                ]
                agents.append(
                    AgentDTO(
                        agent_id=agent_id,
                        name=info.get("name", agent_id),
                        description=info.get("description", ""),
                        model=info.get("model", {}).get("primary_provider", "unknown"),
                        tools=sorted(policy.enabled_names),
                        capabilities=list(info.get("tags", [])),
                        providers=providers,
                        workspace_default=(agent_id == workspace_default_agent_id),
                    )
                )
            return agents

        raise RuntimeError(
            "Agent配置加载失败，没有找到有效的agent定义。\n"
            "请检查工作区配置文件 workspace.jsonc 是否存在并且包含正确的 agents 字段。\n"
            "这是一个故意的崩溃，遵循本地Agent设计原则：失败时快速崩溃，永远不要静默降级，永远不要返回假的默认值。"
        )

    async def set_workspace_default_agent(self, agent_id: str) -> list[AgentDTO]:
        self._config_service.set_workspace_default_agent(agent_id)
        return await self.list()

    async def set_workspace_default_provider(
        self,
        agent_id: str,
        provider_id: str,
    ) -> list[AgentDTO]:
        self._config_service.set_workspace_default_provider(agent_id, provider_id)
        return await self.list()

    async def get(self, agent_id: str) -> AgentDTO:
        agents = {a.agent_id: a for a in await self.list()}
        if agent_id not in agents:
            raise ValueError(f"Agent {agent_id} not found")
        return agents[agent_id]
