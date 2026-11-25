"""真实分片请求、canonical 覆盖与冷恢复；不以局部后缀冒充整个原件。"""

from __future__ import annotations

import time
from dataclasses import replace
from itertools import pairwise

import pytest

from app.agents.summarization.generation import CompactionGenerator, SummaryCallBudget
from app.domain.itemized.compaction import CanonicalTextRange
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.testing.model_stream import responses_frames
from tests.integration.backend.sessions.rollout_context.test_compaction_generation import (
    generation_case as generation_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_context as production_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_derived_summary_request import (
    summary_model as summary_model,  # noqa: PLC0414
)
from tests.support.rollout.context_registry import (
    registry_db as registry_db,  # noqa: PLC0414
)

pytestmark = [
    pytest.mark.parametrize("registry_db", ["protected"], indirect=True),
    pytest.mark.parametrize("summary_model", [
        ("basic-text", "chat_completions", "openai", "/chat/completions"),
        ("responses-basic-text", "responses", "openai", "/responses"),
    ], indirect=True),
]


@pytest.fixture
def distinct_response_assets(summary_model, monkeypatch):
    """复用同一响应资产和协议编码，仅为多次真实请求分配不同的服务端身份。"""
    _model, controller, protocol = summary_model
    if protocol != "responses":
        return
    scenario = controller.scenario
    original = scenario.cassette.interactions[0]
    response = next(frame.payload["response"] for frame in original.response.frames if frame.event == "response.completed")
    controller.ensure_installed()
    coordinator_type = type(controller.transport._coordinator)
    select = coordinator_type.select

    def select_distinct(coordinator, request):
        interaction = select(coordinator, request)
        index = coordinator.hit_count(interaction.index)
        return replace(interaction, response=replace(interaction.response, frames=responses_frames(
            response_id=f"resp_slice_{index}", model=response["model"],
            output=[{**item, "id": f"{item['id']}_slice_{index}"} for item in response["output"]], usage=response["usage"],
        )))

    monkeypatch.setattr(coordinator_type, "select", select_distinct)


@pytest.mark.asyncio
async def test_large_plain_item_is_fully_sliced_and_merged_without_changing_its_original(generation_case, distinct_response_assets):
    saver, session, thread, connection, model, controller, activation, _freeze = generation_case
    text = "".join(f"位置{index:04d}: 中段与末尾都必须保留。\n" for index in range(400))
    accepted = saver.accept_turn(
        session, thread_id=thread, accepted_ingress_id="large-text", acceptance_idempotency_key="accept:large-text",
        job_id="job:large-text", admission_idempotency_key="admit:large-text", payload=text, payload_kind="text",
    )
    candidate = saver._storage.compactions.freeze(
        session, thread, candidate_id="text-range", task_id="text-range-task",
        item_ids=(accepted["root_input_item_id"],), model_config_hash=sha256_jcs({"model": "frozen"}),
    )
    model.profile = {**(model.profile or {}), "max_input_tokens": 1300}
    before = connection.execute("SELECT head_view_id,head_checkpoint_id FROM branches WHERE status='active'").fetchall()
    budget = SummaryCallBudget(time.time() + 60)
    result = await CompactionGenerator(saver=saver, model=model, activation=activation).generate(candidate, budget=budget)
    assert result.coverage == candidate.refs and result.text_ranges == ()
    calls = connection.execute(
        "SELECT call.assembly_id FROM model_calls call JOIN storage_commits admission ON admission.subject_id=call.execution_id "
        "WHERE admission.commit_kind='execution_admission' AND json_extract(admission.metadata_json,'$.request_input.reference.task_id')=? "
        "ORDER BY call.created_at,call.rowid", (candidate.task_id,),
    ).fetchall()
    parts, fragments = [], []
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        for (assembly_id,) in calls:
            snapshot = cold.get_context_assembly(session, thread, assembly_id=assembly_id)
            for frame in snapshot.seal_binding.prefix.state.frames:
                if not isinstance(frame.content, dict) or "text_range" not in frame.content:
                    continue
                part = CanonicalTextRange.from_dict(frame.content["text_range"])
                assert part.ref == candidate.refs[0] and part.text_length == len(text)
                parts.append(part)
                fragments.append(frame.content["message"]["data"]["content"])
                wire = snapshot.request_hash_preimage
                wire_messages = wire.get("messages", wire.get("input"))
                sent_text = [message["content"] if isinstance(message.get("content"), str) else
                             "".join(block.get("text", "") for block in message.get("content", ()))
                             for message in wire_messages]
                assert fragments[-1] in sent_text
        assert len(parts) > 1
        assert parts[0].start == 0 and parts[-1].end == len(text)
        assert all(left.end == right.start for left, right in pairwise(parts))
        assert "".join(fragments) == text
        assert cold._storage.compactions.read_result(session, thread, candidate.candidate_id) == result
        original, = cold.read_canonical_items(session, thread, item_ids=(candidate.refs[0].ref_id,))
        assert original.payload == text
    assert len(calls) == budget.calls_used == len(parts) + 1
    assert sum(controller.transport.hit_counts.values()) == len(calls)
    assert connection.execute("SELECT head_view_id,head_checkpoint_id FROM branches WHERE status='active'").fetchall() == before
