"""ConfigAgentToolsMixin：ConfigService 的 agent 工具策略与 MCP 配置解析方法族（纯搬迁）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.custom_tools import load_custom_tool_factory
from app.agents.policy import (
    ResolvedToolPolicy,
    ToolPolicyResolver,
    build_agent_tool_universe,
    custom_tool_spec_names,
    parse_custom_tool_specs,
    resolve_tool_policy,
    resolve_tool_selectors,
)
from configs.runtime import merge_json_objects


class ConfigAgentToolsMixin:
    def get_agent_tool_config(self, agent_id: str | None = None) -> dict[str, Any]:
        config = self._get_effective_config()
        resolved_agent_id = self._normalize_agent_id(agent_id)
        return self._agent_tool_config_from_loaded(
            config,
            agent_id=resolved_agent_id,
        )

    def get_tool_policy_resolver(
        self,
        agent_id: str | None = None,
    ) -> ToolPolicyResolver:
        """返回当前 Workspace 和 Agent 合并后的唯一工具策略解析器。"""

        config = self._get_effective_config()
        resolved_agent_id = self._normalize_agent_id(agent_id)
        tooling = config.get("tooling", {})
        if tooling is None:
            tooling = {}
        if not isinstance(tooling, dict):
            raise TypeError("tooling 配置必须是对象")

        agents = config.get("agents", {})
        if not isinstance(agents, dict):
            raise TypeError("agents 配置必须是对象")
        agent_config = agents.get(resolved_agent_id, {})
        if not isinstance(agent_config, dict):
            raise TypeError(f"agent {resolved_agent_id} 配置必须是对象")
        agent_tools = agent_config.get("tools", {})
        if agent_tools is None:
            agent_tools = {}
        if not isinstance(agent_tools, dict):
            raise TypeError(f"agent {resolved_agent_id} 的 tools 配置必须是对象")
        agent_policy = agent_tools.get("policy", {})
        if agent_policy is None:
            agent_policy = {}
        if not isinstance(agent_policy, dict):
            raise TypeError(f"agent {resolved_agent_id} 的 tools.policy 必须是对象")

        defaults = tooling.get("policy_defaults", {})
        if not isinstance(defaults, dict):
            raise TypeError("tooling.policy_defaults 必须是对象")
        global_rules = tooling.get("policy_rules", {})
        if not isinstance(global_rules, dict):
            raise TypeError("tooling.policy_rules 必须是对象")
        agent_rules = agent_policy.get("rules", {})
        if not isinstance(agent_rules, dict):
            raise TypeError(f"agent {resolved_agent_id} 的 tools.policy.rules 必须是对象")
        rules = merge_json_objects(global_rules, agent_rules)

        global_restrictions = tooling.get("restrictions", {})
        if not isinstance(global_restrictions, dict):
            raise TypeError("tooling.restrictions 必须是对象")
        agent_restrictions = agent_policy.get("restrictions", {})
        if not isinstance(agent_restrictions, dict):
            raise TypeError(
                f"agent {resolved_agent_id} 的 tools.policy.restrictions 必须是对象"
            )
        restrictions = dict(global_restrictions)
        for name in (
            "execution_disabled",
            "model_hidden",
            "confirmation_required",
        ):
            merged = list(global_restrictions.get(name, []))
            merged.extend(agent_restrictions.get(name, []))
            restrictions[name] = list(dict.fromkeys(merged))

        # 现有 allowlist/denylist 和 confirmation_required 继续作为静态限制，
        # 但统一转换到 ToolPolicyResolver，不再由各个运行时调用方分别解释。
        legacy_policy = self.resolve_agent_tool_policy(resolved_agent_id)
        restrictions["execution_disabled"] = list(
            dict.fromkeys(
                [
                    *restrictions.get("execution_disabled", []),
                    *legacy_policy.disabled_names,
                ]
            )
        )
        legacy_tool_config = self._agent_tool_config_from_loaded(
            config,
            agent_id=resolved_agent_id,
        )
        restrictions["confirmation_required"] = list(
            dict.fromkeys(
                [
                    *restrictions.get("confirmation_required", []),
                    *legacy_tool_config["confirmation_required"],
                ]
            )
        )
        return ToolPolicyResolver(
            policy_defaults=defaults,
            policy_rules=rules,
            restrictions=restrictions,
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

    @staticmethod
    def _preflight_custom_tool_factories(
        config: dict[str, Any],
        *,
        source_path: Path,
    ) -> None:
        agents = config.get("agents")
        if agents is None:
            return
        if not isinstance(agents, dict):
            raise ValueError(f"配置源 agents 必须是对象: {source_path}")
        for agent_id, agent_config in agents.items():
            if not isinstance(agent_config, dict):
                continue
            tools = agent_config.get("tools")
            if not isinstance(tools, dict) or "custom" not in tools:
                continue
            raw_custom = tools["custom"]
            if not isinstance(raw_custom, list):
                raise ValueError(
                    f"配置源 {source_path} 的 agents.{agent_id}.tools.custom 必须是数组"
                )
            specs = parse_custom_tool_specs(
                raw_custom,
                context=(f"配置源 {source_path} 的 agents.{agent_id}.tools.custom"),
            )
            for spec in specs:
                try:
                    load_custom_tool_factory(spec.factory_path)
                except (ImportError, AttributeError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "配置源扩展工具预检失败: "
                        f"path={source_path}, agent={agent_id}, "
                        f"tool={spec.name}, factory={spec.factory_path}"
                    ) from exc

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
                raise ValueError(f"agent {agent_id} 的 tools.{field_name} 必须是数组")
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


__all__ = ["ConfigAgentToolsMixin"]
