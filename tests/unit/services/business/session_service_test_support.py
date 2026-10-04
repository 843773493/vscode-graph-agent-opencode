"""SessionService 单测的隔离工作区与配置替身。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace


def prepare_session_test_workspace(
    *,
    test_file: Path,
    node_id: str,
) -> tuple[TestRunContext, Path, Path]:
    """在正式测试输出树内复制完整 fixture，并按测试节点隔离。"""
    context = TestRunContext.from_test_file(test_file).prepare()
    node_key = hashlib.sha1(node_id.encode("utf-8")).hexdigest()[:12]
    workspace_base = context.workspace_root / f"run-{os.getpid()}-node-{node_key}"
    workspace_root = workspace_base / "workspace"
    template_root = context.project_root / "tests" / "fixtures" / "workspaces" / "default_test_workspace"
    prepare_default_test_workspace(
        workspace_root=workspace_root,
        template_root=template_root,
    )
    return context, workspace_base, workspace_root


class SessionServiceConfigStub:
    """只实现 SessionService 测试需要的配置查询，不访问 provider 或网络。"""

    def __init__(self, config: dict[str, object] | None = None) -> None:
        self._config = (
            config
            if config is not None
            else {
                "default_agent": "default",
                "agents": {
                    "default": {
                        "model": {
                            "primary_provider": "primary",
                            "fallback_providers": [],
                        }
                    }
                },
                "llm": {"providers": [{"id": "primary"}]},
            }
        )
        self._workspace_agent_id: str | None = None
        self._workspace_provider_ids: dict[str, str] = {}

    def resolve_new_session_agent_id(self, agent_id: str | None) -> str:
        if agent_id is not None:
            return self.validate_agent_id(agent_id)
        return self._workspace_agent_id or self._default_agent_id()

    def resolve_new_session_provider_id(self, agent_id: str) -> str:
        return self._workspace_provider_ids.get(
            agent_id,
            self.resolve_agent_provider_id(agent_id),
        )

    def validate_agent_id(self, agent_id: str | None) -> str:
        resolved = (
            self._default_agent_id()
            if agent_id in (None, "deep_agent")
            else agent_id
        )
        agents = self._mapping(self._config.get("agents"))
        if not isinstance(resolved, str) or resolved not in agents:
            raise ValueError(f"agent {resolved} 不存在")
        return resolved

    def resolve_agent_provider_id(
        self,
        agent_id: str | None,
        provider_id: str | None = None,
    ) -> str:
        resolved_agent = self.validate_agent_id(agent_id)
        agent = self._mapping(
            self._mapping(self._config.get("agents")).get(resolved_agent)
        )
        model = self._mapping(agent.get("model"))
        primary = model.get("primary_provider")
        fallbacks = model.get("fallback_providers", [])
        if not isinstance(primary, str) or not primary:
            raise ValueError(f"agent {resolved_agent} 缺少 primary provider")
        allowed = [primary, *(fallbacks if isinstance(fallbacks, list) else [])]
        if provider_id is not None:
            if provider_id not in allowed:
                raise ValueError(f"不允许使用 provider: {provider_id}")
            return provider_id
        return primary

    def set_workspace_default_agent(self, agent_id: str) -> None:
        self._workspace_agent_id = self.validate_agent_id(agent_id)

    def set_workspace_default_provider(self, agent_id: str, provider_id: str) -> None:
        resolved_agent = self.validate_agent_id(agent_id)
        self._workspace_provider_ids[resolved_agent] = self.resolve_agent_provider_id(
            resolved_agent,
            provider_id,
        )

    def _default_agent_id(self) -> str:
        value = self._config.get("default_agent", "default")
        return value if isinstance(value, str) else "default"

    @staticmethod
    def _mapping(value: object) -> dict[str, object]:
        return value if isinstance(value, dict) else {}
