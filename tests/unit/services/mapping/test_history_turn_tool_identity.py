from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.services.mapping.itemized.history_turns.detail import build_detail
from app.services.mapping.itemized.history_turns.tools import tool_items

NOW = datetime(2026, 10, 16, tzinfo=UTC)


def _tool_projection(*items: dict[str, object]) -> dict[str, object]:
    return {
        "status": "completed",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "tool_items": list(items),
    }


def _build_detail(
    *items: dict[str, object],
    tool_call_ids: frozenset[str] | None = None,
):
    return build_detail(
        "session-1",
        1,
        [],
        turn_id="turn-1",
        job_id="job-1",
        root_logical_item_ordinal=0,
        source_view_id="view-1",
        source_view_revision=1,
        projection=_tool_projection(*items),
        tool_call_ids=tool_call_ids,
        include_final=False,
    )


@pytest.mark.parametrize(
    ("result_name", "call_names"),
    [
        ("read_file", ["read_file", "read_file"]),
        ("other_tool", ["read_file"]),
    ],
)
def test_tool_result_without_id_is_rejected_instead_of_fifo_pairing(
    result_name: str,
    call_names: list[str],
) -> None:
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"id": f"call-{index}", "name": name, "args": {}}
                for index, name in enumerate(call_names)
            ],
        ),
        ToolMessage(content="result", tool_call_id="", name=result_name),
    ]

    with pytest.raises(ValueError, match="工具结果缺少非空 tool_call_id"):
        tool_items(
            "session-1",
            "turn-1",
            messages,
            job_id="job-1",
            fallback_timestamp=NOW,
        )


def test_ai_tool_call_without_id_is_rejected_instead_of_synthesizing_one() -> None:
    # 保留损坏历史原件：正常 AIMessage 构造器会先拒绝缺 ID 的工具调用。
    messages = [
        AIMessage.model_construct(
            content="",
            response_metadata={},
            tool_calls=[{"name": "read_file", "args": {}}],
        )
    ]

    with pytest.raises(ValueError, match="AI 工具调用缺少非空 tool_call_id"):
        tool_items(
            "session-1",
            "turn-1",
            messages,
            job_id="job-1",
            fallback_timestamp=NOW,
        )


@pytest.mark.parametrize(
    "items",
    [
        ({"item_kind": "tool_call", "tool_name": "read_file"},),
        (
            {
                "item_kind": "tool_call",
                "tool_call_id": "call-1",
                "tool_name": "read_file",
            },
            {"item_kind": "tool_result", "tool_call_id": "", "tool_name": "read_file"},
        ),
    ],
)
def test_projected_tool_item_without_id_is_rejected_instead_of_fifo_or_synthesis(
    items: tuple[dict[str, object], ...],
) -> None:
    with pytest.raises(ValueError, match="索引工具项缺少非空 tool_call_id"):
        _build_detail(*items)


def test_reused_raw_id_keeps_model_call_scope_and_exact_name_enrichment() -> None:
    messages = [
        AIMessage(
            content="",
            response_metadata={"model_call_id": "model-a"},
            tool_calls=[{"id": "reused", "name": "read_a", "args": {}}],
        ),
        AIMessage(
            content="",
            response_metadata={"model_call_id": "model-b"},
            tool_calls=[{"id": "reused", "name": "read_b", "args": {}}],
        ),
        ToolMessage(
            content="result-a",
            tool_call_id="reused",
            response_metadata={"model_call_id": "model-a"},
        ),
        ToolMessage(
            content="result-b",
            tool_call_id="reused",
            response_metadata={"model_call_id": "model-b"},
        ),
    ]

    items = tool_items(
        "session-1",
        "turn-1",
        messages,
        job_id="job-1",
        fallback_timestamp=NOW,
    )

    assert [item.part_id for item in items] == [
        "model-a:tool-call:reused",
        "model-b:tool-call:reused",
        "model-a:tool-call:reused",
        "model-b:tool-call:reused",
    ]
    assert [item.tool_name for item in items] == ["read_a", "read_b", "read_a", "read_b"]


def test_tool_call_selector_keeps_reused_raw_id_within_model_call_scope() -> None:
    messages = [
        AIMessage(
            content="",
            response_metadata={"model_call_id": "model-a"},
            tool_calls=[{"id": "reused", "name": "read_a", "args": {}}],
        ),
        AIMessage(
            content="",
            response_metadata={"model_call_id": "model-b"},
            tool_calls=[{"id": "reused", "name": "read_b", "args": {}}],
        ),
        ToolMessage(
            content="result-a",
            tool_call_id="reused",
            response_metadata={"model_call_id": "model-a"},
        ),
        ToolMessage(
            content="result-b",
            tool_call_id="reused",
            response_metadata={"model_call_id": "model-b"},
        ),
    ]

    items = tool_items(
        "session-1",
        "turn-1",
        messages,
        job_id="job-1",
        fallback_timestamp=NOW,
        tool_call_ids=frozenset({"model-b:tool-call:reused"}),
    )

    assert [item.part_id for item in items] == [
        "model-b:tool-call:reused",
        "model-b:tool-call:reused",
    ]


def test_tool_call_selector_skips_unselected_malformed_calls_and_results() -> None:
    messages = [
        AIMessage.model_construct(
            content="",
            response_metadata={},
            tool_calls=[
                {"name": "broken", "args": {}},
                {"id": "target", "name": "read_file", "args": {}},
                {"id": "other", "name": "write_file", "args": {}},
            ],
        ),
        ToolMessage(content="broken result", tool_call_id=""),
        ToolMessage(content="target result", tool_call_id="target"),
    ]

    items = tool_items(
        "session-1",
        "turn-1",
        messages,
        job_id="job-1",
        fallback_timestamp=NOW,
        tool_call_ids=frozenset({"target"}),
    )

    assert [item.type for item in items] == ["tool_call_start", "tool_call_end"]
    assert [item.part_id for item in items] == ["target", "target"]


def test_projected_tool_selector_skips_malformed_unselected_items() -> None:
    detail = _build_detail(
        {"item_kind": "tool_call", "tool_name": "broken"},
        {
            "item_kind": "tool_call",
            "tool_call_id": "reused",
            "model_call_id": "model-a",
            "tool_name": "other_scope",
        },
        {
            "item_kind": "tool_result",
            "tool_call_id": "reused",
            "model_call_id": "model-b",
            "tool_name": "selected",
        },
        tool_call_ids=frozenset({"model-b:tool-call:reused"}),
    )

    assert [item.tool_name for item in detail.tool_summary] == ["selected"]
    assert [item.part_id for item in detail.items] == ["model-b:tool-call:reused"]
