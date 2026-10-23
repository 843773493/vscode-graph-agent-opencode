from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import perf_counter

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.execution_admission import InternalExecutionIntent
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ProjectionPartRef,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.prompting import PromptSection, internal_message_factory
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.stream_accumulator import (
    CanonicalBlockAccumulator,
)
from app.services.infrastructure.rollout_context.storage.catalog.turn_projection_helpers import (
    _final_reasoning_source_refs,
    _finalize_activity_projection,
    _logical_activity_key,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)
from app.services.mapping.itemized.canonical_langchain import project_canonical_items
from app.services.mapping.itemized.carrier_dedup import (
    superseded_stream_content_item_ids,
)
from app.services.mapping.itemized.provider_history import (
    reasoning_projection_rows,
    visible_text,
)
from app.services.mapping.turn_response_parts import response_parts_from_records
from tests.support.rollout_view_mutations import mutate_view_members


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

    assert _logical_activity_key(provider, "model-call-1") == _logical_activity_key(checkpoint_shadow, "model-call-1")
    assert _logical_activity_key(provider, "model-call-1") != _logical_activity_key(different_model_call, "model-call-2")


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

    assert _logical_activity_key(first, "model-call-1") != _logical_activity_key(second, "model-call-2")


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

    assert _logical_activity_key(first, "model-call-1") != _logical_activity_key(second, "model-call-2")


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

    assert _logical_activity_key(provider, "model-call-1") == _logical_activity_key(checkpoint_shadow, "model-call-1")


def test_final_reasoning_resolves_preserved_content_part_identity() -> None:
    refs = _final_reasoning_source_refs(
        ItemProjectionIdentity(model_call_id="model-call-1", content_part_refs=(
            ProjectionPartRef("part-reasoning", 0, "reasoning"), ProjectionPartRef("part-text", 1, "text")
        )),
        content_block_index=0,
        item_index=0,
        provider_item_id=None,
    )

    assert refs == {("model-call-1", "part-reasoning")}


def test_final_reasoning_keeps_distinct_provider_item_identity() -> None:
    refs = _final_reasoning_source_refs(
        ItemProjectionIdentity(model_call_id="model-call-1", content_part_refs=(
            ProjectionPartRef("outer-reasoning", 0, "reasoning_items"),
        )),
        content_block_index=0,
        item_index=1,
        provider_item_id="provider-reasoning-2",
    )

    assert refs == {("model-call-1", "provider-reasoning-2")}


def test_final_reasoning_scope_prevents_reused_part_cross_call_merge() -> None:
    refs = _final_reasoning_source_refs(
        ItemProjectionIdentity(model_call_id="model-call-2", content_part_refs=(ProjectionPartRef("part-reused", 0),)),
        content_block_index=0, item_index=0, provider_item_id=None,
    )
    assert refs == {("model-call-2", "part-reused")}
    assert not refs & {("model-call-1", "part-reused")}


@pytest.fixture
def membership_projection_case(source_seal_case):
    case = source_seal_case
    owner, saver = case.owner, case.saver
    admission = saver.execution_admission(
        owner.session_id, thread_id=owner.thread_id,
        execution_id=str(case.accepted["initial_execution_id"]),
    )
    items = tuple(
        CanonicalItemRecord.create(
            item_sequence=number, item_id=f"membership-reasoning-{number}",
            semantic_kind="reasoning", payload_kind="text", status="completed",
            producer_ref={"producer_kind": "provider", "producer_id": f"call-{number}"},
            payload=f"推理 {number}", created_at=f"2026-10-10T00:00:0{number}+00:00",
            turn_id=admission.turn_id, turn_scope="turn_member",
            projection_identity=ItemProjectionIdentity(model_call_id=f"call-{number}", block_id=f"block-{number}", block_index=0),
        ) for number in (2, 3)
    )
    with (
        session_thread_owner_scope(MutationIntentOwner(owner.session_id, owner.thread_id), langgraph_thread_id=owner.thread_id),
        execution_admission_scope(admission, langgraph_thread_id=owner.thread_id),
    ):
        saver.append_items(owner.session_id, items, thread_id=owner.thread_id)
    return case, admission, items


