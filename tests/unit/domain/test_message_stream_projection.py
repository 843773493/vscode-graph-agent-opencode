from copy import deepcopy

from app.domain.message_stream.projection import (
    apply_event,
    backfill_lifecycle_metadata,
)


def test_tool_argument_deltas_replay_without_mutating_prior_state() -> None:
    first = apply_event({}, {"type": "tool_call.delta", "event_seq": 1, "payload": {
        "tool_call_id": "call", "tool_name": "read_file", "arguments_delta": "{", "arguments_complete": False,
    }})
    second = apply_event(first, {"type": "tool_call.delta", "event_seq": 2, "payload": {
        "tool_call_id": "call", "arguments_delta": "}", "arguments": {}, "arguments_complete": True,
    }})
    assert first["tool_calls"][0]["arguments"] == {"raw": "{"}
    assert second["tool_calls"][0]["arguments"] == {}
    assert second["tool_calls"][0]["tool_name"] == "read_file"
    assert "arguments_delta" not in second["tool_calls"][0]


def _block_delta(block_id: str, text: str, event_seq: int) -> dict[str, object]:
    return {
        "type": "block.delta",
        "event_seq": event_seq,
        "emitted_at": f"2026-10-24T00:00:{event_seq:02d}Z",
        "payload": {
            "block_id": block_id,
            "operation": "append",
            "text": text,
        },
    }


def test_block_delta_copies_target_and_preserves_previous_snapshots() -> None:
    initial = {
        "blocks": [
            {
                "block_id": "block_a",
                "status": "running",
                "text": "a",
                "items": [{"id": "item_a", "value": {"parts": [1]}}],
            },
            {
                "block_id": "block_b",
                "status": "running",
                "text": "b",
                "items": [],
            },
        ],
        "tool_calls": [{"tool_call_id": "call_1", "arguments": {"x": 1}}],
        "model_calls": [{"model_call_id": "model_1", "status": "completed"}],
    }
    original = deepcopy(initial)

    after_a = apply_event(initial, _block_delta("block_a", "1", 1))

    assert initial == original
    assert after_a is not initial
    assert after_a["blocks"] is not initial["blocks"]
    assert after_a["blocks"][0] is not initial["blocks"][0]
    assert after_a["blocks"][0]["items"] is not initial["blocks"][0]["items"]
    assert after_a["blocks"][1] is initial["blocks"][1]
    assert after_a["tool_calls"] is initial["tool_calls"]
    assert after_a["model_calls"] is initial["model_calls"]
    assert after_a["blocks"][0]["text"] == "a1"

    after_b = apply_event(after_a, _block_delta("block_b", "2", 2))

    assert after_a["blocks"][1]["text"] == "b"
    assert after_b["blocks"][0] is after_a["blocks"][0]
    assert after_b["blocks"][1] is not after_a["blocks"][1]
    assert after_b["blocks"][1]["text"] == "b2"


def test_backfill_restores_lifecycles_without_overwriting_later_metadata() -> None:
    state = {}
    events = []
    for collection, identity_key, started, completed in (
        ("model_calls", "model_call_id", "model.started", "model.failed"),
        ("blocks", "block_id", "block.started", "block.completed"),
        ("tool_calls", "tool_call_id", "tool_call", "tool_call.completed"),
        ("tool_executions", "tool_execution_id", "tool.started", "tool.completed"),
        ("activities", "activity_id", "activity.started", "activity.failed"),
    ):
        # 同名实体属于各自集合；重复 ID 仍只更新首个实体。
        state[collection] = [{identity_key: "shared"}, {identity_key: "shared"}]
        for seq, kind in ((1, started), (2, completed)):
            events.append({
                "type": kind, "event_seq": seq, "emitted_at": f"time-{seq}",
                "payload": {identity_key: "shared"},
            })
    state["blocks"][0].update(last_event_seq=10, updated_at="later")

    backfill_lifecycle_metadata(state, events)

    for collection, entities in state.items():
        first, duplicate = entities
        assert first["started_seq"] == 1
        assert first["started_at"] == "time-1"
        assert first["completed_seq"] == 2
        assert first["completed_at"] == "time-2"
        assert first["last_event_seq"] == (10 if collection == "blocks" else 2)
        assert first["updated_at"] == ("later" if collection == "blocks" else "time-2")
        assert "started_seq" not in duplicate

    backfill_lifecycle_metadata(state, [{
        "type": "stream.interrupted", "event_seq": 11,
        "emitted_at": "terminal", "payload": {},
    }])
    for entities in state.values():
        assert entities[0]["completed_seq"] == 2
        assert entities[1]["completed_seq"] == 11
        assert entities[1]["completed_at"] == "terminal"
