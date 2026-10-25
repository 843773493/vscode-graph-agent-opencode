"""启动原生错误不得进入调试公开状态和持久动作正文。"""

from __future__ import annotations

import asyncio
import errno
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.process import (
    launch_orchestrator,
    process_lifecycle,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.support.node_debug_dependencies import (
    node_debug_file_paths,
    permissive_node_debug_session_admission,
)
from tests.unit.core.catalog_workspace_helper import (
    CatalogWorkspaceContext,
    build_catalog_workspace,
)


@dataclass
class DebugLaunchContext:
    workspace: CatalogWorkspaceContext
    store: NodeDebugSessionStore
    session_id: str
    thread_id: str
    service: NodeDebugService = field(init=False)

    def cold_service(self) -> NodeDebugService:
        root = self.workspace.workspace_root
        return NodeDebugService(
            workspace_root=root, file_paths=node_debug_file_paths(root),
            config_service=ConfigService(workspace_root=root),
            session_store=self.store,
            session_admission=permissive_node_debug_session_admission(),
            external_resource_leases=ExternalResourceLeaseLedger(),
        )


@pytest.fixture
async def debug_launch_context(tmp_path: Path) -> AsyncIterator[DebugLaunchContext]:
    with build_catalog_workspace(tmp_path / "workspace") as workspace:
        session_id = await workspace.create_session("启动错误边界")
        (workspace.workspace_root / "entry.mjs").write_text("console.log(42);\n")
        store = NodeDebugSessionStore(workspace.resolver)
        fixture = DebugLaunchContext(
            workspace, store, session_id, workspace.resolver.main_thread_id(session_id),
        )
        fixture.service = fixture.cold_service()
        fixture.service._node_bin = "unit-node"
        try:
            yield fixture
        finally:
            await fixture.service.close()


@pytest.fixture(params=["/private/native-launch-secret", r"C:\private\native-launch-secret", r"\\server\private\native-launch-secret"])
def native_launch_error(request: pytest.FixtureRequest) -> PermissionError:
    error = PermissionError(errno.EACCES, f"原生错误: {request.param}", request.param)
    error.winerror = 5
    return error


@pytest.mark.asyncio
async def test_native_launch_error_keeps_cause_and_cold_history_without_native_path(
    debug_launch_context: DebugLaunchContext, native_launch_error: PermissionError,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = debug_launch_context
    attempts = []

    async def fail_spawn(*args: object, **kwargs: object) -> None:
        attempts.append((args, kwargs))
        raise native_launch_error

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    with caplog.at_level(logging.ERROR, logger=launch_orchestrator.__name__), pytest.raises(RuntimeError) as caught:
        await fixture.service.start(
            session_id=fixture.session_id, thread_id=fixture.thread_id,
            path="entry.mjs", args=[], breakpoints=[],
        )
    message = str(caught.value)
    assert caught.value.__cause__ is native_launch_error
    assert fixture.session_id in message and fixture.thread_id in message
    assert "PermissionError" in message and f"errno={errno.EACCES}" in message
    assert "winerror=5" in message
    assert native_launch_error.filename not in message and "native-launch-secret" not in message
    assert len(attempts) == 1 and attempts[0][0][0] == "unit-node"
    assert Path(attempts[0][1]["cwd"]) == fixture.workspace.workspace_root
    assert any(record.exc_info and record.exc_info[1] is native_launch_error for record in caplog.records)

    state = await fixture.service.get_state(fixture.session_id, fixture.thread_id)
    assert state.status == "failed" and state.pid is None and state.error_message == message
    failures = [action for action in state.actions if action.action == "start_failed"]
    assert len(failures) == 1 and failures[0].message == message and failures[0].result == "error"
    manifest_bytes, manifest = fixture.store.read_manifest_payload(fixture.session_id, fixture.thread_id)
    assert manifest_bytes is not None and manifest is not None
    assert "native-launch-secret" not in manifest_bytes.decode()
    assert [action for action in manifest.actions if action.action == "start_failed"] == failures
    claim = fixture.store.read_launch_claim(fixture.session_id, fixture.thread_id)
    assert claim is not None and claim.phase == "settled" and claim.pid is None

    cold = fixture.cold_service()
    try:
        restored = await cold.get_state(fixture.session_id, fixture.thread_id)
        assert restored.status == "idle" and restored.pid is None
        assert restored.active_configuration_id == state.active_configuration_id
        assert [action for action in restored.actions if action.action == "start_failed"] == failures
        assert "native-launch-secret" not in restored.model_dump_json()
    finally:
        await cold.close()


@pytest.mark.asyncio
async def test_non_native_launch_failure_keeps_existing_message_and_cause(
    debug_launch_context: DebugLaunchContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = debug_launch_context
    error = RuntimeError("原有启动协议拒绝")

    async def fail_spawn(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    with pytest.raises(RuntimeError, match="启动 Node Inspector 失败: 原有启动协议拒绝") as caught:
        await fixture.service.start(
            session_id=fixture.session_id, thread_id=fixture.thread_id,
            path="entry.mjs", args=[], breakpoints=[],
        )
    assert caught.value.__cause__ is error


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,action", [
    ("signal", "stop_signal_failed"), ("kill", "stop_kill_failed"), ("wait", "stop_wait_failed"),
])
async def test_native_stop_errors_do_not_publish_paths_into_durable_actions(
    debug_launch_context: DebugLaunchContext, phase: str, action: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = debug_launch_context
    raw_error = PermissionError(errno.EACCES, "原生停止故障", "/private/native-stop-secret")

    class StopProcess:
        returncode: int | None = None
        waits = 0

        def terminate(self) -> None:
            if phase == "signal":
                raise raw_error

        def kill(self) -> None:
            raise raw_error

        async def wait(self) -> int:
            self.waits += 1
            if phase == "kill" and self.waits == 1:
                raise TimeoutError
            self.returncode = 0
            if phase == "wait":
                raise raw_error
            return 0

    runtime = NodeDebugRuntime(
        session_id=fixture.session_id, thread_id=fixture.thread_id,
        workspace_root=fixture.workspace.workspace_root,
        script_path=fixture.workspace.workspace_root / "entry.mjs",
        relative_script_path="entry.mjs", configuration_id="dbgcfg_" + "1" * 32,
        process=StopProcess(),  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.ERROR, logger=process_lifecycle.__name__):
        assert await fixture.service._lifecycle.terminate_and_verify(runtime) is None
    assert len(runtime.actions) == 1
    recorded = runtime.actions[0]
    assert recorded.action == action and recorded.actor == "system" and recorded.result == "error"
    assert fixture.session_id in recorded.message and fixture.thread_id in recorded.message
    assert "PermissionError" in recorded.message and f"errno={errno.EACCES}" in recorded.message
    assert "native-stop-secret" not in recorded.message
    assert any(record.exc_info and record.exc_info[1] is raw_error for record in caplog.records)
