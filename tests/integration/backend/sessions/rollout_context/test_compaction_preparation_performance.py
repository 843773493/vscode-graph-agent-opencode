"""大物理历史下，真实候选校验及模型准备／seal 只消费有界活动选集。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from statistics import median, quantiles
from time import perf_counter

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_compaction_preparation import (
    compaction_runtime_case as compaction_runtime_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_preparation import (
    generation_case as generation_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_preparation import (
    model_preparation_case as model_preparation_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_preparation import (
    production_context as production_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_preparation import (
    registry_db as registry_db,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_preparation import (
    summary_model as summary_model,  # noqa: PLC0414
)

pytestmark = [
    pytest.mark.parametrize("registry_db", ["protected"], indirect=True),
    pytest.mark.parametrize("summary_model", [("basic-text", "chat_completions", "openai", "/chat/completions")], indirect=True),
    pytest.mark.parametrize("model_preparation_case", ["typed-scope-performance"], indirect=True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("history_items", [50_000, 100_000])
async def test_bounded_preparation_and_seal_with_large_canonical_history(
    model_preparation_case, request, monkeypatch, history_items,
):
    execution, incoming, model, tokens, enabled, compactor, registry, connection, controller, session, thread = model_preparation_case
    saver = execution._checkpointer
    storage = saver._storage.storage
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    source_paths = sorted({
        Path(__file__).relative_to(Path.cwd()).as_posix(),
        "tests/integration/backend/sessions/rollout_context/test_compaction_preparation.py",
        "tests/integration/backend/sessions/rollout_context/test_compaction_generation.py",
        "app/agents/model_call_execution.py", "app/agents/model_call_graph.py", "app/agents/summarization/generation.py",
        "app/core/session_control_store/background_work.py",
        "app/domain/itemized/compaction.py", "app/services/mapping/itemized/carrier_dedup.py",
        "app/services/orchestration/background_work/registry.py",
        "app/services/orchestration/background_work/compaction.py",
        "app/runtime/context_compaction.py",
        *(path.relative_to(Path.cwd()).as_posix() for path in
          (Path.cwd() / "app/services/infrastructure/rollout_context").rglob("*.py")),
        *(path.relative_to(Path.cwd()).as_posix() for path in
          (Path.cwd() / "app/services/infrastructure/rollout_context/storage/migrations").rglob("*.sql")),
    })
    before_sources = {path: sha256((Path.cwd() / path).read_bytes()).hexdigest() for path in source_paths}
    admission = execution._request_preparation(incoming("history-admission")).admission
    checkpoint_id = incoming("history-checkpoint").runtime.execution_info.checkpoint_id
    # 新 checkpoint 保护之前的冻结 view，写入历史经真实 owner 后再回退，保留物理原件。
    config = saver.put(build_checkpoint_config(thread, checkpoint_id=checkpoint_id), empty_checkpoint(),
                       {"source": "loop", "step": 0}, {})
    codec = LangChainMessageCodec()
    timestamp = datetime.now(UTC).isoformat()
    seed_started = perf_counter()
    for start in range(0, history_items, 5000):
        items = tuple(item for index in range(start, min(start + 5000, history_items))
                      for item in codec.items_for_message(
                          AIMessage(content=f"历史输出{index}", id=f"performance-history-{index}",
                                    response_metadata={"model_call_id": f"performance-history-model-{index}"}),
                          item_sequence=1, message_id=f"performance-history-{index}",
                          turn_id=admission.turn_id, execution_id=admission.execution_id, timestamp=timestamp,
                      ))
        saver.append_items(session, items, thread_id=thread)
    seed_ms = (perf_counter() - seed_started) * 1000
    assert connection.execute("SELECT COUNT(*) FROM item_catalog").fetchone()[0] >= history_items
    rewind_started = perf_counter()
    saver.rewind(config, checkpoint_id=checkpoint_id)
    rewind_ms = (perf_counter() - rewind_started) * 1000
    owner = MutationIntentOwner(session, thread)
    candidate_ids = compactor.source_item_ids(owner)
    assert 1 <= len(candidate_ids) <= 8
    candidate_items = saver.read_canonical_items(session, thread, item_ids=candidate_ids)
    candidate_scopes = sorted({item.projection_identity.model_call_id for item in candidate_items
                               if item.projection_identity is not None and item.projection_identity.model_call_id is not None})
    assert "performance-active-model" in candidate_scopes
    active_count = connection.execute(
        "SELECT COUNT(*) FROM context_view_items member JOIN branches branch ON branch.head_view_id=member.view_id "
        "WHERE branch.status='active' AND member.visible=1 AND member.context_selected=1 AND member.source_kind='canonical'",
    ).fetchone()[0]
    assert active_count <= 8
    enabled[0] = True
    model.profile = {**model.profile, "max_input_tokens": int(tokens / 0.85)}
    first_started = perf_counter()
    with execution.graph_request_scope() as adopted:
        await execution._aprepare(incoming("large-history-first"))
        assert adopted() is None
    first_prepare_ms = (perf_counter() - first_started) * 1000
    handle, = registry.list_handles(session)
    wait_started = perf_counter()
    async with asyncio.timeout(60):
        frozen, result = await compactor.wait(handle)
    generation_wait_ms = (perf_counter() - wait_started) * 1000
    assert tuple(ref.ref_id for ref in frozen.refs) == candidate_ids
    assert controller.transport.hit_counts == {0: 1}
    cold_started = perf_counter()
    with RolloutCheckpointSaver(storage.sessions_dir) as cold:
        assert cold._storage.compactions.read_result(session, thread, frozen.candidate_id) == result
    cold_ready_read_ms = (perf_counter() - cold_started) * 1000
    measurements = []
    item_reads, sql_count, vm_steps = [], [0], [0]
    read_items, connect = storage.read_items, storage._connect

    def bounded_read(*args, item_ids=None, **kwargs):
        assert item_ids is not None, "模型准备不得读取全量 canonical 历史"
        values = tuple(item_ids)
        assert len(values) <= 8, "真实读入必须受活动选集或候选范围约束"
        item_reads.append(len(values))
        return read_items(*args, item_ids=values, **kwargs)

    def trace(_statement):
        sql_count[0] += 1

    def progress():
        vm_steps[0] += 1000
        return 0

    def observed_connect(*args, **kwargs):
        opened = connect(*args, **kwargs)
        opened.set_trace_callback(trace)
        opened.set_progress_handler(progress, 1000)
        return opened

    assemblies_before = connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone()[0]
    with monkeypatch.context() as patch:
        patch.setattr(storage, "read_items", bounded_read)
        patch.setattr(storage, "_connect", observed_connect)
        for index in range(20):
            item_reads.clear()
            sql_count[0] = vm_steps[0] = 0
            started = perf_counter()
            with execution.graph_request_scope() as adopted:
                prepared = await execution._aprepare(incoming(f"large-history-hot-{index}"))
                assert adopted() is None
            elapsed_ms = (perf_counter() - started) * 1000
            snapshot = prepared["snapshot"]
            measurements.append({"elapsed_ms": elapsed_ms, "assembly_id": snapshot.assembly_id,
                                 "request_hash": snapshot.request_hash, "item_read_sizes": list(item_reads),
                                 "sql_statements": sql_count[0], "sqlite_vm_steps_lower_bound": vm_steps[0]})
            assert "保留新的最终目标" in str(snapshot.request_hash_preimage)
            assert "performance-history-" not in str(snapshot.request_hash_preimage)
    assert connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone()[0] == assemblies_before + 20
    assert len({row["assembly_id"] for row in measurements}) == 20
    assert controller.transport.hit_counts == {0: 1}
    # 采用也走相同模型准备入口，证明 measured ready 不是测试预制的结果。
    model.profile = {**model.profile, "max_input_tokens": int(tokens / 0.95)}
    item_reads.clear()
    sql_count[0] = vm_steps[0] = 0
    adopt_started = perf_counter()
    with monkeypatch.context() as patch:
        patch.setattr(storage, "read_items", bounded_read)
        patch.setattr(storage, "_connect", observed_connect)
        with execution.graph_request_scope() as adopted:
            compacted = await execution._aprepare(incoming("large-history-adopt"))
            assert adopted() is not None
    adoption_prepare_ms = (perf_counter() - adopt_started) * 1000
    assert "需要保留的旧约束。" * 10 not in str(compacted["snapshot"].request_hash_preimage)
    assert connection.execute("SELECT applied_view_id FROM context_compaction_candidates WHERE candidate_id=?",
                              (frozen.candidate_id,)).fetchone()[0] is not None
    samples = [row["elapsed_ms"] for row in measurements]
    p95 = quantiles(samples, n=100, method="inclusive")[94]
    after_sources = {path: sha256((Path.cwd() / path).read_bytes()).hexdigest() for path in source_paths}
    output = context.performance_dir
    output.mkdir(parents=True, exist_ok=True)
    (output / "compaction-preparation-performance.json").write_text(json.dumps({
        "historical_items": history_items, "physical_items": connection.execute("SELECT COUNT(*) FROM item_catalog").fetchone()[0],
        "active_items": active_count, "candidate_refs": len(frozen.refs), "samples": measurements,
        "candidate_model_call_ids": candidate_scopes,
        "p50_ms": median(samples), "p95_ms": p95, "max_ms": max(samples),
        "seed_ms": seed_ms, "rewind_ms": rewind_ms, "first_prepare_ms": first_prepare_ms,
        "generation_wait_ms": generation_wait_ms, "cold_ready_read_ms": cold_ready_read_ms,
        "adoption_prepare_ms": adoption_prepare_ms, "adoption_sqlite_vm_steps_lower_bound": vm_steps[0],
        "sources_before": before_sources, "sources_after": after_sources,
        "boundary": "真实Saver追加并rewind保留物理历史；SDK生成ready；20次主模型准备、模拟与seal，最后真实采用。",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert before_sources == after_sources, "性能样本期间相关源码发生变化"
    assert p95 <= 500 and max(samples) <= 1500 and adoption_prepare_ms <= 1500
    assert max(row["sqlite_vm_steps_lower_bound"] for row in measurements) < 100_000
    assert vm_steps[0] < 100_000
