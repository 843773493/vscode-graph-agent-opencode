"""模型目录按服务商匹配，能力默认不转化成请求参数。"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from app.runtime import litellm_runtime, model_metadata


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_metadata, "local_model_catalog", lambda: {
        "same-name": {"litellm_provider": "openai", "mode": "chat", "max_input_tokens": 1000,
                      "max_output_tokens": 200, "supports_reasoning": True, "supports_vision": True},
        "azure/same-name": {"litellm_provider": "azure", "mode": "chat", "max_input_tokens": 2000,
                            "supports_vision": False},
    })


def test_matching_preserves_provider_identity_and_ignores_output_limit(catalog: None) -> None:
    assert model_metadata.model_metadata("openai", "same-name") == {
        "max_input_tokens": 1000, "supports_reasoning": True, "supports_vision": True,
    }
    assert model_metadata.model_metadata("azure", "same-name") == {"max_input_tokens": 2000, "supports_vision": False}
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
    assert runtime._worker is None and runtime._sdk is None
