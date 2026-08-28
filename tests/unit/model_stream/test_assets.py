from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.testing.model_stream import (
    ModelStreamAssetError,
    ReplaySpec,
    build_cassette,
    data_frame,
    done_frame,
    load_cassette,
    load_cassette_from_object,
    load_scenario,
    promote_recorded_cassette,
)

FIXTURE_ROOT = Path.cwd() / "tests" / "fixtures" / "model_stream"


def test_all_handwritten_scenarios_load_to_the_same_cassette_model() -> None:
    scenario_ids = (
        "basic-text",
        "split-tool-call",
        "reasoning-stream",
        "reasoning-tool",
        "finish-and-usage",
        "responses-basic-text",
        "responses-reasoning-text",
        "responses-reasoning-tool",
    )

    for scenario_id in scenario_ids:
        scenario = load_scenario(FIXTURE_ROOT, scenario_id)
        assert scenario.cassette.kind == "model_stream_cassette"
        assert scenario.cassette.metadata["source"] == "handwritten"
        protocol = scenario.cassette.metadata["protocol"]
        assert protocol in {"openai_chat_sse", "openai_responses_sse"}
        terminal = scenario.cassette.interactions[0].response.frames[-1]
        if protocol == "openai_chat_sse":
            assert terminal.payload == "[DONE]"
        else:
            assert terminal.event == "response.completed"
            assert terminal.payload["type"] == "response.completed"


def test_loader_preserves_unknown_root_and_provider_fields(tmp_path: Path) -> None:
    raw = {
        "schema_version": 1,
        "kind": "model_stream_cassette",
        "metadata": {
            "source": "handwritten",
            "asset_id": "unknown-fields",
            "protocol": "openai_chat_sse",
        },
        "unknown_root_field": {"kept": True},
        "interactions": [
            {
                "request": {
                    "method": "POST",
                    "url": "https://provider.example/v1/chat/completions",
                    "match": {"model": "test-model", "stream": True},
                },
                "response": {
                    "status": 200,
                    "headers": {"content-type": "text/event-stream"},
                    "frames": [
                        {
                            "kind": "data",
                            "encoding": "json",
                            "payload": {
                                "choices": [],
                                "provider_unknown": {"kept": "yes"},
                            },
                        },
                        {"kind": "done", "encoding": "text", "payload": "[DONE]"},
                    ],
                },
            }
        ],
    }
    path = tmp_path / "unknown-fields.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    cassette = load_cassette(path)

    assert cassette.raw["unknown_root_field"] == {"kept": True}
    assert cassette.interactions[0].response.frames[0].payload["provider_unknown"] == {
        "kept": "yes"
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["interactions"][0]["response"]["frames"].pop(),
        lambda raw: raw["interactions"][0]["response"]["frames"][0].update(
            {"encoding": "json", "payload": []}
        ),
        lambda raw: raw["interactions"][0]["response"]["frames"][-1].update(
            {"payload": "not-done"}
        ),
    ],
)
def test_invalid_cassette_is_rejected(mutate) -> None:
    cassette = build_cassette(
        asset_id="invalid-source",
        url="https://provider.example/v1/chat/completions",
        model="test-model",
        frames=[data_frame({"choices": []}), done_frame()],
    )
    raw = json.loads(json.dumps(cassette.raw))
    mutate(raw)

    with pytest.raises(ModelStreamAssetError):
        load_cassette_from_object(raw)


def test_builder_supports_session_sequence_metadata() -> None:
    cassette = build_cassette(
        asset_id="sequence-builder",
        url="https://provider.example/v1/chat/completions",
        model="test-model",
        frames=[data_frame({"choices": []}), done_frame()],
        replay=ReplaySpec(sequence_id="tool-loop", step=0),
    )

    assert cassette.interactions[0].replay is not None
    assert cassette.interactions[0].replay.sequence_id == "tool-loop"
    assert cassette.interactions[0].replay.step == 0


def test_recorded_cassette_promotion_is_explicit_and_scoped(tmp_path: Path) -> None:
    cassette = build_cassette(
        asset_id="promote-me",
        url="https://provider.example/v1/chat/completions",
        model="test-model",
        frames=[data_frame({"choices": []}), done_frame()],
        source="recorded",
    )
    source = tmp_path / "artifacts" / "promote-me.json"
    source.parent.mkdir()
    source.write_text(json.dumps(cassette.raw), encoding="utf-8")
    fixture_root = tmp_path / "fixtures"

    target = promote_recorded_cassette(
        source,
        fixture_root=fixture_root,
        destination="recorded/openai_chat/promote-me.json",
    )

    assert target == fixture_root / "recorded/openai_chat/promote-me.json"
    assert load_cassette(target).metadata["source"] == "recorded"
    with pytest.raises(ModelStreamAssetError, match="recorded/"):
        promote_recorded_cassette(
            source,
            fixture_root=fixture_root,
            destination="handwritten/openai_chat/promote-me.json",
        )
