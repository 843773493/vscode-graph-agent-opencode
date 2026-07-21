from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.policy import DEFAULT_AGENT_TOOL_NAMES
from app.services.business.agent_service import AgentService
from app.services.infrastructure.config_service import ConfigService
from configs.boxteam import build_boxteam_config


@pytest.mark.asyncio
async def test_generated_agents_display_same_effective_tools_as_policy(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "boxteam.jsonc"
    config_path.write_text(
        json.dumps(
            build_boxteam_config(development_assets=False),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
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
