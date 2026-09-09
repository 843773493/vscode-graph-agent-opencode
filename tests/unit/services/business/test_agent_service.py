from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.policy import DEFAULT_AGENT_TOOL_NAMES
from app.services.business.agent_service import AgentService
from app.services.infrastructure.config_service import ConfigService


@pytest.mark.asyncio
async def test_generated_agents_display_same_effective_tools_as_policy(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "workspace.jsonc"
    config_path.write_bytes(Path("configs/workspace_inline.jsonc").read_bytes())
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
    )
    service = AgentService(config_service=config_service)

    agents = {agent.agent_id: agent for agent in await service.list()}

    assert set(agents) == {"default", "coder", "reviewer", "researcher"}
    for agent_id, agent in agents.items():
        assert set(agent.tools) == set(
            config_service.resolve_agent_tool_policy(agent_id).enabled_names
        )
    assert "edit_file" not in agents["coder"].tools
    assert "send_message_to_session" in agents["coder"].tools
    assert set(DEFAULT_AGENT_TOOL_NAMES) <= set(agents["reviewer"].tools)
    assert set(DEFAULT_AGENT_TOOL_NAMES) <= set(agents["researcher"].tools)


@pytest.mark.asyncio
async def test_provider_configuration_error_is_returned_without_hiding_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "workspace.jsonc"
    config_path.write_bytes(Path("configs/workspace_inline.jsonc").read_bytes())
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
    )
    monkeypatch.setattr(
        "app.services.business.agent_service.provider_configuration_error",
        lambda provider, _runtime: (
            "OAuth 凭据缺少账号信息" if provider["id"] == "backup_1" else None
        ),
    )

    agents = await AgentService(config_service=config_service).list()
    default_agent = next(agent for agent in agents if agent.agent_id == "default")
    broken = next(
        provider
        for provider in default_agent.providers
        if provider.provider_id == "backup_1"
    )

    assert broken.available is False
    assert broken.configuration_error == "OAuth 凭据缺少账号信息"
    assert len(default_agent.providers) > 1
