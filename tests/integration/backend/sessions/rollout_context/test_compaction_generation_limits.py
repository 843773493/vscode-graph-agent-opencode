"""真实摘要调用必须缩减来源，并受首次绝对期限约束。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from app.agents.summarization.generation import CompactionGenerator, SummaryCallBudget
from app.agents.summarization.state import (
    CompactionReductionError,
    SummaryGenerationError,
)
from app.domain.itemized.compaction import ReadyCompactionCandidate
from app.domain.itemized.hashing import sha256_jcs
from app.testing.model_stream import chat_completion_frames
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
    pytest.mark.parametrize("summary_model", [("basic-text", "chat_completions", "openai", "/chat/completions")], indirect=True),
]


@pytest.mark.asyncio
async def test_summary_keeps_source_update_after_excluded_canonical_tail(generation_case, production_context):
    saver, session, thread, connection, model, controller, activation, freeze = generation_case
    previous = freeze(count=1, payload_size=400)
    manager, prepare = production_context[4:]
    saver.accept_turn(
        session, thread_id=thread, accepted_ingress_id="excluded-latest", acceptance_idempotency_key="excluded-accept",
        job_id="excluded-job", admission_idempotency_key="excluded-admit", payload="UNSELECTED_LATEST_INPUT", payload_kind="text",
    )
    manager.observe("instruction:R01", "SOURCE_UPDATED_AFTER_TAIL", revision="instructions:2")
    parent = prepare("source-update-after-tail")
    candidate = saver._storage.compactions.freeze(
        session, thread, candidate_id="updated-source-candidate", task_id="updated-source-task",
        item_ids=tuple(ref.ref_id for ref in previous.refs), model_config_hash=previous.model_config_hash,
    )
    assert candidate.source_assembly_id == parent["snapshot"].assembly_id
    before = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall()
    result = await CompactionGenerator(saver=saver, model=model, activation=activation).generate(
        candidate, budget=SummaryCallBudget(time.time() + 30),
    )
    wire = saver.read_context_for_dispatch(result.dispatch_ref)["snapshot"].request_hash_preimage
    assert "SOURCE_UPDATED_AFTER_TAIL" in str(wire)
    assert "UNSELECTED_LATEST_INPUT" not in str(wire)
    assert result.source_versions == (("instruction:R01", "instructions:2"),)
    assert saver._storage.compactions.latest_ready(session, thread) == ReadyCompactionCandidate(candidate, result, False)
    assert connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall() == before
    assert controller.transport.hit_counts == {0: 1}
    manager.observe("instruction:R01", "后续真实来源变化仍须使候选失效", revision="instructions:3")
    assert saver._storage.compactions.latest_ready(session, thread) is None


@pytest.mark.asyncio
async def test_three_merge_levels_cannot_publish_a_partial_result(generation_case):
    saver, session, thread, connection, model, controller, activation, freeze = generation_case
    model.profile = {**(model.profile or {}), "max_input_tokens": 1050}
    frozen = freeze(count=10)
    scenario = controller.scenario
    interaction = scenario.cassette.interactions[0]
    frames = chat_completion_frames(
        response_id="bounded-merge", model=interaction.request.match["model"],
        deltas=({"role": "assistant", "content": "S" * 1000},), finish_reason="stop",
    )
    controller.scenario = replace(scenario, cassette=replace(scenario.cassette, path=None, interactions=(
        replace(interaction, response=replace(interaction.response, frames=frames)),
    )))
    before = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall()
    budget = SummaryCallBudget(time.time() + 60)
    with pytest.raises(SummaryGenerationError, match="compaction-merge-depth-exhausted"):
        await CompactionGenerator(saver=saver, model=model, activation=activation).generate(frozen, budget=budget)
    # 10 块经历 5、2、1 次合并；各层单独剩余结果直接沿用，不重复发送。
    assert budget.calls_used == 18
    assert controller.transport.hit_counts == {0: 18}
    assert saver._storage.compactions.read_result(session, thread, frozen.candidate_id) is None
    assert connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall() == before


@pytest.mark.asyncio
async def test_initial_expanding_summary_is_not_published(generation_case):
    saver, session, thread, connection, model, controller, activation, freeze = generation_case
    frozen = freeze(count=1, payload_size=400)
    scenario = controller.scenario
    interaction = scenario.cassette.interactions[0]
    frames = chat_completion_frames(
        response_id="expanded-summary", model=interaction.request.match["model"],
        deltas=({"role": "assistant", "content": "重复而膨胀的摘要。" * 2000},), finish_reason="stop",
    )
    controller.scenario = replace(scenario, cassette=replace(scenario.cassette, path=None, interactions=(
        replace(interaction, response=replace(interaction.response, frames=frames)),
    )))
    before = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall()
    with pytest.raises(CompactionReductionError, match="compaction-summary-did-not-reduce"):
        await CompactionGenerator(saver=saver, model=model, activation=activation).generate(
            frozen, budget=SummaryCallBudget(time.time() + 30),
        )
    assert saver._storage.compactions.read_result(session, thread, frozen.candidate_id) is None
    assert connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall() == before
    assert controller.transport.hit_counts == {0: 1}


@pytest.mark.asyncio
async def test_same_range_successor_cannot_publish_an_unchanged_summary(generation_case):
    saver, session, thread, connection, model, controller, activation, freeze = generation_case
    first = freeze(count=1)
    generator = CompactionGenerator(saver=saver, model=model, activation=activation)
    result = await generator.generate(first, budget=SummaryCallBudget(time.time() + 30))
    successor = saver._storage.compactions.freeze(
        session, thread, candidate_id="same-range-no-reduction", task_id="same-range-no-reduction-task",
        item_ids=tuple(ref.ref_id for ref in first.refs), model_config_hash=first.model_config_hash,
        predecessor_candidate_id=first.candidate_id, target_constraint_hash=sha256_jcs({"smaller": True}),
    )
    before = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall()
    with pytest.raises(CompactionReductionError, match="compaction-summary-did-not-reduce"):
        await generator.generate(successor, budget=SummaryCallBudget(time.time() + 30), derived_summaries=(result,))
    assert saver._storage.compactions.read_result(session, thread, successor.candidate_id) is None
    assert connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall() == before
    assert controller.transport.hit_counts == {0: 2}


@pytest.mark.asyncio
async def test_absolute_deadline_cancels_the_real_inflight_sdk_request_without_publishing(generation_case, monkeypatch):
    saver, session, thread, connection, model, controller, activation, freeze = generation_case
    frozen = freeze(count=1)
    controller.ensure_installed()
    entered, never = asyncio.Event(), asyncio.Event()

    async def hold_response(_request, **_kwargs):
        entered.set()
        await never.wait()
        raise AssertionError("绝对期限必须先取消请求")

    monkeypatch.setattr(controller.transport, "handle_async_request", hold_response)
    before = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall()
    budget = SummaryCallBudget(time.time() + 2)
    task = asyncio.create_task(CompactionGenerator(saver=saver, model=model, activation=activation).generate(frozen, budget=budget))
    async with asyncio.timeout(2):
        await entered.wait()
    with pytest.raises(TimeoutError):
        await task
    assert budget.calls_used == 1
    assert saver._storage.compactions.read_result(session, thread, frozen.candidate_id) is None
    assert connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall() == before
    assert connection.execute("SELECT COUNT(*) FROM executions WHERE execution_origin='internal' AND outcome='running'").fetchone() == (0,)
