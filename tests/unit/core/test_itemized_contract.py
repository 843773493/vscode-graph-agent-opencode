from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.domain.itemized.assembly_snapshot import (
    context_request_hash,
)
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SelectionKind,
    SemanticKind,
)
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    content_hash,
    contribution_content_hash,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.domain.itemized.selection import ContextSelectionEntry
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.migration.legacy_adapter import (
    LegacyRolloutAdapter,
)
from app.services.infrastructure.rollout_context.provider.native_request import (
    project_native_request,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    default_idempotency_key,
)
from app.services.mapping.itemized.history import project_history_plan
from app.services.mapping.itemized.langchain import (
    project_canonical_items,
    project_context_plan,
)


def _assistant_tool_item(
    *,
    item_sequence: int,
    item_id: str,
    group_id: str,
    tool_call_id: str,
    checkpoint: bool,
    reasoning: str,
) -> tuple[CanonicalItemRecord, CanonicalItemRecord]:
    metadata: dict[str, object] = {}
    if checkpoint:
        metadata = {
            "execution_confirmed": True,
            "projection_message_id": group_id,
            "projection_group": {"ordinal": 0, "size": 2, "content_form": "list"},
        }
    reasoning_item = CanonicalItemRecord.create(
        item_sequence=item_sequence,
        item_id=f"{item_id}-reasoning",
        semantic_kind=SemanticKind.REASONING,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": item_id,
            "invocation_id": "turn-tool",
        },
        payload=reasoning,
        metadata=metadata,
        turn_id="turn-tool",
        turn_scope="turn_member",
        message_group_id=group_id,
        wire_role="assistant",
    )
    tool_metadata = dict(metadata)
    if checkpoint:
        tool_metadata["projection_group"] = {
            "ordinal": 1,
            "size": 2,
            "content_form": "list",
        }
    else:
        tool_metadata.update(block_id=tool_call_id, block_index=1)
    tool_item = CanonicalItemRecord.create(
        item_sequence=item_sequence + 1,
        item_id=f"{item_id}-tool",
        semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind=PayloadKind.TOOL_CALL,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref=reasoning_item.producer_ref,
        payload={"tool_call_id": tool_call_id, "name": "ls", "args": {}},
        metadata=tool_metadata,
        turn_id="turn-tool",
        turn_scope="turn_member",
        message_group_id=group_id,
        wire_role="assistant",
    )
    return reasoning_item, tool_item


def test_provider_projection_prefers_complete_checkpoint_tool_carrier() -> None:
    live = _assistant_tool_item(
        item_sequence=1,
        item_id="live",
        group_id="message-live",
        tool_call_id="call-shared",
        checkpoint=False,
        reasoning="最后一个增量片段",
    )
    checkpoint = _assistant_tool_item(
        item_sequence=3,
        item_id="checkpoint",
        group_id="message-checkpoint",
        tool_call_id="call-shared",
        checkpoint=True,
        reasoning="checkpoint 中完整的思考正文",
    )

    messages = project_canonical_items((*live, *checkpoint))

    assert len(messages) == 1
    assert isinstance(messages[0], AIMessage)
    assert messages[0].content == [
        {
            "type": "reasoning",
            "text": "checkpoint 中完整的思考正文",
            "item_id": "checkpoint-reasoning",
        }
    ]
    assert [call["id"] for call in messages[0].tool_calls] == ["call-shared"]


def test_provider_projection_does_not_deduplicate_same_text_without_tool_identity() -> None:
    live = _assistant_tool_item(
        item_sequence=1,
        item_id="live-distinct",
        group_id="message-live-distinct",
        tool_call_id="call-live",
        checkpoint=False,
        reasoning="正文相同也不能作为身份",
    )
    checkpoint = _assistant_tool_item(
        item_sequence=3,
        item_id="checkpoint-distinct",
        group_id="message-checkpoint-distinct",
        tool_call_id="call-checkpoint",
        checkpoint=True,
        reasoning="正文相同也不能作为身份",
    )

    messages = project_canonical_items((*live, *checkpoint))

    assert len(messages) == 2
    assert all(isinstance(message, AIMessage) for message in messages)
    assert [call["id"] for message in messages for call in message.tool_calls] == [
        "call-live",
        "call-checkpoint",
    ]


