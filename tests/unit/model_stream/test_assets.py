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
        "responses-reasoning-parallel-tool",
        "anthropic-reasoning-stream",
        "anthropic-reasoning-tool",
    )

    for scenario_id in scenario_ids:
        scenario = load_scenario(FIXTURE_ROOT, scenario_id)
        assert scenario.cassette.kind == "model_stream_cassette"
        assert scenario.cassette.metadata["source"] == "handwritten"
        protocol = scenario.cassette.metadata["protocol"]
        assert protocol in {
            "openai_chat_sse",
            "openai_responses_sse",
            "anthropic_messages_sse",
        }
        terminal = scenario.cassette.interactions[0].response.frames[-1]
        if protocol == "openai_chat_sse":
            assert terminal.payload == "[DONE]"
        elif protocol == "openai_responses_sse":
            assert terminal.event == "response.completed"
            assert terminal.payload["type"] == "response.completed"
        else:
            assert terminal.event == "message_stop"
            assert terminal.payload["type"] == "message_stop"


@pytest.mark.parametrize(
    ("scenario_id", "protocol", "reasoning_deltas", "visible_deltas"),
    [
        ("reasoning-stream", "openai_chat_sse", 4, 3),
        ("responses-reasoning-text", "openai_responses_sse", 4, 3),
        ("anthropic-reasoning-stream", "anthropic_messages_sse", 4, 3),
    ],
)
def test_handwritten_provider_samples_keep_complete_multi_delta_streams(
    scenario_id: str,
    protocol: str,
    reasoning_deltas: int,
    visible_deltas: int,
) -> None:
    scenario = load_scenario(FIXTURE_ROOT, scenario_id)
    assert scenario.cassette.metadata["protocol"] == protocol
    frames = scenario.cassette.interactions[0].response.frames

    if protocol == "openai_chat_sse":
        reasoning_count = sum(
            1
            for frame in frames
            if isinstance(frame.payload, dict)
            and isinstance(frame.payload.get("choices"), list)
            and frame.payload["choices"]
            and isinstance(frame.payload["choices"][0], dict)
            and isinstance(frame.payload["choices"][0].get("delta"), dict)
            and frame.payload["choices"][0]["delta"].get("reasoning_content")
        )
        visible_count = sum(
            1
            for frame in frames
            if isinstance(frame.payload, dict)
            and isinstance(frame.payload.get("choices"), list)
            and frame.payload["choices"]
            and isinstance(frame.payload["choices"][0], dict)
            and isinstance(frame.payload["choices"][0].get("delta"), dict)
            and frame.payload["choices"][0]["delta"].get("content")
        )
    elif protocol == "openai_responses_sse":
        reasoning_count = sum(
            1
            for frame in frames
            if frame.event == "response.reasoning_summary_text.delta"
        )
        visible_count = sum(
            1 for frame in frames if frame.event == "response.output_text.delta"
        )
    else:
        reasoning_count = sum(
            1
            for frame in frames
            if isinstance(frame.payload, dict)
            and isinstance(frame.payload.get("delta"), dict)
            and frame.payload["delta"].get("type") == "thinking_delta"
        )
        visible_count = sum(
            1
            for frame in frames
            if isinstance(frame.payload, dict)
            and isinstance(frame.payload.get("delta"), dict)
            and frame.payload["delta"].get("type") == "text_delta"
        )

    assert reasoning_count >= reasoning_deltas
    assert visible_count >= visible_deltas


