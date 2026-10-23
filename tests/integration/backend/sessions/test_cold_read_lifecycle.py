"""真实历史句柄与 child 列表只持一次 Session guard，不长期占有 topology。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import closing
from contextvars import ContextVar
from pathlib import Path

import httpx
import portalocker
import pytest
from fastapi import FastAPI

from app.api import sessions
from app.api.deps import (
    get_request_id,
    get_rollout_checkpoint_saver,
    get_session_service,
    get_team_service,
    get_thread_residency_tracker,
    verify_local_token,
)
from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import workspace_context
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    SessionDeletionPendingError,
    _CrossProcessFileLock,
)
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutReadSnapshot,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_cold_rollout_history import (
    _history_request,
)
from tests.integration.backend.sessions.test_cold_rollout_history import (
    history_client as history_client,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_cold_rollout_history import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_cold_rollout_history import (
    runtime_case as runtime_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    _child,
    _session,
)


def _exclusive_available(path: Path) -> bool:
    # 使用独立 fd 观察真实 OS 锁，不用内存事件冒充跨 owner 互斥。
    with path.open("a+b") as handle:
        try:
            portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.AlreadyLocked:
            return False
        portalocker.unlock(handle)
        return True


@pytest.mark.parametrize("entry", ["bootstrap", "history", "messages", "message", "agent-state"])
async def test_cold_history_releases_topology_but_guards_handles_until_delete(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch, entry: str,
) -> None:
    case = runtime_case
    container = case.runner.container
    service = container.session_service
    seconds = [0.0]
    monkeypatch.setattr(case.tracker, "_clock", lambda: seconds[0])
    session_id, thread_id = await _session(case.runner)
    root = f"/api/v1/sessions/{session_id}"
    admitted, resume_read, marked, resume_delete = (asyncio.Event() for _ in range(4))
    delete_id = f"cold-history-delete-{entry}"
    snapshots = []
    handles = []
    drain_waiting, drain_acquired = asyncio.Event(), asyncio.Event()
    reading_history: ContextVar[bool] = ContextVar("reading_cold_history", default=False)
    read_task = delete_task = None
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        response = await history_client.post(root + "/messages", json={
            "message": {"content": "通过消息入口生成需要冷读保护的历史。"}, "run": {},
        })
        assert response.status_code == 200, response.text
        accepted = response.json()["data"]
        message_url = {
            "messages": root + "/messages",
            "message": root + f"/messages/{accepted['message_id']}",
            "agent-state": root + "/agent-state/messages",
        }.get(entry)
        job = container.job_service._jobs[accepted["job_id"]]
        await asyncio.wait_for(asyncio.shield(job.task), 20)
        assert job.status.value == "completed", job.error_message
        seconds[0] = 1800
        await case.tracker.sweep()
        assert case.tracker.snapshot(session_id, thread_id).residency == "cold"
        compiled = len(case.compiled)
        locator = service.path_resolver.resolve_session_node(session_id)
        control_path = locator / "session-control.sqlite"
        topology = service._navigation_topology_gate.exclusive().path
        session_gate = service._session_lifecycle_gate.shared(session_id).path
        with closing(SessionControlStore(control_path, read_only=True)) as control:
            fence_before = control.get_fence()
        assert fence_before[0] == "active"

        async def observe(operation_id, phase, *, details=None):
            if operation_id == thread_id and phase == "history.read.guarded":
                admitted.set()
                await resume_read.wait()
            elif operation_id == delete_id and phase == "deletion.catalog_deleting":
                marked.set()
                await resume_delete.wait()

        acquire_lock = _CrossProcessFileLock.__aenter__

        async def acquire_observed(lock):
            deleting = lock.path == session_gate and lock._exclusive and asyncio.current_task() is delete_task
            if deleting:
                drain_waiting.set()
            acquired = await acquire_lock(lock)
            if deleting:
                assert handles and all(handle.closed for handle in handles), "删除提前取得独占锁"
                drain_acquired.set()
            return acquired

        close_snapshot = RolloutReadSnapshot.close
        load_checkpoint = container.message_service._load_checkpoint_tuple

        async def load_guarded(*args, **kwargs):
            if message_url is not None and reading_history.get():
                admitted.set()
                await resume_read.wait()
            return await load_checkpoint(*args, **kwargs)

        def close_guarded(snapshot):
            if reading_history.get() and (snapshot.session_id, snapshot.thread_id) == (session_id, thread_id) and not snapshot.closed:
                # 删除方此时可合法取得 topology；读方释放已在发起删除前独立核实。
                assert not _exclusive_available(session_gate), "真实历史句柄尚未关闭就释放了 Session guard"
                assert service.path_resolver.catalog_store.get_node(session_id).state == "deleting"
                assert snapshot.connection.execute("SELECT COUNT(*) FROM item_catalog").fetchone()[0] > 0
                handles.append(snapshot)
                assert delete_task is not None and not delete_task.done()
                assert not drain_acquired.is_set()
                snapshots.append({"thread_id": snapshot.thread_id, "rollout_id": snapshot.rollout_id, "open": not snapshot.closed})
                close_snapshot(snapshot)
                assert snapshot.closed
                assert not _exclusive_available(session_gate), "handle 关闭与读准入退出间失去保护"
            else:
                close_snapshot(snapshot)

        monkeypatch.setattr("app.services.business.session_turn_history.service.observe_phase", observe)
        monkeypatch.setattr("app.core.session_subtree_delete.observe_phase", observe)
        monkeypatch.setattr(RolloutReadSnapshot, "close", close_guarded)
        monkeypatch.setattr(_CrossProcessFileLock, "__aenter__", acquire_observed)
        monkeypatch.setattr(container.message_service, "_load_checkpoint_tuple", load_guarded)

        async def read_history():
            # 作用域标记随 to_thread 传播；同步 close 不查询不存在的事件循环。
            token = reading_history.set(True)
            try:
                if message_url is not None:
                    return await history_client.get(message_url)
                return await _history_request(history_client, session_id, thread_id, entry)
            finally:
                reading_history.reset(token)

        try:
            read_task = asyncio.create_task(read_history())
            await asyncio.wait_for(admitted.wait(), 3)
            assert _exclusive_available(topology), "完成 Session 读准入后应立即释放 topology"
            delete_task = asyncio.create_task(service.path_resolver.delete_subtree(
                idempotency_key=delete_id, root_node_id=session_id,
            ))
            await asyncio.wait_for(marked.wait(), 3)
            assert not delete_task.done() and locator.is_dir()
            with closing(SessionControlStore(control_path, read_only=True)) as control:
                assert control.get_fence() == fence_before
            rejected = None
            if message_url is not None:
                rejected = await history_client.get(message_url)
                assert rejected.status_code == 409, rejected.text
                assert "session_deletion_pending" in rejected.json()["detail"]
                assert "data" not in rejected.json()
            resume_delete.set()
            await asyncio.wait_for(drain_waiting.wait(), 3)
            assert not drain_acquired.is_set() and not delete_task.done()
            resume_read.set()
            loaded = await asyncio.wait_for(read_task, 5)
            assert loaded.status_code == 200, loaded.text
            data = loaded.json()["data"]
            if entry in {"messages", "message"}:
                items = data["items"] if entry == "messages" else [data]
                assert items and {item["thread_id"] for item in items} == {thread_id}
                assert accepted["message_id"] in {item["message_id"] for item in items}
            elif entry == "agent-state":
                assert data["session_id"] == session_id and data["message_count"] > 0
                assert "通过消息入口生成需要冷读保护的历史。" in data["jsonl"]
            else:
                assert data["thread_id"] == thread_id
                turn = data["latest_turn"] if entry == "bootstrap" else data["items"][0]
                assert turn["turn_id"] == accepted["turn_id"]
                assert accepted["message_id"] in turn["source_message_ids"]
            assert snapshots and all(snapshot["open"] for snapshot in snapshots)
            assert len(case.compiled) == compiled
            await asyncio.wait_for(delete_task, 10)
            assert drain_acquired.is_set() and all(handle.closed for handle in handles)
            assert not locator.exists()
            with pytest.raises(KeyError):
                service.path_resolver.catalog_store.get_node(session_id)
            context = TestRunContext.from_test_file(Path(__file__)).prepare()
            (context.artifacts_dir / f"{entry}-{time.time_ns()}.json").write_text(json.dumps({
                "entry": entry, "session_id": session_id, "thread_id": thread_id,
                "turn_id": accepted["turn_id"], "job_id": accepted["job_id"],
                "snapshots": snapshots, "fence_before_drain": fence_before,
                "delete_waited_for_handles": drain_acquired.is_set(), "delete_completed": True, "history": data,
                "new_read_rejection": rejected.json() if rejected is not None else None,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            resume_read.set()
            resume_delete.set()
            for task in (read_task, delete_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (read_task, delete_task) if task is not None), return_exceptions=True)


@pytest.mark.parametrize("entry,reader", [
    ("messages", "checkpoint"), ("message", "checkpoint"),
    ("agent-state", "checkpoint"), ("agent-state", "canonical"),
    ("delta", "delta"),
])
async def test_cancelled_message_read_guards_worker_handles_until_delete(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch, entry: str, reader: str,
) -> None:
    case = runtime_case
    container = case.runner.container
    service = container.session_service
    seconds = [0.0]
    monkeypatch.setattr(case.tracker, "_clock", lambda: seconds[0])
    session_id, thread_id = await _session(case.runner)
    root = f"/api/v1/sessions/{session_id}"
    blocked, marked, drain_waiting, drain_acquired = (asyncio.Event() for _ in range(4))
    resume_worker = threading.Event()
    selected_reader: ContextVar[bool] = ContextVar("cancelled_message_reader", default=False)
    loop = asyncio.get_running_loop()
    snapshots = []
    read_task = delete_task = None
    delete_id = f"cancelled-message-delete-{entry}-{reader}"
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        response = await history_client.post(root + "/messages", json={
            "message": {"content": "取消冷读时，删除必须等待真实读取句柄关闭。"}, "run": {},
        })
        assert response.status_code == 200, response.text
        accepted = response.json()["data"]
        job = container.job_service._jobs[accepted["job_id"]]
        await asyncio.wait_for(asyncio.shield(job.task), 20)
        assert job.status.value == "completed", job.error_message
        seconds[0] = 1800
        await case.tracker.sweep()
        assert case.tracker.snapshot(session_id, thread_id).residency == "cold"
        compiled = len(case.compiled)
        locator = service.path_resolver.resolve_session_node(session_id)
        session_gate = service._session_lifecycle_gate.shared(session_id).path
        topology = service._navigation_topology_gate.exclusive().path
        url = {
            "messages": root + "/messages",
            "message": root + f"/messages/{accepted['message_id']}",
            "agent-state": root + "/agent-state/messages",
        }.get(entry, root + "/messages")
        target, attribute = (container.message_service, "_canonical_item_reader") if reader == "canonical" else (
            case.runner.saver, "get_tuple" if reader == "checkpoint" else "get_delta_channel_history",
        )
        read = getattr(target, attribute)
        close_snapshot = RolloutReadSnapshot.close
        acquire_lock = _CrossProcessFileLock.__aenter__

        def selected_read(*args, **kwargs):
            token = selected_reader.set(True)
            try:
                return read(*args, **kwargs)
            finally:
                selected_reader.reset(token)

        def close_blocked(snapshot):
            if selected_reader.get() and not snapshots and (snapshot.session_id, snapshot.thread_id) == (session_id, thread_id):
                assert not snapshot.closed
                assert snapshot.connection.execute("SELECT COUNT(*) FROM item_catalog").fetchone()[0] > 0
                snapshots.append(snapshot)
                loop.call_soon_threadsafe(blocked.set)
                assert resume_worker.wait(10), "测试未释放同步读取线程"
                assert not _exclusive_available(session_gate), "取消后真实句柄关闭前丢失 Session guard"
                assert not drain_acquired.is_set()
            close_snapshot(snapshot)

        async def observe(operation_id, phase, *, details=None):
            if operation_id == delete_id and phase == "deletion.catalog_deleting":
                marked.set()

        async def acquire_observed(lock):
            deleting = lock.path == session_gate and lock._exclusive and asyncio.current_task() is delete_task
            if deleting:
                drain_waiting.set()
            acquired = await acquire_lock(lock)
            if deleting:
                assert snapshots and all(snapshot.closed for snapshot in snapshots), "取消后删除越过未关闭的真实句柄"
                drain_acquired.set()
            return acquired

        monkeypatch.setattr(target, attribute, selected_read)
        monkeypatch.setattr(RolloutReadSnapshot, "close", close_blocked)
        monkeypatch.setattr(_CrossProcessFileLock, "__aenter__", acquire_observed)
        monkeypatch.setattr("app.core.session_subtree_delete.observe_phase", observe)

        async def read_delta():
            # 普通消息 API 已有完整 view；增量恢复端口按封存引用的 namespace 读取。
            # 历史仍由上面的真实消息执行产生，不手工构造 checkpoint 或增量写入。
            owner = MutationIntentOwner(session_id=session_id, thread_id=thread_id)
            config = build_checkpoint_config(
                thread_id, checkpoint_ns=case.model_dispatches[0].reference.checkpoint_ns,
            )
            async with service.session_read_admission(session_id):
                with session_thread_owner_scope(owner, langgraph_thread_id=thread_id):
                    return await case.runner.saver.aget_delta_channel_history(config=config, channels=["messages"])

        try:
            read_task = asyncio.create_task(read_delta() if reader == "delta" else history_client.get(url))
            async with asyncio.timeout(3):
                while not blocked.is_set():
                    if read_task.done():
                        pytest.fail(f"同步 reader 屏障未到达，读取已结束: {read_task.result()!r}")
                    await asyncio.sleep(0.001)
            assert _exclusive_available(topology)
            assert not _exclusive_available(session_gate)
            delete_task = asyncio.create_task(service.path_resolver.delete_subtree(
                idempotency_key=delete_id, root_node_id=session_id,
            ))
            await asyncio.wait_for(marked.wait(), 3)
            await asyncio.wait_for(drain_waiting.wait(), 3)
            rejected = await history_client.get(url)
            assert rejected.status_code == 409, rejected.text
            assert "session_deletion_pending" in rejected.json()["detail"]
            # 连续取消也不能使不可取消的同步线程逃逸读准入。
            for _ in range(2):
                read_task.cancel()
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(read_task), 0.05)
                assert not _exclusive_available(session_gate)
                assert not drain_acquired.is_set() and not delete_task.done()
                assert all(not snapshot.closed for snapshot in snapshots)
            resume_worker.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(read_task, 5)
            await asyncio.wait_for(delete_task, 10)
            assert drain_acquired.is_set() and all(snapshot.closed for snapshot in snapshots)
            assert not locator.exists()
            assert len(case.compiled) == compiled
        finally:
            resume_worker.set()
            if read_task is not None and not read_task.done():
                read_task.cancel()
            await asyncio.gather(*(task for task in (read_task, delete_task) if task is not None), return_exceptions=True)


async def test_catalog_deleting_rejects_new_control_before_local_fence_closes(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = runtime_case.runner.container
    service = container.session_service
    session_id, thread_id = await _session(runtime_case.runner)
    delete_id = "cold-control-delete"
    marked, resume = asyncio.Event(), asyncio.Event()

    async def observe(operation_id, phase, *, details=None):
        if operation_id == delete_id and phase == "deletion.catalog_deleting":
            marked.set()
            await resume.wait()

    monkeypatch.setattr("app.core.session_subtree_delete.observe_phase", observe)
    with workspace_context(container.workspace_id, runtime_case.runner.workspace, session_catalog=container.session_catalog):
        locator = service.path_resolver.resolve_session_node(session_id)
        with closing(SessionControlStore(locator / "session-control.sqlite", read_only=True)) as control:
            before = control.connection.execute("SELECT * FROM session_operation_leases ORDER BY lease_id").fetchall()
            fence = control.get_fence()
        assert fence[0] == "active"
        deletion = asyncio.create_task(service.path_resolver.delete_subtree(
            idempotency_key=delete_id, root_node_id=session_id,
        ))
        try:
            await asyncio.wait_for(marked.wait(), 3)
            with pytest.raises(SessionDeletionPendingError, match="catalog owner 正在删除"):
                async with service.session_context_control_admission(
                    session_id, thread_id, operation_id="must-not-create-lease",
                    payload={"kind": "test-rejected-control"}, recovery_ref="context-control:rejected",
                ):
                    pytest.fail("catalog deleting 后仍允许新控制操作")
            with closing(SessionControlStore(locator / "session-control.sqlite", read_only=True)) as control:
                assert control.get_fence() == fence
                assert control.connection.execute("SELECT * FROM session_operation_leases ORDER BY lease_id").fetchall() == before
            resume.set()
            await asyncio.wait_for(deletion, 10)
            assert not locator.exists()
        finally:
            resume.set()
            if not deletion.done():
                deletion.cancel()
            await asyncio.gather(deletion, return_exceptions=True)


async def test_child_thread_http_never_reenters_topology_from_session_guard(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case
    container = case.runner.container
    service = container.session_service
    session_id, _main = await _session(case.runner)
    child = await _child(case, session_id)
    application = FastAPI()
    application.include_router(sessions.router, prefix="/api/v1")
    application.dependency_overrides[get_session_service] = lambda: service
    application.dependency_overrides[get_rollout_checkpoint_saver] = lambda: case.runner.saver
    application.dependency_overrides[get_thread_residency_tracker] = lambda: case.tracker
    application.dependency_overrides[get_team_service] = lambda: container.team_service
    application.dependency_overrides[get_request_id] = lambda: "cold-child-list"
    application.dependency_overrides[verify_local_token] = lambda: "local-test"
    entered, topology_held, release_topology = (asyncio.Event() for _ in range(3))
    list_children = service.list_child_threads

    async def list_while_navigation_holds_topology(identity):
        entered.set()
        await topology_held.wait()
        return await list_children(identity)

    async def hold_navigation():
        await entered.wait()
        async with service._navigation_topology_gate.exclusive():
            topology_held.set()
            await release_topology.wait()

    monkeypatch.setattr(service, "list_child_threads", list_while_navigation_holds_topology)
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        navigation = asyncio.create_task(hold_navigation())
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
                response = await asyncio.wait_for(client.get(f"/api/v1/sessions/{session_id}/child-threads"), 3)
            assert response.status_code == 200, response.text
            assert [item["thread_id"] for item in response.json()["data"]["items"]] == [child]
            assert topology_held.is_set() and not navigation.done()
            assert _exclusive_available(service._session_lifecycle_gate.shared(session_id).path)
        finally:
            release_topology.set()
            await asyncio.wait_for(navigation, 3)
