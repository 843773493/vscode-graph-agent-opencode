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
    _finalize_activity_projection,
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
from tests.support.rollout.view_mutations import mutate_view_members
from tests.support.rollout.web_boundary_seeding import (
    _seed_case_0006,
    seed_boundary_turn,
)


@pytest.mark.parametrize("status,outcome", [("completed", "failed"), ("partial", "cancelled")])
def test_nonfinal_text_survives_cold_history_and_activity_reads(
    tmp_path, session_bundle_factory, status, outcome,
):
    """普通中间正文和中断正文都是过程成员；首读、轮廓和点读保持同一身份。"""
    session_id = "ses_019c5123cafb789e8fdf8704f6464f74"
    sessions = tmp_path / ".boxteam" / "sessions"
    session_bundle_factory(sessions, session_id)
    text = "该正文属于过程，不能丢失，也不能提升为 final。"
    item = CanonicalItemRecord.create(
        item_sequence=1, item_id="nonfinal-text", semantic_kind="assistant_output",
        payload_kind="text", status=status, payload=text,
        wire_role="assistant",
        producer_ref={"producer_kind": "provider", "producer_id": "text-model-call"},
        turn_id="text-turn", turn_scope="turn_member",
        projection_identity=ItemProjectionIdentity(
            model_call_id="text-model-call", projection_message_id="text-message",
            completion_reason="user_interrupt" if status == "partial" else None,
        ),
    )
    seed_boundary_turn(sessions, session_id=session_id, turn_id="text-turn",
        user_content="保留非 final 正文", items=(item,), outcome=outcome, turn_status=outcome)
    saver = RolloutCheckpointSaver(sessions)
    thread_id = saver._storage.storage.main_thread_id(session_id)
    initial = saver.load_history(session_id, thread_id, TurnHistoryLoadRequest())
    binding = {"source_view_id": initial.source_view_id, "source_view_revision": initial.source_view_revision}
    pages = [initial, *[
        saver.load_history(session_id, thread_id, TurnHistoryLoadRequest(
            turn_ids=["text-turn"], include=include, **binding,
            **({"item_ids": [item.item_id]} if include == ["text"] else {}),
        )) for include in (["activity_outline"], ["text"])
    ]]
    for page in pages:
        turn = page.items[0]
        assert turn.status == outcome and turn.final_response == ""
        assert turn.activity_stats.item_count == 1
        assert turn.activity_stats.first_item_sequence == turn.activity_stats.last_item_sequence
        part, = turn.response_parts
        assert part.kind == "text" and part.source.item_id == item.item_id
        assert part.text == text
        assert part.part_id == initial.items[0].response_parts[0].part_id


def test_standalone_internal_reasoning_identity_does_not_require_message_projection(source_seal_case):
    """普通思考可携带消息身份，但轮廓不因此要求不存在的结构化消息投影。"""
    case = source_seal_case
    saver, owner = case.saver, case.owner
    prepared = internal_message_factory.build(
        kind="generated_session_result", control="读取独立执行。",
        sections=(PromptSection("control_context", {"source": "standalone-reasoning"}),
                  PromptSection("generated_session_result", "独立执行输入")),
        display_content="独立执行输入",
    )
    notice = HumanMessage(id="standalone-notice", content=prepared.content,
        response_metadata={"message_id": "standalone-notice", "created_at": datetime.now(UTC).isoformat(),
                           "message_metadata": prepared.metadata})
    admission = saver.admit_internal_execution(owner.session_id, thread_id=owner.thread_id,
        job_id="standalone-job", message=notice,
        intent=InternalExecutionIntent(message_id=notice.id, execution_id="standalone-execution",
                                       admission_idempotency_key="standalone-admission"))
    item = CanonicalItemRecord.create(item_sequence=1, item_id="standalone-reasoning",
        semantic_kind="reasoning", payload_kind="text", status="completed", payload="独立思考正文",
        producer_ref={"producer_kind": "runtime", "producer_id": "fixture"},
        projection_identity=ItemProjectionIdentity(projection_message_id="standalone-message",
            model_call_id="standalone-model", block_id="standalone-block", block_index=2))
    saver.converge_execution(owner.session_id, thread_id=owner.thread_id, turn_id=None,
        execution_id=admission.execution_id, outcome="completed", turn_status=None, items=(item,))
    with (session_thread_owner_scope(MutationIntentOwner(owner.session_id, owner.thread_id), langgraph_thread_id=owner.thread_id),
          execution_admission_scope(admission, langgraph_thread_id=owner.thread_id)):
        saver.append_items(owner.session_id, (item,), thread_id=owner.thread_id)
    page = saver.load_history(owner.session_id, owner.thread_id, TurnHistoryLoadRequest())
    execution = next(entry for entry in page.internal_executions if entry.execution_id == admission.execution_id)
    part, = execution.response_parts
    assert part.kind == "reasoning" and part.projection == "summary" and not part.text
    assert part.source.item_id == item.item_id and part.source.part_ordinal == 2
    body = saver.load_history(owner.session_id, owner.thread_id, TurnHistoryLoadRequest(
        execution_id=admission.execution_id, item_ids=[item.item_id], include=["internal", "reasoning_detail"],
        source_view_id=page.source_view_id, source_view_revision=page.source_view_revision))
    detailed, = body.internal_executions[0].response_parts
    assert detailed.part_id == part.part_id and detailed.text == "独立思考正文"


