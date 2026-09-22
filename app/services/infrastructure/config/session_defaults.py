from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

WORKSPACE_SESSION_DEFAULTS_SCHEMA_VERSION = 1


class SessionDefaults:
    """持久化并解析 Workspace 会话默认 Agent/provider 设置。"""

    def __init__(
        self,
        *,
        workspace_root: Path | None,
        validate_agent_id: Callable[[str | None], str],
        resolve_agent_provider_id: Callable[[str | None, str | None], str],
        default_agent_id_provider: Callable[[], str],
    ) -> None:
        self._workspace_root = workspace_root
        self._schema_version = WORKSPACE_SESSION_DEFAULTS_SCHEMA_VERSION
        self._validate_agent_id = validate_agent_id
        self._resolve_agent_provider_id = resolve_agent_provider_id
        self._default_agent_id_provider = default_agent_id_provider

    def get_workspace_default_agent_id(self) -> str:
        payload = self.read_workspace_session_defaults()
        configured = payload.get("default_agent_id")
        if configured is None:
            return self._default_agent_id_provider()
        if not isinstance(configured, str) or not configured:
            raise TypeError("工作区默认 Agent 必须是非空字符串")
        return self._validate_agent_id(configured)

    def get_workspace_default_provider_id(self, agent_id: str) -> str:
        resolved_agent_id = self._validate_agent_id(agent_id)
        payload = self.read_workspace_session_defaults()
        raw_providers = payload.get("provider_by_agent", {})
        if not isinstance(raw_providers, dict):
            raise TypeError("工作区默认 provider 映射必须是对象")
        configured = raw_providers.get(resolved_agent_id)
        if configured is None:
            return self._resolve_agent_provider_id(resolved_agent_id)
        if not isinstance(configured, str) or not configured:
            raise TypeError(
                f"工作区默认 provider 必须是非空字符串: agent={resolved_agent_id}"
            )
        return self._resolve_agent_provider_id(resolved_agent_id, configured)

    def set_workspace_default_agent(self, agent_id: str) -> None:
        resolved_agent_id = self._validate_agent_id(agent_id)
        payload = self.read_workspace_session_defaults()
        payload["default_agent_id"] = resolved_agent_id
        self.write_workspace_session_defaults(payload)

    def set_workspace_default_provider(
        self,
        agent_id: str,
        provider_id: str,
    ) -> None:
        resolved_agent_id = self._validate_agent_id(agent_id)
        resolved_provider_id = self._resolve_agent_provider_id(
            resolved_agent_id,
            provider_id,
        )
        payload = self.read_workspace_session_defaults()
        raw_providers = payload.get("provider_by_agent", {})
        if not isinstance(raw_providers, dict):
            raise TypeError("工作区默认 provider 映射必须是对象")
        payload["provider_by_agent"] = {
            **raw_providers,
            resolved_agent_id: resolved_provider_id,
        }
        self.write_workspace_session_defaults(payload)

    def resolve_new_session_agent_id(self, agent_id: str | None) -> str:
        if agent_id is not None:
            return self._validate_agent_id(agent_id)
        return self.get_workspace_default_agent_id()

    def resolve_new_session_provider_id(self, agent_id: str) -> str:
        return self.get_workspace_default_provider_id(agent_id)

    def workspace_session_defaults_path(self) -> Path:
        if self._workspace_root is None:
            raise RuntimeError("ConfigService 未绑定工作区，无法保存工作区会话默认值")
        return self._workspace_root / ".boxteam" / "settings" / "session_defaults.json"

    def read_workspace_session_defaults(self) -> dict[str, Any]:
        if self._workspace_root is None:
            return {
                "schema_version": self._schema_version,
                "provider_by_agent": {},
            }
        path = self.workspace_session_defaults_path()
        if not path.exists():
            return {
                "schema_version": self._schema_version,
                "provider_by_agent": {},
            }
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(f"工作区会话默认值必须是对象: {path}")
        if (
            payload.get("schema_version")
            != self._schema_version
        ):
            raise ValueError(f"工作区会话默认值版本非法: {path}")
        return payload

    def write_workspace_session_defaults(self, payload: dict[str, Any]) -> None:
        path = self.workspace_session_defaults_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        persisted = {
            **payload,
            "schema_version": self._schema_version,
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(persisted, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
