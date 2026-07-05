from __future__ import annotations

import os
from typing import Optional, Any

import commentjson
import jsonschema

from app.schemas.public_v2.config import ConfigDTO, ConfigUpdateRequest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
CONFIGS_DIR = os.path.join(REPO_ROOT, "configs")

_config_path_override: Optional[str] = None


def set_config_path(config_path: str) -> None:
    global _config_path_override
    _config_path_override = config_path


def get_config_path() -> Optional[str]:
    return _config_path_override


class ConfigService:
    def __init__(self, workspace_root: Optional[str] = None):
        self._boxteam_config: Optional[dict] = None
        self._schema: Optional[dict] = None
        self._workspace_root = workspace_root

    def _load_schema(self) -> dict:
        if self._schema is None:
            schema_path = os.path.join(CONFIGS_DIR, "config.jsonc")
            if not os.path.exists(schema_path):
                schema_path = os.path.join(CONFIGS_DIR, "config.json")
            with open(schema_path, "r", encoding="utf-8") as f:
                self._schema = commentjson.load(f)
        return self._schema

    def _get_boxteam_config_path(self) -> Optional[str]:
        if _config_path_override:
            return _config_path_override
        jsonc_path = os.path.join(CONFIGS_DIR, "boxteam.jsonc")
        if os.path.exists(jsonc_path):
            return jsonc_path
        return os.path.join(CONFIGS_DIR, "boxteam.json")

    def _load_boxteam_config(self) -> dict:
        if self._boxteam_config is None:
            config_path = self._get_boxteam_config_path()
            if config_path and os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    self._boxteam_config = commentjson.load(f)
            else:
                self._boxteam_config = {}
        return self._boxteam_config

    def _apply_workspace_override(self, workspace_root: str) -> None:
        if self._boxteam_config is None:
            return
        override_dir = os.path.join(workspace_root, ".boxteam")
        override_path_jsonc = os.path.join(override_dir, "boxteam.jsonc")
        override_path_json = os.path.join(override_dir, "boxteam.json")
        override_path = None
        if os.path.exists(override_path_jsonc):
            override_path = override_path_jsonc
        elif os.path.exists(override_path_json):
            override_path = override_path_json

        if override_path:
            with open(override_path, "r", encoding="utf-8") as f:
                override_config = commentjson.load(f)
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
        return self.get_agent_runtime_config(self.get_default_agent_id())

    def get_default_agent_id(self) -> str:
        config = self._load_boxteam_config()
        default_agent_id = config.get("default_agent")
        agents = config.get("agents", {})

        if default_agent_id and default_agent_id in agents:
            return default_agent_id

        return "default"

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
        config = self._load_boxteam_config()
        agents = config.get("agents", {})

        if not agents:
            if resolved_agent_id != "default":
                raise ValueError(f"agent {resolved_agent_id} 不存在")
            return resolved_agent_id

        if resolved_agent_id not in agents:
            raise ValueError(f"agent {resolved_agent_id} 不存在")

        return resolved_agent_id

    def list_agents(self) -> dict[str, dict[str, Any]]:
        config = self._load_boxteam_config()
        agents = config.get("agents", {})
        if not isinstance(agents, dict):
            raise ValueError("agents 配置必须是对象")
        return agents

    def get_agent_runtime_config(self, agent_id: str | None = None) -> dict[str, Any]:
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
            if resolved_agent_id != "default":
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

    def get_agent_tool_config(self, agent_id: str | None = None) -> dict[str, Any]:
        config = self._load_boxteam_config()
        agents = config.get("agents", {})
        resolved_agent_id = self._normalize_agent_id(agent_id)

        default_tool_config = {
            "denylist": [],
            "confirmation_required": [],
        }

        if not agents or resolved_agent_id not in agents:
            if resolved_agent_id != "default":
                raise ValueError(f"agent {resolved_agent_id} 不存在")
            return default_tool_config

        tools_config = agents[resolved_agent_id].get("tools", {})
        if not isinstance(tools_config, dict):
            raise ValueError(f"agent {resolved_agent_id} 的 tools 配置必须是对象")

        return {
            "denylist": list(tools_config.get("denylist", default_tool_config["denylist"])),
            "confirmation_required": list(tools_config.get("confirmation_required", default_tool_config["confirmation_required"])),
        }
