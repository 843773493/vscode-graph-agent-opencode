from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event, Thread

import pytest

from app.agents.policy import DEFAULT_AGENT_TOOL_NAMES
from app.services.business.agent_service import AgentService
from app.services.infrastructure.config_service import ConfigService


@pytest.fixture
def agent_config_service(tmp_path: Path) -> ConfigService:
    config_path = tmp_path / "workspace.jsonc"
    config_path.write_bytes(Path("configs/workspace_inline.jsonc").read_bytes())
    return ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
    )


@pytest.mark.asyncio
async def test_generated_agents_display_same_effective_tools_as_policy(
    agent_config_service: ConfigService,
) -> None:
    service = AgentService(config_service=agent_config_service)

    agents = {agent.agent_id: agent for agent in await service.list()}

    assert set(agents) == {"default", "coder", "reviewer", "researcher"}
    for agent_id, agent in agents.items():
        assert set(agent.tools) == set(
            agent_config_service.resolve_agent_tool_policy(agent_id).enabled_names
        )
    assert "edit_file" not in agents["coder"].tools
    assert "send_message_to_session" in agents["coder"].tools
    assert set(DEFAULT_AGENT_TOOL_NAMES) <= set(agents["reviewer"].tools)
    assert set(DEFAULT_AGENT_TOOL_NAMES) <= set(agents["researcher"].tools)


@pytest.mark.asyncio
async def test_provider_configuration_error_is_returned_without_hiding_agent(
    monkeypatch: pytest.MonkeyPatch,
    agent_config_service: ConfigService,
) -> None:
    monkeypatch.setattr(
        "app.services.business.agent_service.provider_configuration_error",
        lambda provider, _runtime: (
            "OAuth 凭据缺少账号信息" if provider["id"] == "backup_1" else None
        ),
    )

    agents = await AgentService(config_service=agent_config_service).list()
    default_agent = next(agent for agent in agents if agent.agent_id == "default")
    broken = next(
        provider
        for provider in default_agent.providers
        if provider.provider_id == "backup_1"
    )

    assert broken.available is False
    assert broken.configuration_error == "OAuth 凭据缺少账号信息"
    assert len(default_agent.providers) > 1


@pytest.mark.asyncio
async def test_provider_validation_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    agent_config_service: ConfigService,
) -> None:
    entered, heartbeat, release = Event(), Event(), Event()
    observed: list[bool] = []
    errors: list[str] = []
    loop = asyncio.get_running_loop()

    def validate(_provider: object, _runtime: object) -> None:
        if not entered.is_set():
            entered.set()
            if not release.wait(10):
                raise RuntimeError("测试观察线程未释放 Provider 校验")

    def observe() -> None:
        try:
            if not entered.wait(10):
                errors.append("Provider 校验未开始")
                return
            loop.call_soon_threadsafe(heartbeat.set)
            # 校验还未返回时，健康检查等同循环请求必须有机会执行。
            observed.append(heartbeat.wait(2))
        finally:
            release.set()

    monkeypatch.setattr(
        "app.services.business.agent_service.provider_configuration_error",
        validate,
    )
    observer = Thread(target=observe, daemon=True)
    observer.start()
    try:
        agents = await AgentService(config_service=agent_config_service).list()
    finally:
        release.set()
        await asyncio.to_thread(observer.join, 10)

    assert not observer.is_alive()
    assert errors == []
    assert observed == [True], "Provider 同步校验阻塞了工作区事件循环"
    assert agents
    assert all(provider.available for agent in agents for provider in agent.providers)
