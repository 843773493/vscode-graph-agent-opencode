"""通过真实配置继承与执行中间件核对压缩开关，不调用远端模型。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends.state import StateBackend
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.summarization.factory import (
    create_cache_preserving_summarization_middleware,
)
from app.agents.summarization.preflight import NoDurableOwnerCompactionPreflight
from app.domain.context_compaction import CompactionPolicy
from app.services.infrastructure.config.policy import workspace_config_policy
from app.services.infrastructure.config_service import ConfigService


@pytest.fixture
async def policy_configuration(tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    parent = tmp_path / "shared.jsonc"
    parent.write_text(json.dumps({
        "config_version": 4, "extends": "builtin:workspace",
        "settings": {"workspace": {"context": {"compaction": {
            "prepare_fraction": 0.65, "apply_fraction": 0.9,
        }}}},
    }), encoding="utf-8")
    source = tmp_path / ".boxteam/workspace.jsonc"
    source.parent.mkdir()
    document = {"config_version": 4, "extends": "../shared.jsonc", "settings": {
        "workspace": {"context": {"compaction": {"apply_policy": "next_request"}},
                      "ui": {"auto_summarize": False}},
    }}
    source.write_text(json.dumps(document), encoding="utf-8")
    service = ConfigService(config_dir=Path.cwd() / "configs", workspace_root=tmp_path)
    try:
        service.validate_workspace_config()
        yield SimpleNamespace(service=service, source=source, document=document)
    finally:
        await service.close()


async def test_policy_uses_actual_parent_chain_and_keeps_frozen_execution_snapshot(policy_configuration):
    case = policy_configuration
    before = case.service.get_snapshot()
    assert workspace_config_policy().activation_scope_for(("/ui/auto_summarize",)) == "next_job"
    assert workspace_config_policy().activation_scope_for(("/context/compaction/prepare_fraction",)) == "next_job"
    assert workspace_config_policy().activation_scope_for(("/ui/default_orchestration",)) == "next_session"
    assert case.service.get_compaction_policy().model_dump() == {
        "prepare_fraction": 0.65, "apply_fraction": 0.9,
        "target_fraction": 0.55, "apply_policy": "next_request",
    }
    assert case.service.is_automatic_compaction_enabled() is False
    case.document["settings"]["workspace"]["context"]["compaction"]["apply_fraction"] = 0.8
    case.document["settings"]["workspace"]["ui"]["auto_summarize"] = True
    case.source.write_text(json.dumps(case.document), encoding="utf-8")
    assert await case.service.reload()
    assert case.service.get_compaction_policy().apply_fraction == 0.8
    assert case.service.is_automatic_compaction_enabled() is True
    with case.service.use_snapshot(before):
        assert case.service.get_compaction_policy().apply_fraction == 0.9
        assert case.service.is_automatic_compaction_enabled() is False


@pytest.mark.parametrize("updates", [
    {"target_fraction": 0.7}, {"prepare_fraction": 0.9},
    {"apply_fraction": 1}, {"apply_policy": "immediate"},
])
async def test_invalid_effective_policy_is_rejected_before_publication(policy_configuration, updates):
    case = policy_configuration
    before = case.service.get_snapshot()
    case.document["settings"]["workspace"]["context"]["compaction"].update(updates)
    case.source.write_text(json.dumps(case.document), encoding="utf-8")
    with pytest.raises(ValueError):
        await case.service.reload()
    assert case.service.get_snapshot() is before


@pytest.mark.parametrize("automatic_enabled,max_input_tokens", [(False, 1000), (True, None)])
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
async def test_disabled_or_unknown_capacity_passes_original_request_without_compaction(
    automatic_enabled, max_input_tokens, use_async,
):
    model = FakeListChatModel(responses=[], profile=(
        {"max_input_tokens": max_input_tokens} if max_input_tokens is not None else {}
    ))
    middleware = create_cache_preserving_summarization_middleware(
        model, StateBackend(), compaction_preflight=NoDurableOwnerCompactionPreflight(),
        policy=CompactionPolicy(), automatic_enabled=automatic_enabled,
    )
    messages = [HumanMessage(content="x" * 340) for _ in range(10)]
    request = ModelRequest(model=model, messages=messages, tools=[])
    assert 850 < middleware._count_request_tokens(request, messages) < 1000
    seen = []
    expected = ModelResponse(result=[AIMessage(content="直接执行")])

    def dispatch(value):
        seen.append(value)
        return expected

    async def adispatch(value):
        return dispatch(value)

    result = await middleware.awrap_model_call(request, adispatch) if use_async else middleware.wrap_model_call(request, dispatch)
    assert result is expected
    assert len(seen) == 1
    assert seen[0].messages == messages
    # 手动请求不受自动开关影响，仍必须进入原规划与durable边界检查。
    with pytest.raises(RuntimeError, match="compaction preflight 缺少 runtime"):
        middleware._prepare_cache_compaction(request.override(state={"_force_cache_compaction": True}))


def test_custom_apply_threshold_is_the_execution_trigger():
    model = FakeListChatModel(responses=[], profile={"max_input_tokens": 1000})
    middleware = create_cache_preserving_summarization_middleware(
        model, StateBackend(), compaction_preflight=NoDurableOwnerCompactionPreflight(),
        policy=CompactionPolicy(apply_fraction=0.9), automatic_enabled=True,
    )
    assert not middleware._should_summarize([], 899)
    assert middleware._should_summarize([], 900)
