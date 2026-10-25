from copy import deepcopy

from app.domain.message_stream.projection import apply_event


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
