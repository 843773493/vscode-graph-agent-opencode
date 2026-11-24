from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.summarization.factory import (
    create_cache_preserving_summarization_middleware,
)
from app.agents.summarization.planning import build_safe_compaction_partition
from app.agents.summarization.preflight import NoDurableOwnerCompactionPreflight
from app.agents.workspace_backend import build_workspace_backend
from app.runtime.context_compaction import AgentSummarizationCompactor
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.infrastructure.config_service import ConfigService


@pytest.fixture
def compaction_case(tmp_path, monkeypatch):
    model = FakeListChatModel(responses=["不能请求上游"])
    closed = AsyncMock()
    monkeypatch.setattr(type(model), "aclose", closed, raising=False)
    selected, unused = {"provider_id": "selected", "model_id": "primary"}, {"provider_id": "unused"}
    config = SimpleNamespace(
        get_snapshot=lambda: object(), use_snapshot=lambda _: nullcontext(),
        get_agent_runtime_config=lambda _: {"models": [selected, unused]},
        resolve_llm_model=AsyncMock(return_value=selected),
    )
    builds = []

    def build(provider, _runtime):
        builds.append(provider)
        return model

    monkeypatch.setattr("app.runtime.provider_model.build_model_from_provider", build)
    monkeypatch.setattr("app.runtime.agent_runtime.prepare_agent_model", lambda *_a, **_k: pytest.fail("压缩不能准备 Agent 候选"))
    return SimpleNamespace(
        compactor=AgentSummarizationCompactor(config_service=config, workspace_root=tmp_path),
        config=config, model=model, closed=closed, builds=builds, selected=selected, root=tmp_path,
    )


async def test_compaction_uses_same_partition_and_only_selected_model(compaction_case):
    case = compaction_case
    messages = [message for i in range(4) for message in (HumanMessage(content=f"用户 {i}"), AIMessage(content=f"回复 {i}"))]
    before = create_cache_preserving_summarization_middleware(
        case.model, build_workspace_backend(case.root), compaction_preflight=NoDurableOwnerCompactionPreflight(),
    )
    partition = build_safe_compaction_partition(before, messages, None)
    expected = len(partition.prefix_messages) + len(partition.messages_to_summarize) if partition else 0
    result = await case.compactor.check(agent_id="default", raw_messages=messages, event=None)
    assert result.cutoff == expected
    assert result.effective_messages == messages
    assert case.builds == [case.selected]
    case.config.resolve_llm_model.assert_awaited_once_with(ModelReferenceDTO(provider_id="selected", model_id="primary"))
    case.closed.assert_awaited_once()


async def test_compaction_failure_closes_its_model_and_preserves_both_errors(compaction_case, monkeypatch):
    case = compaction_case
    failure, release = ValueError("规划失败"), RuntimeError("释放失败")
    case.closed.side_effect = release

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("app.agents.summarization.planning.build_safe_compaction_partition", fail)
    with pytest.raises(BaseExceptionGroup) as error:
        await case.compactor.check(agent_id="default", raw_messages=[], event=None)
    assert error.value.exceptions[0] is failure
    assert error.value.exceptions[1].exceptions[0].__cause__ is release
    case.closed.assert_awaited_once()


async def test_cancelled_check_settles_worker_before_closing_model(compaction_case, monkeypatch):
    from threading import Event

    case = compaction_case
    entered, finish = Event(), Event()

    def partition(*_args):
        entered.set()
        assert finish.wait(5)

    monkeypatch.setattr("app.agents.summarization.planning.build_safe_compaction_partition", partition)
    task = asyncio.create_task(case.compactor.check(agent_id="default", raw_messages=[], event=None))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    case.closed.assert_not_called()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    case.closed.assert_awaited_once()


@pytest.mark.parametrize("fallback_key", ["secret:sec_22222222222222222222222222222222", "${BOXTEAM_TEST_UNUSED_FALLBACK_KEY}"])
async def test_compaction_does_not_expand_unused_fallback_credentials(
    tmp_path, empty_workspace_scope, monkeypatch, fallback_key, workspace_config_snapshots,
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
            "selected": {
                "extends": "builtin:workspace#/profiles/provider.primary",
                "settings": {"model_options": {"api_key": "secret:sec_11111111111111111111111111111111"}},
            },
            "fallback": {
                "extends": "builtin:workspace#/profiles/provider.backup_1",
                "settings": {"model_options": {"api_key": fallback_key}},
            },
        },
    }
    config = workspace_config_snapshots.service_for_document(document, "compaction_workspace.jsonc")
    payload = config.get_snapshot().to_dict()
    provider_id = "primary"
    config._snapshot_store.initialize(
        workspace_config_snapshots.build(payload, graph=config.get_snapshot().graph)
    )
    monkeypatch.delenv("BOXTEAM_TEST_UNUSED_FALLBACK_KEY", raising=False)
    credentials = AsyncMock(return_value={
        "sec_11111111111111111111111111111111": ResolvedSecret(revision=1, value="test-primary-key"),
    })
    monkeypatch.setattr(config, "prepare_llm_credentials", credentials)
    model = FakeListChatModel(responses=["不能请求上游"])
    closed = AsyncMock()
    monkeypatch.setattr(type(model), "aclose", closed, raising=False)
    built = []

    def build(provider, _runtime):
        built.append(provider)
        return model

    monkeypatch.setattr("app.runtime.provider_model.build_model_from_provider", build)
    compactor = AgentSummarizationCompactor(config_service=config, workspace_root=empty_workspace_scope[1])
    result = await compactor.check(agent_id="default", raw_messages=[], event=None)
    assert result.cutoff == 0
    assert [(item["provider_id"], item["api_key"]) for item in built] == [(provider_id, "test-primary-key")]
    assert [item["provider_id"] for item in credentials.await_args.args[0]] == [provider_id]
    credentials.assert_awaited_once()
    closed.assert_awaited_once()
