"""公开 child history 读取的零物化与损坏证据保护合同。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import shutil
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import portalocker
import pytest
from fastapi import FastAPI

from app.api import jobs, messages, session_turns
from app.api.deps import (
    get_event_service,
    get_job_service,
    get_message_service,
    get_request_id,
    get_session_orchestrator,
    get_session_service,
    get_session_turn_history_service,
    get_session_turn_replay_service,
    verify_local_token,
)
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import require_current_execution_admission
from app.core.path_utils import workspace_context
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.enums import PayloadKind
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
    _RolloutFileLock,
)
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    _child,
    _complete_job,
    _session,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414
)


@pytest.fixture
async def history_client(runtime_case: RuntimeCase) -> AsyncIterator[httpx.AsyncClient]:
    application = FastAPI()
    application.include_router(session_turns.router, prefix="/api/v1")
    application.include_router(messages.router, prefix="/api/v1")
    application.include_router(jobs.router, prefix="/api/v1")
    container = runtime_case.runner.container
    application.dependency_overrides[get_job_service] = lambda: container.job_service
    application.dependency_overrides[get_event_service] = lambda: container.event_service
    application.dependency_overrides[get_session_service] = lambda: container.session_service
    application.dependency_overrides[get_message_service] = lambda: container.message_service
    application.dependency_overrides[get_session_orchestrator] = lambda: container.session_orchestrator
    application.dependency_overrides[get_session_turn_replay_service] = lambda: container.session_turn_replay_service
    application.dependency_overrides[get_session_turn_history_service] = (
        lambda: runtime_case.runner.container.session_turn_history_service
    )
    application.dependency_overrides[verify_local_token] = lambda: "local-test"
    application.dependency_overrides[get_request_id] = lambda: "request-cold-history"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        yield client


async def test_public_session_history_and_replay_stay_on_main_with_real_child(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
) -> None:
    """真实消息入口生成两条线程历史；默认读取与重放不能改投 child。"""
    case = runtime_case
    container = case.runner.container
    session_id, main = await _session(case.runner)
    child = await _child(case, session_id)
    base = f"/api/v1/sessions/{session_id}"
    accepted = {main: [], child: []}
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        for thread_id in (main, child):
            for index in range(2):
                endpoint = base + ("" if thread_id == main else f"/threads/{thread_id}")
                response = await history_client.post(endpoint + "/messages", json={
                    "message": {"content": f"{thread_id} 第 {index + 1} 条真实输入"}, "run": {},
                })
                assert response.status_code == 200, response.text
                data = response.json()["data"]
                assert data["thread_id"] == thread_id
                assert data["dispatch"]["thread_id"] == thread_id
                job = container.job_service._jobs[data["job_id"]]
                await asyncio.wait_for(asyncio.shield(job.task), 20)
                assert job.status.value == "completed", job.error_message
                accepted[thread_id].append(data)

        bootstrap = await history_client.get(base + "/bootstrap")
        assert bootstrap.status_code == 200, bootstrap.text
        assert bootstrap.json()["data"]["thread_id"] == main
        assert bootstrap.json()["data"]["latest_turn"]["turn_id"] == accepted[main][-1]["turn_id"]
        visible = await history_client.get(base + "/messages")
        assert visible.status_code == 200, visible.text
        visible_items = visible.json()["data"]["items"]
        assert visible_items and {item["thread_id"] for item in visible_items} == {main}
        visible_ids = {item["message_id"] for item in visible_items}
        assert {item["message_id"] for item in accepted[main]} <= visible_ids
        assert not {item["message_id"] for item in accepted[child]} & visible_ids

        agent_state = await history_client.get(base + "/agent-state/messages")
        assert agent_state.status_code == 200, agent_state.text
        state = agent_state.json()["data"]
        assert state["session_id"] == session_id
        records = [json.loads(line) for line in state["jsonl"].splitlines()]
        assert state["message_count"] == len(records) and records
        for index in range(2):
            assert f"{main} 第 {index + 1} 条真实输入" in state["jsonl"]
            assert f"{child} 第 {index + 1} 条真实输入" not in state["jsonl"]

        pages = {}
        for thread_id in (main, child):
            endpoint = base + ("" if thread_id == main else f"/threads/{thread_id}") + "/history"
            response = await history_client.post(endpoint, json={"turns": 1})
            assert response.status_code == 200, response.text
            page = response.json()["data"]
            assert page["thread_id"] == thread_id
            assert [turn["turn_id"] for turn in page["items"]] == [accepted[thread_id][-1]["turn_id"]]
            assert page["has_before"] and page["before_cursor"]
            pages[thread_id] = page
        for target, source in ((main, child), (child, main)):
            endpoint = base + ("" if target == main else f"/threads/{target}") + "/history"
            response = await history_client.post(endpoint, json={
                "direction": "before", "cursor": pages[source]["before_cursor"],
            })
            assert response.status_code == 400, response.text
            assert response.json() == {"detail": "历史游标不属于当前 rollout"}

        before = {}
        for thread_id in (main, child):
            with session_thread_owner_scope(
                MutationIntentOwner(session_id=session_id, thread_id=thread_id), langgraph_thread_id=thread_id,
            ):
                before[thread_id] = await case.runner.saver.aget_tuple(build_checkpoint_config(thread_id))
        child_target = accepted[child][0]
        for selector, target_id in (("messages", child_target["message_id"]), ("turns", child_target["turn_id"])):
            response = await history_client.post(base + f"/{selector}/{target_id}/replay", json={
                "action": "edit_and_continue", "content": "不得写入 main 或 child", "acknowledge_context_only": True,
            })
            assert response.status_code == 409, response.text
            assert "当前有效上下文" in response.text
        for thread_id in (main, child):
            with session_thread_owner_scope(
                MutationIntentOwner(session_id=session_id, thread_id=thread_id), langgraph_thread_id=thread_id,
            ):
                assert await case.runner.saver.aget_tuple(build_checkpoint_config(thread_id)) == before[thread_id]
        assert len(case.model_admissions) == 4


@pytest.fixture
async def materialized_history_owner(runtime_case: RuntimeCase) -> tuple[str, str, str]:
    session_id, thread_id = await _session(runtime_case.runner)
    admission = await _complete_job(runtime_case, session_id, thread_id)
    assert admission.turn_id is not None
    return session_id, thread_id, admission.turn_id


async def test_public_pending_and_job_control_preserve_real_thread_owner(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 main/child 并行执行时，队列聚合、定点修改与取消保持原 owner。"""
    case = runtime_case
    container = case.runner.container
    session_id, main = await _session(case.runner)
    child = await _child(case, session_id)
    base = f"/api/v1/sessions/{session_id}"
    entered = {thread: asyncio.Event() for thread in (main, child)}
    release = asyncio.Event()
    start_tool = MessageStreamRuntime.start_tool
    (case.runner.workspace / "queue-owner.txt").write_text("真实队列归属验证。\n")

    def model_reply(_payload):
        thread = require_current_execution_admission().thread_id
        return [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{
                "index": 0, "id": f"queue-owner-{thread}", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "queue-owner.txt"})},
            }]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]

    async def hold_started(runtime, **kwargs):
        result = await start_tool(runtime, **kwargs)
        entered[runtime.writer.thread_id].set()
        await release.wait()
        return result

    case.response_chunks = model_reply
    monkeypatch.setattr(MessageStreamRuntime, "start_tool", hold_started)
    active = {}
    queued = {}

    async def send(thread: str, content: str):
        endpoint = base + ("" if thread == main else f"/threads/{thread}") + "/messages"
        response = await history_client.post(endpoint, json={"message": {"content": content}, "run": {}})
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["thread_id"] == data["dispatch"]["thread_id"] == thread
        return data

    async def request(method: str, endpoint: str, **kwargs):
        response = await history_client.request(method, endpoint, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()["data"]

    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        try:
            for thread in (main, child):
                active[thread] = await send(thread, "执行真实工具并等待用户控制。")
                await asyncio.wait_for(entered[thread].wait(), 15)
                queued[thread] = await send(thread, "保留精确 Thread 的排队消息。")
                assert queued[thread]["status"] == "queued"
            child_tail = await send(child, "通过 Job ID 取消的 child 队尾。")
            aggregate = await request("GET", base + "/pending-requests")
            assert aggregate["thread_id"] is None
            expected = {queued[thread]["message_id"]: thread for thread in (main, child)}
            expected[child_tail["message_id"]] = child
            assert {row["message_id"]: row["thread_id"] for row in aggregate["requests"]} == expected
            main_pending = await request("GET", base + "/main-thread/pending-requests")
            assert main_pending["thread_id"] == main
            assert [row["message_id"] for row in main_pending["requests"]] == [queued[main]["message_id"]]
            assert main_pending["requests"][0]["thread_id"] == main

            changed = await request("PATCH", base + f"/pending-requests/{queued[child]['message_id']}/policy", json={
                "delivery_policy": "after_tool_result", "expected_snapshot_version": aggregate["snapshot_version"],
            })
            policies = {row["message_id"]: (row["thread_id"], row["delivery_policy"]) for row in changed["requests"]}
            assert policies[queued[child]["message_id"]] == (child, "after_tool_result")
            assert policies[queued[main]["message_id"]] == (main, "after_turn")
            removed = await request("DELETE", base + f"/pending-requests/{queued[main]['message_id']}")
            assert {row["message_id"]: row["thread_id"] for row in removed["requests"]} == {
                queued[child]["message_id"]: child, child_tail["message_id"]: child,
            }

            cancelled = await request("POST", f"/api/v1/jobs/{child_tail['job_id']}/control", json={"action": "cancel"})
            assert (cancelled["session_id"], cancelled["thread_id"], cancelled["job_id"], cancelled["status"]) == (
                session_id, child, child_tail["job_id"], "cancelled",
            )
            remaining = await request("GET", base + "/pending-requests")
            assert [(row["message_id"], row["thread_id"]) for row in remaining["requests"]] == [(queued[child]["message_id"], child)]
            cleared = await request("DELETE", base + "/pending-requests")
            assert cleared["thread_id"] is None and cleared["requests"] == []
            for thread, accepted in [(main, queued[main]), (child, queued[child]), (child, child_tail)]:
                record = case.runner.saver.execution_queue_record(session_id, thread, accepted["job_id"])
                assert record.phase == "terminal" and record.outcome == "cancelled"

            for thread in (main, child):
                job_id = active[thread]["job_id"]
                detail = await request("GET", f"/api/v1/jobs/{job_id}")
                assert (detail["session_id"], detail["thread_id"], detail["job_id"]) == (session_id, thread, job_id)
                events = await request("GET", f"/api/v1/jobs/{job_id}/events")
                assert events and {event["job_id"] for event in events} == {job_id}
                owners = [event["payload"] for event in events if event["type"] in {"job_created", "job_started"}]
                assert owners and {(row["session_id"], row["thread_id"]) for row in owners} == {(session_id, thread)}
            main_job = container.job_service._jobs[active[main]["job_id"]]
            original_main_task = main_job.task
            for thread in (child, main):
                job_id = active[thread]["job_id"]
                result = await request("POST", f"/api/v1/jobs/{job_id}/control", json={"action": "cancel"})
                assert (result["session_id"], result["thread_id"], result["job_id"]) == (session_id, thread, job_id)
                task = container.job_service._jobs[job_id].task
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
                assert container.job_service._jobs[job_id].status.value == "cancelled"
                if thread == child:
                    assert main_job.task is original_main_task and not original_main_task.done()
                    assert main_job.status.value in {"running", "streaming"}
        finally:
            release.set()


async def _history_request(
    client: httpx.AsyncClient, session_id: str, thread_id: str, entry: str,
) -> httpx.Response:
    if entry == "bootstrap":
        return await client.get(f"/api/v1/sessions/{session_id}/bootstrap")
    return await client.post(f"/api/v1/sessions/{session_id}/threads/{thread_id}/history", json={})


async def _wait_for_rollout_readers(lock: _RolloutFileLock, size: int) -> None:
    admission = lock._admission
    assert admission is not None
    async with asyncio.timeout(2):
        while True:
            with admission._condition:
                if len(admission._queue) == size:
                    return
            await asyncio.sleep(0)


@pytest.mark.parametrize("entry", ["bootstrap", "history"])
@pytest.mark.parametrize("failure_stage", ["open", "file_acquisition", "release"])
async def test_public_history_lock_failures_preserve_committed_history(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
    materialized_history_owner: tuple[str, str, str], entry: str, failure_stage: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """产品执行生成历史；只在原生锁调用点注入故障，修复后沿原历史读取。"""
    session_id, thread_id, turn_id = materialized_history_owner
    container = runtime_case.runner.container
    storage = runtime_case.runner.saver._storage.storage
    lock_path = storage.read_lock(session_id, thread_id)._path
    canonical = storage.jsonl_path(session_id, thread_id=thread_id)
    original = canonical.read_bytes()
    opened = []
    original_open = Path.open
    native_failure = PermissionError(errno.EACCES, f"原生错误 {lock_path}", str(lock_path))
    failure = native_failure if failure_stage == "open" else portalocker.LockException(native_failure)

    def open_lock(target, *args, **kwargs):
        if target == lock_path and failure_stage == "open":
            raise failure
        handle = original_open(target, *args, **kwargs)
        if target == lock_path:
            opened.append(handle)
        return handle

    operation = "unlock" if failure_stage == "release" else "lock"
    original_operation = getattr(portalocker, operation)

    def fail_target_operation(file, *args):
        if os.path.samestat(os.fstat(file), lock_path.stat()):
            raise failure
        return original_operation(file, *args)

    with workspace_context(container.workspace_id, runtime_case.runner.workspace, session_catalog=container.session_catalog):
        before = await _history_request(history_client, session_id, thread_id, entry)
        assert before.status_code == 200, before.text
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", open_lock)
            if failure_stage != "open":
                patch.setattr(portalocker, operation, fail_target_operation)
            response = await _history_request(history_client, session_id, thread_id, entry)
        assert response.status_code == 500, response.text
        detail = response.json()["detail"]
        assert detail["session_id"] == session_id
        assert "artifact=rollout_lock" in detail["message"]
        assert f"stage={failure_stage}" in detail["message"]
        assert str(lock_path) not in response.text
        assert str(runtime_case.runner.workspace) not in response.text
        assert str(lock_path) in caplog.text
        assert any(record.exc_info and record.exc_info[1] is failure for record in caplog.records)
        assert all(handle.closed for handle in opened)
        assert canonical.read_bytes() == original

        # 故障后的独立写者可重新获取锁，冷 Saver 和公开入口都读取原 owner。
        with ThreadPoolExecutor(max_workers=1) as executor:
            def probe():
                with storage._lock(thread_id, "", session_id=session_id):
                    pass
            executor.submit(probe).result(timeout=2)
        with RolloutCheckpointSaver(storage.sessions_dir) as cold:
            assert cold._storage.storage.read_items(session_id, thread_id) == storage.read_items(session_id, thread_id)
        recovered = await _history_request(history_client, session_id, thread_id, entry)
        assert recovered.status_code == 200, recovered.text
        data = recovered.json()["data"]
        assert data["thread_id"] == thread_id
        if entry == "bootstrap":
            assert data["latest_turn"]["turn_id"] == turn_id
        else:
            assert data["items"] == before.json()["data"]["items"]
        assert canonical.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="公开历史入口与真实 POSIX 读写锁交错")
@pytest.mark.parametrize("entry", ["bootstrap", "history"])
async def test_public_history_awaits_writer_without_blocking_holder_task(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
    materialized_history_owner: tuple[str, str, str], entry: str,
) -> None:
    session_id, thread_id, turn_id = materialized_history_owner
    storage = runtime_case.runner.saver._storage.storage
    original = storage.jsonl_path(session_id, thread_id=thread_id).read_bytes()
    holder = storage.read_lock(session_id, thread_id)
    await holder.acquire_async()
    writer_ready = threading.Event()
    release_writer = threading.Event()

    def write() -> None:
        with storage._lock(thread_id, "", session_id=session_id):
            writer_ready.set()
            assert release_writer.wait(timeout=2)

    request: asyncio.Task[httpx.Response] | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(write)
        try:
            await _wait_for_rollout_readers(holder, 1)
            request = asyncio.create_task(_history_request(history_client, session_id, thread_id, entry))
            await _wait_for_rollout_readers(holder, 2)
            assert not request.done() and not writer_ready.is_set()
            holder.release()
            assert await asyncio.to_thread(writer_ready.wait, 1)
            assert not request.done()
            release_writer.set()
            response = await asyncio.wait_for(request, timeout=2)
            writer.result(timeout=1)
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert data["thread_id"] == thread_id
            if entry == "bootstrap":
                assert data["latest_turn"]["turn_id"] == turn_id
            else:
                assert data["items"][0]["turn_id"] == turn_id
        finally:
            holder.release()
            release_writer.set()
            if request is not None:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
    # 已物化 snapshot 的游标异常也必须释放读 lease，后续真实写者仍能进入。
    invalid = await history_client.post(
        f"/api/v1/sessions/{session_id}/threads/{thread_id}/history", json={"cursor": "malformed"},
    )
    assert invalid.status_code == 400, invalid.text

    def probe_after_error() -> None:
        with storage._lock(thread_id, "", session_id=session_id):
            pass

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(probe_after_error).result(timeout=1)
    assert storage.jsonl_path(session_id, thread_id=thread_id).read_bytes() == original


@pytest.mark.parametrize("entry", ["bootstrap", "history"])
async def test_public_history_cancellation_withdraws_read_admission(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
    materialized_history_owner: tuple[str, str, str], entry: str,
) -> None:
    session_id, thread_id, _turn_id = materialized_history_owner
    storage = runtime_case.runner.saver._storage.storage
    async with storage._lock(thread_id, "", session_id=session_id) as holder:
        admission = holder._admission
        assert admission is not None
        request = asyncio.create_task(_history_request(history_client, session_id, thread_id, entry))
        try:
            await _wait_for_rollout_readers(holder, 1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert not admission._queue and len(admission._holders) == 1
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    assert not admission._holders
    response = await _history_request(history_client, session_id, thread_id, entry)
    assert response.status_code == 200, response.text


async def test_item_projection_reads_exact_thread_with_same_item_identity(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id, main_thread = await _session(runtime_case.runner)
    child_thread = await _child(runtime_case, session_id)
    saver = runtime_case.runner.saver
    storage = saver._storage.storage
    for thread_id, payload in ((main_thread, "main 的真实输入"), (child_thread, "child 的真实输入")):
        saver.accept_turn(
            session_id, thread_id=thread_id, accepted_ingress_id="same-ingress",
            acceptance_idempotency_key="same-acceptance", job_id="same-job",
            admission_idempotency_key="same-admission", payload=payload,
            turn_id="same-turn", root_item_id="same-root", initial_execution_id="same-execution",
        )

    with RolloutCheckpointSaver(storage.sessions_dir) as cold:
        for thread_id, payload in ((main_thread, "main 的真实输入"), (child_thread, "child 的真实输入")):
            (item,) = cold.read_canonical_items(session_id, thread_id)
            assert (item.item_id, item.turn_id, item.payload) == ("same-root", "same-turn", payload)
            assert cold.read_canonical_items(session_id, thread_id) == tuple(storage.read_items(session_id, thread_id))

    original_open = type(storage.sessions_dir).open
    jsonl_paths = {storage.jsonl_path(session_id, thread_id=thread_id)
                   for thread_id in (main_thread, child_thread)}

    def forbid_body(path, *args, **kwargs):
        if path in jsonl_paths:
            pytest.fail("轻量投影读取不得打开任一Thread的canonical正文")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(type(storage.sessions_dir), "open", forbid_body)
    for thread_id, payload in ((main_thread, "main 的真实输入"), (child_thread, "child 的真实输入")):
        rows = storage.read_item_projections(session_id, thread_id, item_ids=("same-root",))
        assert len(rows) == 1
        assert rows[0]["item_id"] == "same-root"
        assert rows[0]["turn_id"] == "same-turn"
        assert rows[0]["content"] == payload


async def test_cold_global_checkpoints_include_published_child_without_materializing_idle_child(
    runtime_case: RuntimeCase,
) -> None:
    """由真实执行生成两条历史，冷 Saver 的全局枚举必须保留精确 Thread。"""
    session_id, main = await _session(runtime_case.runner)
    child = await _child(runtime_case, session_id, creation_key="checkpoint-child")
    idle = await _child(runtime_case, session_id, creation_key="checkpoint-idle")
    await _complete_job(runtime_case, session_id, main)
    await _complete_job(runtime_case, session_id, child)
    storage = runtime_case.runner.saver._storage.storage
    idle_root = storage.root(session_id, thread_id=idle)
    idle_lock = idle_root.parent / ".rollout.write.lock"
    assert not idle_root.exists() and not idle_lock.exists()
    authority = tuple(
        path
        for thread_id in (main, child)
        for path in (
            storage.index_path(session_id, thread_id=thread_id),
            storage.jsonl_path(session_id, thread_id=thread_id),
        )
    )
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in authority}
    compiled = len(runtime_case.compiled)
    with RolloutCheckpointSaver(storage.sessions_dir) as cold:
        expected = []
        for thread_id in (main, child):
            with session_thread_owner_scope(
                MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id,
            ):
                entries = list(cold.list({"configurable": {"thread_id": thread_id}}))
            assert entries
            assert all(entry.config["configurable"]["thread_id"] == thread_id for entry in entries)
            expected.extend(entries)
        # 不依赖当前 execution/Thread 上下文，也不能把 idle child 回退为 main。
        assert list(cold.list(None)) == expected
        assert [entry async for entry in cold.alist(None)] == expected
        assert list(cold.list(None, limit=1)) == expected[:1]
        assert [entry async for entry in cold.alist(None, limit=1)] == expected[:1]
    assert not idle_root.exists() and not idle_lock.exists()
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in authority} == before
    assert len(runtime_case.compiled) == compiled


async def test_idle_child_public_history_does_not_materialize_rollout(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
) -> None:
    session_id, _main = await _session(runtime_case.runner)
    child = await _child(runtime_case, session_id)
    storage = runtime_case.runner.saver._storage.storage
    runtime_case.runner.saver.accept_turn(
        session_id, thread_id=_main, accepted_ingress_id="main-ingress",
        acceptance_idempotency_key="main-acceptance", job_id="main-job",
        admission_idempotency_key="main-admission", payload="主线程已有的权威历史",
        turn_id="main-turn", root_item_id="main-root",
    )
    main_root = storage.root(session_id, thread_id=_main)
    main_lock = main_root.parent / ".rollout.write.lock"
    # 由真实 acceptance 初始化 main；child 只读入口必须保留这份权威数据。
    assert main_root.is_dir() and main_lock.is_file()
    main_authority = (main_root / "index.sqlite", main_root / "rollout.jsonl", main_lock)
    main_before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in main_authority}
    main_projections = storage.read_item_projections(session_id, _main)
    assert main_projections
    root = storage.root(session_id, thread_id=child)
    lock = root.parent / ".rollout.write.lock"
    assert not root.exists() and not lock.exists()
    assert storage.read_item_projections(session_id, child) == []
    assert runtime_case.runner.saver.read_canonical_items(session_id, child) == ()
    with pytest.raises(KeyError, match="item projection 缺少请求的 canonical item: main-root"):
        storage.read_item_projections(session_id, child, item_ids=("main-root",))

    response = await history_client.post(
        f"/api/v1/sessions/{session_id}/threads/{child}/history", json={},
    )

    assert response.status_code == 200, response.text
    page = response.json()["data"]
    assert page["thread_id"] == child
    assert page["items"] == page["summaries"] == page["internal_executions"] == []
    assert page["has_more"] is False and page["projection_epoch"] == 1
    saver = runtime_case.runner.saver
    with session_thread_owner_scope(MutationIntentOwner(session_id, child), langgraph_thread_id=child):
        config = {"configurable": {"thread_id": child}}
        assert saver.get_tuple(config) is None
        assert list(saver.list(config)) == []
        assert [item async for item in saver.alist(config)] == []
        assert saver.resolve_latest_completed_turn_anchor(config) is None
    assert storage.read_items(session_id, child) == []
    assert saver._storage.executions.pending_runtime_wakeups(session_id, child) == ()
    assert saver._storage.assemblies.list_context_contributions(session_id, child) == ()
    assert saver._storage.assemblies.list_source_overlays(session_id, child) == ()
    assert saver._storage.checkpoints.read_context_source_control_states(session_id, thread_id=child) == ()
    assert saver._storage.checkpoints.read_context_source_rebuild_rows(session_id, child, checkpoint_ns="") is None
    with pytest.raises(RolloutNotMaterializedError):
        saver._storage.checkpoints.pending_writes(session_id, child, "", "checkpoint-missing")
    assert storage.read_item_projections(session_id, _main) == main_projections
    with pytest.raises(KeyError, match="item projection 缺少请求的 canonical item: item-missing"):
        storage.read_item_projections(session_id, _main, item_ids=("item-missing",))
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in main_authority} == main_before
    assert not root.exists() and not lock.exists()
    assert runtime_case.compiled == []