def test_parallel_tool_history_keeps_committed_outcomes(tmp_path, session_bundle_factory):
    """并行调用和交错回执沿正式 writer 入库，轻轮廓不得把已确认成功显示为未知。"""
    session_id = "ses_019b7a50d46c7e7f86c9148c7069cf97"
    sessions = tmp_path / ".boxteam" / "sessions"
    session_bundle_factory(sessions, session_id)
    _seed_case_0006(sessions)
    saver = RolloutCheckpointSaver(sessions)
    thread_id = saver._storage.storage.main_thread_id(session_id)
    page = saver.load_history(session_id, thread_id, TurnHistoryLoadRequest(
        include=["tool_call", "tool_result", "final_response"],
    ))
    turn = page.items[0]
    tools = [part for part in turn.response_parts if part.kind in {"tool_call", "tool_result"}]
    assert len(tools) == 4
    assert all(part.status == "completed" for part in tools)
    assert [part.result for part in tools if part.kind == "tool_result"] == ['{"result": "README ok"}', '{"matches": 3}']
    binding = {"source_view_id": page.source_view_id, "source_view_revision": page.source_view_revision}
    outline = saver.load_history(session_id, thread_id, TurnHistoryLoadRequest(
        turn_ids=[turn.turn_id], include=["activity_outline"], **binding,
    ))
    projected = [part for part in outline.items[0].response_parts if part.kind in {"tool_call", "tool_result"}]
    assert [(part.part_id, part.tool_call_id, part.status) for part in projected] == [
        (part.part_id, part.tool_call_id, part.status) for part in tools
    ]
    for part in tools:
        point = saver.load_history(session_id, thread_id, TurnHistoryLoadRequest(
            turn_ids=[turn.turn_id], item_ids=[part.source.item_id],
            include=["tool_call", "tool_result"], **binding,
        ))
        selected, = point.items[0].response_parts
        assert selected.part_id == part.part_id and selected.status == "completed"
        assert selected.outcome_unknown is not True
        assert selected.arguments == part.arguments and selected.result == part.result


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


