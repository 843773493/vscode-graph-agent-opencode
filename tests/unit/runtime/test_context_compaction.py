from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.summarization import SummarizationMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.summarization.planning import compaction_retention_cutoff
from app.runtime.context_compaction import AgentSummarizationCompactor
from app.services.infrastructure.config_service import ConfigService


@pytest.mark.parametrize("keep", [("messages", 2), ("tokens", 150), ("fraction", 0.1)])
@pytest.mark.parametrize("message_count", [0, 1, 10])
def test_readonly_retention_preserves_execution_cutoffs(keep, message_count):
    messages = [HumanMessage(content="历史正文" * 60) for _ in range(message_count)]
    if message_count > 1:
        messages[-2:] = [AIMessage(content="", tool_calls=[
            {"name": "lookup", "args": {}, "id": "call-1", "type": "tool_call"},
        ]), ToolMessage(content="结果" * 200, tool_call_id="call-1")]
    execution = SummarizationMiddleware(
        model=FakeListChatModel(responses=[], profile={"max_input_tokens": 1000}), keep=keep,
    )
    assert compaction_retention_cutoff(
        messages, keep=keep, max_input_tokens=1000,
        token_counter=execution.token_counter,
        partial_token_counter=execution._partial_token_counter,
    ) == execution._determine_cutoff_index(messages)


@pytest.fixture
def compaction_case(monkeypatch):
    selected = {"api_mode": {"protocol": "chat_completions", "model_info": {"max_input_tokens": 1000}}}
    config = SimpleNamespace(
        get_snapshot=lambda: object(), use_snapshot=lambda _: nullcontext(),
        get_agent_runtime_config=lambda _: {"models": [selected]},
    )
    monkeypatch.setattr("app.runtime.provider_model.build_model_from_provider",
                        lambda *_args, **_kwargs: pytest.fail("只读压缩预览不得创建模型"))
    monkeypatch.setattr("app.runtime.agent_runtime.prepare_agent_model",
                        lambda *_args, **_kwargs: pytest.fail("只读压缩预览不得创建执行图"))
    return SimpleNamespace(compactor=AgentSummarizationCompactor(config_service=config), selected=selected)


async def test_preview_keeps_latest_input_and_complete_tool_group(compaction_case):
    messages = [HumanMessage(content="旧输入" * 200), AIMessage(content="", tool_calls=[
        {"name": "lookup", "args": {}, "id": "call-1", "type": "tool_call"},
    ]), ToolMessage(content="查询结果" * 100, tool_call_id="call-1"), HumanMessage(content="当前输入")]
    result = await compaction_case.compactor.check(agent_id="default", raw_messages=messages, event=None)
    assert result.cutoff == 3
    assert result.effective_messages == messages


async def test_preview_reports_original_planning_failure(compaction_case, monkeypatch):
    failure = ValueError("规划失败")

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("app.runtime.context_compaction.build_safe_compaction_partition", fail)
    with pytest.raises(ValueError) as captured:
        await compaction_case.compactor.check(agent_id="default", raw_messages=[], event=None)
    assert captured.value is failure


async def test_cancelled_preview_settles_owned_worker(compaction_case, monkeypatch):
    entered, finish, settled = Event(), Event(), Event()

    def partition(*_args, **_kwargs):
        entered.set()
        assert finish.wait(5)
        settled.set()

    monkeypatch.setattr("app.runtime.context_compaction.build_safe_compaction_partition", partition)
    task = asyncio.create_task(compaction_case.compactor.check(agent_id="default", raw_messages=[], event=None))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert settled.is_set()


@pytest.mark.parametrize("fallback_key", ["secret:sec_22222222222222222222222222222222", "${BOXTEAM_TEST_UNUSED_FALLBACK_KEY}"])
async def test_real_configuration_preview_never_resolves_credentials(
    tmp_path, monkeypatch, fallback_key,
):
    document = {
        "config_version": 4,
        "extends": "builtin:workspace",
        "settings": {"workspace": {"llm": {"providers": {
            "primary": {"config_ref": "#/profiles/selected"},
            "backup_1": {"config_ref": "#/profiles/fallback"},
        }}, "agents": {"default": {"model": {
            "primary": {"provider_id": "primary", "model_id": "default"},
            "fallbacks": [{"provider_id": "backup_1", "model_id": "default"}],
        }}}}},
        "profiles": {
            "selected": {"extends": "builtin:workspace#/profiles/provider.primary", "settings": {
                "model_options": {"api_key": "secret:sec_11111111111111111111111111111111"},
            }},
            "fallback": {"extends": "builtin:workspace#/profiles/provider.backup_1", "settings": {
                "model_options": {"api_key": fallback_key},
            }},
        },
    }
    source = tmp_path / ".boxteam/workspace.jsonc"
    source.parent.mkdir()
    source.write_text(json.dumps(document), encoding="utf-8")
    config = ConfigService(config_dir=Path.cwd() / "configs", workspace_root=tmp_path)
    async def reject_credentials(*_args, **_kwargs):
        pytest.fail("只读预览不得解析任何模型凭据")

    monkeypatch.setattr(config, "prepare_llm_credentials", reject_credentials)
    monkeypatch.setattr("app.runtime.provider_model.build_model_from_provider",
                        lambda *_args, **_kwargs: pytest.fail("只读预览不得构造模型"))
    try:
        result = await AgentSummarizationCompactor(config_service=config).check(
            agent_id="default", raw_messages=[], event=None,
        )
        assert result.cutoff == 0
    finally:
        await config.close()
