"""SessionService 单测的隔离工作区与配置替身。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import RLock

from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_test_workspace

PRIMARY_MODEL = ModelReferenceDTO(provider_id="team", model_id="primary")
BACKUP_MODEL = ModelReferenceDTO(provider_id="team", model_id="backup")


def prepare_session_test_workspace(
    *,
    test_file: Path,
    node_id: str,
    template_items: tuple[str, ...] | None = None,
) -> tuple[TestRunContext, Path, Path]:
    """在正式测试输出树内创建节点隔离工作区，可只复制需要的模板项。"""
    context = TestRunContext.from_test_file(test_file).prepare()
    workspace_fixture_root = context.runtime_root_for_node(node_id)
    workspace_root = workspace_fixture_root / "workspace"
    template_root = context.project_root / "tests" / "fixtures" / "workspaces" / "default_test_workspace"
    prepare_test_workspace(
        workspace_root=workspace_root,
        template_root=template_root,
        template_items=template_items,
    )
    return context, workspace_fixture_root, workspace_root


class SessionServiceConfigStub:
    """只实现 SessionService 测试需要的配置查询，不访问 provider 或网络。"""

    def __init__(self, config: dict[str, object] | None = None) -> None:
        self.publication_lock = RLock()
        self._config = (
            config
            if config is not None
            else {
                "default_agent": "default",
                "agents": {
                    "default": {
                        "model": {
                            "primary": PRIMARY_MODEL.model_dump(mode="json"),
                            "fallbacks": [],
                        }
                    },
                    "coder": {
                        "model": {
                            "primary": PRIMARY_MODEL.model_dump(mode="json"),
                            "fallbacks": [],
                        }
                    },
                },
            }
        )
        self._workspace_agent_id: str | None = None
        self._workspace_models: dict[str, ModelReferenceDTO] = {}

    def resolve_new_session_agent_id(self, agent_id: str | None) -> str:
        if agent_id is not None:
            return self.validate_agent_id(agent_id)
        return self._workspace_agent_id or self._default_agent_id()

    def get_revision(self) -> str:
        return hashlib.sha256(json.dumps(self._config, sort_keys=True).encode()).hexdigest()

    def resolve_new_session_model(self, agent_id: str) -> ModelReferenceDTO:
        return self.resolve_agent_model(agent_id, self._workspace_models.get(agent_id))

    def get_workspace_default_model(self, agent_id: str) -> ModelReferenceDTO:
        return self.resolve_new_session_model(agent_id)

    def get_workspace_default_agent_id(self) -> str:
        return self.resolve_new_session_agent_id(None)

    def set_workspace_default_model(self, agent_id: str, model: ModelReferenceDTO) -> None:
        self._workspace_models[self.validate_agent_id(agent_id)] = self.resolve_agent_model(
            agent_id,
            model,
        )

    def validate_agent_id(self, agent_id: str | None) -> str:
        resolved = self._default_agent_id() if agent_id is None else agent_id
        agents = self._mapping(self._config.get("agents"))
        if not isinstance(resolved, str) or resolved not in agents:
            raise ValueError(f"agent {resolved} 不存在")
        return resolved

    def resolve_agent_model(
        self,
        agent_id: str | None,
        model: ModelReferenceDTO | None = None,
    ) -> ModelReferenceDTO:
        resolved_agent = self.validate_agent_id(agent_id)
        agent = self._mapping(
            self._mapping(self._config.get("agents")).get(resolved_agent)
        )
        options = self._mapping(agent.get("model"))
        allowed = [ModelReferenceDTO.model_validate(value)
                   for value in (options["primary"], *options.get("fallbacks", []))]
        if model is not None and model not in allowed:
            raise ValueError(f"不允许使用模型: {model}")
        return allowed[0] if model is None else model

    def set_workspace_default_agent(self, agent_id: str) -> None:
        self._workspace_agent_id = self.validate_agent_id(agent_id)

    def _default_agent_id(self) -> str:
        value = self._config.get("default_agent", "default")
        return value if isinstance(value, str) else "default"

    @staticmethod
    def _mapping(value: object) -> dict[str, object]:
        return value if isinstance(value, dict) else {}