def test_real_projection_uses_exact_selected_view_membership(membership_projection_case):
    case, admission, items = membership_projection_case
    storage = case.saver._storage.storage
    with storage.open_read_snapshot(admission.session_id, admission.thread_id) as snapshot:
        with closing(storage._connect(admission.thread_id, session_id=admission.session_id)) as copy_source:
            view_id = storage._active_context_view_id(snapshot.connection, snapshot.checkpoint_ns)
            assert view_id is not None
            mutate_view_members(copy_source, "UPDATE context_view_items SET logical_item_ordinal=20 WHERE view_id=? AND item_id=?", (view_id, items[0].item_id))
            mutate_view_members(copy_source, "UPDATE context_view_items SET logical_item_ordinal=4 WHERE view_id=? AND item_id=?", (view_id, items[1].item_id))
            copy_source.commit()
        with storage.open_read_snapshot(admission.session_id, admission.thread_id) as selected:
            projection = storage.read_turn_projections(selected, view_id, (admission.turn_id,))[admission.turn_id]
            parts = response_parts_from_records([], projection=projection, mode="summary", include=frozenset({"thinking"}))
            assert [part.source.item_id for part in parts] == [items[1].item_id, items[0].item_id]
            assert [part.source.logical_item_ordinal for part in parts] == [4, 20]
            assert [part.source.item_sequence for part in parts] == [3, 2]
            assert [part.source.logical_item_ordinal for part in response_parts_from_records(
                [], projection=storage.read_turn_projections(snapshot, view_id, (admission.turn_id,))[admission.turn_id],
                mode="summary", include=frozenset({"thinking"}),
            )] == [1, 2]
    history = case.saver.load_history(
        admission.session_id, admission.thread_id,
        TurnHistoryLoadRequest(include=["reasoning_detail"]),
    ).items[0]
    assert [part.source.item_id for part in history.response_parts] == [items[1].item_id, items[0].item_id]
    assert (history.activity_stats.first_item_sequence, history.activity_stats.last_item_sequence) == (2, 3)


def test_projection_excludes_hidden_view_member(membership_projection_case):
    case, admission, items = membership_projection_case
    storage = case.saver._storage.storage
    with closing(storage._connect(admission.thread_id, session_id=admission.session_id)) as connection:
        view_id = storage._active_context_view_id(connection, "")
        mutate_view_members(connection, "UPDATE context_view_items SET visible=0 WHERE view_id=? AND item_id=?", (view_id, items[0].item_id))
        connection.commit()
    with storage.open_read_snapshot(admission.session_id, admission.thread_id) as snapshot:
        projection = storage.read_turn_projections(snapshot, view_id, (admission.turn_id,))[admission.turn_id]
    assert [item["item_id"] for item in projection["activity_items"]] == [items[1].item_id]
    assert projection["activity_stats"]["item_count"] == 1