def test_provider_projection_splits_legacy_execution_stream_group_by_model_call() -> None:
    stream_first = tuple(
        replace(
            item,
            metadata={**item.metadata, "model_call_id": "model-call-1"},
        )
        for item in _assistant_tool_item(
            item_sequence=1,
            item_id="stream-first",
            group_id="message-shared-execution",
            tool_call_id="call-first",
            checkpoint=False,
            reasoning="第一次调用的增量",
        )
    )
    stream_second = tuple(
        replace(
            item,
            metadata={**item.metadata, "model_call_id": "model-call-2"},
        )
        for item in _assistant_tool_item(
            item_sequence=3,
            item_id="stream-second",
            group_id="message-shared-execution",
            tool_call_id="call-second",
            checkpoint=False,
            reasoning="第二次调用的增量",
        )
    )
    checkpoint_first = _assistant_tool_item(
        item_sequence=5,
        item_id="checkpoint-first",
        group_id="message-checkpoint-first",
        tool_call_id="call-first",
        checkpoint=True,
        reasoning="第一次调用的完整 carrier",
    )
    checkpoint_second = _assistant_tool_item(
        item_sequence=7,
        item_id="checkpoint-second",
        group_id="message-checkpoint-second",
        tool_call_id="call-second",
        checkpoint=True,
        reasoning="第二次调用的完整 carrier",
    )

    messages = project_canonical_items(
        (*stream_first, *stream_second, *checkpoint_first, *checkpoint_second)
    )

    assert len(messages) == 2
    assert all(isinstance(message, AIMessage) for message in messages)
    assert [
        call["id"] for message in messages for call in message.tool_calls
    ] == ["call-first", "call-second"]


def test_provider_projection_deduplicates_stream_parts_against_final_carrier() -> None:
    checkpoint = _assistant_tool_item(
        item_sequence=1,
        item_id="checkpoint-carrier",
        group_id="message-checkpoint-carrier",
        tool_call_id="call-final-carrier",
        checkpoint=True,
        reasoning="工具调用前的 reasoning",
    )
    tool_result = CanonicalItemRecord.create(
        item_sequence=3,
        item_id="tool-result-final-carrier",
        semantic_kind=SemanticKind.TOOL_RESULT,
        payload_kind=PayloadKind.TOOL_RESULT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "tool",
            "producer_id": "tool-result-final-carrier",
            "invocation_id": "turn-final-carrier",
        },
        payload={
            "content": "工具结果",
            "name": "ls",
            "result_id": "result-final-carrier",
            "tool_call_id": "call-final-carrier",
            "tool_outcome": "success",
        },
        metadata={"execution_confirmed": True},
        turn_id="turn-tool",
        turn_scope="turn_member",
        wire_role="tool",
    )
    stream_reasoning = CanonicalItemRecord.create(
        item_sequence=4,
        item_id="stream-after-tool-reasoning",
        semantic_kind=SemanticKind.REASONING,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "model-call-after-tool",
            "invocation_id": "turn-tool",
        },
        payload="工具返回后的 reasoning",
        metadata={
            "block_id": "model-call-after-tool:block:part-after-tool-reasoning",
            "block_index": 0,
            "model_call_id": "model-call-after-tool",
        },
        turn_id="turn-tool",
        turn_scope="turn_member",
        message_group_id="message-stream-after-tool",
        wire_role="assistant",
    )
    stream_text = CanonicalItemRecord.create(
        item_sequence=5,
        item_id="stream-after-tool-text",
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref=stream_reasoning.producer_ref,
        payload="最终文本",
        metadata={
            "block_id": "model-call-after-tool:block:part-after-tool-text",
            "block_index": 1,
            "model_call_id": "model-call-after-tool",
        },
        turn_id="turn-tool",
        turn_scope="turn_member",
        message_group_id="message-stream-after-tool",
        wire_role="assistant",
    )
    checkpoint_shadow = CanonicalItemRecord.create(
        item_sequence=6,
        item_id="checkpoint-shadow-after-tool",
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "lc-run-after-tool",
            "invocation_id": "turn-tool",
        },
        payload=[
            {"reasoning_content": "工具返回后的 reasoning", "type": "reasoning_content"},
            {"text": "最终文本", "type": "text"},
        ],
        metadata={
            "execution_confirmed": True,
            "projection_message_id": "lc-run-after-tool",
            "model_call_id": "model-call-after-tool",
        },
        turn_id="turn-tool",
        turn_scope="turn_member",
        message_group_id="message-checkpoint-shadow-after-tool",
        wire_role="assistant",
    )
    final_carrier = CanonicalItemRecord.create(
        item_sequence=7,
        item_id="final-carrier-after-tool",
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "final-carrier-after-tool",
            "invocation_id": "turn-tool",
        },
        payload=checkpoint_shadow.payload,
        metadata={
            "content_part_refs": [
                {"id": "part-after-tool-reasoning", "index": 0},
                {"id": "part-after-tool-text", "index": 1},
            ],
            "supersedes_message_id": "lc-run-after-tool",
            "execution_confirmed": True,
        },
        turn_id="turn-tool",
        turn_scope="turn_member",
        message_group_id="message-final-carrier-after-tool",
        wire_role="assistant",
    )

    messages = project_canonical_items(
        (*checkpoint, tool_result, stream_reasoning, stream_text, checkpoint_shadow, final_carrier)
    )

    assert [message.type for message in messages] == ["ai", "tool", "ai"]
    assert messages[0].tool_calls[0]["id"] == "call-final-carrier"
    assert messages[2].content == [
        {"reasoning_content": "工具返回后的 reasoning", "type": "reasoning_content"},
        {"text": "最终文本", "type": "text"},
    ]


