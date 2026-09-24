from __future__ import annotations

from app.services.infrastructure.rollout_context.storage.catalog.turn_projection_helpers import (
    _final_reasoning_source_refs,
    _finalize_activity_projection,
    _logical_activity_key,
    _source_ref_matches,
)


def test_plain_turn_has_zero_expandable_items() -> None:
    projection: dict[str, object] = {
        "created_at": "2026-09-09T00:00:00+00:00",
        "activity_items": [],
        "activity_stats": {
            "duration_ms": 7100,
            "item_count": 99,
            "first_item_sequence": 1,
            "last_item_sequence": 9,
        },
    }

    _finalize_activity_projection(projection)

    assert projection["activity_stats"] == {
        "duration_ms": 7100,
        "item_count": 0,
        "first_item_sequence": None,
        "last_item_sequence": None,
    }


def test_activity_projection_counts_identity_order_and_item_elapsed_time() -> None:
    activity_items = [
        {
            "item_id": "item-reasoning-1",
            "item_sequence": 2,
            "kind": "reasoning",
            "text": "相同正文",
            "created_at": "2026-09-09T00:00:01+00:00",
        },
        {
            "item_id": "item-tool-call",
            "item_sequence": 3,
            "kind": "tool_call",
            "tool_call_id": "call-1",
            "tool_name": "read_file",
            "status": "completed",
            "message_sequence": 5,
            "assistant_message_sequence": 5,
            "call_index": 0,
            "created_at": "2026-09-09T00:00:03+00:00",
        },
        {
            "item_id": "item-tool-result",
            "item_sequence": 4,
            "kind": "tool_result",
            "tool_call_id": "call-1",
            "tool_name": "read_file",
            "status": "completed",
            "message_sequence": 6,
            "assistant_message_sequence": 5,
            "result_message_sequence": 6,
            "call_index": 0,
            "created_at": "2026-09-09T00:00:04+00:00",
        },
        {
            "item_id": "item-reasoning-2",
            "item_sequence": 7,
            "kind": "reasoning",
            "text": "相同正文",
            "created_at": "2026-09-09T00:00:07+00:00",
        },
    ]
    projection: dict[str, object] = {
        "created_at": "2026-09-09T00:00:00+00:00",
        "activity_items": activity_items,
        "activity_stats": {
            "duration_ms": 7000,
            "item_count": 10,
            "first_item_sequence": None,
            "last_item_sequence": None,
        },
    }

    _finalize_activity_projection(projection)

    assert projection["activity_stats"] == {
        "duration_ms": 7000,
        "item_count": 4,
        "first_item_sequence": 2,
        "last_item_sequence": 7,
    }
    assert [item["elapsed_ms"] for item in activity_items] == [1000, 2000, 1000, 3000]
    assert [item["text"] for item in activity_items if item["kind"] == "reasoning"] == [
        "相同正文",
        "相同正文",
    ]


def test_checkpoint_shadow_uses_producer_identity_instead_of_text() -> None:
    provider = {
        "kind": "reasoning",
        "producer_ref": {"producer_id": "model-call-1"},
        "block_ordinal": 0,
    }
    checkpoint_shadow = {
        "kind": "reasoning",
        "producer_ref": {"producer_id": "lc_run--model-call-1"},
        "block_ordinal": 0,
    }
    different_model_call = {
        "kind": "reasoning",
        "producer_ref": {"producer_id": "model-call-2"},
        "block_ordinal": 0,
    }

    assert _logical_activity_key(provider) == _logical_activity_key(checkpoint_shadow)
    assert _logical_activity_key(provider) != _logical_activity_key(different_model_call)


def test_distinct_persisted_reasoning_blocks_keep_identity_with_same_block_ordinal() -> None:
    first = {
        "kind": "reasoning",
        "producer_ref": {"producer_id": "model-call-1"},
        "block_ordinal": 0,
        "block_id": "part-before-tool",
    }
    second = {
        "kind": "reasoning",
        "producer_ref": {"producer_id": "model-call-1"},
        "block_ordinal": 0,
        "block_id": "part-after-tool",
    }

    assert _logical_activity_key(first) != _logical_activity_key(second)


def test_reused_tool_call_id_keeps_distinct_model_call_provenance() -> None:
    first = {
        "kind": "tool_call",
        "tool_call_id": "reused",
        "producer_ref": {"producer_id": "model-call-1"},
        "call_index": 0,
    }
    second = {
        "kind": "tool_call",
        "tool_call_id": "reused",
        "producer_ref": {"producer_id": "model-call-2"},
        "call_index": 0,
    }

    assert _logical_activity_key(first) != _logical_activity_key(second)


def test_checkpoint_tool_shadow_uses_model_call_provenance() -> None:
    provider = {
        "kind": "tool_call",
        "tool_call_id": "call-1",
        "producer_ref": {"producer_id": "model-call-1"},
        "call_index": 0,
    }
    checkpoint_shadow = {
        "kind": "tool_call",
        "tool_call_id": "call-1",
        "producer_ref": {"producer_id": "lc_run--model-call-1"},
        "call_index": 0,
    }

    assert _logical_activity_key(provider) == _logical_activity_key(checkpoint_shadow)


def test_final_reasoning_resolves_preserved_content_part_identity() -> None:
    refs = _final_reasoning_source_refs(
        {
            "content_part_refs": [
                {"id": "part-reasoning", "index": 0, "type": "reasoning"},
                {"id": "part-text", "index": 1, "type": "text"},
            ]
        },
        content_block_index=0,
        item_index=0,
        provider_item_id=None,
    )

    assert refs == {"part-reasoning"}


def test_final_reasoning_keeps_distinct_provider_item_identity() -> None:
    refs = _final_reasoning_source_refs(
        {
            "content_part_refs": [
                {"id": "outer-reasoning", "index": 0, "type": "reasoning_items"}
            ]
        },
        content_block_index=0,
        item_index=1,
        provider_item_id="provider-reasoning-2",
    )

    assert refs == {"provider-reasoning-2"}


def test_final_reasoning_matches_raw_part_against_scoped_block_identity() -> None:
    assert _source_ref_matches(
        "part-after-tool",
        {"model-call-2:block:part-after-tool"},
    )
