from __future__ import annotations
import json
import os
from typing import Optional, Any

import jsonschema
from jsonschema import ValidationError

from app.schemas.config import ConfigDTO, ConfigUpdateRequest

CONFIGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "configs")

_config_path_override: Optional[str] = None


def set_config_path(config_path: str) -> None:
    global _config_path_override
    _config_path_override = config_path


def get_config_path() -> Optional[str]:
    return _config_path_override


class ConfigService:
    _instance: Optional[ConfigService] = None

    def __init__(self):
        self._boxteam_config: Optional[dict] = None
        self._schema: Optional[dict] = None

    @classmethod
    def get_instance(cls) -> "ConfigService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def _load_schema(self) -> dict:
        if self._schema is None:
            schema_path = os.path.join(CONFIGS_DIR, "config.json")
            with open(schema_path, "r", encoding="utf-8") as f:
                self._schema = json.load(f)
        return self._schema

    def _get_boxteam_config_path(self) -> Optional[str]:
        if _config_path_override:
            return _config_path_override
        return os.path.join(CONFIGS_DIR, "boxteam.json")

    def _load_boxteam_config(self) -> dict:
        if self._boxteam_config is None:
            config_path = self._get_boxteam_config_path()
            if config_path and os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    self._boxteam_config = json.load(f)
            else:
                self._boxteam_config = {}
        return self._boxteam_config

    def _apply_workspace_override(self, workspace_root: str) -> None:
        if self._boxteam_config is None:
            return
        override_path = os.path.join(workspace_root, ".boxteam", "boxteam.json")
        if os.path.exists(override_path):
            with open(override_path, "r", encoding="utf-8") as f:
                override_config = json.load(f)
            self._boxteam_config = self._merge_config(self._boxteam_config, override_config)

    def _merge_config(self, base: dict, override: dict) -> dict:
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value
        return result

    def validate_boxteam_config(self) -> None:
        schema = self._load_schema()
        config = self._load_boxteam_config()
        jsonschema.validate(config, schema)

    def get_llm_providers(self) -> list[dict]:
        config = self._load_boxteam_config()
        providers = config.get("llm", {}).get("providers", [])
        
        # 展开每个 provider 的 api_key 环境变量引用
        result = []
        for provider in providers:
            expanded = provider.copy()
            api_key = provider.get("api_key", "")
            if api_key.startswith("${") and api_key.endswith("}"):
                var_name = api_key[2:-1]
                env_value = os.environ.get(var_name)
                if env_value is None:
                    raise ValueError(f'环境变量 {var_name} 未设置')
                expanded["api_key"] = env_value
            result.append(expanded)
        
        return result

    def get_default_agent_runtime_config(self) -> dict[str, Any]:
        """解析 default_agent 运行时配置。"""
        return self.get_agent_runtime_config(self.get_default_agent_id())

    def get_default_agent_id(self) -> str:
        config = self._load_boxteam_config()
        default_agent_id = config.get("default_agent")
        agents = config.get("agents", {})

        if default_agent_id and default_agent_id in agents:
            return default_agent_id

        # 兼容旧链路：历史上默认使用 deep_agent
        return "deep_agent"

    def _normalize_agent_id(self, agent_id: str | None) -> str:
        if not agent_id:
            return self.get_default_agent_id()

        # 兼容旧调用方：deep_agent 映射到 default_agent
        if agent_id == "deep_agent":
            return self.get_default_agent_id()

        return agent_id

    def resolve_agent_id(self, agent_id: str | None) -> str:
        return self._normalize_agent_id(agent_id)

    def validate_agent_id(self, agent_id: str | None) -> str:
        """校验 agent_id 是否可用，返回规范化后的 agent_id。"""
        resolved_agent_id = self._normalize_agent_id(agent_id)
        config = self._load_boxteam_config()
        agents = config.get("agents", {})

        # 兼容旧配置：未定义 agents 时仅保留 deep_agent 链路
        if not agents:
            if resolved_agent_id != "deep_agent":
                raise ValueError(f"agent {resolved_agent_id} 不存在")
            return resolved_agent_id

        if resolved_agent_id not in agents:
            raise ValueError(f"agent {resolved_agent_id} 不存在")

        return resolved_agent_id

    def get_agent_runtime_config(self, agent_id: str | None = None) -> dict[str, Any]:
        """按 agent_id 解析运行时配置。

        兼容旧配置：当不存在 agents 配置时，回退到旧的 provider 全量顺序与默认参数。
        """
        config = self._load_boxteam_config()
        providers = self.get_llm_providers()

        if not providers:
            raise ValueError("未配置任何 LLM provider")

        default_runtime = {
            "system_prompt": "You are a helpful assistant.",
            "providers": providers,
            "temperature": 0.2,
            "top_p": 1,
            "max_output_tokens": 4000,
        }

        agents = config.get("agents", {})

        resolved_agent_id = self._normalize_agent_id(agent_id)
        if not agents or resolved_agent_id not in agents:
            if resolved_agent_id != "deep_agent":
                raise ValueError(f"agent {resolved_agent_id} 不存在")
            return default_runtime

        target_agent = agents[resolved_agent_id]
        instructions = target_agent.get("instructions", {})
        model_cfg = target_agent.get("model", {})

        provider_map: dict[str, dict[str, Any]] = {}
        for index, provider in enumerate(providers):
            provider_id = provider.get("id")
            if not provider_id:
                provider_id = f"provider_{index}"
            provider_map[provider_id] = provider

        primary_provider = model_cfg.get("primary_provider")
        fallback_providers = model_cfg.get("fallback_providers", [])

        if not primary_provider:
            raise ValueError(f"agent {resolved_agent_id} 缺少 model.primary_provider 配置")

        provider_ids = [primary_provider, *fallback_providers]
        selected_providers = []
        for provider_id in provider_ids:
            provider = provider_map.get(provider_id)
            if provider is None:
                raise ValueError(
                    f"agent {resolved_agent_id} 引用了不存在的 provider: {provider_id}"
                )
            selected_providers.append(provider)

        return {
            "system_prompt": instructions.get("system_prompt", default_runtime["system_prompt"]),
            "providers": selected_providers,
            "temperature": model_cfg.get("temperature", default_runtime["temperature"]),
            "top_p": model_cfg.get("top_p", default_runtime["top_p"]),
            "max_output_tokens": model_cfg.get("max_output_tokens", default_runtime["max_output_tokens"]),
        }

    async def get(self) -> ConfigDTO:
        return ConfigDTO(
            default_model="gpt-4.1",
            default_orchestration="hierarchical",
            max_concurrent_agents=4,
            allow_shell_tools=False,
            ignored_paths=[".git", "node_modules", "__pycache__", ".venv"],
            auto_summarize=True,
            metadata={
                "version": "1.0.0",
                "environment": "development"
            }
        )

    async def update(self, update_request: ConfigUpdateRequest) -> ConfigDTO:
        current = await self.get()
        update_data = update_request.model_dump(exclude_unset=True)
        return ConfigDTO(**{**current.model_dump(), **update_data})