def test_provider_projection_does_not_remove_reused_part_id_from_another_model_call() -> None:
    def stream_item(
        *, item_sequence: int, item_id: str, model_call_id: str, text: str
    ) -> CanonicalItemRecord:
        return CanonicalItemRecord.create(
            item_sequence=item_sequence,
            item_id=item_id,
            semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
            payload_kind=PayloadKind.TEXT,
            status=CanonicalItemStatus.COMPLETED,
            producer_ref={
                "producer_kind": "provider",
                "producer_id": model_call_id,
                "invocation_id": "turn-reused-part-id",
            },
            payload=text,
            metadata={
                "block_id": f"{model_call_id}:block:part-reused",
                "block_index": 0,
                "model_call_id": model_call_id,
            },
            turn_id="turn-reused-part-id",
            turn_scope="turn_member",
            message_group_id=f"message-{model_call_id}",
            wire_role="assistant",
        )

    first_call = stream_item(
        item_sequence=1,
        item_id="stream-first-reused-part",
        model_call_id="model-call-first",
        text="第一调用的 shadow",
    )
    second_call = stream_item(
        item_sequence=2,
        item_id="stream-second-reused-part",
        model_call_id="model-call-second",
        text="第二调用的独立内容",
    )
    final_carrier = CanonicalItemRecord.create(
        item_sequence=3,
        item_id="final-reused-part-carrier",
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "final-reused-part-carrier",
            "invocation_id": "turn-reused-part-id",
        },
        payload=[{"text": "最终内容", "type": "text"}],
        metadata={
            "content_part_refs": [{"id": "part-reused", "index": 0}],
            "execution_confirmed": True,
        },
        turn_id="turn-reused-part-id",
        turn_scope="turn_member",
        message_group_id="message-final-reused-part-carrier",
        wire_role="assistant",
    )

    messages = project_canonical_items((first_call, second_call, final_carrier))

    assert [message.content for message in messages] == [
        "第一调用的 shadow",
        "第二调用的独立内容",
        [{"text": "最终内容", "type": "text"}],
    ]


def test_provider_projection_uses_superseded_checkpoint_identity_without_loading_shadow() -> None:
    """最终 carrier 自带 lc_run producer identity 时，不依赖未选中的 checkpoint item。"""
    model_call_id = "model-call-selected-only"
    stream = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="selected-stream-shadow",
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": model_call_id,
            "invocation_id": "turn-selected-only",
        },
        payload="stream shadow",
        metadata={
            "model_call_id": model_call_id,
            "block_id": f"{model_call_id}:block:part-selected",
            "block_index": 0,
        },
        turn_id="turn-selected-only",
        turn_scope="turn_member",
        message_group_id="message-selected-only-stream",
        wire_role="assistant",
    )
    final = CanonicalItemRecord.create(
        item_sequence=2,
        item_id="selected-final-carrier",
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "final-selected-only",
            "invocation_id": "turn-selected-only",
        },
        payload=[{"text": "final", "type": "text"}],
        metadata={
            "content_part_refs": [{"id": "part-selected", "index": 0}],
            "supersedes_message_id": f"lc_run--{model_call_id}",
            "execution_confirmed": True,
        },
        turn_id="turn-selected-only",
        turn_scope="turn_member",
        message_group_id="message-selected-only-final",
        wire_role="assistant",
    )

    messages = project_canonical_items((stream, final))

    assert [message.content for message in messages] == [
        [{"text": "final", "type": "text"}]
    ]


