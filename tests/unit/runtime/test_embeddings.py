from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.runtime.embeddings import LiteLLMEmbeddingComputer


@pytest.fixture
def embedding_computer() -> LiteLLMEmbeddingComputer:
    return LiteLLMEmbeddingComputer.from_provider(
        {
            "id": "openrouter_embedding",
            "endpoint": "https://openrouter.ai/api/v1",
            "model": "openai/text-embedding-3-small",
            "api_key": "test-key",
            "custom_llm_provider": "openrouter",
        }
    )


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