@pytest.fixture
def membership_projection_case(source_seal_case, request):
    case = source_seal_case
    owner, saver = case.owner, case.saver
    admission = saver.execution_admission(
        owner.session_id, thread_id=owner.thread_id,
        execution_id=str(case.accepted["initial_execution_id"]),
    )
    root = next(item for item in saver._storage.storage.read_items(owner.session_id, owner.thread_id)
                if item.semantic_kind == "user_input")
    start = datetime.fromisoformat(root.created_at)
    # 成员顺序用例会交换逻辑坐标；相同时刻不会把合法重排混成时间倒退。
    offsets = getattr(request, "param", (1, 1))
    items = tuple(
        CanonicalItemRecord.create(
            item_sequence=number, item_id=f"membership-reasoning-{number}",
            semantic_kind="reasoning", payload_kind="text", status="completed",
            producer_ref={"producer_kind": "provider", "producer_id": f"call-{number}"},
            payload=f"推理 {number}", created_at=(start + timedelta(seconds=seconds)).isoformat(),
            turn_id=admission.turn_id, turn_scope="turn_member",
            projection_identity=ItemProjectionIdentity(model_call_id=f"call-{number}", block_id=f"block-{number}", block_index=0),
        ) for number, seconds in zip((2, 3), offsets, strict=True)
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


@pytest.mark.parametrize("membership_projection_case", [(-1, 0), (2, 1), (2, 1.999999)], indirect=True)
def test_history_rejects_activity_time_going_backwards(membership_projection_case):
    case, admission, _items = membership_projection_case
    storage = case.saver._storage.storage
    with storage.open_read_snapshot(admission.session_id, admission.thread_id) as snapshot:
        before = tuple(snapshot.connection.iterdump())
    jsonl = storage.jsonl_path(admission.session_id, thread_id=admission.thread_id)
    before_jsonl = jsonl.read_bytes()
    # 冷 Saver 沿正式 history 入口读持久时间，不依赖热投影或测试提供的 metadata。
    cold = RolloutCheckpointSaver(storage.sessions_dir)
    with pytest.raises(RuntimeError, match="activity item 创建时间倒退"):
        cold.load_history(admission.session_id, admission.thread_id, TurnHistoryLoadRequest())
    with storage.open_read_snapshot(admission.session_id, admission.thread_id) as snapshot:
        assert tuple(snapshot.connection.iterdump()) == before
    assert jsonl.read_bytes() == before_jsonl


@pytest.mark.parametrize("delta", [timedelta(seconds=-1), timedelta(microseconds=-1)])
def test_cold_history_rejects_turn_duration_time_going_backwards(source_seal_case, delta):
    case = source_seal_case
    owner = case.owner
    storage = case.saver._storage.storage
    with storage._connect(owner.thread_id, session_id=owner.session_id) as connection:
        (created_at,) = connection.execute(
            "SELECT created_at FROM turns WHERE turn_id=?", (case.accepted["turn_id"],),
        ).fetchone()
        connection.execute(
            "UPDATE turns SET updated_at=? WHERE turn_id=?",
            ((datetime.fromisoformat(created_at) + delta).isoformat(), case.accepted["turn_id"]),
        )
    with storage.open_read_snapshot(owner.session_id, owner.thread_id) as snapshot:
        before = tuple(snapshot.connection.iterdump())
    jsonl = storage.jsonl_path(owner.session_id, thread_id=owner.thread_id)
    before_jsonl = jsonl.read_bytes()
    with (
        RolloutCheckpointSaver(storage.sessions_dir) as cold,
        pytest.raises(ValueError, match="历史 Turn projection 结束时间早于创建时间"),
    ):
        cold.load_history(owner.session_id, owner.thread_id, TurnHistoryLoadRequest())
    with storage.open_read_snapshot(owner.session_id, owner.thread_id) as snapshot:
        assert tuple(snapshot.connection.iterdump()) == before
    assert jsonl.read_bytes() == before_jsonl


@pytest.mark.parametrize("offsets", [(2, 5), (2, 1), (2, 1.999999)])
def test_internal_elapsed_keeps_same_predecessor_across_pages(source_seal_case, offsets):
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
    ) for seconds in offsets)
    with (
        session_thread_owner_scope(MutationIntentOwner(owner.session_id, owner.thread_id), langgraph_thread_id=owner.thread_id),
        execution_admission_scope(admission, langgraph_thread_id=owner.thread_id),
    ):
        saver.append_items(owner.session_id, items, thread_id=owner.thread_id)
    with storage.open_read_snapshot(owner.session_id, owner.thread_id) as snapshot:
        reader = saver._context_reader
        chain = reader.resolve_chain(snapshot)
        bounds = reader.read_history_snapshot_bounds(snapshot, chain)
        def read_page():
            return reader.read_internal_execution_history_page(
                snapshot, chain, direction="before", anchor_logical_item_ordinal=None,
                lower_logical_item_ordinal=None, upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
                snapshot_upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
                limit=1, execution_id=admission.execution_id,
            )

        if offsets[1] < offsets[0]:
            before = tuple(snapshot.connection.iterdump())
            with pytest.raises(RuntimeError, match="activity item 创建时间倒退"):
                read_page()
            assert tuple(snapshot.connection.iterdump()) == before
            return
        newest = read_page()
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
        notice_page = reader.read_internal_execution_history_page(
            snapshot, chain, direction="before", anchor_logical_item_ordinal=previous_page.entries[0].logical_item_ordinal,
            lower_logical_item_ordinal=None, upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            snapshot_upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
            limit=1, execution_id=admission.execution_id,
        )
        assert notice_page.entries[0].item.semantic_kind == "runtime_notice"
        assert notice_page.entries[0].elapsed_ms == 0


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
    if getattr(request, "param", None) == "relation_scope":
        final_message.response_metadata.pop("model_call_id")
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
    ) for number in range(request.param if isinstance(getattr(request, "param", None), int) else 0))
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


