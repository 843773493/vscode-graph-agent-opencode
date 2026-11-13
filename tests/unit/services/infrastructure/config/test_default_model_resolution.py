from __future__ import annotations

import pytest

from app.services.infrastructure.config.llm_resolution import LlmResolution


def test_default_model_resolution_rejects_missing_agent_instead_of_using_first_provider():
    resolver = LlmResolution(
        effective_config_provider=dict,
        default_agent_id_provider=lambda: "default",
    )
    config = {
        "agents": {},
        "llm": {"providers": [{"id": "first", "model": "first-provider-model"}]},
    }

    with pytest.raises(
        ValueError,
        match=r"^default_agent 引用了不存在的 Agent: default$",
    ):
        resolver.resolve_default_model(config)
