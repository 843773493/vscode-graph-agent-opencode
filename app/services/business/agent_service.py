from __future__ import annotations

import asyncio

from app.runtime.provider_model import provider_configuration_error
from app.schemas.internal_v2.agent import AgentDTO, AgentModelDTO
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.infrastructure.config_service import ConfigService


class AgentService:
    def __init__(self, *, config_service: ConfigService):
        self._config_service = config_service

    async def list(self) -> list[AgentDTO]:
        # 配置展开、工具策略和凭据读取均是同步工作，整次读取只切换一次线程。
        return await asyncio.to_thread(self._list)

    def _list(self) -> list[AgentDTO]:
        if self._config_service is None:
            raise RuntimeError("AgentService 未绑定 ConfigService")
        config_service = self._config_service
        with config_service.use_snapshot(config_service.get_snapshot()):
            agents_config = config_service.list_agents()
            providers = config_service.get_snapshot().to_dict()["llm"]["providers"]

            if agents_config:
                workspace_default_agent_id = config_service.get_workspace_default_agent_id()
                agent_dtos: list[AgentDTO] = []
                for agent_id, agent_info in agents_config.items():
                    policy = config_service.resolve_agent_tool_policy(agent_id)
                    agent_runtime_config = config_service.get_agent_runtime_config(agent_id)
                    workspace_default_model = (
                        config_service.get_workspace_default_model(agent_id)
                    )
                    model_dtos: list[AgentModelDTO] = []
                    for provider_config in agent_runtime_config["models"]:
                        definition = providers[provider_config["provider_id"]]
                        # 列表只检查本地配置与必要凭据；SDK 构造由首次执行校验。
                        # OAuth 凭据可独立更新，逐次在线程中检查，不按配置 revision 缓存。
                        configuration_error = provider_configuration_error(provider_config)
                        model_dtos.append(
                            AgentModelDTO(
                                model_ref=ModelReferenceDTO(provider_id=provider_config["provider_id"], model_id=provider_config["model_id"]),
                                model=str(provider_config["model"]),
                                name=definition["models"][provider_config["model_id"]].get("name"),
                                provider_name=definition.get("name"),
                                custom_llm_provider=str(
                                    provider_config["custom_llm_provider"]
                                ),
                                workspace_default=(
                                    ModelReferenceDTO(provider_id=provider_config["provider_id"], model_id=provider_config["model_id"]) == workspace_default_model
                                ),
                                available=configuration_error is None,
                                configuration_error=configuration_error,
                            )
                        )
                    agent_dtos.append(
                        AgentDTO(
                            agent_id=agent_id,
                            name=agent_info.get("name", agent_id),
                            description=agent_info.get("description", ""),
                            model=agent_runtime_config["models"][0]["model"],
                            tools=sorted(policy.enabled_names),
                            capabilities=list(agent_info.get("tags", [])),
                            models=model_dtos,
                            workspace_default=(agent_id == workspace_default_agent_id),
                        )
                    )
                return agent_dtos

        raise RuntimeError(
            "Agent配置加载失败，没有找到有效的agent定义。\n"
            "请检查工作区配置文件 workspace.jsonc 是否存在并且包含正确的 agents 字段。\n"
            "这是一个故意的崩溃，遵循本地Agent设计原则：失败时快速崩溃，永远不要静默降级，永远不要返回假的默认值。"
        )

    async def set_workspace_default_agent(self, agent_id: str) -> list[AgentDTO]:
        self._config_service.set_workspace_default_agent(agent_id)
        return await self.list()

    async def set_workspace_default_model(
        self,
        agent_id: str,
        model: ModelReferenceDTO,
    ) -> list[AgentDTO]:
        self._config_service.set_workspace_default_model(agent_id, model)
        return await self.list()

    async def get(self, agent_id: str) -> AgentDTO:
        agent_dtos_by_id = {agent.agent_id: agent for agent in await self.list()}
        if agent_id not in agent_dtos_by_id:
            raise ValueError(f"Agent {agent_id} not found")
        return agent_dtos_by_id[agent_id]