def test_internal_elapsed_keeps_same_predecessor_across_pages(source_seal_case):
    case = source_seal_case
    owner, saver = case.owner, case.saver
    prepared = internal_message_factory.build(
        kind="generated_session_result", control="处理已提交的内部任务。",
        sections=(
            PromptSection("control_context", {"source": "elapsed-test"}),
            PromptSection("generated_session_result", "内部输入"),
        ), display_content="内部输入",
    )
    notice = HumanMessage(
        id="elapsed-notice", content=prepared.content,
        response_metadata={
            "message_id": "elapsed-notice",
            "created_at": "2026-09-09T00:00:00+00:00",
            "message_metadata": prepared.metadata,
        },
    )
    admission = saver.admit_internal_execution(
        owner.session_id, thread_id=owner.thread_id, job_id="elapsed-job",
        intent=InternalExecutionIntent(message_id=notice.id, execution_id="elapsed-execution", admission_idempotency_key="elapsed-admission"),
        message=notice,
    )
    storage = saver._storage.storage
    with closing(storage._connect(owner.thread_id, session_id=owner.session_id)) as connection:
        initial_view_id = storage._active_context_view_id(connection, "")
        assert initial_view_id is not None
        previous = connection.execute(
            "SELECT item.created_at FROM item_catalog AS item JOIN context_view_items AS member ON member.item_id=item.item_id "
            "WHERE item.execution_id=? AND member.view_id=? "
            "ORDER BY member.logical_item_ordinal DESC LIMIT 1",
            (admission.execution_id, initial_view_id),
        ).fetchone()
        assert previous is not None
        start = datetime.fromisoformat(previous[0])
    items = tuple(CanonicalItemRecord.create(
        item_sequence=1, item_id=f"elapsed-output-{seconds}", semantic_kind="assistant_output",
        wire_role="assistant",
        payload_kind="text", status="completed", producer_ref={"producer_kind": "runtime", "producer_id": admission.execution_id},
        payload=f"内部输出 {seconds}", created_at=(start + timedelta(seconds=seconds)).isoformat(),
    ) for seconds in (2, 5))
    with (
        session_thread_owner_scope(MutationIntentOwner(owner.session_id, owner.thread_id), langgraph_thread_id=owner.thread_id),
        execution_admission_scope(admission, langgraph_thread_id=owner.thread_id),
    ):
        saver.append_items(owner.session_id, items, thread_id=owner.thread_id)
    with storage.open_read_snapshot(owner.session_id, owner.thread_id) as snapshot:
        reader = saver._context_reader
        chain = reader.resolve_chain(snapshot)
        bounds = reader.read_history_snapshot_bounds(snapshot, chain)
        newest = reader.read_internal_execution_history_page(
            snapshot, chain, direction="before", anchor_logical_item_ordinal=None,
            lower_logical_item_ordinal=None, upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            snapshot_upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            limit=1, execution_id=admission.execution_id,
        )
        previous_page = reader.read_internal_execution_history_page(
            snapshot, chain, direction="before", anchor_logical_item_ordinal=newest.entries[0].logical_item_ordinal,
            lower_logical_item_ordinal=None, upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            snapshot_upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            limit=1, execution_id=admission.execution_id,
        )
        assert newest.entries[0].item.item_id == items[1].item_id
        assert newest.entries[0].elapsed_ms == 3000
        assert previous_page.entries[0].item.item_id == items[0].item_id
        assert previous_page.entries[0].elapsed_ms == 2000


