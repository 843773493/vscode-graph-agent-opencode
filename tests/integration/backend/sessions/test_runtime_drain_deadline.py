"""真实消息执行后，runtime 排空超时不得隔离或删除 Session 的恢复依据。"""

from __future__ import annotations

import asyncio

import pytest

from app.core.session_control_store import SessionControlStore
from tests.integration.backend.sessions.test_attachment_runtime_lifecycle import (
    execute_upload,
    isolate_unused_external_managers,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import RuntimeCase
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414
)


async def test_delete_timeout_keeps_locator_control_history_and_original_unload(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case
    isolate_unused_external_managers(case, monkeypatch)
    sid, tid, _ref = await execute_upload(case)
    container = case.runner.container
    resolver = container.session_service.path_resolver
    node = resolver.resolve_session_node(sid)
    history = case.runner.saver._storage.storage.jsonl_path(sid, thread_id=tid)
    original_history = history.read_bytes()
    entered, finish = asyncio.Event(), asyncio.Event()
    attempts: list[tuple[str, str, int]] = []
    generation = case.tracker.snapshot(sid, tid).generation

    async def held_release(session: str, thread: str, current_generation: int) -> None:
        attempts.append((session, thread, current_generation))
        entered.set()
        await finish.wait()

    case.tracker.add_unload_listener(held_release)
    key = "runtime-drain-deadline"
    deleting = asyncio.create_task(resolver.delete_subtree(idempotency_key=key, root_node_id=sid))
    try:
        await entered.wait()
        state = case.tracker._states[(sid, tid)]
        original_unload = state.runtime_unload
        assert original_unload is not None
        loop = asyncio.get_running_loop()
        original_time = loop.time
        monkeypatch.setattr(loop, "time", lambda: original_time() + 30)
        for _ in range(12):
            await asyncio.sleep(0)
        assert deleting.done(), "真实 Session 删除应收到 runtime 排空超时"
        with pytest.raises(ExceptionGroup) as failure:
            await deleting
        assert any(isinstance(error, TimeoutError) and f"thread_id={tid}" in str(error)
                   for error in failure.value.exceptions)
        assert state.runtime_unload is original_unload and not original_unload.done()
        assert node.is_dir() and history.read_bytes() == original_history
        record = resolver.catalog_store.get_subtree_delete_record(key)
        assert record.state == "deleting" and record.drained_session_ids == ()
        assert resolver.catalog_store.get_node(sid).storage_relative_locator == record.frozen_session_locators[sid]
        assert not (container.workspace_root / ".boxteam/sessions/.deleting" / key / sid).exists()
        control = SessionControlStore(node / "session-control.sqlite")
        try:
            assert control.get_fence() == ("active", 1)
        finally:
            control.close()
        assert attempts == [(sid, tid, generation)]
        finish.set()
        await original_unload
        result = await resolver.delete_subtree(idempotency_key=key, root_node_id=sid)
        assert result.record_state == "completed" and not node.exists()
        assert attempts == [(sid, tid, generation)]
    finally:
        finish.set()
        await asyncio.gather(deleting, return_exceptions=True)