def test_provider_projection_deduplicates_scoped_stream_call_and_result_against_checkpoint():
    stream_call_id = "model-call-1:tool-call:call-shared"
    stream_call = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="stream-tool-call",
        semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind=PayloadKind.TOOL_CALL,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "model-call-1",
            "invocation_id": "turn-scoped-tool",
        },
        payload={
            "tool_call_id": stream_call_id,
            "name": "ls",
            "args": {"path": "."},
        },
        metadata={
            "model_call_id": "model-call-1",
            "block_id": stream_call_id,
            "block_index": 0,
        },
        turn_id="turn-scoped-tool",
        turn_scope="turn_member",
        message_group_id="message-model-call-1",
        wire_role="assistant",
    )
    stream_result = CanonicalItemRecord.create(
        item_sequence=2,
        item_id="stream-tool-result",
        semantic_kind=SemanticKind.TOOL_RESULT,
        payload_kind=PayloadKind.TOOL_RESULT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "tool",
            "producer_id": "stream-tool-result",
            "invocation_id": "turn-scoped-tool",
        },
        payload={
            "content": "stream result",
            "name": "ls",
            "result_id": "stream-result-id",
            "tool_call_id": stream_call_id,
            "tool_outcome": "success",
        },
        metadata={"model_call_id": "model-call-1", "execution_confirmed": True},
        turn_id="turn-scoped-tool",
        turn_scope="turn_member",
        wire_role="tool",
    )
    checkpoint_call = CanonicalItemRecord.create(
        item_sequence=3,
        item_id="checkpoint-tool-call",
        semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind=PayloadKind.TOOL_CALL,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "lc_run--model-call-1",
            "invocation_id": "turn-scoped-tool",
        },
        payload={
            "tool_calls": [
                {"id": "call-shared", "name": "ls", "args": {"path": "."}}
            ]
        },
        metadata={
            "execution_confirmed": True,
            "projection_message_id": "lc_run--model-call-1",
            "projection_group": {"ordinal": 0, "size": 1, "content_form": "list"},
        },
        turn_id="turn-scoped-tool",
        turn_scope="turn_member",
        message_group_id="message-lc_run--model-call-1",
        wire_role="assistant",
    )
    checkpoint_result = CanonicalItemRecord.create(
        item_sequence=4,
        item_id="checkpoint-tool-result",
        semantic_kind=SemanticKind.TOOL_RESULT,
        payload_kind=PayloadKind.TOOL_RESULT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "tool",
            "producer_id": "checkpoint-tool-result",
            "invocation_id": "turn-scoped-tool",
        },
        payload={
            "content": "checkpoint result",
            "name": "ls",
            "result_id": "checkpoint-result-id",
            "tool_call_id": "call-shared",
            "tool_outcome": "success",
        },
        metadata={
            "execution_confirmed": True,
            "projection_message_id": "checkpoint-tool-result",
        },
        turn_id="turn-scoped-tool",
        turn_scope="turn_member",
        wire_role="tool",
    )

    messages = project_canonical_items(
        (stream_call, stream_result, checkpoint_call, checkpoint_result)
    )

    assert len(messages) == 2
    assert isinstance(messages[0], AIMessage)
    assert [call["id"] for call in messages[0].tool_calls] == ["call-shared"]
    assert isinstance(messages[1], ToolMessage)
    assert messages[1].content == "checkpoint result"


def _native_tool_plan(
    *,
    session_id: str,
    plan_id: str,
    assembly_id: str,
    items: tuple[CanonicalItemRecord, ...],
    tool_set_ref: ContextRef | None = None,
) -> ContextRequestPlan:
    selection = tuple(
        ContextSelectionEntry(
            assembly_id=assembly_id,
            plan_ordinal=index,
            ref=ContextRef.canonical_item(item, session_id=session_id, thread_id="thread-1"),
            selection_kind=SelectionKind.CANONICAL_HISTORY,
            source_revision=ContextRef.canonical_item(
                item, session_id=session_id, thread_id="thread-1"
            ).source_revision,
            content_length=ContextRef.canonical_item(
                item, session_id=session_id, thread_id="thread-1"
            ).content_length,
            content_hash=ContextRef.canonical_item(
                item, session_id=session_id, thread_id="thread-1"
            ).content_hash,
            visibility="internal",
            protection="public",
            availability="available",
        )
        for index, item in enumerate(items)
    )
    refs = tuple(entry.ref for entry in selection)
    if tool_set_ref is not None:
        selection = (
            *selection,
            ContextSelectionEntry(
                assembly_id=assembly_id,
                plan_ordinal=len(selection),
                ref=tool_set_ref,
                selection_kind=SelectionKind.TOOL_SET,
                source_revision=tool_set_ref.source_revision,
                content_length=tool_set_ref.content_length,
                content_hash=tool_set_ref.content_hash,
                visibility=tool_set_ref.visibility,
                protection=tool_set_ref.protection,
                availability=tool_set_ref.availability,
            ),
        )
        refs = (*refs, tool_set_ref)
    return ContextRequestPlan(
        session_id=session_id,
        plan_id=plan_id,
        refs=refs,
    ).seal_for_assembly(assembly_id, selection=selection)