@pytest.fixture
def internal_content_carrier_case(source_seal_case, request):
    """真实准入和 canonical commit 保留三份正文，以 typed 引用决定展示 survivor。"""
    case = source_seal_case
    saver, owner = case.saver, case.owner
    stamp = datetime(2026, 9, 9, tzinfo=UTC)
    prepared_notice = internal_message_factory.build(
        kind="generated_session_result", control="处理内部报告。",
        sections=(PromptSection("control_context", {"source": "content-carrier"}),
                  PromptSection("generated_session_result", "内部输入")),
        display_content="内部输入",
    )
    notice = HumanMessage(id="internal-content-notice", content=prepared_notice.content,
                          response_metadata={"message_id": "internal-content-notice", "created_at": stamp.isoformat(),
                                             "message_metadata": prepared_notice.metadata})
    admission = saver.admit_internal_execution(
        owner.session_id, thread_id=owner.thread_id, job_id="internal-content-job",
        intent=InternalExecutionIntent(message_id=notice.id, execution_id="internal-content-execution",
                                       admission_idempotency_key="internal-content-admission"),
        message=notice,
    )
    model_call_id = "internal-content-model"
    content = [
        {"type": "reasoning_content", "reasoning_content": "核对初始化报告。"},
        {"type": "text", "text": "初始化报告的私有正文。"},
    ]
    accumulator = CanonicalBlockAccumulator(turn_id=None, execution_id=admission.execution_id,
                                            producer_id=model_call_id, model_call_id=model_call_id)
    for index, (part_id, kind, text) in enumerate((
        ("part_internal_reasoning", "reasoning_content", "核对初始化报告。"),
        ("part_internal_text", "text", "初始化报告的私有正文。"),
    )):
        accumulator.accept({"block_id": f"{model_call_id}:block:{part_id}", "block_index": index,
                            "carrier_type": kind, "text": text})
    stream = tuple(replace(item, created_at=(stamp + timedelta(seconds=index + 3)).isoformat())
                   for index, item in enumerate(accumulator.finalize(first_item_sequence=1)))
    codec = LangChainMessageCodec()
    checkpoint_id = f"lc_run--{model_call_id}"
    checkpoint = codec.items_for_message(
        AIMessage(id=checkpoint_id, content=content, response_metadata={"model_call_id": model_call_id}),
        item_sequence=1, message_id=checkpoint_id, turn_id=None, execution_id=admission.execution_id,
        timestamp=(stamp + timedelta(seconds=5)).isoformat(),
    )
    final_message = AIMessage(id="internal-content-final", content=content, response_metadata={
        "model_call_id": model_call_id, "phase": "final_answer",
        "content_part_refs": [{"id": "part_internal_reasoning", "index": 0}, {"id": "part_internal_text", "index": 1}],
        "provenance_relations": [{"relation": "replaces", "target_kind": "projection_message",
                                 "target_id": checkpoint_id, "target_model_call_id": model_call_id}],
    })
    final_items = codec.items_for_message(
        final_message, item_sequence=1, message_id=final_message.id, turn_id=None, execution_id=admission.execution_id,
        timestamp=(stamp + timedelta(seconds=7)).isoformat(),
    )
    independent = tuple(CanonicalItemRecord.create(
        item_sequence=1, item_id=f"internal-independent-{number}", semantic_kind="assistant_output",
        wire_role="assistant", payload_kind="text", status="completed",
        producer_ref={"producer_kind": "provider", "producer_id": model_call_id},
        projection_identity=ItemProjectionIdentity(model_call_id=scope),
        payload="初始化报告的私有正文。", created_at=(stamp + timedelta(seconds=number + 1)).isoformat(),
    ) for number, scope in enumerate((model_call_id, "another-internal-model")))
    fillers = tuple(CanonicalItemRecord.create(
        item_sequence=1, item_id=f"internal-page-filler-{number}", semantic_kind="assistant_output",
        wire_role="assistant", payload_kind="text", status="completed",
        producer_ref={"producer_kind": "provider", "producer_id": model_call_id},
        projection_identity=ItemProjectionIdentity(
            model_call_id=model_call_id, block_id=f"{model_call_id}:block:independent-{number}",
            block_index=number + 10,
        ),
        payload=f"独立输出 {number}", created_at=(stamp + timedelta(seconds=6)).isoformat(),
    ) for number in range(getattr(request, "param", 0)))
    with (
        session_thread_owner_scope(MutationIntentOwner(owner.session_id, owner.thread_id), langgraph_thread_id=owner.thread_id),
        execution_admission_scope(admission, langgraph_thread_id=owner.thread_id),
    ):
        saver.append_items(owner.session_id, (*independent, *stream, *checkpoint, *fillers), thread_id=owner.thread_id)
        saver.converge_execution(owner.session_id, thread_id=owner.thread_id, turn_id=None,
                                 execution_id=admission.execution_id, outcome="completed", turn_status=None,
                                 items=final_items)
        # 原子终态先持久化 final，再由 canonical sink 幂等发布其 view membership。
        saver.append_items(owner.session_id, final_items, thread_id=owner.thread_id)
    raw = saver._storage.storage.read_items(owner.session_id, owner.thread_id)
    final = next(item for item in raw if item.item_id == "item-internal-content-final")
    return case, admission, raw, final, independent