async def test_idle_child_history_validates_cursor_and_turn_references_without_writes(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
) -> None:
    session_id, _main = await _session(runtime_case.runner)
    child = await _child(runtime_case, session_id)
    root = runtime_case.runner.saver._storage.storage.root(session_id, thread_id=child)
    cases = [
        ({"cursor": "malformed"}, 400),
        ({"cursor": "malformed", "turn_ids": ["turn-missing"]}, 400),
        ({"turn_ids": ["turn-missing"]}, 409),
        ({"direction": "around"}, 422),
        ({"direction": "around", "anchor_turn_id": "turn-missing"}, 409),
    ]
    for request, status in cases:
        response = await history_client.post(
            f"/api/v1/sessions/{session_id}/threads/{child}/history", json=request,
        )
        assert response.status_code == status, response.text
        assert not root.exists() and not (root.parent / ".rollout.write.lock").exists()
    assert runtime_case.compiled == []


async def test_idle_child_history_rejects_dangling_rollout_path_without_writes(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
) -> None:
    session_id, _main = await _session(runtime_case.runner)
    child = await _child(runtime_case, session_id)
    root = runtime_case.runner.saver._storage.storage.root(session_id, thread_id=child)
    root.symlink_to("missing-rollout", target_is_directory=True)

    response = await history_client.post(
        f"/api/v1/sessions/{session_id}/threads/{child}/history", json={},
    )

    assert response.status_code == 500, response.text
    assert "recovery_required" in response.json()["detail"]["message"]
    assert str(root) not in response.text
    assert str(root.parent) not in response.text
    assert root.is_symlink() and str(root.readlink()) == "missing-rollout"
    assert not root.exists() and not (root.parent / ".rollout.write.lock").exists()
    assert runtime_case.compiled == []