def _scoped_stream_and_checkpoint_items(
    *,
    session_id: str,
    model_call_id: str,
    tool_call_id: str,
) -> tuple[CanonicalItemRecord, ...]:
    """构造同一 Turn 的 scoped stream carrier 与其 checkpoint carrier。"""
    scoped_id = f"{model_call_id}:tool-call:{tool_call_id}"
    stream_call = CanonicalItemRecord.create(
        item_sequence=1,
        item_id=f"stream-tool-call-{tool_call_id}",
        semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind=PayloadKind.TOOL_CALL,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": model_call_id,
            "invocation_id": "turn-native-tool",
        },
        payload={"tool_call_id": scoped_id, "name": "ls", "args": {"path": "."}},
        metadata={
            "model_call_id": model_call_id,
            "block_id": scoped_id,
            "block_index": 0,
        },
        turn_id="turn-native-tool",
        turn_scope="turn_member",
        message_group_id=f"message-{model_call_id}",
        wire_role="assistant",
    )
    stream_result = CanonicalItemRecord.create(
        item_sequence=2,
        item_id=f"stream-tool-result-{tool_call_id}",
        semantic_kind=SemanticKind.TOOL_RESULT,
        payload_kind=PayloadKind.TOOL_RESULT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "tool",
            "producer_id": f"stream-tool-result-{tool_call_id}",
            "invocation_id": "turn-native-tool",
        },
        payload={
            "content": "stream result",
            "name": "ls",
            "result_id": f"stream-result-{tool_call_id}",
            "tool_call_id": scoped_id,
            "tool_outcome": "success",
        },
        metadata={"model_call_id": model_call_id, "execution_confirmed": True},
        turn_id="turn-native-tool",
        turn_scope="turn_member",
        wire_role="tool",
    )
    checkpoint_call = CanonicalItemRecord.create(
        item_sequence=3,
        item_id=f"checkpoint-tool-call-{tool_call_id}",
        semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind=PayloadKind.TOOL_CALL,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": f"lc_run--{model_call_id}",
            "invocation_id": "turn-native-tool",
        },
        payload={"tool_calls": [{"id": tool_call_id, "name": "ls", "args": {"path": "."}}]},
        metadata={
            "execution_confirmed": True,
            "projection_message_id": f"lc_run--{model_call_id}",
            "projection_group": {"ordinal": 0, "size": 1, "content_form": "list"},
        },
        turn_id="turn-native-tool",
        turn_scope="turn_member",
        message_group_id=f"message-lc_run--{model_call_id}",
        wire_role="assistant",
    )
    checkpoint_result = CanonicalItemRecord.create(
        item_sequence=4,
        item_id=f"checkpoint-tool-result-{tool_call_id}",
        semantic_kind=SemanticKind.TOOL_RESULT,
        payload_kind=PayloadKind.TOOL_RESULT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "tool",
            "producer_id": f"checkpoint-tool-result-{tool_call_id}",
            "invocation_id": "turn-native-tool",
        },
        payload={
            "content": "checkpoint result",
            "name": "ls",
            "result_id": f"checkpoint-result-{tool_call_id}",
            "tool_call_id": tool_call_id,
            "tool_outcome": "success",
        },
        metadata={
            "execution_confirmed": True,
            "projection_message_id": f"checkpoint-tool-result-{tool_call_id}",
        },
        turn_id="turn-native-tool",
        turn_scope="turn_member",
        wire_role="tool",
    )
    return (stream_call, stream_result, checkpoint_call, checkpoint_result)


def test_native_projection_restores_scoped_ids_and_drops_stream_shadow() -> None:
    """native wire 必须还原 scoped call_id 且不重复发送 stream shadow。"""
    model_call_id = "01a099c7-3bf3-7722-b46c-0ee4a7174e96"
    tool_call_id = "chatcmpl-tool-9e4b5d651f4e7a0e"
    items = _scoped_stream_and_checkpoint_items(
        session_id="session-native-scoped",
        model_call_id=model_call_id,
        tool_call_id=tool_call_id,
    )
    plan = _native_tool_plan(
        session_id="session-native-scoped",
        plan_id="plan-native-scoped",
        assembly_id="assembly-native-scoped",
        items=items,
    )

    projection = project_native_request(
        plan,
        items,
        request_only_content={},
    )

    inputs = projection["request"]["input"]
    call_ids = [
        item["call_id"] for item in inputs if item["type"] == "function_call"
    ]
    output_ids = [
        item["call_id"] for item in inputs if item["type"] == "function_call_output"
    ]
    # provider 只接受原始 ID；scoped 前缀会超出 ChatGPT 的 64 字符上限。
    assert call_ids == [tool_call_id]
    assert output_ids == [tool_call_id]
    assert all(len(call_id) <= 64 for call_id in (*call_ids, *output_ids))
    # stream shadow 的 result 不能和 checkpoint carrier 一起发送。
    outputs = [item["output"] for item in inputs if item["type"] == "function_call_output"]
    assert outputs == ["checkpoint result"]


