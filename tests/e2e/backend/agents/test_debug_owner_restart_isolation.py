"""真实 Node 进程的 SessionThread owner stop/restart 隔离验收。

覆盖同一 Session 的 main/child 与另一 Session 的 main 同时持有独立 Node
Inspector 实例；重启一个 owner 不能改变其它 owner 的状态、方案、动作或
durable launch claim。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import httpx
import pytest

from app.schemas.internal_v2.node_debug import NodeDebugConfigurationCreateRequest
from tests.e2e.backend.agents.test_debug_tools import (
    _build_debug_service,
    _create_catalog_debug_owners,
    _write_debug_fixture,
)


def _state_fingerprint(state: object) -> dict[str, object]:
    """把 Pydantic 状态冻结为可比较的公开快照。"""
    model_dump = getattr(state, "model_dump", None)
    if not callable(model_dump):
        raise TypeError(f"调试状态不是可序列化模型: {type(state)!r}")
    result = model_dump(mode="json")
    if not isinstance(result, dict):
        raise TypeError("调试状态序列化结果不是对象")
    return result


def _assert_process_alive(pid: int | None) -> None:
    assert pid is not None
    os.kill(pid, 0)


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js Inspector")
async def test_owner_restart_preserves_same_session_child_and_other_session(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
) -> None:
    workspace_root = Path(e2e_workspace_root_path).resolve()
    resolver, parent_main, child = await _create_catalog_debug_owners(
        client,
        workspace_root,
    )
    other_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Debug independent owner"},
    )
    assert other_response.status_code == 200, other_response.text
    other_session_id = other_response.json()["data"]["session_id"]
    other_owner = (other_session_id, "main")

    fixture_path, breakpoint_line = _write_debug_fixture(workspace_root)
    service, store = _build_debug_service(
        workspace_root,
        Path(e2e_workspace_config_path),
        resolver,
    )
    try:
        for session_id, thread_id, name in (
            (parent_main.session_id, parent_main.thread_id, "parent-main"),
            (child.session_id, child.thread_id, "same-session-child"),
            (other_session_id, "main", "other-session-main"),
        ):
            created = await service.create_configuration(
                NodeDebugConfigurationCreateRequest(
                    session_id=session_id,
                    thread_id=thread_id,
                    name=name,
                    script_path=fixture_path.name,
                    breakpoints=[
                        {"path": fixture_path.name, "line": breakpoint_line}
                    ],
                )
            )
            assert created.active_configuration_id is not None

        for session_id, thread_id in (
            (parent_main.session_id, parent_main.thread_id),
            (child.session_id, child.thread_id),
            other_owner,
        ):
            started = await service.start(
                session_id=session_id,
                thread_id=thread_id,
                path=fixture_path.name,
                working_directory="",
                args=[],
                breakpoints=[],
            )
            assert started.status == "paused"

        child_before = await service.get_state(child.session_id, child.thread_id)
        other_before = await service.get_state(*other_owner)
        child_claim_before = store.read_launch_claim(
            child.session_id,
            child.thread_id,
        )
        other_claim_before = store.read_launch_claim(*other_owner)
        assert child_claim_before is not None
        assert other_claim_before is not None
        assert child_claim_before.phase == "running"
        assert other_claim_before.phase == "running"
        _assert_process_alive(child_before.pid)
        _assert_process_alive(other_before.pid)
        child_manifest_before = store.read_manifest(
            child.session_id,
            child.thread_id,
        )
        other_manifest_before = store.read_manifest(*other_owner)
        assert child_manifest_before is not None
        assert other_manifest_before is not None

        restarted = await service.restart(parent_main.session_id, thread_id="main")
        assert restarted.status == "paused"

        child_after = await service.get_state(child.session_id, child.thread_id)
        other_after = await service.get_state(*other_owner)
        child_claim_after = store.read_launch_claim(
            child.session_id,
            child.thread_id,
        )
        other_claim_after = store.read_launch_claim(*other_owner)
        assert child_claim_after == child_claim_before
        assert other_claim_after == other_claim_before
        assert _state_fingerprint(child_after) == _state_fingerprint(child_before)
        assert _state_fingerprint(other_after) == _state_fingerprint(other_before)
        assert store.read_manifest(child.session_id, child.thread_id) == (
            child_manifest_before
        )
        assert store.read_manifest(*other_owner) == other_manifest_before
        _assert_process_alive(child_after.pid)
        _assert_process_alive(other_after.pid)
    finally:
        await service.close()


__all__ = ["test_owner_restart_preserves_same_session_child_and_other_session"]
