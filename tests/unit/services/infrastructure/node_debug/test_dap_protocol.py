"""DAP 协议的准入顺序、失效引用与断点替换失败边界。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.node_debug.session.dap_protocol import (
    NodeDebugDAPSession,
)
from tests.support.node_debug_dependencies import TEST_THREAD_ID, node_debug_file_paths


@pytest.fixture
def dap_owner(tmp_path: Path):
    runtime = NodeDebugRuntime(
        session_id="ses_00000000000070008000000000000001", thread_id=TEST_THREAD_ID,
        configuration_id="dbgcfg_test", workspace_root=tmp_path,
        script_path=tmp_path / "main.mjs", relative_script_path="main.mjs", status="paused",
    )
    owner = (runtime.session_id, runtime.thread_id)
    phases: list[str] = []
    lock = asyncio.Lock()

    @asynccontextmanager
    async def admission(*key: str) -> AsyncIterator[tuple[str, str]]:
        assert key == owner
        assert not lock.locked(), "必须先取得 Session 准入，再取得 owner 锁"
        phases.append("admitted")
        try:
            yield owner
        finally:
            assert not lock.locked()
            phases.append("released")

    async def action(key: tuple[str, str], **_kwargs: object):
        assert key == owner and lock.locked() and phases[-1] == "admitted"
        return SimpleNamespace(breakpoints=[])

    service = SimpleNamespace(
        _admit_mutation=admission, _owner_lock=lambda key: lock,
        _runtimes={owner: runtime},
        _apply_action_admitted=AsyncMock(side_effect=action),
        _get_state_admitted=AsyncMock(return_value=SimpleNamespace(breakpoints=[
            SimpleNamespace(path="main.mjs", breakpoint_id="existing"),
        ])),
        _file_paths=node_debug_file_paths(tmp_path),
    )
    return NodeDebugDAPSession(cast(NodeDebugService, service), runtime), service, runtime, phases


async def test_dap_uses_one_admission_before_owner_lock_and_same_action_chain(dap_owner):
    dap, service, _runtime, phases = dap_owner
    response = await dap.dispatch({"type": "request", "seq": 1, "command": "terminate"})
    assert response["success"] is True
    assert phases == ["admitted", "released"]
    service._apply_action_admitted.assert_awaited_once()
    assert service._apply_action_admitted.await_args.kwargs["command"].action == "stop"


async def test_dap_rejects_old_pause_handles_and_replaced_run_without_mutation(dap_owner):
    dap, service, runtime, _phases = dap_owner
    dap._frames[1] = ("old-frame", runtime.pause_generation)
    dap._scopes[2] = ("local", 1, runtime.pause_generation)
    runtime.pause_generation += 1
    for command, arguments in [
        ("variables", {"variablesReference": 2}),
        ("evaluate", {"frameId": 1, "expression": "state.counter++"}),
    ]:
        response = await dap.dispatch({"type": "request", "seq": 1, "command": command, "arguments": arguments})
        assert response["success"] is False and "已失效" in response["message"]
    service._runtimes[(runtime.session_id, runtime.thread_id)] = object()
    response = await dap.dispatch({"type": "request", "seq": 2, "command": "terminate"})
    assert response["success"] is False and "run 已更换" in response["message"]
    service._apply_action_admitted.assert_not_awaited()


@pytest.mark.parametrize("breakpoints", [
    [{"line": 0}],
    [{"line": 1}, {"line": 2, "hitCondition": "0"}],
    [{"line": 1, "column": 0}],
    [{"line": 1, "logMessage": ""}],
])
async def test_invalid_breakpoint_candidate_preserves_existing_source_breakpoints(dap_owner, breakpoints):
    dap, service, runtime, _phases = dap_owner
    response = await dap.dispatch({"type": "request", "seq": 1, "command": "setBreakpoints", "arguments": {
        "source": {"path": service._file_paths.reference(runtime.script_path)}, "breakpoints": breakpoints,
    }})
    assert response["success"] is False
    service._get_state_admitted.assert_not_awaited()
    service._apply_action_admitted.assert_not_awaited()


@pytest.mark.parametrize("command,arguments", [
    ("source", {"sourceReference": 7}),
    ("setExceptionBreakpoints", {"filters": ["uncaught"]}),
    ("attach", {}), ("restart", {}), ("cancel", {"requestId": 1}),
])
async def test_unsupported_dap_capabilities_fail_explicitly_without_actions(dap_owner, command, arguments):
    dap, service, _runtime, _phases = dap_owner
    response = await dap.dispatch({"type": "request", "seq": 3, "command": command, "arguments": arguments})
    assert response["success"] is False and response["message"]
    service._apply_action_admitted.assert_not_awaited()