@pytest.mark.parametrize("internal_content_carrier_case", [300, 1200], indirect=True)
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
        statements = []
        snapshot.connection.set_trace_callback(
            lambda sql: statements.append(sql) if sql.startswith("WITH page_candidates") else None
        )
        while True:
            progress_ticks = 0

            def count_steps():
                nonlocal progress_ticks
                progress_ticks += 1
                return 0

            snapshot.connection.set_progress_handler(count_steps, 1000)
            started = perf_counter()
            try:
                page = reader.read_internal_execution_history_page(
                    snapshot, chain, direction="before", anchor_logical_item_ordinal=anchor,
                    lower_logical_item_ordinal=None, upper_logical_item_ordinal=bound,
                    snapshot_upper_logical_item_ordinal=bound,
                    execution_id=admission.execution_id, limit=256,
                )
            finally:
                snapshot.connection.set_progress_handler(None, 0)
            assert 0 < len(page.entries) <= 256
            assert max(rows_by_cursor.values()) <= 257
            # 指令预算允许每页检查完整冻结范围，拒绝每个候选再次扫描全范围。
            assert (progress_ticks + 1) * 1000 <= len(members) * 1000 + 100000
            print({"internal_members": len(members), "page_entries": len(page.entries),
                   "max_cursor_rows": max(rows_by_cursor.values()), "seconds": perf_counter() - started,
                   "sqlite_vm_steps_upper": (progress_ticks + 1) * 1000})
            seen[0:0] = [entry.item.item_id for entry in page.entries]
            if not page.has_more:
                break
            anchor = page.entries[0].logical_item_ordinal
        snapshot.connection.set_trace_callback(None)
        print({"query_plan": snapshot.connection.execute("EXPLAIN QUERY PLAN " + statements[0]).fetchall()})
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


@pytest.mark.parametrize("internal_content_carrier_case", [0, "relation_scope"], indirect=True)
def test_internal_content_cold_history_and_provider_keep_one_final_with_reasoning(
    internal_content_carrier_case,
):
    case, admission, raw, final, independent = internal_content_carrier_case
    restarted = RolloutCheckpointSaver(case.saver._storage.storage.sessions_dir)
    page = restarted.load_history(admission.session_id, admission.thread_id,
        TurnHistoryLoadRequest(execution_id=admission.execution_id,
                               include=["internal", "final_response", "reasoning_detail"]))
    point = restarted.load_history(admission.session_id, admission.thread_id,
        TurnHistoryLoadRequest(execution_id=admission.execution_id,
            item_ids=[independent[0].item_id, final.item_id],
            source_view_id=page.source_view_id, source_view_revision=page.source_view_revision,
            include=["internal", "text", "final_response", "reasoning_detail"]))
    assert point.next_cursor is None and not point.has_more
    assert {part.source.item_id for execution in point.internal_executions for part in execution.response_parts
            if part.kind != "context_notice"} == {independent[0].item_id, final.item_id}
    with pytest.raises(ValueError, match="不属于当前可见历史或已被替代"):
        restarted.load_history(admission.session_id, admission.thread_id,
            TurnHistoryLoadRequest(execution_id=admission.execution_id,
                item_ids=["item-lc_run--internal-content-model"],
                source_view_id=page.source_view_id, source_view_revision=page.source_view_revision,
                include=["internal", "text"]))
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
    _merge_activity_items(result, tools, activity, {})
    assert [(
        item["item_id"], item["tool_name"], item["status"], item["assistant_message_sequence"],
    ) for item in result["turn"]["activity_items"]] == [
        ("version-1", "read_file", "completed", 1),
        ("version-2", "write_file", "running", 2),
    ]


