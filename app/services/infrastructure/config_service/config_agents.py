"""ConfigAgentsMixin：ConfigService 的 agent 身份解析、默认值与 agent-runtime 配置方法族（纯搬迁）。"""

from __future__ import annotations

from typing import Any


class ConfigAgentsMixin:
    def get_default_agent_id(self) -> str:
        config = self._get_effective_config()
        default_agent_id = config.get("default_agent")
        agents = config.get("agents", {})

        if default_agent_id and default_agent_id in agents:
            return default_agent_id

        return "default"

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
        if not agent_id:
            return self.get_default_agent_id()

        # TODO: 兼容历史别名 deep_agent，后续移除
        if agent_id == "deep_agent":
            return self.get_default_agent_id()

        return agent_id

    def resolve_agent_id(self, agent_id: str | None) -> str:
        return self._normalize_agent_id(agent_id)

    def validate_agent_id(self, agent_id: str | None) -> str:
        resolved_agent_id = self._normalize_agent_id(agent_id)
        config = self._get_effective_config()
        agents = config.get("agents", {})

        if not agents:
            if resolved_agent_id != "default":
                raise ValueError(f"agent {resolved_agent_id} 不存在")
            return resolved_agent_id

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
        providers = self.get_llm_providers()

        if not providers:
            raise ValueError("未配置任何 LLM provider")

        default_runtime = {
            "system_prompt": "You are a helpful assistant.",
            "providers": providers,
            "require_delegated_report": False,
        }

        agents = config.get("agents", {})

        resolved_agent_id = self._normalize_agent_id(agent_id)
        if not agents or resolved_agent_id not in agents:
            if resolved_agent_id != "default":
                raise ValueError(f"agent {resolved_agent_id} 不存在")
            return default_runtime

        target_agent = agents[resolved_agent_id]
        instructions = target_agent.get("instructions", {})
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
        for index, provider in enumerate(providers):
            provider_id = provider.get("id")
            if not provider_id:
                provider_id = f"provider_{index}"
            provider_map[provider_id] = provider

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
            selected_providers.append(provider)

        runtime_config: dict[str, Any] = {
            "system_prompt": instructions.get(
                "system_prompt", default_runtime["system_prompt"]
            ),
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
