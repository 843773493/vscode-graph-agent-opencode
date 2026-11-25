"""通过真实配置继承与执行中间件核对压缩开关，不调用远端模型。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.context_compaction import AgentSummarizationCompactor
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
    runtime = AgentSummarizationCompactor(config_service=case.service, saver=None, registry=None, sessions=None)
    assert runtime.policy() == (case.service.get_compaction_policy(), False)
    case.document["settings"]["workspace"]["context"]["compaction"]["apply_fraction"] = 0.8
    case.document["settings"]["workspace"]["ui"]["auto_summarize"] = True
    case.source.write_text(json.dumps(case.document), encoding="utf-8")
    assert await case.service.reload()
    assert case.service.get_compaction_policy().apply_fraction == 0.8
    assert case.service.is_automatic_compaction_enabled() is True
    assert runtime.policy() == (case.service.get_compaction_policy(), True)
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