def test_parallel_checkpoint_tool_items_keep_unique_refs_and_one_provider_message() -> None:
    """四个并行 invocation 拆成 item 后，native/chat 两条投影仍保持同一组。"""

    codec = LangChainMessageCodec()
    model_call_id = "parallel-native-model"
    calls = [
        {"id": f"parallel-call-{index}", "name": "read_file", "args": {"path": f"{index}.ts"}}
        for index in range(4)
    ]
    checkpoint_group = codec.items_for_message(
        AIMessage(
            id=f"lc_run--{model_call_id}",
            content=[{"type": "reasoning_content", "text": "并行读取"}],
            tool_calls=calls,
        ),
        item_sequence=1,
        message_id=f"lc_run--{model_call_id}",
        turn_id="parallel-native-turn",
        timestamp="2026-09-21T00:00:00+00:00",
        model_call_id=model_call_id,
    )
    results: list[CanonicalItemRecord] = []
    for index, call in enumerate(calls):
        (result,) = codec.items_for_message(
            ToolMessage(
                id=f"parallel-result-{index}",
                content=f"result-{index}",
                tool_call_id=call["id"],
                name=call["name"],
                status="success",
            ),
            item_sequence=10 + index,
            message_id=f"parallel-result-{index}",
            turn_id="parallel-native-turn",
            timestamp="2026-09-21T00:00:00+00:00",
            model_call_id=model_call_id,
        )
        results.append(result)
    items = (*checkpoint_group, *results)
    call_items = tuple(
        item for item in items if item.semantic_kind == SemanticKind.TOOL_CALL.value
    )
    assert len(call_items) == 4
    assert len({item.item_id for item in call_items}) == 4
    projected = codec.project_message(checkpoint_group)
    assert [call["id"] for call in projected["data"]["tool_calls"]] == [
        call["id"] for call in calls
    ]
    plan = _native_tool_plan(
        session_id="session-parallel-native",
        plan_id="plan-parallel-native",
        assembly_id="assembly-parallel-native",
        items=items,
    )
    native = project_native_request(plan, items, request_only_content={})
    inputs = native["request"]["input"]
    assert [item["call_id"] for item in inputs if item["type"] == "function_call"] == [
        call["id"] for call in calls
    ]
    assert [
        item["call_id"] for item in inputs if item["type"] == "function_call_output"
    ] == [call["id"] for call in calls]


@pytest.mark.parametrize(
    ("payload", "chat_content", "responses_content"),
    [
        (
            {"text": "运行时提醒"},
            [{"type": "text", "text": "运行时提醒"}],
            [{"type": "input_text", "text": "运行时提醒"}],
        ),
        (
            [{"type": "input_text", "text": "运行时提醒"}],
            [{"type": "text", "text": "运行时提醒"}],
            [{"type": "input_text", "text": "运行时提醒"}],
        ),
    ],
)
def test_runtime_notice_uses_same_user_text_contract_for_both_provider_formats(
    payload: object,
    chat_content: object,
    responses_content: object,
) -> None:
    item = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-runtime-notice",
        semantic_kind=SemanticKind.RUNTIME_NOTICE,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "runtime", "producer_id": "source"},
        payload=payload,
        turn_scope="pending_next_turn",
        wire_role="user",
    )
    plan = _native_tool_plan(
        session_id="session-runtime-notice",
        plan_id="plan-runtime-notice",
        assembly_id="assembly-runtime-notice",
        items=(item,),
    )

    messages = project_context_plan(
        plan,
        (item,),
        include_runtime_notices=True,
    )
    native = project_native_request(plan, (item,), request_only_content={})

    assert len(messages) == 1
    assert isinstance(messages[0], HumanMessage)
    assert messages[0].content == chat_content
    assert native["request"]["input"] == [
        {"role": "user", "content": responses_content}
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "image_url", "image_url": "https://example.invalid/image.png"},
        {"encoding": "base64url", "value": "cHJvdGVjdGVk"},
    ],
)
def test_runtime_notice_rejects_non_text_payload_for_both_provider_formats(
    payload: object,
) -> None:
    item = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-runtime-notice-invalid",
        semantic_kind=SemanticKind.RUNTIME_NOTICE,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "runtime", "producer_id": "source"},
        payload=payload,
        turn_scope="pending_next_turn",
        wire_role="user",
    )
    plan = _native_tool_plan(
        session_id="session-runtime-notice-invalid",
        plan_id="plan-runtime-notice-invalid",
        assembly_id="assembly-runtime-notice-invalid",
        items=(item,),
    )

    with pytest.raises((TypeError, ValueError), match="runtime_notice"):
        project_context_plan(plan, (item,), include_runtime_notices=True)
    with pytest.raises((TypeError, ValueError), match="runtime_notice"):
        project_native_request(plan, (item,), request_only_content={})


@pytest.fixture
def cross_language_vectors() -> dict[str, object]:
    return json.loads(
        (Path.cwd() / "tests/fixtures/itemized/hash_vectors.json").read_text(
            encoding="utf-8"
        )
    )


def test_storage_idempotency_uses_the_cross_language_golden(
    cross_language_vectors: dict[str, object],
) -> None:
    vector = next(
        row for row in cross_language_vectors["preimages"] if row["id"] == "idempotency"
    )
    call = cross_language_vectors["idempotency"]
    assert (
        default_idempotency_key(
            commit_kind=call["commit_kind"],
            subject_id=call["subject_id"],
            outcome=call["outcome"],
            metadata=vector["preimage"],
        )
        == call["key"]
    )


