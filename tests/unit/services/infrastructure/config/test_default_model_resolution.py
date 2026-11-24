from __future__ import annotations

import pytest

from app.services.infrastructure.config.llm_resolution import LlmResolution


def test_default_model_resolution_rejects_missing_agent_instead_of_using_first_provider(workspace_config_schema: dict, model_graph_factory):
    resolver = LlmResolution(
        graph_provider=lambda: graph,
        default_agent_id_provider=lambda: "default",
     schema_provider=lambda: workspace_config_schema)
    graph = model_graph_factory({"config_version": 4, "settings": {"workspace": {
        "agents": {},
        "llm": {"providers": {"first": {"models": {"general": {"model": "first-provider-model"}}}}},
    }}})

    with pytest.raises(
        ValueError,
        match=r"^default_agent 引用了不存在的 Agent: default$",
    ):
        resolver.resolve_default_model(graph)