def test_activity_outline_pages_and_item_body_share_frozen_binding(membership_projection_case, monkeypatch):
    """真实存储入口证明轮廓不读正文、分页不漏项，远处正文无需遍历轮廓。"""
    case, admission, initial = membership_projection_case
    saver = case.saver
    items = tuple(CanonicalItemRecord.create(
        item_sequence=number, item_id=f"outline-reasoning-{number}", semantic_kind="reasoning",
        payload_kind="text", status="completed", producer_ref={"producer_kind":"provider", "producer_id":f"outline-call-{number}"},
        payload="LONG_REASONING_BODY" * 2048, created_at=initial[-1].created_at,
        turn_id=admission.turn_id, turn_scope="turn_member",
        projection_identity=ItemProjectionIdentity(model_call_id=f"outline-call-{number}", block_id=f"outline-block-{number}", block_index=0),
    ) for number in range(4, 605))
    with session_thread_owner_scope(MutationIntentOwner(admission.session_id, admission.thread_id), langgraph_thread_id=admission.thread_id), execution_admission_scope(admission, langgraph_thread_id=admission.thread_id):
        saver.append_items(admission.session_id, items, thread_id=admission.thread_id)
    initial_page = saver.load_history(admission.session_id, admission.thread_id, TurnHistoryLoadRequest(include=["metadata"]))
    binding = {"source_view_id": initial_page.source_view_id, "source_view_revision": initial_page.source_view_revision}
    storage = saver._storage.storage
    original_read = storage.read_items
    body_reads = []
    def guarded_read(*args, **kwargs):
        body_reads.extend(kwargs.get("item_ids") or [])
        raise AssertionError("轮廓不应打开 canonical 正文")
    monkeypatch.setattr(storage, "read_items", guarded_read)
    page = saver.load_history(admission.session_id, admission.thread_id,
        TurnHistoryLoadRequest(turn_ids=[admission.turn_id], include=["activity_outline", "assistant_text"], **binding))
    seen = []
    cursors = set()
    while True:
        parts = page.items[0].response_parts
        assert len(parts) <= 256
        assert all(part.projection == "summary" and not part.text for part in parts)
        seen.extend(part.source.item_id for part in parts)
        if not page.before_cursor:
            break
        assert page.before_cursor not in cursors
        cursors.add(page.before_cursor)
        # 续页只传游标，不能依赖前端重复声明 owner / include。
        page = saver.load_history(admission.session_id, admission.thread_id,
            TurnHistoryLoadRequest(direction="before", cursor=page.before_cursor))
    assert set(seen) == {item.item_id for item in (*initial, *items)}
    assert len(seen) == len(set(seen))
    assert not body_reads
    monkeypatch.setattr(storage, "read_items", original_read)
    point = saver.load_history(admission.session_id, admission.thread_id,
        TurnHistoryLoadRequest(turn_ids=[admission.turn_id], item_ids=[initial[0].item_id], include=["reasoning_detail"], **binding))
    assert [part.source.item_id for part in point.items[0].response_parts] == [initial[0].item_id]
    assert point.items[0].response_parts[0].text == initial[0].payload
    assert not point.has_more and point.next_cursor is None
    from app.services.infrastructure.rollout_history.errors import StaleTurnCursorError
    with pytest.raises(StaleTurnCursorError) as stale:
        saver.load_history(admission.session_id, admission.thread_id,
            TurnHistoryLoadRequest(turn_ids=[admission.turn_id], item_ids=[initial[0].item_id], include=["reasoning_detail"],
                source_view_id=binding["source_view_id"], source_view_revision=binding["source_view_revision"] + 1))

    assert stale.value.detail.current_history_view_revision == binding["source_view_revision"]


