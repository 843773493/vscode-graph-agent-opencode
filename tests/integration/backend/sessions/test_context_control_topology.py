"""真实 Skill 控制准入在释放 topology 前持久建立 lease。"""

from __future__ import annotations

import asyncio
import json
from contextlib import closing
from typing import cast

import httpx
import pytest

from app.core.path_utils import workspace_context
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import _CrossProcessFileLock
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.testing.model_stream import chat_completion_frames
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
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    skill_tracking_client as skill_tracking_client,  # noqa: PLC0414
)


@pytest.mark.parametrize("child", [False, True], ids=["main", "child"])
@pytest.mark.parametrize("cancelled", [False, True], ids=["completed", "cancelled"])
async def test_skill_untrack_lease_precedes_topology_release_and_delete(
    runtime_case: RuntimeCase, skill_tracking_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch, child: bool, cancelled: bool,
) -> None:
    case = runtime_case
    container = case.runner.container
    service = container.session_service
    name = "topology-tracked-skill"
    skill = case.runner.workspace / ".boxteam" / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(f"---\nname: {name}\ndescription: 控制准入竞争\n---\n冻结的 Skill 正文。\n")

    def model_reply(_payload):
        # 使用既有响应资产，不另维护 SSE 编码或假模型协议。
        loading = len(case.model_requests) == 1
        delta = {"role": "assistant", **({"tool_calls": [{
            "index": 0, "id": "topology-skill-load", "type": "function",
            "function": {"name": "skill_load", "arguments": json.dumps({"name": name, "mode": "tracked"})},
        }]} if loading else {"content": "Skill 已跟踪。"})}
        return [cast(dict[str, object], frame.payload) for frame in chat_completion_frames(
            response_id="chatcmpl-topology-skill", model="runtime-skill-model", deltas=[delta],
            finish_reason="tool_calls" if loading else "stop",
        ) if frame.kind == "data"]

    case.response_chunks = model_reply
    session_id, thread_id = await _session(case.runner)
    if child:
        thread_id = await _child(case, session_id)
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        await _complete_job(case, session_id, thread_id, timeout_seconds=25)
        await container.job_service.drain_finalizations()
        owner = ContextSourceOwnerKey(session_id, thread_id)
        states = case.runner.saver.load_context_source_control_states(owner)
        tracked = [state for state in states if state.source_kind == "skill" and state.tracking_status == "tracked"]
        assert len(tracked) == 1
        assert len(case.model_requests) == 2
        locator = service.path_resolver.resolve_session_node(session_id)
        control_path = locator / "session-control.sqlite"
        topology = service._navigation_topology_gate.shared().path
        delete_id = "delete-during-skill-untrack"
        released, resume_control, marked, resume_delete = (asyncio.Event() for _ in range(4))
        control_task = delete_task = None
        exit_lock = _CrossProcessFileLock.__aexit__

        async def pause_after_topology(lock, *args):
            result = await exit_lock(lock, *args)
            if lock.path == topology and not lock._exclusive and asyncio.current_task() is control_task:
                released.set()
                await resume_control.wait()
            return result

        async def observe_delete(operation_id, phase, *, details=None):
            if operation_id == delete_id and phase == "deletion.catalog_deleting":
                marked.set()
                await resume_delete.wait()

        monkeypatch.setattr(_CrossProcessFileLock, "__aexit__", pause_after_topology)
        monkeypatch.setattr("app.core.session_subtree_delete.observe_phase", observe_delete)
        try:
            control_task = asyncio.create_task(skill_tracking_client.post(
                f"/api/v1/sessions/{session_id}/threads/{thread_id}/skills/untrack", json={"name": name},
            ))
            await asyncio.wait_for(released.wait(), 3)
            delete_task = asyncio.create_task(service.path_resolver.delete_subtree(
                idempotency_key=delete_id, root_node_id=session_id,
            ))
            await asyncio.wait_for(marked.wait(), 3)
            assert service.path_resolver.catalog_store.get_node(session_id).state == "deleting"
            with closing(SessionControlStore(control_path, read_only=True)) as control:
                fence = control.get_fence()
                leases = [lease for lease in control.list_non_terminal_leases()
                          if lease.operation_identity.startswith("context-control:skill-untrack:")]
                assert len(leases) == 1, "释放 topology 时尚未建立 Skill 控制 lease，删除已抢先提交"
                lease = leases[0]
                assert lease.state == "settling" and lease.captured_lifecycle_generation == fence[1]
                assert fence[0] == "active"
            # 删除已可取得 topology 并标记；旧控制仍在 Session gate 内完成或失败结算。
            if cancelled:
                control_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await control_task
            else:
                resume_control.set()
                response = await asyncio.wait_for(control_task, 5)
                assert response.status_code == 200, response.text
                receipt = response.json()["data"]
                assert (receipt["session_id"], receipt["thread_id"], receipt["name"]) == (session_id, thread_id, name)
                assert receipt["error"] is None and receipt["tracked"] is False
            with closing(SessionControlStore(control_path, read_only=True)) as control:
                settled = control.get_lease(lease.lease_id)
                assert settled.state == ("failed" if cancelled else "completed")
                assert settled.operation_identity == lease.operation_identity
                assert settled.preimage_hash == lease.preimage_hash
                assert settled.captured_lifecycle_generation == lease.captured_lifecycle_generation
            state = next(state for state in case.runner.saver.load_context_source_control_states(owner)
                         if state.source_id == tracked[0].source_id)
            assert state.tracking_status == ("tracked" if cancelled else "untracked")
            assert len(case.model_requests) == 2
            resume_delete.set()
            await asyncio.wait_for(delete_task, 10)
            assert not locator.exists()
        finally:
            resume_control.set()
            resume_delete.set()
            await asyncio.gather(*(task for task in (control_task, delete_task) if task is not None), return_exceptions=True)
