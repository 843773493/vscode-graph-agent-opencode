from __future__ import annotations

from copy import deepcopy

import pytest

from app.runtime.provider_model import build_model_from_provider
from app.schemas.gateway.secrets import ResolvedSecret
from app.services.infrastructure.config.llm_resolution import LlmResolution
from app.services.infrastructure.config.source_document import redact_source_payload
from app.services.infrastructure.config.state import (
    build_secret_binding_summary,
    prepare_config_for_persistence,
    redact_config_payload,
    restore_environment_secret_references,
)

SECRET_ID = "sec_00000000000040008000000000000001"


@pytest.fixture
def config() -> dict:
    return {"llm": {"providers": {name: {
        "defaults": {"api_key": "secret:" + SECRET_ID,
                     "api_mode": {"protocol": "chat_completions",
                                  "model_info": {"supports_function_calling": True, "supports_reasoning": False},
                                  "supports_reasoning": {}}},
        "models": {"general": {"model": "test-model"}},
    } for name in ("aa", "bb")}}}


def test_reference_roundtrip_stays_visible_but_never_resolves_into_snapshots(config: dict) -> None:
    assert prepare_config_for_persistence(config, resolve_environment=True) == config
    assert restore_environment_secret_references(prepare_config_for_persistence(config)) == config
    assert redact_config_payload(config) == redact_source_payload(config) == config
    bindings = build_secret_binding_summary(config, resolve_environment=True)
    assert bindings["/llm/providers/aa/defaults/api_key"]["secret_ref"] == "secret:" + SECRET_ID


def test_two_providers_share_scoped_value_and_rotation_does_not_mutate_config(config: dict, workspace_config_schema: dict) -> None:
    original = deepcopy(config)
    resolver = LlmResolution(effective_config_provider=lambda: config, default_agent_id_provider=lambda: "default", schema_provider=lambda: workspace_config_schema)
    first_model = {"provider_id": "aa", "model_id": "general"}
    assert resolver.get_model(first_model)["api_key"] == "secret:" + SECRET_ID
    first = {SECRET_ID: ResolvedSecret(revision=1, value="before")}
    with resolver.use_secrets(first):
        assert [model["api_key"] for model in resolver.list_models(config)] == ["before", "before"]
    with resolver.use_secrets({SECRET_ID: ResolvedSecret(revision=2, value="after")}):
        assert resolver.get_model(first_model)["api_key"] == "after"
    assert first[SECRET_ID].value.get_secret_value() == "before"
    assert config == original
    assert resolver.get_model({"provider_id": "bb", "model_id": "general"})["api_key"] == "secret:" + SECRET_ID
    with resolver.use_secrets({}), pytest.raises(ValueError, match="未准备"):
        resolver.get_model(first_model)


def test_unresolved_reference_cannot_be_sent_as_model_api_key(config: dict, workspace_config_schema: dict) -> None:
    resolver = LlmResolution(effective_config_provider=lambda: config, default_agent_id_provider=lambda: "default", schema_provider=lambda: workspace_config_schema)
    with pytest.raises(ValueError, match="尚未解析"):
        build_model_from_provider(resolver.get_model({"provider_id": "aa", "model_id": "general"}), {})
