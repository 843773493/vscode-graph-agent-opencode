"""真实进程退出窗口中的后台租约、消息与 checkpoint 恢复。"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_contracts import (
    BackgroundTaskDescriptor,
    BackgroundToolResultIdentity,
)
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.execution_admission_context import execution_admission_scope
from app.core.path_utils import get_session_path_resolver
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ToolResultEvidence,
)
from app.domain.itemized.identity.tool_call_identity import software_tool_operation_id
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.background_work_handlers import (
    SystemTimeEmitterHandler,
)
from app.services.infrastructure.background_work_store import BackgroundWorkStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.stream_accumulator import (
    CanonicalBlockAccumulator,
)
from app.services.orchestration.execution_step.stream_bindings import CanonicalItemSink
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from tests.harness.python.run_context import TestRunContext
from tests.support.background_work import ControlledBackgroundHandler

_PROCESS = r'''
import asyncio, os, sys
from pathlib import Path
from app.core.background_task_contracts import BackgroundTaskDescriptor
from app.core.background_task_registry import BackgroundTaskRegistry
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.background_work_handlers import SystemTimeEmitterHandler
from app.services.infrastructure.background_work_store import BackgroundWorkStore
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from tests.support.session_catalog_owner import fixture_catalog_scope
with fixture_catalog_scope():
    mode, root, session, thread, raw = sys.argv[1:]
    store = BackgroundWorkStore(sessions_dir=Path(root))
    descriptor = BackgroundTaskDescriptor.model_validate_json(raw)
    handler = SystemTimeEmitterHandler()
    task_id = "bgt_process-window"
    if mode == "create":
        with store.control(session) as control:
            control.connection.set_trace_callback(lambda sql: os._exit(81) if sql.startswith("INSERT INTO background_tasks ") else None)
            control.create_background_task(task_id=task_id, session_id=session, thread_id=thread, agent_id="agent", descriptor=descriptor,
                checkpoint=handler.initial_checkpoint(descriptor), preimage_hash=sha256_jcs({"session_id":session,"thread_id":thread,"agent_id":"agent","descriptor":descriptor.model_dump(mode="json")}).split(":")[-1])
    elif mode == "commit":
        with store.control(session) as control:
            handle = control.get_background_task(task_id)
            step = asyncio.run(handler.step(handle))
            control.connection.set_trace_callback(lambda sql: os._exit(82) if sql.startswith("UPDATE background_tasks SET checkpoint_json") else None)
            control.commit_background_step(task_id, token=handle.fencing_token, step=step)
    elif mode == "canonical":
        from app.core.execution_admission_context import execution_admission_scope
        from app.core.session_thread_context import session_thread_owner_scope
        from app.domain.itemized.mutation_intents import MutationIntentOwner
        from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
        from app.services.infrastructure.rollout_context.runtime.stream_accumulator import CanonicalBlockAccumulator
        from app.services.orchestration.execution_step.stream_bindings import CanonicalItemSink
        async def commit_result():
            saver = RolloutCheckpointSaver(Path(root))
            admission = saver.execution_admission(session, thread_id=thread, execution_id="execution-consumer")
            with session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread), execution_admission_scope(admission, langgraph_thread_id=thread):
                accumulator = CanonicalBlockAccumulator(turn_id=admission.turn_id, execution_id=admission.execution_id, producer_id="consumer", model_call_id="model-consumer")
                call_id = "model-consumer:tool-call:collect-call"
                accumulator.accept({"block_id":"consumer-call", "carrier_type":"tool_call", "name":"collect_background_messages", "args":{}, "tool_call_id":call_id})
                calls = accumulator.finalize(first_item_sequence=1, status="completed")
                accumulator.accept({"block_id":"consumer-result", "carrier_type":"tool_result", "name":"collect_background_messages", "tool_call_id":call_id, "tool_attempt_id":"consumer-attempt", "text":"durable batch"})
                results = accumulator.finalize(first_item_sequence=1, status="completed")
                sink = CanonicalItemSink(saver, session_id=session, thread_id=thread, checkpoint_ns="", committed_item_observer=lambda *_: os._exit(83))
                await sink.append((*calls, *results))
        asyncio.run(commit_result())
    else:
        async def run():
            registry = BackgroundTaskRegistry(store=store, residency_tracker=ThreadResidencyTracker(), handlers=(handler,))
            await registry.spawn(session_id=session, thread_id=thread, agent_id="agent", task_id=task_id, descriptor=descriptor)
            while True:
                with store.control(session, read_only=True) as control:
                    if control.get_background_task(task_id).checkpoint["next_index"] == 1:
                        print("ready", flush=True)
                        break
                await asyncio.sleep(0.001)
            await asyncio.Event().wait()
        asyncio.run(run())
'''


@dataclass(frozen=True)
class BackgroundCase:
    context: TestRunContext
    store: BackgroundWorkStore
    session_id: str
    thread_id: str


@pytest.fixture
def process_case(request: pytest.FixtureRequest, session_bundle_factory):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = context.workspace_root
    shutil.rmtree(workspace)
    workspace.mkdir()
    sessions = workspace / ".boxteam" / "sessions"
    session_id = "ses_019c3df7f8527d568e834acf3c0f6612"
    session_bundle_factory(sessions, session_id)
    return BackgroundCase(context, BackgroundWorkStore(sessions_dir=sessions), session_id,
        get_session_path_resolver(sessions).main_thread_id(session_id))


def descriptor(interval: float = 0.03) -> BackgroundTaskDescriptor:
    return BackgroundTaskDescriptor(kind="emit_system_time_messages", version=1, payload={
        "interval_seconds": interval, "message_count": 3, "source_id": "process-stream",
        "started_at": datetime.now(UTC).isoformat(),
    })


def command(case: BackgroundCase, mode: str, value: BackgroundTaskDescriptor) -> list[str]:
    return [sys.executable, "-c", _PROCESS, mode, str(case.store.sessions_dir), case.session_id, case.thread_id, value.model_dump_json()]


async def finish_recovery(case: BackgroundCase) -> None:
    registry = BackgroundTaskRegistry(store=case.store, residency_tracker=ThreadResidencyTracker(), handlers=(SystemTimeEmitterHandler(),))
    try:
        await registry.start()
        task = registry.get_task(case.session_id, "bgt_process-window")
        assert task is not None
        await asyncio.wait_for(task, 5)
        with case.store.control(case.session_id, read_only=True) as control:
            assert control.get_background_task("bgt_process-window").status == "completed"
            assert control.list_non_terminal_leases() == ()
            assert [message.message_id for message in control.list_background_messages(case.thread_id, "agent")] == [f"bgt_process-window:message:{index}" for index in range(3)]
    finally:
        await registry.stop()


async def test_background_task_starts_with_independent_context(process_case: BackgroundCase):
    inherited: ContextVar[str | None] = ContextVar("creator-job-state", default=None)
    observed = []

    class ContextObserver(ControlledBackgroundHandler):
        async def step(self, handle):
            observed.append(inherited.get())
            return await super().step(handle)

    handler = ContextObserver()
    handler.finish.set()
    registry = BackgroundTaskRegistry(store=process_case.store, residency_tracker=ThreadResidencyTracker(), handlers=(handler,))
    token = inherited.set("old-pinned-job")
    try:
        handle = await registry.spawn(
            session_id=process_case.session_id, thread_id=process_case.thread_id,
            task_id="bgt_independent-context", agent_id="agent",
            descriptor=BackgroundTaskDescriptor(kind=handler.kind, version=handler.version, payload={}),
        )
        assert registry.residency_blockers(process_case.session_id, process_case.thread_id)
        await registry.get_task(process_case.session_id, handle.task_id)
        assert observed == [None]
        assert inherited.get() == "old-pinned-job"
        assert registry.residency_blockers(process_case.session_id, process_case.thread_id) == ()
    finally:
        inherited.reset(token)
        await registry.stop()


async def test_process_exit_between_lease_and_descriptor_rolls_back_both(process_case: BackgroundCase):
    value = descriptor()
    process = await asyncio.create_subprocess_exec(*command(process_case, "create", value), cwd=Path.cwd(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output, _ = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode == 81, output.decode()
    with process_case.store.control(process_case.session_id, read_only=True) as control:
        assert control.list_background_tasks() == []
        assert control.list_non_terminal_leases() == ()
    registry = BackgroundTaskRegistry(store=process_case.store, residency_tracker=ThreadResidencyTracker(), handlers=(SystemTimeEmitterHandler(),))
    try:
        handle = await registry.spawn(session_id=process_case.session_id, thread_id=process_case.thread_id,
            agent_id="agent", task_id="bgt_process-window", descriptor=value)
        await asyncio.wait_for(registry.get_task(process_case.session_id, handle.task_id), 5)
        assert registry.get_handle(process_case.session_id, handle.task_id).status == "completed"
    finally:
        await registry.stop()


async def test_process_exit_between_emit_and_checkpoint_rolls_back_and_recovers(process_case: BackgroundCase):
    value = descriptor()
    handler = SystemTimeEmitterHandler()
    from app.domain.itemized.hashing import sha256_jcs
    with process_case.store.control(process_case.session_id) as control:
        handle = control.create_background_task(task_id="bgt_process-window", session_id=process_case.session_id,
            thread_id=process_case.thread_id, agent_id="agent", descriptor=value,
            checkpoint=handler.initial_checkpoint(value), preimage_hash=sha256_jcs({
                "session_id": process_case.session_id, "thread_id": process_case.thread_id,
                "agent_id": "agent", "descriptor": value.model_dump(mode="json"),
            }).split(":")[-1])
        handle = control.claim_background_task(handle.task_id, expected_token=handle.fencing_token, runtime_generation=0)
    process = await asyncio.create_subprocess_exec(*command(process_case, "commit", value), cwd=Path.cwd(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output, _ = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode == 82, output.decode()
    with process_case.store.control(process_case.session_id, read_only=True) as control:
        assert control.get_background_task(handle.task_id).checkpoint["next_index"] == 0
        assert control.list_background_messages(process_case.thread_id, "agent") == []
    await finish_recovery(process_case)


async def test_real_process_restart_resumes_committed_timer_checkpoint(process_case: BackgroundCase):
    process = await asyncio.create_subprocess_exec(*command(process_case, "running", descriptor(0.3)), cwd=Path.cwd(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    observed = []
    try:
        async with asyncio.timeout(15):
            assert process.stdout is not None
            while line := await process.stdout.readline():
                line = line.decode()
                observed.append(line)
                if line.strip() == "ready":
                    break
        assert observed and observed[-1].strip() == "ready", "".join(observed)
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), 5)
    with process_case.store.control(process_case.session_id, read_only=True) as control:
        assert control.get_background_task("bgt_process-window").checkpoint["next_index"] == 1
    await finish_recovery(process_case)


async def test_process_exit_after_canonical_commit_before_ack_reconciles_exact_batch(process_case: BackgroundCase):
    case = process_case
    saver = RolloutCheckpointSaver(case.store.sessions_dir)
    saver.accept_turn(case.session_id, thread_id=case.thread_id,
        accepted_ingress_id="ingress-consumer", acceptance_idempotency_key="accept-consumer",
        job_id="job-consumer", admission_idempotency_key="admission-consumer",
        payload="收集后台消息", payload_kind="text", turn_id="turn-consumer",
        root_item_id="item-consumer", initial_execution_id="execution-consumer")
    identity = BackgroundToolResultIdentity(model_call_id="model-consumer", tool_call_id="collect-call")
    consumption_id = software_tool_operation_id("background_collect", case.session_id, case.thread_id,
        identity.model_call_id, identity.tool_call_id)
    bus = BackgroundMessageBus(store=case.store)
    await bus.emit(case.session_id, case.thread_id, "agent", "stop", message_id="consumer-stop", kind="interrupt")
    batch = await bus.collect(case.session_id, case.thread_id, "agent", consumption_id=consumption_id,
        timeout_seconds=1, tool_result_identity=identity)
    assert await bus.reconcile(saver) == 0
    process = await asyncio.create_subprocess_exec(*command(case, "canonical", descriptor()), cwd=Path.cwd(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output, _ = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode == 83, output.decode()
    with case.store.control(case.session_id, read_only=True) as control:
        assert len(control.unacknowledged_background_consumptions()) == 1
    cold = BackgroundMessageBus(store=case.store)
    assert await cold.reconcile(RolloutCheckpointSaver(case.store.sessions_dir)) == 1
    assert await cold.reconcile(saver) == 0
    with case.store.control(case.session_id, read_only=True) as control:
        assert control.unacknowledged_background_consumptions() == []
    assert await cold.collect(case.session_id, case.thread_id, "agent", consumption_id=consumption_id,
        timeout_seconds=1, tool_result_identity=identity) == batch


async def test_canonical_batch_ack_reuses_one_control_store_connection(
    process_case: BackgroundCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = process_case
    bus = BackgroundMessageBus(store=case.store)
    identities = (
        BackgroundToolResultIdentity(
            model_call_id="model-first", tool_call_id="call-first"
        ),
        BackgroundToolResultIdentity(
            model_call_id="model-second", tool_call_id="call-second"
        ),
    )
    items = []
    for index, identity in enumerate(identities):
        consumption_id = software_tool_operation_id(
            "background_collect",
            case.session_id,
            case.thread_id,
            identity.model_call_id,
            identity.tool_call_id,
        )
        await bus.emit(
            case.session_id,
            case.thread_id,
            "agent",
            "已停止",
            message_id=f"batch-stop-{index}",
            kind="interrupt",
        )
        await bus.collect(
            case.session_id,
            case.thread_id,
            "agent",
            consumption_id=consumption_id,
            timeout_seconds=1,
            tool_result_identity=identity,
        )
        scoped_tool_call_id = (
            f"{identity.model_call_id}:tool-call:{identity.tool_call_id}"
        )
        items.append(
            CanonicalItemRecord.create(
                item_sequence=index + 1,
                item_id=f"batch-result-{index}",
                semantic_kind="tool_result",
                payload_kind="text",
                status="completed",
                producer_ref={
                    "producer_kind": "tool",
                    "producer_id": "collect_background_messages",
                    "invocation_id": f"execution-{index}",
                },
                payload="已提交的后台消息",
                projection_identity=ItemProjectionIdentity(
                    model_call_id=identity.model_call_id
                ),
                tool_result_evidence=ToolResultEvidence(
                    scoped_tool_call_id, f"result-{index}", True
                ),
            ),
        )

    original_control = case.store.control
    opened_controls = 0

    @contextmanager
    def count_control_opens(session_id: str, *, read_only: bool = False):
        nonlocal opened_controls
        opened_controls += 1
        with original_control(session_id, read_only=read_only) as control:
            yield control

    monkeypatch.setattr(case.store, "control", count_control_opens)
    bus.acknowledge_tool_results(case.session_id, case.thread_id, items)

    assert opened_controls == 1
    with original_control(case.session_id, read_only=True) as control:
        assert control.unacknowledged_background_consumptions() == []


@pytest.fixture
def append_real_tool_results(process_case: BackgroundCase):
    """沿真实 admission、accumulator 与 sink 写入同 call ID 的不同 model 调用。"""
    async def append(saver: RolloutCheckpointSaver, model_ids: list[str]) -> None:
        admission = saver.execution_admission(process_case.session_id,
            thread_id=process_case.thread_id, execution_id="execution-locator")
        with session_thread_owner_scope(MutationIntentOwner(process_case.session_id, process_case.thread_id),
            langgraph_thread_id=process_case.thread_id), execution_admission_scope(admission,
            langgraph_thread_id=process_case.thread_id):
            items = []
            for model_id in model_ids:
                accumulator = CanonicalBlockAccumulator(turn_id=admission.turn_id,
                    execution_id=admission.execution_id, producer_id="locator", model_call_id=model_id)
                call_id = f"{model_id}:tool-call:collect-call"
                accumulator.accept({"block_id": f"{model_id}-call", "carrier_type": "tool_call",
                    "name": "collect_background_messages", "args": {}, "tool_call_id": call_id})
                items.extend(accumulator.finalize(first_item_sequence=1, status="completed"))
                accumulator.accept({"block_id": f"{model_id}-result", "carrier_type": "tool_result",
                    "name": "collect_background_messages", "tool_call_id": call_id,
                    "tool_attempt_id": f"{model_id}-attempt", "text": "已提交的后台消息 batch"})
                items.extend(accumulator.finalize(first_item_sequence=1, status="completed"))
            await CanonicalItemSink(saver, session_id=process_case.session_id,
                thread_id=process_case.thread_id, checkpoint_ns="").append(items)
    return append


async def test_cold_canonical_locator_uses_exact_index_without_body_read_and_wrong_identity_ack(
    process_case: BackgroundCase, append_real_tool_results, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = process_case
    saver = RolloutCheckpointSaver(case.store.sessions_dir)
    saver.accept_turn(case.session_id, thread_id=case.thread_id,
        accepted_ingress_id="ingress-locator", acceptance_idempotency_key="accept-locator",
        job_id="job-locator", admission_idempotency_key="admission-locator", payload="真实 locator",
        initial_execution_id="execution-locator")
    await append_real_tool_results(saver, ["model-target", *[f"noise-small-{index}" for index in range(16)]])
    measurements = []
    for population in (17, 529):
        if population == 529:
            await append_real_tool_results(saver, [f"noise-large-{index}" for index in range(512)])
        cold = RolloutCheckpointSaver(case.store.sessions_dir)
        storage = cold._storage
        counts: Counter[str] = Counter()
        statements: list[str] = []
        current = [""]
        connect = storage.storage._connect

        def observed_connect(*args, _connect=connect, _counts=counts, _current=current,
            _statements=statements, **kwargs):
            connection = _connect(*args, **kwargs)
            def trace(sql: str) -> None:
                _current[0] = "locator" if sql.startswith("SELECT item_id,jsonl_offset,jsonl_length,commit_id FROM item_catalog") else "boundary"
                if _current[0] == "locator":
                    _statements.append(sql)
            connection.set_trace_callback(trace)
            connection.set_progress_handler(lambda: _counts.update((_current[0],)) or 0, 1)
            return connection

        def forbidden_body(*args, **kwargs):
            raise AssertionError("冷 ToolResult 确认不得读取 canonical 正文")
        with monkeypatch.context() as patch:
            patch.setattr(storage.storage, "_connect", observed_connect)
            patch.setattr(storage.storage, "_read_v2_item_at", forbidden_body)
            assert cold.committed_tool_result_id(case.session_id, case.thread_id, "model-target", "collect-call")
            assert not cold.committed_tool_result_id(case.session_id, case.thread_id, "wrong-model", "collect-call")
            assert not cold.committed_tool_result_id(case.session_id, case.thread_id, "model-target", "wrong-call")
            lookup_counts = dict(counts)
            counts.clear()
            with storage.storage.open_read_snapshot(case.session_id, case.thread_id):
                pass
        with connect(case.thread_id, session_id=case.session_id, read_only=True) as connection:
            plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + statements[0])]
            assert any("SEARCH item_catalog USING INDEX item_catalog_background_result_identity" in line for line in plan), plan
        assert lookup_counts["locator"] <= 160, lookup_counts
        measurements.append({"completed_tool_results": population,
            "locator_vm_steps": lookup_counts["locator"],
            "locator_boundary_vm_steps": lookup_counts["boundary"],
            "full_snapshot_vm_steps": counts["boundary"], "query_plan": plan})
    assert measurements[1]["locator_vm_steps"] == measurements[0]["locator_vm_steps"]
    assert measurements[1]["locator_boundary_vm_steps"] == measurements[0]["locator_boundary_vm_steps"]
    bus = BackgroundMessageBus(store=case.store)
    for agent, identity in (
        ("right", BackgroundToolResultIdentity(model_call_id="model-target", tool_call_id="collect-call")),
        ("wrong-model", BackgroundToolResultIdentity(model_call_id="absent-model", tool_call_id="collect-call")),
        ("wrong-call", BackgroundToolResultIdentity(model_call_id="model-target", tool_call_id="absent-call")),
    ):
        await bus.emit(case.session_id, case.thread_id, agent, "结束 batch", message_id=f"stop-{agent}", kind="interrupt")
        await bus.collect(case.session_id, case.thread_id, agent, consumption_id=software_tool_operation_id(
            "background_collect", case.session_id, case.thread_id, identity.model_call_id, identity.tool_call_id),
            timeout_seconds=1, tool_result_identity=identity)
    assert await BackgroundMessageBus(store=case.store).reconcile(RolloutCheckpointSaver(case.store.sessions_dir)) == 1
    with case.store.control(case.session_id, read_only=True) as control:
        pending = control.unacknowledged_background_consumptions()
        assert {(identity.model_call_id, identity.tool_call_id) for _, _, identity in pending} == {
            ("absent-model", "collect-call"), ("model-target", "absent-call")}
    case.context.artifacts_dir.joinpath("canonical-locator-read-work.json").write_text(
        json.dumps(measurements, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"canonical_locator_read_work": measurements}))


@pytest.mark.parametrize("damage", ["owner", "missing-commit", "uncommitted", "outside-boundary", "ambiguous"])
async def test_cold_canonical_locator_rejects_damaged_commit_without_ack_or_rewrite(
    process_case: BackgroundCase, append_real_tool_results, damage: str,
) -> None:
    case = process_case
    saver = RolloutCheckpointSaver(case.store.sessions_dir)
    saver.accept_turn(case.session_id, thread_id=case.thread_id,
        accepted_ingress_id="ingress-locator", acceptance_idempotency_key="accept-locator",
        job_id="job-locator", admission_idempotency_key="admission-locator", payload="真实 locator",
        initial_execution_id="execution-locator")
    await append_real_tool_results(saver, ["model-target", "model-noise"])
    identity = BackgroundToolResultIdentity(model_call_id="model-target", tool_call_id="collect-call")
    bus = BackgroundMessageBus(store=case.store)
    await bus.emit(case.session_id, case.thread_id, "agent", "结束 batch", message_id="damaged-stop", kind="interrupt")
    await bus.collect(case.session_id, case.thread_id, "agent", consumption_id=software_tool_operation_id(
        "background_collect", case.session_id, case.thread_id, identity.model_call_id, identity.tool_call_id),
        timeout_seconds=1, tool_result_identity=identity)
    storage = saver._storage
    with storage.storage._connect(case.thread_id, session_id=case.session_id) as connection:
        commit_id = connection.execute(
            "SELECT commit_id FROM item_catalog WHERE semantic_kind='tool_result' AND tool_call_id=?",
            ("model-target:tool-call:collect-call",),
        ).fetchone()[0]
        if damage == "owner":
            connection.execute("UPDATE database_meta SET thread_id='wrong-owner'")
        elif damage == "missing-commit":
            connection.execute("DELETE FROM storage_commits WHERE commit_id=?", (commit_id,))
        elif damage == "uncommitted":
            connection.execute("UPDATE storage_commits SET status='prepared' WHERE commit_id=?", (commit_id,))
        elif damage == "outside-boundary":
            connection.execute("UPDATE item_catalog SET jsonl_length=(SELECT committed_jsonl_offset FROM database_meta) "
                "WHERE semantic_kind='tool_result' AND tool_call_id=?", ("model-target:tool-call:collect-call",))
        else:
            connection.execute("UPDATE item_catalog SET tool_call_id='collect-call', "
                "projection_manifest_json=json_set(projection_manifest_json,'$.projection_identity.model_call_id','model-target') "
                "WHERE semantic_kind='tool_result' AND tool_call_id=?", ("model-noise:tool-call:collect-call",))
    sources = (storage.storage.index_path(case.session_id, thread_id=case.thread_id),
        storage.storage.jsonl_path(case.session_id, thread_id=case.thread_id))
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in sources]
    expected = "owner/state" if damage == "owner" else "identity 歧义" if damage == "ambiguous" else "真实提交边界"
    with pytest.raises(RuntimeError, match=expected):
        await BackgroundMessageBus(store=case.store).reconcile(RolloutCheckpointSaver(case.store.sessions_dir))
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in sources] == before
    with case.store.control(case.session_id, read_only=True) as control:
        assert len(control.unacknowledged_background_consumptions()) == 1


async def test_cold_background_task_claim_rejects_late_old_token_callback(process_case: BackgroundCase) -> None:
    case = process_case
    value = descriptor()
    handler = SystemTimeEmitterHandler()
    with case.store.control(case.session_id) as control:
        created = control.create_background_task(task_id="bgt-late-callback", session_id=case.session_id,
            thread_id=case.thread_id, agent_id="agent", descriptor=value,
            checkpoint=handler.initial_checkpoint(value), preimage_hash=sha256_jcs({
                "session_id": case.session_id, "thread_id": case.thread_id, "agent_id": "agent",
                "descriptor": value.model_dump(mode="json"),
            }).split(":")[-1])
        old = control.claim_background_task(created.task_id, expected_token=0, runtime_generation=1)
    late = await handler.step(old)
    cold = BackgroundWorkStore(sessions_dir=case.store.sessions_dir)
    with cold.control(case.session_id) as control:
        current = control.claim_background_task(old.task_id, expected_token=old.fencing_token, runtime_generation=2)
        assert current.fencing_token == old.fencing_token + 1
        with pytest.raises(RuntimeError, match="checkpoint fencing"):
            control.commit_background_step(old.task_id, token=old.fencing_token, step=late)
        with pytest.raises(RuntimeError, match="终结 fencing"):
            control.finish_background_task(old.task_id, token=old.fencing_token, status="failed")
        assert control.get_background_task(old.task_id).checkpoint["next_index"] == 0
        assert control.list_background_messages(case.thread_id, "agent") == []
        committed = control.commit_background_step(current.task_id, token=current.fencing_token,
            step=await handler.step(current))
        assert committed.runtime_generation == 2
        assert committed.checkpoint["next_index"] == 1
        assert [message.message_id for message in control.list_background_messages(case.thread_id, "agent")] == ["bgt-late-callback:message:0"]