@pytest.fixture
def user_item() -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-user-1",
        semantic_kind=SemanticKind.USER_INPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "user",
            "producer_id": "ingress-1",
            "invocation_id": "turn-1",
        },
        payload="读取 README",
        created_at="2026-09-07T00:00:00+00:00",
        metadata={"source_revision": "rev-1"},
        turn_id="turn-1",
        turn_scope="turn_root",
        message_group_id="group-1",
        wire_role="user",
    )


def test_content_plan_request_and_idempotency_golden_vectors(
    cross_language_vectors: dict[str, object],
) -> None:
    vectors = {row["id"]: row for row in cross_language_vectors["preimages"]}
    assert content_hash("text", "golden") == (
        "sha256:jcs:v1:cc530bb3997805ec119bda25acfabb17250517cafe6522ea5550dd00d7775c6a"
    )

    item = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-golden-1",
        semantic_kind=SemanticKind.USER_INPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "user",
            "producer_id": "ingress-golden",
            "invocation_id": "turn-golden",
        },
        payload="golden input",
        created_at="2026-09-07T00:00:00+00:00",
        metadata={"source_revision": "rev-golden"},
        turn_id="turn-golden",
        turn_scope="turn_root",
        message_group_id="group-golden",
        wire_role="user",
    )
    ref = ContextRef.canonical_item(item, session_id="session-golden", thread_id="thread-1")
    selection = ContextSelectionEntry(
        assembly_id="assembly-golden",
        plan_ordinal=0,
        ref=ref,
        selection_kind=SelectionKind.CANONICAL_HISTORY,
        source_revision=ref.source_revision,
        content_length=ref.content_length,
        content_hash=ref.content_hash,
        visibility=ref.visibility,
        protection=ref.protection,
        availability=ref.availability,
    )
    plan = ContextRequestPlan(
        session_id="session-golden", plan_id="plan-golden", refs=(ref,)
    ).seal_for_assembly("assembly-golden", selection=(selection,))
    assert plan.plan_hash() == vectors["core-plan"]["hash"]
    assert (
        context_request_hash(plan, "provider-golden", target_format="native")
        == vectors["core-request"]["hash"]
    )
    assert default_idempotency_key(
        commit_kind="terminal_convergence",
        subject_id="turn-golden",
        outcome="completed",
        metadata={"execution_id": "execution-golden", "final_item_id": item.item_id},
    ) == (
        "terminal_convergence:turn-golden:completed:"
        "sha256:jcs:v1:1abb23c5879377c9adbe350eefbf01b95e8f8ec2554ba3ffd8df2f594da36c9e"
    )


def test_legacy_adapter_uses_the_cross_language_golden(
    cross_language_vectors: dict[str, object],
) -> None:
    rows = {row["id"]: row for row in cross_language_vectors["preimages"]}
    message = rows["legacy-message"]["preimage"]
    record = {
        name: value for name, value in message.items() if name != "source_session_id"
    }
    record.update(
        format_version=1,
        record_type="message",
        metadata={},
        turn_id="legacy-turn-golden",
    )
    candidate = LegacyRolloutAdapter(message["source_session_id"]).group_candidates(
        [record]
    )[0]
    assert candidate["candidate_status"] == "accepted"
    assert candidate["legacy_seed_hash"] == rows["legacy-seed"]["hash"]


@pytest.mark.parametrize(
    ("tool_status", "tool_outcome"),
    [("success", "success"), ("error", "failure")],
)
def test_tool_result_codec_preserves_tool_outcome_status(
    tool_status: str,
    tool_outcome: str,
) -> None:
    codec = LangChainMessageCodec()
    message = ToolMessage(
        content="工具返回",
        id=f"tool-result-{tool_status}",
        name="read_file",
        tool_call_id="call-1",
        status=tool_status,
    )
    (item,) = codec.items_for_message(
        message,
        item_sequence=1,
        message_id=str(message.id),
        turn_id="turn-1",
        timestamp="2026-09-07T00:00:00+00:00",
    )
    assert item.payload["tool_outcome"] == tool_outcome
    projected = codec.project_message((item,))
    assert projected["data"]["status"] == tool_status


def test_message_codec_restores_internal_visibility_from_acceptance_metadata() -> None:
    codec = LangChainMessageCodec()
    item = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-internal-goal",
        semantic_kind=SemanticKind.USER_INPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "user",
            "producer_id": "msg-internal-goal",
            "invocation_id": "execution-internal-goal",
        },
        payload="<system_reminder>继续 Goal</system_reminder>",
        created_at="2026-09-09T00:00:00+00:00",
        metadata={
            "projection_message_id": "msg-internal-goal",
            "message_metadata": {"internal": True},
        },
        turn_id="job-internal-goal",
        turn_scope="turn_root",
        wire_role="user",
    )

    projected = codec.project_message((item,))
    restored = codec.from_dict(projected)

    assert isinstance(restored, HumanMessage)
    assert restored.response_metadata["internal"] is True


