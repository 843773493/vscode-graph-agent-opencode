from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import commentjson
import jsonschema

from app.core.path_utils import get_user_config_path
from app.schemas.public_v2.config import ConfigDTO, ConfigUpdateRequest

_config_path_override: Path | None = None


def set_config_path(config_path: str | Path | None) -> None:
    global _config_path_override
    _config_path_override = Path(config_path).expanduser().resolve() if config_path else None


def get_config_path() -> str | None:
    return str(_config_path_override) if _config_path_override is not None else None


class ConfigService:
    def __init__(
        self,
        *,
        config_dir: str | Path | None = None,
        config_path: str | Path | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        resolved_config_dir = (
            Path(config_dir).expanduser().resolve()
            if config_dir
            else get_user_config_path().parent
        )
        self._config_dir = resolved_config_dir
        self._config_path = Path(config_path).expanduser().resolve() if config_path else None
        self._boxteam_config: dict[str, Any] | None = None
        self._schema: dict[str, Any] | None = None
        self._workspace_root = Path(workspace_root).expanduser().resolve() if workspace_root else None
        self._runtime_config_overrides: dict[str, Any] = {}

    def _first_existing_file(self, *paths: Path) -> Path:
        for path in paths:
            if path.exists():
                return path
        checked_paths = "\n".join(str(path) for path in paths)
        raise FileNotFoundError(f"未找到配置文件，已检查:\n{checked_paths}")

    def _load_schema(self) -> dict:
        if self._schema is None:
            config_path = self._get_boxteam_config_path()
            config_schema_path: Path | None = None
            if config_path.is_file():
                with config_path.open("r", encoding="utf-8") as config_stream:
                    schema_reference = commentjson.load(config_stream).get("$schema")
                if (
                    isinstance(schema_reference, str)
                    and schema_reference
                    and "://" not in schema_reference
                ):
                    referenced_path = (config_path.parent / schema_reference).resolve()
                    if referenced_path.is_file():
                        config_schema_path = referenced_path
            schema_path = self._first_existing_file(
                config_path.parent / "config.schema.jsonc",
                *([config_schema_path] if config_schema_path is not None else []),
                self._config_dir / "config.schema.jsonc",
                self._config_dir / "config.jsonc",
                self._config_dir / "config.json",
            )
            with schema_path.open("r", encoding="utf-8") as f:
                self._schema = commentjson.load(f)
        return self._schema

    def _get_boxteam_config_path(self) -> Path:
        if self._config_path is not None:
            return self._config_path
        if _config_path_override is not None:
            return _config_path_override
        return get_user_config_path()

    def _load_boxteam_config(self) -> dict:
        config_path = self._get_boxteam_config_path()
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as f:
                config = commentjson.load(f)
        else:
            config = {}
        if self._workspace_root is not None:
            config = self._apply_workspace_override(config, self._workspace_root)
        self._boxteam_config = config
        return config

    def _apply_workspace_override(self, base_config: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
        override_dir = workspace_root / ".boxteam"
        override_path_jsonc = override_dir / "boxteam.jsonc"
        override_path_json = override_dir / "boxteam.json"
        override_path: Path | None = None
        if override_path_jsonc.exists():
            override_path = override_path_jsonc
        elif override_path_json.exists():
            override_path = override_path_json

        if override_path:
            with override_path.open("r", encoding="utf-8") as f:
                override_config = commentjson.load(f)
            return self._merge_config(base_config, override_config)
        return base_config

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

    async def get(self) -> ConfigDTO:
        return self._build_public_config()

    async def update(self, payload: ConfigUpdateRequest) -> ConfigDTO:
        update_data = payload.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            if value is None:
                self._runtime_config_overrides.pop(key, None)
            else:
                self._runtime_config_overrides[key] = value
        return self._build_public_config()

    def _build_public_config(self) -> ConfigDTO:
        config = self._load_boxteam_config()
        public_config = self._load_public_runtime_config(config)

        default_model = public_config.get("default_model")
        if default_model is None:
            default_model = self._resolve_default_model(config)

        default_orchestration = public_config.get("default_orchestration", "single_agent")
        max_concurrent_agents = public_config.get("max_concurrent_agents", 4)
        allow_shell_tools = public_config.get("allow_shell_tools", False)
        ignored_paths = public_config.get("ignored_paths", [])
        auto_summarize = public_config.get("auto_summarize", True)

        return ConfigDTO(
            default_model=default_model,
            default_orchestration=default_orchestration,
            max_concurrent_agents=max_concurrent_agents,
            allow_shell_tools=allow_shell_tools,
            ignored_paths=ignored_paths,
            auto_summarize=auto_summarize,
            metadata={
                "default_agent_id": self.get_default_agent_id(),
                "config_path": str(self._get_boxteam_config_path()),
                "source": "boxteam",
                "runtime_overrides": sorted(self._runtime_config_overrides.keys()),
            },
        )

    def _load_public_runtime_config(self, config: dict[str, Any]) -> dict[str, Any]:
        raw_public_config = config.get("ui", {})
        if raw_public_config is None:
            raw_public_config = {}
        if not isinstance(raw_public_config, dict):
            raise ValueError("ui 配置必须是对象")
        return {**raw_public_config, **self._runtime_config_overrides}

    def _resolve_default_model(self, config: dict[str, Any]) -> str:
        default_agent_id = self.get_default_agent_id()
        agents = config.get("agents", {})
        if not isinstance(agents, dict):
            raise ValueError("agents 配置必须是对象")

        agent_config = agents.get(default_agent_id)
        if not isinstance(agent_config, dict):
            return self._resolve_first_provider_model(config)

        model_config = agent_config.get("model", {})
        if not isinstance(model_config, dict):
            raise ValueError(f"agent {default_agent_id} 的 model 配置必须是对象")

        primary_provider_id = model_config.get("primary_provider")
        if not isinstance(primary_provider_id, str) or not primary_provider_id:
            raise ValueError(f"agent {default_agent_id} 缺少 model.primary_provider 配置")

        return self._resolve_provider_model(config, primary_provider_id)

    def _resolve_first_provider_model(self, config: dict[str, Any]) -> str:
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

    def _resolve_provider_model(self, config: dict[str, Any], provider_id: str) -> str:
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
        config = self._load_boxteam_config()
        providers = config.get("llm", {}).get("providers", [])

        return [self._expand_provider(provider) for provider in providers]

    def get_llm_provider(self, provider_id: str) -> dict[str, Any]:
        config = self._load_boxteam_config()
        providers = config.get("llm", {}).get("providers", [])
        for provider in providers:
            if not isinstance(provider, dict):
                raise TypeError("llm.providers 配置项必须是对象")
            if provider.get("id") == provider_id:
                return self._expand_provider(provider)
        raise ValueError(f"不存在的 LLM provider: {provider_id}")

    def _expand_provider(self, provider: object) -> dict[str, Any]:
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

    def get_gateway_config(self) -> dict[str, Any]:
        config = self._load_boxteam_config()
        gateway = config.get("gateway", {})
        if gateway is None:
            return {}
        if not isinstance(gateway, dict):
            raise ValueError("gateway 配置必须是对象")
        return gateway

    def development_test_tools_enabled(self) -> bool:
        config = self._load_boxteam_config()
        development = config.get("development", {})
        if development is None:
            return False
        if not isinstance(development, dict):
            raise ValueError("development 配置必须是对象")
        enabled = development.get("test_tools", False)
        if not isinstance(enabled, bool):
            raise ValueError("development.test_tools 必须是布尔值")
        return enabled

    def get_agent_runtime_config(self, agent_id: str | None = None) -> dict[str, Any]:
        config = self._load_boxteam_config()
        providers = self.get_llm_providers()

        if not providers:
            raise ValueError("未配置任何 LLM provider")

        default_runtime = {
            "system_prompt": "You are a helpful assistant.",
            "providers": providers,
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

        runtime_config: dict[str, Any] = {
            "system_prompt": instructions.get("system_prompt", default_runtime["system_prompt"]),
            "providers": selected_providers,
        }
        for option_name in ("temperature", "top_p", "max_output_tokens"):
            if option_name in model_cfg:
                runtime_config[option_name] = model_cfg[option_name]
        return runtime_config

    def get_agent_tool_config(self, agent_id: str | None = None) -> dict[str, Any]:
        config = self._load_boxteam_config()
        agents = config.get("agents", {})
        resolved_agent_id = self._normalize_agent_id(agent_id)

        default_tool_config = {
            "denylist": [],
            "confirmation_required": [],
            "custom": [],
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
            "custom": list(tools_config.get("custom", default_tool_config["custom"])),
        }
