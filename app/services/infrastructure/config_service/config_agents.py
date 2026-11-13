"""ConfigAgentsMixin：ConfigService 的 agent 身份解析、默认值与 agent-runtime 配置方法族（纯搬迁）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from app.services.infrastructure.config_service import ConfigService


class ConfigAgentsMixin:
    def _validate_agent_definition_references(self, config: dict[str, object]) -> None:
        """只核完整候选及本工作区会话默认值，不展开凭据或查询历史。"""
        service = cast("ConfigService", self)
        llm = cast(dict[str, object], config["llm"])
        providers = cast(list[dict[str, object]], llm["providers"])
        provider_ids = [provider["id"] for provider in providers]
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError("llm.providers 的 Provider ID 必须唯一")
        agents = cast(dict[str, dict[str, object]], config["agents"])
        if config["default_agent"] not in agents:
            raise ValueError("default_agent 引用了不存在的 Agent")
        defaults = service._session_defaults.read_workspace_session_defaults()
        default_agent = defaults.get("default_agent_id")
        if default_agent is not None and default_agent not in agents:
            raise ValueError("工作区默认 Agent 引用了不存在的 Agent: field=default_agent_id")
        provider_defaults = defaults.get("provider_by_agent", {})
        if not isinstance(provider_defaults, dict):
            raise TypeError("工作区默认 provider 映射必须是对象: artifact=session_defaults")
        allowed: dict[str, list[str]] = {}
        for agent_id, agent in agents.items():
            model = cast(dict[str, object], agent["model"])
            references = [cast(str, model["primary_provider"]), *cast(list[str], model.get("fallback_providers", []))]
            for provider_id in references:
                if provider_id not in provider_ids:
                    raise ValueError(f"Agent {agent_id} 引用了不存在的 Provider: {provider_id}")
            allowed[agent_id] = references
        for agent_id, provider_id in provider_defaults.items():
            if agent_id not in allowed or provider_id not in allowed[agent_id]:
                raise ValueError(f"工作区默认 Provider 不再属于 Agent {agent_id}: field=provider_by_agent")

    def get_default_agent_id(self) -> str:
        config = self._get_effective_config()
        default_agent_id = config["default_agent"]
        if default_agent_id not in config["agents"]:
            raise ValueError(f"default_agent 引用了不存在的 Agent: {default_agent_id}")
        return default_agent_id

    def get_workspace_default_agent_id(self) -> str:
        return self._session_defaults.get_workspace_default_agent_id()

    def get_workspace_default_provider_id(self, agent_id: str) -> str:
        return self._session_defaults.get_workspace_default_provider_id(agent_id)

    def set_workspace_default_agent(self, agent_id: str) -> None:
        self._session_defaults.set_workspace_default_agent(agent_id)

    def set_workspace_default_provider(
        self,
        agent_id: str,
        provider_id: str,
    ) -> None:
        self._session_defaults.set_workspace_default_provider(agent_id, provider_id)

    def resolve_new_session_agent_id(self, agent_id: str | None) -> str:
        return self._session_defaults.resolve_new_session_agent_id(agent_id)

    def resolve_new_session_provider_id(self, agent_id: str) -> str:
        return self._session_defaults.resolve_new_session_provider_id(agent_id)

    def _normalize_agent_id(self, agent_id: str | None) -> str:
        return self.get_default_agent_id() if agent_id is None else agent_id

    def resolve_agent_id(self, agent_id: str | None) -> str:
        return self._normalize_agent_id(agent_id)

    def validate_agent_id(self, agent_id: str | None) -> str:
        resolved_agent_id = self._normalize_agent_id(agent_id)
        config = self._get_effective_config()
        agents = config.get("agents", {})

        if resolved_agent_id not in agents:
            raise ValueError(f"agent {resolved_agent_id} 不存在")

        return resolved_agent_id

    def list_agents(self) -> dict[str, dict[str, Any]]:
        config = self._get_effective_config()
        agents = config.get("agents", {})
        if not isinstance(agents, dict):
            raise ValueError("agents 配置必须是对象")
        return agents

    def get_agent_runtime_config(
        self,
        agent_id: str | None = None,
        preferred_provider_id: str | None = None,
    ) -> dict[str, Any]:
        config = self._get_effective_config()
        agents = config["agents"]
        resolved_agent_id = config["default_agent"] if agent_id is None else agent_id
        if resolved_agent_id not in agents:
            raise ValueError(f"agent {resolved_agent_id} 不存在")

        target_agent = agents[resolved_agent_id]
        instructions = target_agent["instructions"]
        model_cfg = target_agent.get("model", {})
        execution_cfg = target_agent.get("execution", {})
        if not isinstance(execution_cfg, dict):
            raise TypeError(f"agent {resolved_agent_id} 的 execution 配置必须是对象")
        require_delegated_report = execution_cfg.get(
            "require_delegated_report",
            False,
        )
        if not isinstance(require_delegated_report, bool):
            raise TypeError(
                f"agent {resolved_agent_id} 的 "
                "execution.require_delegated_report 必须是布尔值"
            )

        provider_map: dict[str, dict[str, Any]] = {}
        for provider in config["llm"]["providers"]:
            provider_map[provider["id"]] = provider

        primary_provider = model_cfg.get("primary_provider")
        fallback_providers = model_cfg.get("fallback_providers", [])

        if not primary_provider:
            raise ValueError(
                f"agent {resolved_agent_id} 缺少 model.primary_provider 配置"
            )

        provider_ids = [primary_provider, *fallback_providers]
        if preferred_provider_id is not None:
            if preferred_provider_id not in provider_ids:
                raise ValueError(
                    f"agent {resolved_agent_id} 不允许使用 provider: "
                    f"{preferred_provider_id}"
                )
            provider_ids = [
                preferred_provider_id,
                *(item for item in provider_ids if item != preferred_provider_id),
            ]
        selected_providers = []
        for provider_id in provider_ids:
            provider = provider_map.get(provider_id)
            if provider is None:
                raise ValueError(
                    f"agent {resolved_agent_id} 引用了不存在的 provider: {provider_id}"
                )
            # 只展开此 Agent 的运行凭据；不相关定义缺凭据不能掩盖目标身份错误。
            selected_providers.append(self._llm_resolution.expand_provider(provider))

        runtime_config: dict[str, Any] = {
            "system_prompt": instructions["system_prompt"],
            # 未配置说明时只有软件身份事实；不在 Python 另设提示词正文兜底。
            "runtime_identity_description": instructions.get("runtime_identity", {}).get("description", ""),
            "providers": selected_providers,
            "require_delegated_report": require_delegated_report,
        }
        for option_name in ("temperature", "top_p", "max_output_tokens"):
            if option_name in model_cfg:
                runtime_config[option_name] = model_cfg[option_name]
        return runtime_config

    def resolve_agent_provider_id(
        self,
        agent_id: str | None,
        provider_id: str | None = None,
    ) -> str:
        runtime = self.get_agent_runtime_config(
            agent_id,
            preferred_provider_id=provider_id,
        )
        providers = runtime["providers"]
        if not providers:
            raise ValueError(
                f"agent {self._normalize_agent_id(agent_id)} 没有可用 provider"
            )
        resolved = providers[0].get("id")
        if not isinstance(resolved, str) or not resolved:
            raise ValueError("LLM provider 缺少非空 id")
        return resolved


__all__ = ["ConfigAgentsMixin"]
