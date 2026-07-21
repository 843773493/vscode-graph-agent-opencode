from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

import commentjson
import jsonschema

from app.agents.policy import (
    ResolvedToolPolicy,
    build_agent_tool_universe,
    custom_tool_spec_names,
    parse_custom_tool_specs,
    resolve_tool_policy,
    resolve_tool_selectors,
)
from app.core.path_utils import get_user_config_path
from app.schemas.public_v2.config import ConfigDTO, ConfigUpdateRequest
from app.services.infrastructure.config import (
    ConfigFileWatcher,
    ConfigReloadStatus,
    ConfigSnapshot,
    ConfigSnapshotStore,
    build_config_snapshot,
)

_config_path_override: Path | None = None


ConfigCandidateApplier = Callable[[ConfigSnapshot, ConfigSnapshot], Awaitable[None]]


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
        overlay_paths: tuple[str | Path, ...] | None = None,
    ) -> None:
        resolved_config_dir = (
            Path(config_dir).expanduser().resolve()
            if config_dir
            else get_user_config_path().parent
        )
        self._config_dir = resolved_config_dir
        self._config_path = Path(config_path).expanduser().resolve() if config_path else None
        self._schema: dict[str, Any] | None = None
        self._workspace_root = Path(workspace_root).expanduser().resolve() if workspace_root else None
        configured_overlays = (
            overlay_paths
            if overlay_paths is not None
            else tuple(
                path
                for path in os.environ.get(
                    "BOXTEAM_CONFIG_OVERLAY_PATHS",
                    "",
                ).split(os.pathsep)
                if path
            )
        )
        self._overlay_paths = tuple(
            Path(path).expanduser().resolve() for path in configured_overlays
        )
        self._runtime_config_overrides: dict[str, Any] = {}
        self._mcp_tool_names: frozenset[str] | None = None
        self._snapshot_store = ConfigSnapshotStore(
            candidate_builder=self._build_candidate_snapshot,
        )
        self._pinned_snapshot: ContextVar[ConfigSnapshot | None] = ContextVar(
            f"config_snapshot_{id(self)}",
            default=None,
        )
        self._watcher: ConfigFileWatcher | None = None

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

    def _read_effective_config(self) -> tuple[dict[str, Any], tuple[Path, ...]]:
        config_path = self._get_boxteam_config_path()
        source_paths: list[Path] = []
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as f:
                config = commentjson.load(f)
            source_paths.append(config_path)
        else:
            config = {}
        for overlay_path in self._overlay_paths:
            if not overlay_path.is_file():
                raise FileNotFoundError(f"配置 overlay 不存在: {overlay_path}")
            with overlay_path.open("r", encoding="utf-8") as stream:
                overlay_config = commentjson.load(stream)
            if not isinstance(overlay_config, dict):
                raise TypeError(f"配置 overlay 根节点必须是对象: {overlay_path}")
            config = self._merge_config(config, overlay_config)
            source_paths.append(overlay_path)
        if self._workspace_root is not None:
            config, workspace_path = self._apply_workspace_override(
                config,
                self._workspace_root,
            )
            if workspace_path is not None:
                source_paths.append(workspace_path)
        self._validate_agent_tool_policies(config)
        return config, tuple(source_paths)

    def _apply_workspace_override(
        self,
        base_config: dict[str, Any],
        workspace_root: Path,
    ) -> tuple[dict[str, Any], Path | None]:
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
            return self._merge_config(base_config, override_config), override_path
        return base_config, None

    def _merge_config(self, base: dict, override: dict) -> dict:
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value
        return result

    def _build_candidate_snapshot(
        self,
        *,
        validate_schema: bool = True,
    ) -> ConfigSnapshot:
        config, source_paths = self._read_effective_config()
        if validate_schema:
            jsonschema.validate(config, self._load_schema())
        return build_config_snapshot(
            config,
            source_paths=source_paths,
        )

    def validate_boxteam_config(self) -> None:
        self._snapshot_store.initialize()

    def _require_snapshot(self) -> ConfigSnapshot:
        pinned = self._pinned_snapshot.get()
        if pinned is not None:
            return pinned
        if not self._snapshot_store.has_snapshot():
            # 普通 getter 保持只解析所需字段的既有语义；应用启动和热重载
            # 都会显式走完整 Schema 校验后再提交候选。
            self._snapshot_store.initialize(
                self._build_candidate_snapshot(validate_schema=False),
            )
        return self._snapshot_store.current()

    @contextmanager
    def use_snapshot(self, snapshot: ConfigSnapshot) -> Iterator[None]:
        token = self._pinned_snapshot.set(snapshot)
        try:
            yield
        finally:
            self._pinned_snapshot.reset(token)

    def get_snapshot(self) -> ConfigSnapshot:
        return self._require_snapshot()

    def get_revision(self) -> str:
        return self._require_snapshot().revision

    def get_reload_status(self) -> ConfigReloadStatus:
        return self._snapshot_store.status()

    def config_from_snapshot(self, snapshot: ConfigSnapshot) -> dict[str, Any]:
        return snapshot.to_dict()

    def validate_candidate(
        self,
        snapshot: ConfigSnapshot,
        *,
        mcp_tool_names: frozenset[str],
    ) -> None:
        config = snapshot.to_dict()
        self._validate_agent_tool_policies(
            config,
            mcp_tool_names=mcp_tool_names,
        )

    async def reload(
        self,
        *,
        candidate_applier: ConfigCandidateApplier | None = None,
    ) -> bool:
        return await self._snapshot_store.reload(
            candidate_applier=candidate_applier,
        )

    async def start_watching(
        self,
        *,
        candidate_applier: ConfigCandidateApplier | None = None,
    ) -> None:
        if self._watcher is not None:
            raise RuntimeError("配置文件监听器不允许重复启动")
        self._require_snapshot()
        directories = {self._get_boxteam_config_path().parent}
        candidate_paths = {self._get_boxteam_config_path()}
        for overlay_path in self._overlay_paths:
            directories.add(overlay_path.parent)
            candidate_paths.add(overlay_path)
        if self._workspace_root is not None:
            workspace_config_dir = self._workspace_root / ".boxteam"
            directories.add(workspace_config_dir)
            candidate_paths.update(
                {
                    workspace_config_dir / "boxteam.jsonc",
                    workspace_config_dir / "boxteam.json",
                }
            )
        watcher = ConfigFileWatcher(
            directories=directories,
            candidate_paths=candidate_paths,
            on_change=lambda: self._reload_from_watcher(
                candidate_applier=candidate_applier,
            ),
        )
        await watcher.start()
        self._watcher = watcher

    async def stop_watching(self) -> None:
        watcher = self._watcher
        self._watcher = None
        if watcher is None:
            return
        await watcher.stop()

    async def _reload_from_watcher(
        self,
        *,
        candidate_applier: ConfigCandidateApplier | None,
    ) -> None:
        try:
            await self.reload(candidate_applier=candidate_applier)
        except Exception:
            # reload 已记录完整异常与失败状态；监听循环必须继续处理后续修复。
            return

    def _get_effective_config(self) -> dict[str, Any]:
        return self._require_snapshot().to_dict()

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
        config = self._get_effective_config()
        snapshot = self._require_snapshot()
        reload_status = self.get_reload_status()
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
                "revision": snapshot.revision,
                "source_paths": [str(path) for path in snapshot.source_paths],
                "reload": {
                    "healthy": reload_status.healthy,
                    "restart_required": reload_status.restart_required,
                    "reason": reload_status.reason,
                    "changed_sections": list(reload_status.changed_sections),
                    "last_success_at": reload_status.last_success_at.isoformat(),
                    "last_attempt_at": reload_status.last_attempt_at.isoformat(),
                    "last_error": reload_status.last_error,
                },
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
        config = self._get_effective_config()
        providers = config.get("llm", {}).get("providers", [])

        return [self._expand_provider(provider) for provider in providers]

    def get_llm_provider(self, provider_id: str) -> dict[str, Any]:
        config = self._get_effective_config()
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
        config = self._get_effective_config()
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

    def get_gateway_config(self) -> dict[str, Any]:
        config = self._get_effective_config()
        gateway = config.get("gateway", {})
        if gateway is None:
            return {}
        if not isinstance(gateway, dict):
            raise ValueError("gateway 配置必须是对象")
        return gateway

    def get_logger_level(self) -> str:
        config = self._get_effective_config()
        logger_config = config.get("logger", {})
        if not isinstance(logger_config, dict):
            raise ValueError("logger 配置必须是对象")
        level = logger_config.get("level", "info")
        if not isinstance(level, str):
            raise ValueError("logger.level 必须是字符串")
        normalized_level = level.strip().upper()
        if normalized_level not in {
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
        }:
            raise ValueError(
                "logger.level 仅支持 debug、info、warning、error、critical"
            )
        return normalized_level

    def development_test_tools_enabled(self) -> bool:
        config = self._get_effective_config()
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
        config = self._get_effective_config()
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
        config = self._get_effective_config()
        resolved_agent_id = self._normalize_agent_id(agent_id)
        return self._agent_tool_config_from_loaded(
            config,
            agent_id=resolved_agent_id,
        )

    def get_mcp_config(self) -> dict[str, Any]:
        config = self._get_effective_config()
        raw_mcp_config = config.get("mcp", {})
        if not isinstance(raw_mcp_config, dict):
            raise TypeError("mcp 配置必须是对象")
        return dict(raw_mcp_config)

    def set_mcp_tool_names(self, tool_names: frozenset[str]) -> None:
        """注册当前进程实际发现的 MCP 工具，并重新严格校验工具策略。"""

        previous_tool_names = self._mcp_tool_names
        self._mcp_tool_names = frozenset(tool_names)
        try:
            self._validate_agent_tool_policies(self._get_effective_config())
        except Exception:
            self._mcp_tool_names = previous_tool_names
            raise

    def resolve_agent_tool_policy(
        self,
        agent_id: str | None = None,
    ) -> ResolvedToolPolicy:
        """返回配置、目录展示和运行时共同使用的权威工具策略。"""

        config = self._get_effective_config()
        resolved_agent_id = self._normalize_agent_id(agent_id)
        tool_config = self._agent_tool_config_from_loaded(
            config,
            agent_id=resolved_agent_id,
        )
        custom_tool_names = custom_tool_spec_names(
            tool_config["custom"],
            context=f"agent {resolved_agent_id} 的 tools.custom",
        )
        extension_names = custom_tool_names | self._resolved_mcp_tool_names(tool_config)
        development = config.get("development") or {}
        universe = build_agent_tool_universe(
            extension_names=extension_names,
            include_test_tools=development.get("test_tools", False),
        )
        return resolve_tool_policy(
            universe_names=universe,
            extension_names=extension_names,
            allowlist=tool_config["allowlist"],
            denylist=tool_config["denylist"],
            context=f"agent {resolved_agent_id} 的工具策略",
        )

    def resolve_agent_confirmation_tool_names(
        self,
        agent_id: str | None = None,
    ) -> frozenset[str]:
        config = self._get_effective_config()
        resolved_agent_id = self._normalize_agent_id(agent_id)
        tool_config = self._agent_tool_config_from_loaded(
            config,
            agent_id=resolved_agent_id,
        )
        custom_tool_names = custom_tool_spec_names(
            tool_config["custom"],
            context=f"agent {resolved_agent_id} 的 tools.custom",
        )
        extension_names = custom_tool_names | self._resolved_mcp_tool_names(tool_config)
        development = config.get("development") or {}
        universe = build_agent_tool_universe(
            extension_names=extension_names,
            include_test_tools=development.get("test_tools", False),
        )
        return resolve_tool_selectors(
            selectors=tool_config["confirmation_required"],
            universe_names=universe,
            extension_names=extension_names,
            context=f"agent {resolved_agent_id} 的 tools.confirmation_required",
        )

    def _agent_tool_config_from_loaded(
        self,
        config: dict[str, Any],
        *,
        agent_id: str,
    ) -> dict[str, Any]:
        agents = config.get("agents", {})
        if not agents or agent_id not in agents:
            if agent_id != "default":
                raise ValueError(f"agent {agent_id} 不存在")
            return {
                "allowlist": [],
                "denylist": [],
                "confirmation_required": [],
                "custom": [],
            }
        if not isinstance(agents, dict):
            raise ValueError("agents 配置必须是对象")
        agent_config = agents[agent_id]
        if not isinstance(agent_config, dict):
            raise ValueError(f"agent {agent_id} 的配置必须是对象")
        return self._parse_agent_tool_config(
            agent_config.get("tools", {}),
            agent_id=agent_id,
        )

    def _validate_agent_tool_policies(
        self,
        config: dict[str, Any],
        *,
        mcp_tool_names: frozenset[str] | None = None,
    ) -> None:
        agents = config.get("agents", {})
        if agents is None:
            return
        if not isinstance(agents, dict):
            raise ValueError("agents 配置必须是对象")
        development = config.get("development", {})
        if development is None:
            development = {}
        if not isinstance(development, dict):
            raise ValueError("development 配置必须是对象")
        include_test_tools = development.get("test_tools", False)
        if not isinstance(include_test_tools, bool):
            raise ValueError("development.test_tools 必须是布尔值")

        for agent_id, agent_config in agents.items():
            if not isinstance(agent_id, str) or not agent_id:
                raise ValueError("agents 的键必须是非空字符串")
            if not isinstance(agent_config, dict):
                raise ValueError(f"agent {agent_id} 的配置必须是对象")
            tool_config = self._parse_agent_tool_config(
                agent_config.get("tools", {}),
                agent_id=agent_id,
            )
            raw_tools_config = agent_config.get("tools")
            if isinstance(raw_tools_config, dict) and "custom" in raw_tools_config:
                # ConfigService 返回的配置即为运行时权威配置，因此在校验入口
                # 写回共享解析器产生的 strip/类型归一化结果，避免 schema、
                # 工具目录和 factory 分别观察到不同的扩展工具声明。
                raw_tools_config["custom"] = tool_config["custom"]
            custom_tool_names = custom_tool_spec_names(
                tool_config["custom"],
                context=f"agent {agent_id} 的 tools.custom",
            )
            extension_names = custom_tool_names | self._resolved_mcp_tool_names(
                tool_config,
                mcp_tool_names=mcp_tool_names,
            )
            universe = build_agent_tool_universe(
                extension_names=extension_names,
                include_test_tools=include_test_tools,
            )
            resolve_tool_policy(
                universe_names=universe,
                extension_names=extension_names,
                allowlist=tool_config["allowlist"],
                denylist=tool_config["denylist"],
                context=f"agent {agent_id} 的工具策略",
            )
            resolve_tool_selectors(
                selectors=tool_config["confirmation_required"],
                universe_names=universe,
                extension_names=extension_names,
                context=f"agent {agent_id} 的 tools.confirmation_required",
            )

    def _resolved_mcp_tool_names(
        self,
        tool_config: dict[str, Any],
        *,
        mcp_tool_names: frozenset[str] | None = None,
    ) -> frozenset[str]:
        if mcp_tool_names is not None:
            return mcp_tool_names
        if self._mcp_tool_names is not None:
            return self._mcp_tool_names
        referenced_names = {
            name
            for field_name in ("allowlist", "denylist", "confirmation_required")
            for name in tool_config[field_name]
            if isinstance(name, str) and name.startswith("mcp__")
        }
        return frozenset(referenced_names)

    @staticmethod
    def _parse_agent_tool_config(
        raw_tools_config: object,
        *,
        agent_id: str,
    ) -> dict[str, Any]:
        if raw_tools_config is None:
            raw_tools_config = {}
        if not isinstance(raw_tools_config, dict):
            raise ValueError(f"agent {agent_id} 的 tools 配置必须是对象")

        result: dict[str, Any] = {}
        for field_name in ("allowlist", "denylist", "confirmation_required"):
            value = raw_tools_config.get(field_name, [])
            if not isinstance(value, list):
                raise ValueError(
                    f"agent {agent_id} 的 tools.{field_name} 必须是数组"
                )
            result[field_name] = list(value)
        raw_custom = raw_tools_config.get("custom", [])
        if not isinstance(raw_custom, list):
            raise ValueError(f"agent {agent_id} 的 tools.custom 必须是数组")
        result["custom"] = [
            spec.to_config()
            for spec in parse_custom_tool_specs(
                raw_custom,
                context=f"agent {agent_id} 的 tools.custom",
            )
        ]
        return result