@pytest.mark.parametrize("internal_content_carrier_case", [300], indirect=True)
def test_internal_content_large_history_bounds_metadata_before_reading_bodies(
    internal_content_carrier_case, monkeypatch,
):
    case, admission, raw, final, _independent = internal_content_carrier_case
    storage, reader = case.saver._storage.storage, case.saver._context_reader
    body_reads = []
    original_read = storage.read_items

    def bounded_read(*args, **kwargs):
        body_reads.append(tuple(kwargs["item_ids"]))
        return original_read(*args, **kwargs)

    monkeypatch.setattr(storage, "read_items", bounded_read)
    with reader.open_snapshot(admission.session_id, admission.thread_id) as snapshot:
        chain = reader.resolve_chain(snapshot)
        bound = reader.read_history_snapshot_bounds(snapshot, chain).upper_logical_item_ordinal
        members = {row[0] for row in snapshot.connection.execute(
            "SELECT item.item_id FROM item_catalog AS item JOIN context_view_items AS member "
            "ON member.item_id=item.item_id WHERE member.view_id=? AND item.execution_id=? "
            "AND member.visible=1 AND member.source_kind='display_only'",
            (reader.context_view_id(chain), admission.execution_id),
        )}
        superseded = superseded_stream_content_item_ids([item for item in raw if item.item_id in members])
        expected = members - superseded
        assert len(expected) > 256 and len(superseded) >= 3
        # 观察真实 SQLite cursor 返回的行，而非 mock storage 的分页结果。
        rows_by_cursor = {}

        def observe_row(cursor, row):
            rows_by_cursor[cursor] = rows_by_cursor.get(cursor, 0) + 1
            return row

        snapshot.connection.row_factory = observe_row
        seen = []
        anchor = None
        while True:
            started = perf_counter()
            page = reader.read_internal_execution_history_page(
                snapshot, chain, direction="before", anchor_logical_item_ordinal=anchor,
                lower_logical_item_ordinal=None, upper_logical_item_ordinal=bound,
                snapshot_upper_logical_item_ordinal=bound,
                execution_id=admission.execution_id, limit=256,
            )
            assert 0 < len(page.entries) <= 256
            assert max(rows_by_cursor.values()) <= 257
            print({"internal_members": len(members), "page_entries": len(page.entries),
                   "max_cursor_rows": max(rows_by_cursor.values()), "seconds": perf_counter() - started})
            seen[0:0] = [entry.item.item_id for entry in page.entries]
            if not page.has_more:
                break
            anchor = page.entries[0].logical_item_ordinal
        assert len(seen) == len(set(seen)) == len(expected)
        assert set(seen) == expected
        assert seen[-1] == final.item_id
        # final 在窗口外时仍替代旧正文；冻结上界排除 final 时旧正文恢复可读。
        final_ordinal = snapshot.connection.execute(
            "SELECT logical_item_ordinal FROM context_view_items WHERE item_id=? AND view_id=?",
            (final.item_id, reader.context_view_id(chain)),
        ).fetchone()[0]
        for frozen_upper, removed in ((bound, superseded), (final_ordinal - 1, set())):
            page = reader.read_internal_execution_history_page(
                snapshot, chain, direction="after", anchor_logical_item_ordinal=None,
                lower_logical_item_ordinal=None, upper_logical_item_ordinal=final_ordinal - 1,
                snapshot_upper_logical_item_ordinal=frozen_upper,
                execution_id=admission.execution_id, limit=256,
            )
            ids = {entry.item.item_id for entry in page.entries}
            assert ids.isdisjoint(removed)
            if not removed:
                assert superseded <= ids
        assert max(rows_by_cursor.values()) <= 257
    assert len(body_reads) >= 4
    assert all(len(ids) <= 256 for ids in body_reads)


@pytest.mark.parametrize("invalid_scope", ["missing", "conflicting"])
def test_internal_content_invalid_part_scope_fails_before_body_read(
    internal_content_carrier_case, invalid_scope, monkeypatch,
):
    case, admission, _raw, final, _independent = internal_content_carrier_case
    storage, reader = case.saver._storage.storage, case.saver._context_reader
    manifest = final.projection_manifest()
    if invalid_scope == "missing":
        manifest["projection_identity"].pop("model_call_id")
        manifest["provenance_relations"] = []
    else:
        manifest["provenance_relations"][0]["target_model_call_id"] = "conflicting-model-call"
    with closing(storage._connect(admission.thread_id, session_id=admission.session_id)) as connection:
        connection.execute("UPDATE item_catalog SET projection_manifest_json=? WHERE item_id=?",
                           (canonical_json_text(manifest), final.item_id))
        connection.commit()
        before = list(connection.iterdump())

    def reject_body_read(*args, **kwargs):
        pytest.fail("损坏的 part scope 应在 canonical 正文读取前明确拒绝")

    monkeypatch.setattr(storage, "read_items", reject_body_read)
    with reader.open_snapshot(admission.session_id, admission.thread_id) as snapshot:
        chain = reader.resolve_chain(snapshot)
        bound = reader.read_history_snapshot_bounds(snapshot, chain).upper_logical_item_ordinal
        with pytest.raises(ValueError, match="唯一 typed model-call scope"):
            reader.read_internal_execution_history_page(
                snapshot, chain, direction="before", anchor_logical_item_ordinal=None,
                lower_logical_item_ordinal=None, upper_logical_item_ordinal=bound,
                snapshot_upper_logical_item_ordinal=bound,
                execution_id=admission.execution_id, limit=1,
            )
    with closing(storage._connect(admission.thread_id, session_id=admission.session_id)) as connection:
        assert list(connection.iterdump()) == before


