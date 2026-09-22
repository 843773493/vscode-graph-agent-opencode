from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any


class LlmResolution:
    """从生效配置解析 LLM provider 与默认模型。"""

    def __init__(
        self,
        *,
        effective_config_provider: Callable[[], dict[str, Any]],
        default_agent_id_provider: Callable[[], str],
    ) -> None:
        self._effective_config_provider = effective_config_provider
        self._default_agent_id_provider = default_agent_id_provider

    def resolve_default_model(self, config: dict[str, Any]) -> str:
        default_agent_id = self._default_agent_id_provider()
        agents = config.get("agents", {})
        if not isinstance(agents, dict):
            raise ValueError("agents 配置必须是对象")

        agent_config = agents.get(default_agent_id)
        if not isinstance(agent_config, dict):
            return self.resolve_first_provider_model(config)

        model_config = agent_config.get("model", {})
        if not isinstance(model_config, dict):
            raise ValueError(f"agent {default_agent_id} 的 model 配置必须是对象")

        primary_provider_id = model_config.get("primary_provider")
        if not isinstance(primary_provider_id, str) or not primary_provider_id:
            raise ValueError(
                f"agent {default_agent_id} 缺少 model.primary_provider 配置"
            )

        return self.resolve_provider_model(config, primary_provider_id)

    def resolve_first_provider_model(self, config: dict[str, Any]) -> str:
        providers = config.get("llm", {}).get("providers", [])
        if not providers:
            raise ValueError("未配置任何 LLM provider")
        first_provider = providers[0]
        if not isinstance(first_provider, dict):
            raise ValueError("llm.providers 配置项必须是对象")
        model = first_provider.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("llm.providers[0].model 必须是非空字符串")
        return model

    def resolve_provider_model(self, config: dict[str, Any], provider_id: str) -> str:
        providers = config.get("llm", {}).get("providers", [])
        if not isinstance(providers, list):
            raise ValueError("llm.providers 配置必须是数组")

        for provider in providers:
            if not isinstance(provider, dict):
                raise ValueError("llm.providers 配置项必须是对象")
            if provider.get("id") != provider_id:
                continue
            model = provider.get("model")
            if not isinstance(model, str) or not model:
                raise ValueError(f"provider {provider_id} 缺少有效 model 配置")
            return model

        raise ValueError(f"default agent 引用了不存在的 provider: {provider_id}")

    def get_llm_providers(self) -> list[dict]:
        config = self._effective_config_provider()
        providers = config.get("llm", {}).get("providers", [])

        return [self.expand_provider(provider) for provider in providers]

    def get_llm_provider(self, provider_id: str) -> dict[str, Any]:
        config = self._effective_config_provider()
        providers = config.get("llm", {}).get("providers", [])
        for provider in providers:
            if not isinstance(provider, dict):
                raise TypeError("llm.providers 配置项必须是对象")
            if provider.get("id") == provider_id:
                return self.expand_provider(provider)
        raise ValueError(f"不存在的 LLM provider: {provider_id}")

    def expand_provider(self, provider: object) -> dict[str, Any]:
        if not isinstance(provider, dict):
            raise TypeError("llm.providers 配置项必须是对象")
        expanded = provider.copy()
        api_key = provider.get("api_key", "")
        if not isinstance(api_key, str):
            raise TypeError("llm.providers[].api_key 必须是字符串")
        if api_key.startswith("${") and api_key.endswith("}"):
            var_name = api_key[2:-1]
            env_value = os.environ.get(var_name)
            if env_value is None:
                raise ValueError(f"环境变量 {var_name} 未设置")
            expanded["api_key"] = env_value
        return expanded