@pytest.mark.parametrize("internal_content_carrier_case", [300], indirect=True)
def test_internal_partial_pages_keep_true_segment_and_structured_group_start(internal_content_carrier_case, monkeypatch):
    case, admission, _raw, final, independent = internal_content_carrier_case
    saver = case.saver
    storage = saver._storage.storage
    original_read = storage.read_items
    def reject_activity_bodies(*args, **kwargs):
        ids = kwargs.get("item_ids") or ()
        assert not set(ids).intersection({part.item_id for part in (*independent, final)}), "首屏摘要不读取思考正文"
        return original_read(*args, **kwargs)
    monkeypatch.setattr(storage, "read_items", reject_activity_bodies)
    summary_page = saver.load_history(admission.session_id, admission.thread_id, TurnHistoryLoadRequest())
    summary_parts = summary_page.internal_executions[0].response_parts
    assert any(part.kind == "reasoning" for part in summary_parts), "纯思考执行也有可展开的活动轮廓"
    assert all(part.projection == "summary" and not part.text for part in summary_parts if part.kind == "reasoning")
    monkeypatch.setattr(storage, "read_items", original_read)
    initial = saver.load_history(admission.session_id, admission.thread_id, TurnHistoryLoadRequest(include=["metadata"]))
    outline = saver.load_history(admission.session_id, admission.thread_id,
        TurnHistoryLoadRequest(execution_id=admission.execution_id,
            include=["internal", "activity_outline", "assistant_text"],
            source_view_id=initial.source_view_id, source_view_revision=initial.source_view_revision))
    assert outline.before_cursor
    parts = outline.internal_executions[0].response_parts
    final_reasoning = next(part for part in parts if part.source.item_id == final.item_id and part.kind == "reasoning")
    # 当前尾页没有段首，但标题必须使用该段的真实首项，补前页不能改变标题。
    assert independent[0].item_id not in {part.source.item_id for part in parts}
    assert final_reasoning.source.process_segment_item_id == independent[0].item_id
    assert final_reasoning.source.activity_group_item_id == final.item_id
    assert final_reasoning.source.activity_group_part_ordinal == 0
    point = saver.load_history(admission.session_id, admission.thread_id,
        TurnHistoryLoadRequest(execution_id=admission.execution_id, item_ids=[final.item_id],
            include=["internal", "reasoning_detail", "text"],
            source_view_id=outline.source_view_id, source_view_revision=outline.source_view_revision))
    body = next(part for part in point.internal_executions[0].response_parts if part.kind == "reasoning")
    assert body.part_id == final_reasoning.part_id
    assert body.source.process_segment_item_id == final_reasoning.source.process_segment_item_id
    assert body.source.activity_group_item_id == final_reasoning.source.activity_group_item_id
    assert body.text == "核对初始化报告。"


