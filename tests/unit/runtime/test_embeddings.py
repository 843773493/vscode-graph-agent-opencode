from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.runtime.embeddings import LiteLLMEmbeddingComputer


@pytest.fixture
def embedding_computer() -> LiteLLMEmbeddingComputer:
    return LiteLLMEmbeddingComputer.from_model(
        {
            "provider_id": "openrouter_embedding",
            "model_id": "small",
            "endpoint": "https://openrouter.ai/api/v1",
            "model": "openai/text-embedding-3-small",
            "api_key": "test-key",
            "custom_llm_provider": "openrouter",
        }
    )


@pytest.mark.parametrize("api_key", ["", None])
def test_model_identity_and_no_auth_are_preserved(api_key: str | None) -> None:
    resolved = {
        "provider_id": "local", "model_id": "semantic", "model": "embedding",
        "endpoint": "http://127.0.0.1:1234/v1", "custom_llm_provider": "openai",
    }
    if api_key is not None:
        resolved["api_key"] = api_key
    computer = LiteLLMEmbeddingComputer.from_model(resolved)
    assert (computer.provider_id, computer.model_id, computer.model) == ("local", "semantic", "embedding")
    assert computer.api_key == ""


@pytest.mark.parametrize("reference", ["secret:sec_00000000000040008000000000000001", "${UNRESOLVED_EMBEDDING_KEY}"])
def test_unresolved_credentials_cannot_reach_embedding_transport(reference: str) -> None:
    with pytest.raises(ValueError, match="尚未解析"):
        LiteLLMEmbeddingComputer.from_model({
            "provider_id": "local", "model_id": "semantic", "model": "embedding",
            "endpoint": "http://127.0.0.1:1234/v1", "custom_llm_provider": "openai", "api_key": reference,
        })


def test_other_embedding_families_keep_explicit_nonempty_credentials() -> None:
    with pytest.raises(ValueError, match="需要显式非空凭据"):
        LiteLLMEmbeddingComputer.from_model({
            "provider_id": "router", "model_id": "semantic", "model": "embedding",
            "endpoint": "https://embedding.example/v1", "custom_llm_provider": "openrouter", "api_key": "",
        })


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key", ["", "selected-model-key"])
async def test_compute_uses_selected_credentials_without_ambient_key_fallback(
    monkeypatch: pytest.MonkeyPatch, api_key: str,
) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-process-key")
    requests: list[httpx.Request] = []

    async def send(self, request: httpx.Request, **kwargs: object) -> httpx.Response:
        requests.append(request)
        assert request.url == "https://embedding.example/v1/embeddings"
        return httpx.Response(200, request=request, json={
            "object": "list", "model": "embedding-model",
            "data": [{"object": "embedding", "index": 0, "embedding": [1.0, 0.0]}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        })

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    computer = LiteLLMEmbeddingComputer.from_model({
        "provider_id": "team", "model_id": "semantic", "model": "embedding-model",
        "endpoint": "https://embedding.example/v1", "custom_llm_provider": "openai",
        "api_key": api_key, "request_options": {"no_proxy": True, "default_headers": {"X-Route": "semantic"}},
    })
    assert await computer.compute(["selected model input"]) == [[1.0, 0.0]]
    assert len(requests) == 1
    assert requests[0].headers.get("Authorization") == (f"Bearer {api_key}" if api_key else None)
    assert requests[0].headers["X-Route"] == "semantic"


@pytest.mark.asyncio
async def test_compute_restores_input_order_from_response_indexes(
    embedding_computer: LiteLLMEmbeddingComputer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_aembedding(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            data=[
                {"index": 1, "embedding": [0, 1]},
                {"index": 0, "embedding": [1, 0]},
            ]
        )

    monkeypatch.setattr("litellm.aembedding", fake_aembedding)

    embeddings = await embedding_computer.compute(["first", "second"])

    assert embeddings == [[1.0, 0.0], [0.0, 1.0]]
    assert captured["max_retries"] == 3
    assert captured["model"] == "openai/text-embedding-3-small"


@pytest.mark.asyncio
async def test_compute_uses_response_position_when_index_is_omitted(
    embedding_computer: LiteLLMEmbeddingComputer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_aembedding(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            data=[
                {"embedding": [1, 0]},
                {"embedding": [0, 1]},
            ]
        )

    monkeypatch.setattr("litellm.aembedding", fake_aembedding)

    assert await embedding_computer.compute(["first", "second"]) == [
        [1.0, 0.0],
        [0.0, 1.0],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("indexes", "expected_indexes"),
    [([0, 0], "[0, 0]"), ([0, 2], "[0, 2]"), ([-1, 1], "[-1, 1]")],
)
async def test_compute_rejects_duplicate_or_out_of_range_indexes(
    indexes: list[int],
    expected_indexes: str,
    embedding_computer: LiteLLMEmbeddingComputer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_aembedding(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            data=[{"index": index, "embedding": [1, 0]} for index in indexes]
        )

    monkeypatch.setattr("litellm.aembedding", fake_aembedding)

    with pytest.raises(RuntimeError) as error:
        await embedding_computer.compute(["first", "second"])

    assert str(error.value) == (
        f"Embedding API 返回的 index 不连续: expected=[0, 1], actual={expected_indexes}"
    )
