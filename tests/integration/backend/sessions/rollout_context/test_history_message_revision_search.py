"""真实图替换与 rewind 后，正文查询只消费对应 view 的 canonical 版本。"""

from time import monotonic
from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import AIMessage, AnyMessage
from langgraph.graph import END, START, StateGraph

from app.core.checkpoint_config import build_checkpoint_config
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.schemas.internal_v2.turn_search import TurnMessageSearchRequest
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    MessagesDeltaChannel,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_history import message_search
from app.services.infrastructure.turn_history.models import StaleTurnReferenceError
from tests.integration.backend.sessions.rollout_context.test_message_revisions import (
    _execution_scope,
)
from tests.integration.backend.sessions.rollout_context.test_message_revisions import (
    revision_case as revision_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_message_revisions import (
    start_model_call as start_model_call,  # noqa: PLC0414
)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_message_search_and_around_follow_graph_replacement_and_rewind(revision_case, asynchronous):
    saver, admission, _index, source, _revision, child = revision_case
    session, thread = admission.session_id, admission.thread_id
    original = saver.search_history_messages(session, thread, TurnMessageSearchRequest(query="原始正文"))
    assert original.complete and len(original.items) == 1
    old_reference = original.items[0].reference
    assert old_reference.item_id == source.item_id

    class State(TypedDict):
        messages: Annotated[list[AnyMessage], MessagesDeltaChannel(snapshot_frequency=2)]

    graph = StateGraph(State)
    graph.add_node("replace", lambda _: {"messages": [AIMessage(
        id="revision-answer", content="😀替换后的新正文",
        response_metadata={"model_call_id": source.projection_identity.model_call_id},
    )]})
    graph.add_edge(START, "replace")
    graph.add_edge("replace", END)
    compiled = graph.compile(checkpointer=saver)
    with _execution_scope(admission):
        if asynchronous:
            await compiled.ainvoke({"messages": []}, child)
        else:
            compiled.invoke({"messages": []}, child)
    # 冷 reader 仍使用不可变 locator，不能拿相同 message ID 猜测正文版本。
    cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
    old = cold.search_history_messages(session, thread, TurnMessageSearchRequest(query="原始正文"))
    assert old.complete and old.items == []
    updated = cold.search_history_messages(session, thread, TurnMessageSearchRequest(query="新正文"))
    assert updated.complete and len(updated.items) == 1
    reference = updated.items[0].reference
    assert reference.item_id != source.item_id and reference.start_codepoint == 5
    assert updated.items[0].message_id == original.items[0].message_id == "revision-answer"
    around = TurnHistoryLoadRequest(direction="around", anchor_turn_id=admission.turn_id, anchor_message=reference)
    assert cold.load_history(session, thread, around).items[0].turn_id == admission.turn_id
    with pytest.raises(StaleTurnReferenceError):
        cold.load_history(session, thread, around.model_copy(update={"anchor_message": old_reference}))
    with _execution_scope(admission):
        cold.rewind(build_checkpoint_config(thread), checkpoint_id=child["configurable"]["checkpoint_id"])
    restored = cold.search_history_messages(session, thread, TurnMessageSearchRequest(query="原始正文"))
    assert restored.complete and [hit.reference for hit in restored.items] == [old_reference]
    assert cold.search_history_messages(session, thread, TurnMessageSearchRequest(query="新正文")).items == []
    with pytest.raises(StaleTurnReferenceError):
        cold.load_history(session, thread, around)


def test_message_search_byte_and_time_budgets_keep_explicit_coverage(revision_case, monkeypatch: pytest.MonkeyPatch):
    saver, admission, _index, _source, _revision, _child = revision_case
    session, thread = admission.session_id, admission.thread_id
    reader = saver._context_reader
    with reader.open_snapshot(session, thread) as snapshot:
        rows = reader.read_message_search_candidates(
            snapshot, reader.resolve_chain(snapshot), lower_ordinal=0, limit=10,
            deadline=monotonic() + 1,
        )
    assert len(rows) == 2
    with monkeypatch.context() as patch:
        # 使用真实 locator 长度限定每页只容纳一个正文，不伪造扫描 metadata。
        patch.setattr(message_search, "MAX_SEARCH_BYTES", max(row[3] for row in rows))
        page = saver.search_history_messages(session, thread, TurnMessageSearchRequest(query="不存在的字面量"))
        assert page.limited_reason == "byte_limit" and not page.complete and page.cursor
        assert page.scanned_items == 1 and page.scanned_bytes == rows[0][3] and page.items == []
        next_page = saver.search_history_messages(session, thread, TurnMessageSearchRequest(query="不存在的字面量", cursor=page.cursor))
        assert next_page.complete and next_page.cursor is None and next_page.scanned_items == 1
        assert next_page.scanned_bytes == rows[1][3] and next_page.items == []
    with monkeypatch.context() as patch:
        patch.setattr(message_search, "SEARCH_SECONDS", 0)
        page = saver.search_history_messages(session, thread, TurnMessageSearchRequest(query="原始正文"))
        assert page.limited_reason == "deadline" and not page.complete and page.cursor
        assert page.scanned_items == page.scanned_bytes == 0 and page.items == []
