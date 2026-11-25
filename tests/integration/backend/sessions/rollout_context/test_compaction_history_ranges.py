"""真实采用后，大单 Item 的安全原文回读只消费所选页的原字节。"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.agents.summarization.generation import CompactionGenerator, SummaryCallBudget
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.compaction import ApplyCompactionCandidateIntent
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.mapping.itemized.history import project_history_items
from tests.integration.backend.sessions.rollout_context.test_compaction_generation import (
    generation_case as generation_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_generation import (
    production_context as production_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_generation import (
    registry_db as registry_db,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_generation import (
    summary_model as summary_model,  # noqa: PLC0414
)

pytestmark = [
    pytest.mark.parametrize("registry_db", ["protected"], indirect=True),
    pytest.mark.parametrize("summary_model", [("basic-text", "chat_completions", "openai", "/chat/completions")], indirect=True),
]


@pytest.mark.asyncio
async def test_adopted_large_item_pages_read_bounded_original_bytes_and_protect_reasoning(generation_case, monkeypatch):
    saver, session, thread, connection, model, _controller, activation, freeze = generation_case
    model.profile = {**(model.profile or {}), "max_input_tokens": 1_000_000}
    admission = saver.execution_admission(session, thread_id=thread, execution_id=connection.execute(
        "SELECT execution_id FROM executions WHERE job_id='registry-job'",
    ).fetchone()[0])
    secret, signature = "受保护密文不能展示", "受保护签名不能展示"
    items = LangChainMessageCodec().items_for_message(
        AIMessage(id="history-reasoning", content=[
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "可见推理第一段。"},
                                               {"type": "reasoning_text", "text": "可见推理第二段。"}],
             "summary": [{"type": "summary_text", "text": "可见推理摘要。"}], "encrypted_content": secret},
            {"type": "thinking", "thinking": "可见思考。", "signature": signature},
        ]), item_sequence=1, message_id="history-reasoning", turn_id=admission.turn_id,
        execution_id=admission.execution_id, timestamp=datetime.now(UTC).isoformat(),
    )
    with (session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
          execution_admission_scope(admission, langgraph_thread_id=thread)):
        saver.append_items(session, items, thread_id=thread)
    candidate = freeze(count=1, payload_size=262_144)
    original = saver.read_canonical_items(session, thread)
    result = await CompactionGenerator(saver=saver, model=model, activation=activation).generate(
        candidate, budget=SummaryCallBudget(time.time() + 60),
    )
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "history-range-parent"
    checkpoint["channel_values"] = {"messages": project_history_items(original)}
    checkpoint["channel_versions"] = {"messages": "1"}
    with (session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
          execution_admission_scope(admission, langgraph_thread_id=thread)):
        config = saver.put(build_checkpoint_config(thread), checkpoint, {"source": "loop", "step": 0}, {"messages": "1"})
        view = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchone()[0]
        saver.adopt_candidate(config, ApplyCompactionCandidateIntent(candidate, view))
    assert result.coverage == candidate.refs
    owner = saver._storage.compactions
    for item in original:
        if item.semantic_kind != "reasoning":
            continue
        start, count = connection.execute("SELECT line_start,line_count FROM compaction_history_ranges WHERE item_id=?", (item.item_id,)).fetchone()
        text = owner.read_history(session, thread, line_offset=start, limit=count)
        assert secret not in text and signature not in text
        assert "可见" in text
        assert "encrypted_hash" in text or "signature_present" in text
    large = max(original, key=lambda item: len(str(item.payload)))
    start, count = connection.execute("SELECT line_start,line_count FROM compaction_history_ranges WHERE item_id=?", (large.item_id,)).fetchone()
    assert count > 200
    source = saver._storage.storage.jsonl_path(session, thread_id=thread)
    real_open = Path.open
    reads = []

    class ObservedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.stream.close()

        def seek(self, offset):
            return self.stream.seek(offset)

        def read(self, length):
            reads.append(length)
            return self.stream.read(length)

    def open_observed(path, *args, **kwargs):
        stream = real_open(path, *args, **kwargs)
        return ObservedReader(stream) if path == source else stream

    def reject_whole_item(*_args, **_kwargs):
        raise AssertionError("分页不得读取整个 canonical Item")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_observed)
        patch.setattr(saver._storage.storage, "read_items", reject_whole_item)
        page = owner.read_history(session, thread, line_offset=start + 100, limit=3)
    assert len(page.splitlines()) == 3
    assert 0 < sum(reads) <= 3 * 6144 and max(reads) <= 6144
    stored = connection.execute("SELECT prefix,source_offset,source_length,source_hash,literal FROM compaction_history_lines").fetchall()
    assert all(secret not in str(row) and signature not in str(row) and "原始约束0" not in str(row) for row in stored)
    offset = connection.execute("SELECT jsonl_offset FROM item_catalog WHERE item_id=?", (large.item_id,)).fetchone()[0]
    source_offset = connection.execute(
        "SELECT source_offset FROM compaction_history_lines WHERE item_id=? AND line_ordinal=100", (large.item_id,),
    ).fetchone()[0]
    with source.open("r+b") as stream:
        stream.seek(offset + source_offset)
        before = stream.read(1)
        stream.seek(offset + source_offset)
        stream.write(b"x" if before != b"x" else b"y")
        stream.flush()
        try:
            with pytest.raises(ValueError, match="原文片段与采用时冻结字节不一致"):
                owner.read_history(session, thread, line_offset=start + 100, limit=1)
        finally:
            stream.seek(offset + source_offset)
            stream.write(before)


@pytest.mark.asyncio
async def test_rewind_then_adopt_appends_only_new_original_ranges_and_pruning_preserves_positions(generation_case):
    saver, session, thread, connection, model, _controller, activation, freeze = generation_case
    first = freeze(count=1, payload_size=400)
    original = saver.read_canonical_items(session, thread)
    admission = saver.execution_admission(session, thread_id=thread, execution_id=connection.execute(
        "SELECT execution_id FROM executions WHERE job_id='job-0'",
    ).fetchone()[0])
    generator = CompactionGenerator(saver=saver, model=model, activation=activation)
    await generator.generate(first, budget=SummaryCallBudget(time.time() + 60))
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "history-before-rewind"
    checkpoint["channel_values"] = {"messages": project_history_items(original)}
    checkpoint["channel_versions"] = {"messages": "1"}
    with (session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
          execution_admission_scope(admission, langgraph_thread_id=thread)):
        config = saver.put(build_checkpoint_config(thread), checkpoint, {"source": "loop", "step": 0}, {"messages": "1"})
        view = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchone()[0]
        applied = saver.adopt_candidate(config, ApplyCompactionCandidateIntent(first, view))
        before = saver._storage.compactions.read_history(session, thread)
        ranges = connection.execute("SELECT item_id,line_start,line_count FROM compaction_history_ranges ORDER BY line_start").fetchall()
        next_line = ranges[-1][1] + ranges[-1][2]
        config = saver.rewind(applied.config, checkpoint_id=checkpoint["id"])
        assert saver._storage.compactions.read_history(session, thread) == before
        new_items = LangChainMessageCodec().items_for_message(
            AIMessage(id="history-after-rewind", content="回退后的新目标"), item_sequence=1, message_id="history-after-rewind",
            turn_id=admission.turn_id, execution_id=admission.execution_id, timestamp=datetime.now(UTC).isoformat(),
        )
        saver.append_items(session, new_items, thread_id=thread)
        with saver._context_reader.open_snapshot(session, thread, "") as snapshot:
            selected = saver._storage.checkpoints.committed_context_items(snapshot, predecessor=None, include_pending_notices=False)
        second = saver._storage.compactions.freeze(
            session, thread, candidate_id="history-after-rewind", task_id="history-after-rewind-task",
            item_ids=tuple(item.item_id for item in selected), model_config_hash=sha256_jcs({"model": "frozen"}),
        )
        await generator.generate(second, budget=SummaryCallBudget(time.time() + 60))
        next_checkpoint = {**checkpoint, "id": "history-second-parent", "channel_values": {"messages": project_history_items(selected)},
                           "channel_versions": {"messages": "2"}}
        config = saver.put(config, next_checkpoint, {"source": "loop", "step": 1}, {"messages": "2"})
        view = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchone()[0]
        saver.adopt_candidate(config, ApplyCompactionCandidateIntent(second, view))
        assert connection.execute(
            "SELECT item_id,line_start,line_count FROM compaction_history_ranges ORDER BY line_start LIMIT ?", (len(ranges),),
        ).fetchall() == ranges
        assert connection.execute("SELECT line_start FROM compaction_history_ranges WHERE item_id=?", (new_items[0].item_id,)).fetchone() == (next_line,)
        assert saver._storage.compactions.read_history(session, thread, line_offset=1, limit=next_line-1) == before
        after = saver._storage.compactions.read_history(session, thread)
        assert "回退后的新目标" in after
        plan = saver.plan_pruning(thread)
        assert plan.candidates
        saver.execute_pruning(thread, plan)
        assert saver._storage.compactions.read_history(session, thread) == after
        # 模拟原 owner 已释放早段正文；位置索引继续存在，后段不能向前补位。
        later_page = saver._storage.compactions.read_history(session, thread, line_offset=next_line, limit=200)
        missing_row = connection.execute("SELECT * FROM item_catalog WHERE item_id=?", (ranges[0][0],)).fetchone()
        try:
            connection.execute("DELETE FROM item_catalog WHERE item_id=?", (ranges[0][0],))
            connection.commit()
            with pytest.raises(ValueError, match=f"detail-unavailable:.*line_start={ranges[0][1]}"):
                saver._storage.compactions.read_history(session, thread, line_offset=ranges[0][1], limit=1)
            assert saver._storage.compactions.read_history(session, thread, line_offset=next_line, limit=200) == later_page
            assert later_page.startswith(f"{next_line}:")
            assert "回退后的新目标" in later_page
            assert connection.execute(
                "SELECT item_id,line_start,line_count FROM compaction_history_ranges ORDER BY line_start LIMIT ?", (len(ranges),),
            ).fetchall() == ranges
        finally:
            connection.execute(f"INSERT INTO item_catalog VALUES ({','.join('?' for _ in missing_row)})", missing_row)
            connection.commit()
