"""Provider 候选来自完整 LiteLLM 目录，模板和实际接入范围分别表达。"""

from __future__ import annotations

import pytest

from app.services.infrastructure.config import llm_catalog


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_catalog, "local_model_catalog", lambda **_kwargs: {
        "sample_spec": {"litellm_provider": "文档示例，不是 Provider"},
        "fallback_generalizations": {},
        "known": {"litellm_provider": "openai", "mode": "chat"},
        "azure/known": {"litellm_provider": "azure", "mode": "chat"},
        "custom/response": {"litellm_provider": "new-provider", "mode": "responses"},
        "custom/embed": {"litellm_provider": "new-provider", "mode": "embedding"},
        "image": {"litellm_provider": "image-only", "mode": "image_generation"},
    })


def test_all_providers_remain_selectable_without_claiming_connection_support(catalog: None) -> None:
    providers = llm_catalog.list_catalog_providers().providers
    assert [item.provider for item in providers] == ["azure", "image-only", "new-provider", "openai"]
    azure, image, custom, openai = providers
    assert azure.default_endpoint is None and azure.protocol is None and not azure.upstream_discovery
    assert image.model_count == 1 and image.chat_model_count == 0
    assert custom.model_count == 2 and custom.chat_model_count == 1
    assert openai.default_endpoint == "https://api.openai.com/v1" and openai.protocol == "responses"
    assert openai.upstream_discovery