def test_internal_content_survivor_is_selected_before_keyset_and_body_reads(
    internal_content_carrier_case, monkeypatch,
):
    case, admission, raw, final, independent = internal_content_carrier_case
    storage, reader = case.saver._storage.storage, case.saver._context_reader
    jsonl_path = storage.jsonl_path(admission.session_id, thread_id=admission.thread_id)
    before = jsonl_path.read_bytes()
    requested = []
    original_read = storage.read_items

    def bounded_read(*args, **kwargs):
        requested.append(tuple(kwargs["item_ids"]))
        return original_read(*args, **kwargs)

    monkeypatch.setattr(storage, "read_items", bounded_read)
    with reader.open_snapshot(admission.session_id, admission.thread_id) as snapshot:
        chain = reader.resolve_chain(snapshot)
        bound = reader.read_history_snapshot_bounds(snapshot, chain).upper_logical_item_ordinal

        def page(direction, *, anchor=None, lower=None, upper=bound, frozen_upper=bound):
            return reader.read_internal_execution_history_page(
                snapshot, chain, direction=direction, anchor_logical_item_ordinal=anchor,
                lower_logical_item_ordinal=lower, upper_logical_item_ordinal=upper,
                snapshot_upper_logical_item_ordinal=frozen_upper,
                execution_id=admission.execution_id, limit=1,
            )

        tail = page("before")
        assert [entry.item.item_id for entry in tail.entries] == [final.item_id]
        assert tail.has_more
        final_ordinal = tail.entries[0].logical_item_ordinal
        preceding = page("before", anchor=final_ordinal)
        assert preceding.entries[0].item.item_id == independent[1].item_id
        assert preceding.has_more
        following = page("after", anchor=preceding.entries[0].logical_item_ordinal)
        assert following.entries[0].item.item_id == final.item_id and not following.has_more
        # 替代项位于 anchor 页外，旧 stream/checkpoint 仍必须被排除。
        first_independent_ordinal = snapshot.connection.execute(
            "SELECT logical_item_ordinal FROM context_view_items WHERE view_id=? AND item_id=?",
            (reader.context_view_id(chain), independent[0].item_id),
        ).fetchone()[0]
        head = page("after", lower=first_independent_ordinal)
        assert head.entries[0].item.item_id == independent[0].item_id and head.has_more
        assert page("after", lower=final_ordinal).entries[0].logical_item_ordinal == final_ordinal
        assert page("before", upper=final_ordinal - 1).entries[0].item.item_id == independent[1].item_id
        # frozen upper 外的 final 不参与当前选择，旧 carrier 坐标和正文仍能读取。
        frozen = page("before", upper=final_ordinal - 1, frozen_upper=final_ordinal - 1)
        assert frozen.entries[0].item.item_id == "item-lc_run--internal-content-model"
        # 前一物理 checkpoint 虽被隐藏，elapsed 仍沿它起算，不能跳到前一 survivor。
        assert tail.entries[0].elapsed_ms == 2000
    assert all(len(ids) == 1 for ids in requested)
    assert jsonl_path.read_bytes() == before
    assert [item.to_dict() for item in original_read(admission.session_id, admission.thread_id)] == [item.to_dict() for item in raw]


@pytest.mark.parametrize("membership_change", ["visible=0", "source_kind='canonical'"])
def test_internal_content_hidden_or_other_source_replacement_cannot_hide_visible_stream(
    internal_content_carrier_case, membership_change,
):
    case, admission, _raw, final, _independent = internal_content_carrier_case
    storage, reader = case.saver._storage.storage, case.saver._context_reader
    with closing(storage._connect(admission.thread_id, session_id=admission.session_id)) as connection:
        view_id = storage._active_context_view_id(connection, "")
        mutate_view_members(connection, f"UPDATE context_view_items SET {membership_change} WHERE view_id=? AND item_id=?", (view_id, final.item_id))
        connection.commit()
    with reader.open_snapshot(admission.session_id, admission.thread_id) as snapshot:
        chain = reader.resolve_chain(snapshot)
        bounds = reader.read_history_snapshot_bounds(snapshot, chain)
        page = reader.read_internal_execution_history_page(
            snapshot, chain, direction="after", anchor_logical_item_ordinal=None,
            lower_logical_item_ordinal=None, upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            snapshot_upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            limit=256, execution_id=admission.execution_id,
        )
    ids = [entry.item.item_id for entry in page.entries]
    assert final.item_id not in ids
    assert "item-lc_run--internal-content-model" in ids
    assert len([entry for entry in page.entries if entry.item.semantic_kind == "reasoning"]) == 1
    assert len([entry for entry in page.entries if entry.item.semantic_kind == "assistant_output"]) == 4