def test_internal_execution_input_remains_turn_root_during_checkpoint_roundtrip() -> None:
    codec = LangChainMessageCodec()
    message = HumanMessage(
        content="<system_reminder>继续 Goal</system_reminder>",
        id="msg-internal-goal",
        response_metadata={
            "message_metadata": {
                "internal": True,
                "turn_id": "job-internal-goal",
                "job_id": "job-internal-goal",
            }
        },
    )

    (item,) = codec.items_for_message(
        message,
        item_sequence=1,
        message_id=str(message.id),
        turn_id="job-internal-goal",
        timestamp="2026-09-09T00:00:00+00:00",
    )

    assert item.semantic_kind == SemanticKind.USER_INPUT
    assert item.turn_id == "job-internal-goal"
    assert item.turn_scope == "turn_root"
    assert item.metadata["internal"] is True


def test_context_source_provenance_survives_canonical_roundtrip() -> None:
    codec = LangChainMessageCodec()
    message = HumanMessage(
        content="Skill 增量",
        id="context-source-skill-revision-delta",
        response_metadata={
            "context_source_kind": "skill",
            "context_source_id": "skill:debugging",
            "context_source_name": "debugging",
            "context_wire_role": "user",
            "context_revision": "sha256:revision",
        },
    )

    (item,) = codec.items_for_message(
        message,
        item_sequence=1,
        message_id=str(message.id),
        turn_id="turn-1",
        timestamp="2026-09-09T00:00:00+00:00",
    )
    restored = codec.from_dict(codec.project_message((item,)))

    assert item.metadata["context_source_id"] == "skill:debugging"
    assert restored.response_metadata["context_source_kind"] == "skill"
    assert restored.response_metadata["context_source_name"] == "debugging"
    assert restored.response_metadata["context_wire_role"] == "user"
    assert restored.response_metadata["context_revision"] == "sha256:revision"


def test_history_and_provider_consume_the_same_sealed_selection_order(
    user_item: CanonicalItemRecord,
) -> None:
    item = user_item
    item_ref = ContextRef.canonical_item(item, session_id="session-shared-selection", thread_id="thread-1")
    body = {"text": "必须先读取配置"}
    contribution = ContextContribution(
        contribution_id="contribution-shared-selection",
        source_kind="system_policy",
        source_revision="policy-rev-shared",
        content_hash=contribution_content_hash("prompt", body),
        body=body,
        content_length=len(canonical_json_bytes(body)),
        source_ordinal=0,
        # system policy 属于 root 内容；显式声明 root_eligible 进 system root。
        root_placement="root_eligible",
    )
    request_ref = ContextRef.request_only_ref(
        contribution.contribution_id,
        session_id="session-shared-selection", thread_id="thread-1",
        plan_id="plan-shared-selection",
        source_revision=contribution.source_revision,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        content_length=contribution.content_length,
        content_hash_value=contribution.content_hash,
        source_ref="shared-policy",
    )
    canonical_selection = ContextSelectionEntry(
        assembly_id="assembly-shared-selection",
        plan_ordinal=0,
        ref=item_ref,
        selection_kind=SelectionKind.CANONICAL_HISTORY,
        source_revision=item_ref.source_revision,
        content_length=item_ref.content_length,
        content_hash=item_ref.content_hash,
        visibility=item_ref.visibility,
        protection=item_ref.protection,
        availability=item_ref.availability,
    )
    request_selection = ContextSelectionEntry(
        assembly_id="assembly-shared-selection",
        plan_ordinal=1,
        ref=request_ref,
        selection_kind=SelectionKind.REQUEST_ONLY,
        source_revision=request_ref.source_revision,
        content_length=request_ref.content_length,
        content_hash=request_ref.content_hash,
        visibility=request_ref.visibility,
        protection=request_ref.protection,
        availability=request_ref.availability,
        detail_ref=DetailRef(
            "session-shared-selection", "assembly-shared-selection", "detail-shared-selection"
        ),
        contribution_id=contribution.contribution_id,
        contribution_ordinal=0,
    )
    plan = ContextRequestPlan(
        session_id="session-shared-selection",
        plan_id="plan-shared-selection",
        refs=(item_ref, request_ref),
        contributions=(contribution,),
    ).seal_for_assembly(
        "assembly-shared-selection",
        selection=(canonical_selection, request_selection),
    )

    provider_messages = project_context_plan(
        plan,
        (item,),
        request_only_content={contribution.contribution_id: body},
    )
    history_messages = project_history_plan(plan, (item,))

    assert [
        message.id for message in provider_messages if isinstance(message, HumanMessage)
    ] == [item.item_id]
    assert any(isinstance(message, SystemMessage) for message in provider_messages)
    assert [message.id for message in history_messages] == [item.item_id]
    assert all(not isinstance(message, SystemMessage) for message in history_messages)