@pytest.mark.parametrize(
    ("scenario_id", "protocol"),
    [
        ("reasoning-tool", "openai_chat_sse"),
        ("responses-reasoning-tool", "openai_responses_sse"),
        ("anthropic-reasoning-tool", "anthropic_messages_sse"),
    ],
)
def test_handwritten_tool_samples_cover_reasoning_arguments_and_final_deltas(
    scenario_id: str,
    protocol: str,
) -> None:
    scenario = load_scenario(FIXTURE_ROOT, scenario_id)
    assert scenario.cassette.metadata["protocol"] == protocol
    first, final = scenario.cassette.interactions

    if protocol == "openai_chat_sse":
        first_deltas = [
            frame.payload["choices"][0]["delta"]
            for frame in first.response.frames
            if isinstance(frame.payload, dict)
            and isinstance(frame.payload.get("choices"), list)
            and frame.payload["choices"]
            and isinstance(frame.payload["choices"][0], dict)
            and isinstance(frame.payload["choices"][0].get("delta"), dict)
        ]
        final_deltas = [
            frame.payload["choices"][0]["delta"]
            for frame in final.response.frames
            if isinstance(frame.payload, dict)
            and isinstance(frame.payload.get("choices"), list)
            and frame.payload["choices"]
            and isinstance(frame.payload["choices"][0], dict)
            and isinstance(frame.payload["choices"][0].get("delta"), dict)
        ]
        assert sum("reasoning_content" in delta for delta in first_deltas) >= 4
        assert sum("arguments" in call.get("function", {}) for delta in first_deltas for call in delta.get("tool_calls", [])) >= 3
        assert sum("reasoning_content" in delta for delta in final_deltas) >= 3
        assert sum("content" in delta for delta in final_deltas) >= 2
    elif protocol == "openai_responses_sse":
        assert sum(
            frame.event == "response.reasoning_summary_text.delta"
            for frame in first.response.frames
        ) >= 4
        assert sum(
            frame.event == "response.function_call_arguments.delta"
            for frame in first.response.frames
        ) >= 3
        assert sum(
            frame.event == "response.reasoning_summary_text.delta"
            for frame in final.response.frames
        ) >= 3
        assert sum(
            frame.event == "response.output_text.delta"
            for frame in final.response.frames
        ) >= 3
    else:
        assert sum(
            isinstance(frame.payload, dict)
            and isinstance(frame.payload.get("delta"), dict)
            and frame.payload["delta"].get("type") == "thinking_delta"
            for frame in first.response.frames
        ) >= 4
        assert sum(
            isinstance(frame.payload, dict)
            and isinstance(frame.payload.get("delta"), dict)
            and frame.payload["delta"].get("type") == "input_json_delta"
            for frame in first.response.frames
        ) >= 3
        assert sum(
            isinstance(frame.payload, dict)
            and isinstance(frame.payload.get("delta"), dict)
            and frame.payload["delta"].get("type") == "thinking_delta"
            for frame in final.response.frames
        ) >= 3
        assert sum(
            isinstance(frame.payload, dict)
            and isinstance(frame.payload.get("delta"), dict)
            and frame.payload["delta"].get("type") == "text_delta"
            for frame in final.response.frames
        ) >= 3


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


def test_responses_parallel_tool_asset_preserves_interleaved_item_identity() -> None:
    scenario = load_scenario(FIXTURE_ROOT, "responses-reasoning-parallel-tool")
    first = scenario.cassette.interactions[0].response.frames

    added = [
        frame
        for frame in first
        if frame.event == "response.output_item.added"
        and isinstance(frame.payload, dict)
        and isinstance(frame.payload.get("item"), dict)
        and frame.payload["item"].get("type") == "function_call"
    ]
    assert [frame.payload["output_index"] for frame in added] == [1, 2]
    assert [frame.payload["item"]["id"] for frame in added] == [
        "fc_parallel_one",
        "fc_parallel_two",
    ]

    deltas = [
        frame
        for frame in first
        if frame.event == "response.function_call_arguments.delta"
    ]
    assert [frame.payload["item_id"] for frame in deltas] == [
        "fc_parallel_one",
        "fc_parallel_two",
        "fc_parallel_one",
        "fc_parallel_two",
    ]
    assert [frame.payload["output_index"] for frame in deltas] == [1, 2, 1, 2]