def test_structured_turn_pages_keep_reasoning_and_true_group_coordinates(membership_projection_case):
    """一条多部位消息跨页，普通正文分组边界与点读正文保持同一真实身份。"""
    case, admission, initial = membership_projection_case
    saver = case.saver
    content = [{"type": "text", "text": "先说明检查范围。"}, *[
        {"type": "reasoning_content", "reasoning_content": f"检查步骤 {index}"}
        for index in range(300)
    ], {"type": "text", "text": "中间说明。"}, {"type": "reasoning_content", "reasoning_content": "第二组思考。"}]
    message = AIMessage(id="structured-process", content=content, response_metadata={"model_call_id": "structured-call"})
    items = LangChainMessageCodec().items_for_message(message, item_sequence=1,
        message_id=message.id, turn_id=admission.turn_id, execution_id=admission.execution_id,
        timestamp=initial[-1].created_at)
    with session_thread_owner_scope(MutationIntentOwner(admission.session_id, admission.thread_id), langgraph_thread_id=admission.thread_id), execution_admission_scope(admission, langgraph_thread_id=admission.thread_id):
        saver.append_items(admission.session_id, items, thread_id=admission.thread_id)
    metadata = saver.load_history(admission.session_id, admission.thread_id, TurnHistoryLoadRequest(include=["metadata"]))
    binding = {"source_view_id": metadata.source_view_id, "source_view_revision": metadata.source_view_revision}
    page = saver.load_history(admission.session_id, admission.thread_id,
        TurnHistoryLoadRequest(turn_ids=[admission.turn_id], include=["activity_outline", "assistant_text"], **binding))
    parts = page.items[0].response_parts
    first_group = [part for part in parts if part.source.item_id == items[0].item_id and part.kind == "reasoning" and part.source.part_ordinal < 301]
    assert first_group and first_group[0].source.part_ordinal > 1
    assert {part.source.activity_group_part_ordinal for part in first_group} == {1}
    assert all(not part.text for part in first_group)
    second_group = next(part for part in parts if part.source.part_ordinal == 302)
    assert second_group.source.activity_group_part_ordinal == 302
    seen = {part.part_id for part in parts}
    assert page.before_cursor
    earlier = saver.load_history(admission.session_id, admission.thread_id,
        TurnHistoryLoadRequest(direction="before", cursor=page.before_cursor))
    assert not seen & {part.part_id for part in earlier.items[0].response_parts}
    assert len(seen | {part.part_id for part in earlier.items[0].response_parts}) == 305
    detail = saver.load_history(admission.session_id, admission.thread_id,
        TurnHistoryLoadRequest(turn_ids=[admission.turn_id], item_ids=[items[0].item_id],
            include=["text", "reasoning_detail"], **binding))
    body = next(part for part in detail.items[0].response_parts if part.source.part_ordinal == 302)
    assert body.text == "第二组思考。"
    assert body.part_id == second_group.part_id
    assert body.source.activity_group_part_ordinal == second_group.source.activity_group_part_ordinal


def test_catalog_activity_identity_is_chosen_before_paging(membership_projection_case):
    """真实 reader 保留不同调用和不同块，相同来源的重复载体在截页前择一。"""
    case, admission, initial = membership_projection_case
    coordinates = (("same", "block-a"), ("same", "block-a"), ("other", "block-a"), ("same", "block-b"))
    items = tuple(CanonicalItemRecord.create(
        item_sequence=number, item_id=f"identity-{number}", semantic_kind="reasoning",
        payload_kind="text", status="completed", payload="相同正文",
        producer_ref={"producer_kind":"provider", "producer_id":model},
        created_at=initial[-1].created_at, turn_id=admission.turn_id, turn_scope="turn_member",
        projection_identity=ItemProjectionIdentity(model_call_id=model, block_id=block, block_index=0),
    ) for number, (model, block) in enumerate(coordinates, 4))
    with session_thread_owner_scope(MutationIntentOwner(admission.session_id, admission.thread_id), langgraph_thread_id=admission.thread_id), execution_admission_scope(admission, langgraph_thread_id=admission.thread_id):
        case.saver.append_items(admission.session_id, items, thread_id=admission.thread_id)
    binding=case.saver.load_history(admission.session_id, admission.thread_id, TurnHistoryLoadRequest(include=["metadata"]))
    page=case.saver.load_history(admission.session_id, admission.thread_id, TurnHistoryLoadRequest(
        turn_ids=[admission.turn_id], include=["activity_outline"],
        source_view_id=binding.source_view_id, source_view_revision=binding.source_view_revision,
    ))
    projected=[part for part in page.items[0].response_parts if part.source.item_id.startswith("identity-")]
    assert [part.source.item_id for part in projected] == ["identity-4", "identity-6", "identity-7"]
    assert all(part.text == "" for part in projected)
