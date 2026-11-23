"""ConfigAgentsMixin：ConfigService 的 agent 身份解析、默认值与 agent-runtime 配置方法族（纯搬迁）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from app.schemas.internal_v2.model_reference import ModelReferenceDTO

if TYPE_CHECKING:
    from app.services.infrastructure.config_service import ConfigService


class ConfigAgentsMixin:
    def _validate_agent_definition_references(self, config: dict[str, object]) -> None:
        """只核完整候选及本工作区会话默认值，不展开凭据或查询历史。"""
        service = cast("ConfigService", self)
        agents = cast(dict[str, dict[str, object]], config["agents"])
        if config["default_agent"] not in agents:
            raise ValueError("default_agent 引用了不存在的 Agent")
        defaults = service._session_defaults.read_workspace_session_defaults()
        default_agent = defaults.get("default_agent_id")
        if default_agent is not None and default_agent not in agents:
            raise ValueError("工作区默认 Agent 引用了不存在的 Agent: field=default_agent_id")
        model_defaults = defaults.get("model_by_agent", {})
        if not isinstance(model_defaults, dict):
            raise TypeError("工作区默认模型映射必须是对象: artifact=session_defaults")
        allowed: dict[str, list[ModelReferenceDTO]] = {}
        for agent_id, agent in agents.items():
            references = self._agent_model_references(agent)
            for reference in references:
                service._llm_resolution.resolve_model_reference(config, reference)
            allowed[agent_id] = references
        for agent_id, model in model_defaults.items():
            reference = ModelReferenceDTO.model_validate(model)
            if agent_id not in allowed or reference not in allowed[agent_id]:
                raise ValueError(f"工作区默认模型不再属于 Agent {agent_id}: field=model_by_agent")

    @staticmethod
    def _agent_model_references(agent: Mapping[str, Any]) -> list[ModelReferenceDTO]:
        model = agent["model"]
        return [ModelReferenceDTO.model_validate(dict(value)) for value in
                (model["primary"], *model.get("fallbacks", []))]

    def get_default_agent_id(self) -> str:
        config = cast("ConfigService", self)._require_snapshot().read_only
        default_agent_id = config["default_agent"]
        if default_agent_id not in config["agents"]:
            raise ValueError(f"default_agent 引用了不存在的 Agent: {default_agent_id}")
        return default_agent_id

    def get_workspace_default_agent_id(self) -> str:
        return self._session_defaults.get_workspace_default_agent_id()

    def get_workspace_default_model(self, agent_id: str) -> ModelReferenceDTO:
        return self._session_defaults.get_workspace_default_model(agent_id)

    def set_workspace_default_agent(self, agent_id: str) -> None:
        self._session_defaults.set_workspace_default_agent(agent_id)

    def set_workspace_default_model(
        self,
        agent_id: str,
        model: ModelReferenceDTO,
    ) -> None:
        self._session_defaults.set_workspace_default_model(agent_id, model)

    def resolve_new_session_agent_id(self, agent_id: str | None) -> str:
        return self._session_defaults.resolve_new_session_agent_id(agent_id)

    def resolve_new_session_model(self, agent_id: str) -> ModelReferenceDTO:
        return self._session_defaults.get_workspace_default_model(agent_id)

    def _normalize_agent_id(self, agent_id: str | None) -> str:
        return self.get_default_agent_id() if agent_id is None else agent_id

    def resolve_agent_id(self, agent_id: str | None) -> str:
        return self._normalize_agent_id(agent_id)

    def validate_agent_id(self, agent_id: str | None) -> str:
        resolved_agent_id = self._normalize_agent_id(agent_id)
        config = cast("ConfigService", self)._require_snapshot().read_only
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
        preferred_model: ModelReferenceDTO | None = None,
    ) -> dict[str, Any]:
        config = cast("ConfigService", self)._require_snapshot().read_only
        agents = config["agents"]
        resolved_agent_id = config["default_agent"] if agent_id is None else agent_id
        if resolved_agent_id not in agents:
            raise ValueError(f"agent {resolved_agent_id} 不存在")

        target_agent = agents[resolved_agent_id]
        instructions = target_agent["instructions"]
        model_cfg = target_agent.get("model", {})
        execution_cfg = target_agent.get("execution", {})
        if not isinstance(execution_cfg, Mapping):
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

        references = self._agent_model_references(target_agent)
        if preferred_model is not None:
            if preferred_model not in references:
                raise ValueError(
                    f"agent {resolved_agent_id} 不允许使用模型: {preferred_model}"
                )
            references = [
                preferred_model,
                *(item for item in references if item != preferred_model),
            ]
        selected_models = [self._llm_resolution.resolve_model_reference(config, reference)
                           for reference in references]

        runtime_config: dict[str, Any] = {
            "system_prompt": instructions["system_prompt"],
            # 未配置说明时只有软件身份事实；不在 Python 另设提示词正文兜底。
            "runtime_identity_description": instructions.get("runtime_identity", {}).get("description", ""),
            "models": selected_models,
            "require_delegated_report": require_delegated_report,
        }
        for option_name in ("temperature", "top_p", "max_output_tokens"):
            if option_name in model_cfg:
                runtime_config[option_name] = model_cfg[option_name]
        return runtime_config

    def resolve_agent_model(
        self,
        agent_id: str | None,
        model: ModelReferenceDTO | None = None,
    ) -> ModelReferenceDTO:
        resolved_agent_id = self.validate_agent_id(agent_id)
        agents = cast("ConfigService", self)._require_snapshot().read_only["agents"]
        references = self._agent_model_references(agents[resolved_agent_id])
        if model is not None and model not in references:
            raise ValueError(f"agent {resolved_agent_id} 不允许使用模型: {model}")
        reference = references[0] if model is None else model
        self._llm_resolution.get_model(reference)
        return reference


__all__ = ["ConfigAgentsMixin"]
