from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.policy import ToolMetadata
from app.services.infrastructure.config_service import ConfigService


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConfigService:
    config = {
        "default_agent": "coder",
        "agents": {
            "coder": {
                "tools": {
                    "denylist": ["write_file"],
                    "policy": {
                        "rules": {
                            "by_tool": {
                                "write_file": {"execution_enabled": False}
                            }
                        }
                    }
                }
            }
        },
        "tooling": {
            "policy_defaults": {
                "execution_enabled": True,
                "model_visible": True,
                "confirmation_required": False,
            },
            "policy_rules": {
                "by_origin": {},
                "by_kind": {},
                "by_group": {},
                "by_tool": {"read_file": {"model_visible": False}},
            },
            "restrictions": {},
        },
        "development": {"test_tools": False},
    }
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=tmp_path / "workspace.jsonc",
    )
    monkeypatch.setattr(service, "_get_effective_config", lambda: config)
    return service


def test_omitted_agent_id_resolves_to_configured_default_without_default_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path, monkeypatch)

    assert service.get_agent_tool_config() == {
        "allowlist": [],
        "denylist": ["write_file"],
        "confirmation_required": [],
        "custom": [],
    }
    assert service.get_tool_policy_resolver() is not None


@pytest.mark.parametrize("agent_id", ["default", "missing"])
def test_explicit_unknown_agent_id_fails_tool_config_and_policy_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_id: str,
) -> None:
    service = _service(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match=f"agent {agent_id} 不存在"):
        service.get_agent_tool_config(agent_id)
    with pytest.raises(ValueError, match=f"agent {agent_id} 不存在"):
        service.get_tool_policy_resolver(agent_id)


def test_tool_policy_resolver_keeps_global_and_agent_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path, monkeypatch)

    resolver = service.get_tool_policy_resolver()
    assert "write_file" in service.resolve_agent_tool_policy().disabled_names
    global_policy = resolver.resolve(
        ToolMetadata(
            tool_id="read_file",
            origin="builtin",
            kind="default",
            group_id="default",
        )
    )
    agent_policy = resolver.resolve(
        ToolMetadata(
            tool_id="write_file",
            origin="builtin",
            kind="default",
            group_id="default",
        )
    )

    assert global_policy.model_visible is False
    assert agent_policy.execution_enabled is False