def test_internal_content_cold_history_and_provider_keep_one_final_with_reasoning(
    internal_content_carrier_case,
):
    case, admission, raw, final, independent = internal_content_carrier_case
    restarted = RolloutCheckpointSaver(case.saver._storage.storage.sessions_dir)
    page = restarted.load_history(admission.session_id, admission.thread_id,
        TurnHistoryLoadRequest(execution_id=admission.execution_id,
                               include=["internal", "final_response", "reasoning_detail"]))
    assert len(page.internal_executions) == 1
    internal = page.internal_executions[0]
    assert internal.status == "completed"
    assert (internal.execution_id, internal.job_id, internal.session_id, internal.thread_id) == (
        admission.execution_id, admission.job_id, admission.session_id, admission.thread_id,
    )
    assert [part.source.item_id for part in internal.response_parts] == [
        independent[0].item_id, independent[1].item_id, final.item_id, final.item_id,
    ]
    assert [part.kind for part in internal.response_parts] == ["text", "text", "reasoning", "text"]
    assert [part.text for part in internal.response_parts if part.source.item_id == final.item_id] == [
        "核对初始化报告。", "初始化报告的私有正文。",
    ]
    independent_ids = {item.item_id for item in independent}
    messages = project_canonical_items([item for item in raw if item.item_id not in independent_ids])
    final_messages = [message for message in messages if isinstance(message, AIMessage) and message.id == "internal-content-final"]
    assert len(final_messages) == 1
    assert not any(message.id == "lc_run--internal-content-model" for message in messages)
    assert visible_text(final_messages[0].content) == "初始化报告的私有正文。"
    assert reasoning_projection_rows(final_messages[0].content)[0]["reasoning_text"] == "核对初始化报告。"
    assert final.projection_identity.content_part_refs[1].index == 1
    assert final.provenance_relations[0].target_id == "lc_run--internal-content-model"
    assert len([item for item in raw if item.semantic_kind == "assistant_output" and item.item_id not in {entry.item_id for entry in independent}]) == 3


@pytest.fixture
def versioned_activity_rows():
    """局部投影装具：相同 logical ID 的正文由不同 canonical anchor 标识。"""
    import json

    items = tuple(CanonicalItemRecord.create(
        item_sequence=index, item_id=f"version-{index}", semantic_kind="tool_call",
        payload_kind="tool_call", status="completed",
        producer_ref={"producer_kind": "provider", "producer_id": f"producer-{index}"},
        payload={"tool_calls": [{"id": f"call-{index}", "name": name, "args": {}}]},
        created_at="2026-10-21T00:00:00+00:00", turn_id="turn", turn_scope="turn_member",
        projection_identity=ItemProjectionIdentity(
            projection_message_id="same-logical-id", model_call_id=f"model-{index}",
        ),
    ) for index, name in ((1, "read_file"), (2, "write_file")))
    tools = [(
        "turn", item.item_id, f"call-{index}", name, status, None, index, 0,
        json.dumps(item.projection_manifest()),
    ) for index, (item, name, status) in enumerate(zip(
        items, ("read_file", "write_file"), ("completed", "running"), strict=True,
    ), 1)]
    activity = [(
        "turn", item.item_sequence, item.item_id, item.semantic_kind, item.payload_kind,
        item.status, item.created_at, json.dumps(dict(item.producer_ref)),
        json.dumps(item.projection_manifest()), "工具调用", 0, None, index,
    ) for index, item in enumerate(items)]
    return tools, activity


def test_tool_activity_uses_exact_canonical_version(versioned_activity_rows):
    from app.services.infrastructure.rollout_context.storage.catalog.turn_activity_merge import (
        _merge_activity_items,
    )

    tools, activity = versioned_activity_rows
    result = {"turn": {"activity_items": []}}
    _merge_activity_items(result, tools, activity)
    assert [(
        item["item_id"], item["tool_name"], item["status"], item["assistant_message_sequence"],
    ) for item in result["turn"]["activity_items"]] == [
        ("version-1", "read_file", "completed", 1),
        ("version-2", "write_file", "running", 2),
    ]
