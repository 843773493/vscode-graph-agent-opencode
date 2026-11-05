from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from threading import Event, Thread

import commentjson
import pytest

from app.agents import agent_factory
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
        lambda provider: (
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
async def test_agent_list_defers_sdk_failure_to_execution(
    monkeypatch: pytest.MonkeyPatch, agent_config_service: ConfigService,
) -> None:
    calls = []
    error = RuntimeError("SDK 初始化失败")

    def fail_sdk(provider, _runtime, **_kwargs):
        calls.append(provider["id"])
        raise error

    monkeypatch.setattr(agent_factory, "build_model_from_provider", fail_sdk)
    agents = await AgentService(config_service=agent_config_service).list()
    assert agents and all(provider.available for agent in agents for provider in agent.providers)
    assert calls == []
    with pytest.raises(RuntimeError, match="当前选择的模型配置不可用") as caught:
        agent_factory.build_runtime_for_agent("default", agent_config_service)
    assert caught.value.__cause__ is error
    assert calls == [agent_config_service.get_agent_runtime_config("default")["providers"][0]["id"]]


@pytest.mark.asyncio
async def test_agent_list_rechecks_oauth_credentials_without_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.runtime import chatgpt_auth

    config = commentjson.loads(Path("configs/workspace_inline.jsonc").read_text())
    primary = config["agents"]["default"]["model"]["primary_provider"]
    provider = next(value for value in config["llm"]["providers"] if value["id"] == primary)
    provider.update({"custom_llm_provider": "chatgpt", "model": "chatgpt/gpt-5.6-codex",
        "auth": {"type": "oauth", "method": "chatgpt"},
        "api_mode": {"protocol": "responses", "model_info": {"supports_function_calling": True}}})
    config_path = tmp_path / "oauth-workspace.jsonc"
    config_path.write_text(json.dumps(config))
    auth_dir = tmp_path / "oauth"
    auth_dir.mkdir()
    auth_file = auth_dir / "auth.json"
    expires_at = int(time.time()) + 3600
    auth_file.write_text(json.dumps({"expires_at": expires_at}))
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(auth_dir))
    monkeypatch.setenv("CHATGPT_AUTH_FILE", "auth.json")

    def unexpected_sdk(*_args, **_kwargs):
        raise AssertionError("Agent 列表不得初始化 SDK 或登记 SDK 模型能力")

    monkeypatch.setattr(agent_factory, "build_model_from_provider", unexpected_sdk)
    monkeypatch.setattr(chatgpt_auth, "ensure_litellm_chatgpt_model_capabilities", unexpected_sdk)
    service = AgentService(config_service=ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path))

    def selected(agents):
        default = next(agent for agent in agents if agent.agent_id == "default")
        return next(value for value in default.providers if value.provider_id == primary)

    first = selected(await service.list())
    assert not first.available and "access_token" in first.configuration_error
    assert str(auth_file) not in first.configuration_error
    # 配置未变，仅更新凭据；下一次列表必须重新读取并反映可用状态。
    auth_file.write_text(json.dumps({"access_token": "unit-token", "expires_at": expires_at}))
    second = selected(await service.list())
    assert second.available and second.configuration_error is None


@pytest.mark.asyncio
async def test_provider_validation_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    agent_config_service: ConfigService,
) -> None:
    entered, heartbeat, release = Event(), Event(), Event()
    observed: list[bool] = []
    errors: list[str] = []
    loop = asyncio.get_running_loop()

    def validate(_provider: object) -> None:
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