@pytest.mark.parametrize("missing", ["index.sqlite", "rollout.jsonl", "rollout"])
async def test_child_history_missing_authority_preserves_remaining_bytes(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient, missing: str,
) -> None:
    session_id, _main = await _session(runtime_case.runner)
    child = await _child(runtime_case, session_id)
    saver = runtime_case.runner.saver
    saver.accept_turn(
        session_id, thread_id=child, accepted_ingress_id="ingress-cold",
        acceptance_idempotency_key="acceptance-cold", job_id="job-cold",
        admission_idempotency_key="admission-cold", payload="不能被读请求销毁的历史",
        payload_kind=PayloadKind.TEXT, turn_id="turn-cold", root_item_id="item-cold",
        initial_execution_id="execution-cold",
    )
    root = saver._storage.storage.root(session_id, thread_id=child)
    good = await history_client.post(
        f"/api/v1/sessions/{session_id}/threads/{child}/history", json={},
    )
    assert good.status_code == 200 and good.json()["data"]["items"]
    original = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.iterdir() if path.is_file()}
    if missing == "rollout":
        shutil.rmtree(root)
        remaining = {}
    else:
        (root / missing).unlink()
        remaining = {name: digest for name, digest in original.items() if name != missing}

    response = await history_client.post(
        f"/api/v1/sessions/{session_id}/threads/{child}/history", json={},
    )

    assert response.status_code == 500, response.text
    assert "recovery_required" in response.json()["detail"]["message"]
    assert str(root) not in response.text
    assert str(root.parent) not in response.text
    with pytest.raises(RuntimeError, match="recovery_required"):
        saver._storage.checkpoints.read_context_source_control_states(session_id, thread_id=child)
    with pytest.raises(RuntimeError, match="recovery_required"):
        saver._storage.checkpoints.read_context_source_rebuild_rows(session_id, child, checkpoint_ns="")
    with pytest.raises(RuntimeError, match="recovery_required"):
        saver._storage.checkpoints.pending_writes(session_id, child, "", "checkpoint-missing")
    actual = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in root.iterdir() if path.is_file()} if root.exists() else {}
    assert actual == remaining
    assert not root.exists() if missing == "rollout" else not (root / missing).exists()
    assert runtime_case.compiled == []
