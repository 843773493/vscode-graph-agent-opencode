"""真实历史句柄与 child 列表只持一次 Session guard，不长期占有 topology。"""

from __future__ import annotations

import asyncio
import json
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
from app.core.path_utils import workspace_context
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    SessionDeletionPendingError,
    _CrossProcessFileLock,
)
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


@pytest.mark.parametrize("entry", ["bootstrap", "history"])
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

        async def read_history():
            # 作用域标记随 to_thread 传播；同步 close 不查询不存在的事件循环。
            token = reading_history.set(True)
            try:
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
            resume_delete.set()
            await asyncio.wait_for(drain_waiting.wait(), 3)
            assert not drain_acquired.is_set() and not delete_task.done()
            resume_read.set()
            loaded = await asyncio.wait_for(read_task, 5)
            assert loaded.status_code == 200, loaded.text
            data = loaded.json()["data"]
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
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            resume_read.set()
            resume_delete.set()
            for task in (read_task, delete_task):
                if task is not None and not task.done():
                    task.cancel()
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
