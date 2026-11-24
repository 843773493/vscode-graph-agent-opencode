"""模型目录按服务商匹配，能力默认不转化成请求参数。"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from app.runtime import litellm_runtime, model_metadata
from app.runtime.provider_model import provider_model_capacity


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_metadata, "local_model_catalog", lambda **_kwargs: {
        "same-name": {"litellm_provider": "openai", "mode": "chat", "max_input_tokens": 1000, "context_window": 1200,
                      "max_output_tokens": 200, "supports_reasoning": True, "supports_vision": True},
        "azure/same-name": {"litellm_provider": "azure", "mode": "chat", "max_input_tokens": 2000, "context_window": 2200,
                            "supports_vision": False},
    })


def test_matching_preserves_provider_identity_and_ignores_output_limit(catalog: None) -> None:
    assert model_metadata.model_metadata("openai", "same-name") == {
        "max_input_tokens": 1000, "context_window": 1200, "supports_reasoning": True, "supports_vision": True,
    }
    assert model_metadata.model_metadata("azure", "same-name") == {
        "max_input_tokens": 2000, "context_window": 2200, "supports_vision": False,
    }
    assert model_metadata.model_metadata("openai", "openai/same-name") == model_metadata.model_metadata("openai", "same-name")
    assert model_metadata.model_metadata("unrelated", "same-name") is None
    assert model_metadata.model_metadata("openai", "unknown") is None


def test_protocol_defaults_and_explicit_disable_do_not_write_request_parameters(catalog: None) -> None:
    options = {"custom_llm_provider": "openai", "api_mode": {"protocol": "chat_completions"}}
    result = model_metadata.apply_model_metadata(options, "same-name")
    assert result["api_mode"]["supports_reasoning"] == {"reasoning_content": True}
    assert "request_options" not in result
    options["api_mode"]["supports_reasoning"] = {}
    result = model_metadata.apply_model_metadata(options, "same-name")
    assert result["api_mode"]["model_info"]["supports_reasoning"] is False
    assert "model_info" not in options["api_mode"]


def test_ambiguous_catalog_limits_do_not_become_total_window_or_output_budget(monkeypatch):
    monkeypatch.setattr(model_metadata, "local_model_catalog", lambda **_: {
        "private": {"litellm_provider": "openai", "mode": "chat",
                    "max_tokens": 1000, "max_output_tokens": 200},
    })
    options = {"custom_llm_provider": "openai", "api_mode": {"protocol": "chat_completions"}}
    resolved = model_metadata.apply_model_metadata(options, "private")
    capacity = provider_model_capacity(resolved)
    assert not capacity.capacity_known
    assert not capacity.output_reservation_known
    assert capacity.input_budget_tokens is None
    assert "request_options" not in resolved


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic_messages"])
@pytest.mark.parametrize("field_name", ["max_tokens", "max_output_tokens"])
@pytest.mark.parametrize("output_tokens", [None, 200])
def test_capacity_reads_existing_output_aliases_and_null_without_model_construction(
    protocol, field_name, output_tokens, monkeypatch,
):
    monkeypatch.setattr("app.runtime.provider_model.build_model_from_provider",
                        lambda *_args, **_kwargs: pytest.fail("读取容量不得构造模型"))
    provider = {"api_key": "secret:unresolved", "api_mode": {
        "protocol": protocol, "model_info": {"max_input_tokens": 900, "context_window": 1000},
    }, "request_options": {"parameters": {field_name: output_tokens}}}
    original = json.loads(json.dumps(provider))
    capacity = provider_model_capacity(provider)
    assert capacity.output_reservation_tokens == output_tokens
    assert capacity.output_reservation_known is (output_tokens is not None)
    assert capacity.input_budget_tokens == (900 if output_tokens is None else 800)
    assert provider == original
    # 调用侧已完成覆盖时，空参数不能重新借用 Provider 默认值。
    assert provider_model_capacity(provider, request_parameters={}).output_reservation_tokens is None
    assert provider_model_capacity(provider, request_parameters={field_name: 300}).input_budget_tokens == 700


def test_final_chat_completion_limit_is_used_and_ambiguous_limits_fail():
    provider = {"api_mode": {"protocol": "chat_completions", "model_info": {"context_window": 1000}}}
    capacity = provider_model_capacity(provider, request_parameters={"max_completion_tokens": 250})
    assert capacity.output_reservation_tokens == 250
    assert capacity.input_budget_tokens == 750
    with pytest.raises(ValueError, match="多个非空长度参数"):
        provider_model_capacity(provider, request_parameters={"max_tokens": 200, "max_completion_tokens": 250})


@pytest.mark.parametrize("output_tokens", [0, -1, True, "200"])
def test_invalid_configured_output_reservation_is_not_treated_as_unknown(output_tokens):
    provider = {"api_mode": {"protocol": "responses"},
                "request_options": {"parameters": {"max_output_tokens": output_tokens}}}
    with pytest.raises(ValueError, match="output_reservation_tokens"):
        provider_model_capacity(provider)


def test_catalog_reads_existing_cache_without_import_or_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)
    monkeypatch.setenv("LITELLM_MODEL_COST_MAP_URL", "https://catalog.example/models.json")
    runtime = litellm_runtime.ModelMetadataRuntime(cache_root=tmp_path)
    cached = tmp_path / (sha256(b"https://catalog.example/models.json").hexdigest() + ".json")
    cached.write_text(json.dumps({"source_url": "https://catalog.example/models.json", "fetched_at": 1,
        "catalog": {"cached-only": {"litellm_provider": "openai", "mode": "chat"}}}))

    def forbidden():
        pytest.fail("只读目录不允许导入 SDK 或开启刷新任务")

    monkeypatch.setattr(litellm_runtime, "_import_litellm", forbidden)
    monkeypatch.setattr(runtime, "_refresh", forbidden)
    assert "cached-only" in runtime.catalog()
    assert runtime._worker is None
