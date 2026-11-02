"""真实 Node 启动中的接收及进程身份失败，核对 HTTP、持久动作与冷读。"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest
import websockets
from fastapi import FastAPI, HTTPException
from websockets.asyncio.client import ClientConnection

from app.api.deps import get_node_debug_service, verify_local_token
from app.api.node_debug import router
from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import load_or_create_workspace_id
from app.main import workspace_http_exception_handler
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.process import process_identity
from app.services.infrastructure.node_debug.process.inspector import NodeDebugInspector
from app.services.infrastructure.node_debug.process.process_identity import (
    probe_process_identity,
)
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.node_debug_dependencies import (
    node_debug_file_paths,
    permissive_node_debug_session_admission,
)
from tests.unit.core.catalog_workspace_helper import build_catalog_workspace

DebugReceiveClient = tuple[
    httpx.AsyncClient, NodeDebugSessionStore, dict[str, str], Path, Callable[[bool], Awaitable[None]],
]


async def test_native_stderr_stack_stays_in_diagnostics_after_exit_and_cold_read(
    debug_receive_client: DebugReceiveClient, caplog: pytest.LogCaptureFixture,
) -> None:
    client, store, owner, root, reopen = debug_receive_client
    script = root / "native-stderr-secret.mjs"
    script.write_text(
        "Error.stackTraceLimit = 1;\n"
        "const failure = new Error('native-stderr-failure');\n"
        "setTimeout(() => {\n"
        "  process.stderr.write(failure.stack + '\\n');\n"
        "  process.exit(7);\n"
        "}, 100);\n",
        encoding="utf-8",
    )
    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "真实 stderr 失败后重试", "script_path": script.name,
    })
    assert configured.status_code == 200, configured.text
    configuration_id = configured.json()["data"]["active_configuration_id"]
    payload = {**owner, "configuration_id": configuration_id, "path": script.name}
    with caplog.at_level(logging.INFO):
        started = await client.post("/api/v1/debug/node/start", json={
            **payload, "breakpoints": [{"path": script.name, "line": 2}],
        })
        assert started.status_code == 200, started.text
        before = started.json()["data"]
        assert before["status"] == "paused" and isinstance(before["pid"], int)
        assert before["call_stack"][0]["line"] == 2
        continued = await client.post("/api/v1/debug/node/action", json={**owner, "action": "continue"})
        assert continued.status_code == 200, continued.text
        deadline = time.monotonic() + 5
        while True:
            failed = await client.get("/api/v1/debug/node", params=owner)
            assert failed.status_code == 200, failed.text
            data = failed.json()["data"]
            if data["status"] == "failed":
                break
            assert time.monotonic() < deadline, failed.text
            await asyncio.sleep(0.02)
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    (context.artifacts_dir / "stderr-before-assertion.json").write_text(json.dumps({
        "started": started.json(), "continued": continued.json(), "failed": failed.json(),
        "diagnostics": caplog.text,
    }, ensure_ascii=False, indent=2) + "\n")
    assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
    assert data["pid"] == before["pid"] and probe_process_identity(data["pid"]) is None
    assert data["error_message"] == "Node 调试进程退出，退出码: 7"
    assert data["breakpoints"][0]["source_line"] == "const failure = new Error('native-stderr-failure');"
    assert "Error: native-stderr-failure" in data["output"]
    assert any(f"{script}:2:" in line for line in data["output"])
    assert len(data["output"]) <= 100
    assert all("Error: native-stderr-failure" not in action["message"] for action in data["actions"])
    assert f"{script}:2:" in caplog.text and "Error: native-stderr-failure" in caplog.text
    assert owner["session_id"] in caplog.text and owner["thread_id"] in caplog.text
    manifest_bytes, manifest = store.read_manifest_payload(**owner)
    assert manifest_bytes is not None and manifest is not None
    assert str(root) not in manifest_bytes.decode() and "Error: native-stderr-failure" not in manifest_bytes.decode()
    claim = store.read_launch_claim(**owner)
    assert claim is not None and claim.phase == "settled" and claim.pid == before["pid"]
    await reopen(True)
    cold = await client.get("/api/v1/debug/node", params=owner)
    assert cold.status_code == 200, cold.text
    restored = cold.json()["data"]
    assert restored["status"] == "idle" and restored["pid"] is None
    assert restored["active_configuration_id"] == configuration_id
    assert restored["actions"] == data["actions"]
    assert restored["breakpoints"][0]["source_line"] == data["breakpoints"][0]["source_line"]
    assert str(root) not in cold.text and "Error: native-stderr-failure" not in cold.text
    script.write_text("const value = 43;\nsetInterval(() => {}, 1000);\n", encoding="utf-8")
    retried = await client.post("/api/v1/debug/node/start", json=payload)
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["active_configuration_id"] == configuration_id
    stopped = await client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"})
    assert stopped.status_code == 200 and stopped.json()["data"]["status"] == "exited", stopped.text
    (context.artifacts_dir / "stderr-recovered.json").write_text(json.dumps({
        "cold": cold.json(), "retried": retried.json(), "stopped": stopped.json(),
    }, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.parametrize("phase", ["connect", "response", "result"])
async def test_launch_inspector_boundary_keeps_durable_failure_private_and_retries(
    debug_receive_client: DebugReceiveClient, phase: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    client, store, owner, root, reopen = debug_receive_client
    private = str(root / "inspector-boundary-secret")
    raw = RuntimeError(f"Inspector 边界拒绝 {private}；" + "完整诊断" * 80)
    original_connect = websockets.connect
    opened: list[ClientConnection] = []
    replaced: list[int] = []

    class BoundarySocket:
        def __init__(self, socket: ClientConnection) -> None:
            self.socket = socket
            self.command_id: int | None = None

        async def send(self, message: str) -> None:
            payload = json.loads(message)
            if payload["method"] == "Runtime.enable":
                self.command_id = payload["id"]
            await self.socket.send(message)

        async def close(self) -> None:
            await self.socket.close()

        def __aiter__(self) -> AsyncIterator[str | bytes]:
            async def messages() -> AsyncIterator[str | bytes]:
                async for message in self.socket:
                    payload = json.loads(message)
                    if self.command_id is not None and payload.get("id") == self.command_id:
                        replaced.append(self.command_id)
                        yield json.dumps({"id": self.command_id, **(
                            {"error": {"code": -32000, "message": str(raw)}}
                            if phase == "response" else {"result": [private]}
                        )})
                    else:
                        yield message
            return messages()

    async def connect(uri: str, *, ping_interval: float | None) -> BoundarySocket:
        if phase == "connect":
            raise raw
        socket = await original_connect(uri, ping_interval=ping_interval)
        opened.append(socket)
        return BoundarySocket(socket)

    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "Inspector 边界故障后重试", "script_path": "entry.mjs",
    })
    assert configured.status_code == 200, configured.text
    configuration_id = configured.json()["data"]["active_configuration_id"]
    payload = {**owner, "configuration_id": configuration_id, "path": "entry.mjs"}
    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
        patch.setattr(websockets, "connect", connect)
        failed = await client.post("/api/v1/debug/node/start", json=payload)
    assert failed.status_code == 409, failed.text
    assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
    assert f"stage=node-debug-{'command' if phase == 'response' else phase}" in failed.text
    assert private not in failed.text
    assert private in caplog.text
    if phase == "connect":
        assert any(record.exc_info and record.exc_info[1] is raw for record in caplog.records)
        assert not opened and not replaced
    else:
        assert len(opened) == len(replaced) == 1
        assert opened[0].close_code is not None
        assert "method=Runtime.enable" in failed.text
    state = await client.get("/api/v1/debug/node", params=owner)
    assert state.status_code == 200, state.text
    data = state.json()["data"]
    assert data["status"] == "failed" and isinstance(data["pid"], int)
    assert probe_process_identity(data["pid"]) is None
    assert data["active_configuration_id"] == configuration_id
    assert data["error_message"] == failed.json()["detail"]
    failed_actions = [a for a in data["actions"] if a["action"] == "start_failed"]
    assert len(failed_actions) == 1 and failed_actions[0]["message"] == data["error_message"]
    manifest_bytes, manifest = store.read_manifest_payload(**owner)
    assert manifest_bytes is not None and manifest is not None
    assert private not in manifest_bytes.decode() + state.text
    assert [a.model_dump(mode="json") for a in manifest.actions] == data["actions"]
    claim = store.read_launch_claim(**owner)
    assert claim is not None and claim.phase == "settled" and claim.pid == data["pid"]
    await reopen(True)
    cold = await client.get("/api/v1/debug/node", params=owner)
    assert cold.status_code == 200, cold.text
    assert cold.json()["data"]["actions"] == data["actions"]
    assert cold.json()["data"]["active_configuration_id"] == configuration_id
    assert cold.json()["data"]["status"] == "idle" and cold.json()["data"]["pid"] is None
    assert private not in cold.text
    retried = await client.post("/api/v1/debug/node/start", json=payload)
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["status"] == "running"
    assert retried.json()["data"]["active_configuration_id"] == configuration_id
    stopped = await client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["data"]["status"] == "exited"
    assert [a for a in stopped.json()["data"]["actions"] if a["action"] == "start_failed"] == failed_actions
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    (context.artifacts_dir / f"inspector-boundary-{phase}.json").write_text(json.dumps({
        "failed": failed.json(), "live": state.json(), "cold": cold.json(),
        "retried": retried.json(), "stopped": stopped.json(),
    }, ensure_ascii=False, indent=2) + "\n")


async def test_source_invalidation_failure_keeps_original_process_and_safe_cold_actions(
    debug_receive_client: DebugReceiveClient, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, store, owner, root, reopen = debug_receive_client
    source = root / "entry.mjs"
    source.write_text("const value = 42;\nsetInterval(() => {}, 1000);\n", encoding="utf-8")
    raw = PermissionError(13, "断点移除发送失败；" + "完整诊断" * 80, str(root / "breakpoint-cleanup-secret"))
    original_connect = websockets.connect
    removals: list[str] = []

    class RemoveFailingSocket:
        def __init__(self, socket: ClientConnection) -> None:
            self.socket = socket

        async def send(self, message: str) -> None:
            payload = json.loads(message)
            if payload["method"] == "Debugger.removeBreakpoint":
                removals.append(payload["params"]["breakpointId"])
                raise raw
            await self.socket.send(message)

        async def close(self) -> None:
            await self.socket.close()

        def __aiter__(self) -> AsyncIterator[str | bytes]:
            return self.socket.__aiter__()

    async def connect(uri: str, *, ping_interval: float | None) -> RemoveFailingSocket:
        return RemoveFailingSocket(await original_connect(uri, ping_interval=ping_interval))

    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "源码失效清理", "script_path": "entry.mjs",
    })
    assert configured.status_code == 200, configured.text
    configuration_id = configured.json()["data"]["active_configuration_id"]
    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
        patch.setattr(websockets, "connect", connect)
        started = await client.post("/api/v1/debug/node/start", json={
            **owner, "configuration_id": configuration_id, "path": "entry.mjs",
            "breakpoints": [{"path": "entry.mjs", "line": 2}],
        })
        assert started.status_code == 200, started.text
        before = started.json()["data"]
        assert before["status"] == "paused" and isinstance(before["pid"], int)
        assert before["call_stack"][0]["line"] == 2
        breakpoint_id = before["breakpoints"][0]["breakpoint_id"]
        source.write_text("const value = 43;\nsetInterval(() => {}, 1000);\n", encoding="utf-8")
        changed = await client.get("/api/v1/debug/node", params=owner)
    assert changed.status_code == 200, changed.text
    assert changed.json()["request_id"] == changed.headers["X-Request-ID"]
    data = changed.json()["data"]
    assert data["status"] == "paused" and data["pid"] == before["pid"]
    assert data["requires_restart"] is True and data["source_changed_paths"] == ["entry.mjs"]
    assert data["breakpoints"][0]["breakpoint_id"] == breakpoint_id
    assert data["breakpoints"][0]["relocation_status"] == "pending_update"
    failures = [a for a in data["actions"] if a["action"] == "breakpoint_invalidation_failed"]
    assert len(failures) == len(removals) == 1
    assert failures[0]["actor"] == "system" and failures[0]["result"] == "error"
    assert "stage=node-debug-breakpoint-invalidation, error=PermissionError, errno=13" in failures[0]["message"]
    assert owner["session_id"] in failures[0]["message"] and owner["thread_id"] in failures[0]["message"]
    assert breakpoint_id in failures[0]["message"]
    assert "breakpoint-cleanup-secret" not in changed.text
    assert str(raw) in caplog.text
    assert any(record.exc_info and record.exc_info[1] is raw for record in caplog.records)
    assert probe_process_identity(data["pid"]) is not None
    manifest_bytes, manifest = store.read_manifest_payload(**owner)
    assert manifest_bytes is not None and manifest is not None
    assert "breakpoint-cleanup-secret" not in manifest_bytes.decode()
    assert [a.model_dump(mode="json") for a in manifest.actions] == data["actions"]
    again = await client.get("/api/v1/debug/node", params=owner)
    assert again.status_code == 200, again.text
    assert again.json()["data"]["actions"] == data["actions"] and len(removals) == 1
    continued = await client.post("/api/v1/debug/node/action", json={**owner, "action": "continue"})
    assert continued.status_code == 200, continued.text
    assert continued.json()["data"]["status"] == "running" and continued.json()["data"]["pid"] == before["pid"]
    stopped = await client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"})
    assert stopped.status_code == 200 and stopped.json()["data"]["status"] == "exited", stopped.text
    await reopen(True)
    cold = await client.get("/api/v1/debug/node", params=owner)
    assert cold.status_code == 200, cold.text
    assert cold.json()["data"]["active_configuration_id"] == configuration_id
    assert [a for a in cold.json()["data"]["actions"] if a["action"] == "breakpoint_invalidation_failed"] == failures
    assert "breakpoint-cleanup-secret" not in cold.text and len(removals) == 1
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    (context.artifacts_dir / "breakpoint-invalidation.json").write_text(json.dumps({
        "started": started.json(), "changed": changed.json(), "continued": continued.json(),
        "stopped": stopped.json(), "cold": cold.json(),
    }, ensure_ascii=False, indent=2) + "\n")


@pytest.fixture
async def debug_receive_client(integration_workspace_root_path: str) -> AsyncIterator[DebugReceiveClient]:
    root = Path(integration_workspace_root_path)
    with build_catalog_workspace(root.parent, workspace_id=load_or_create_workspace_id(root)) as workspace:
        session_id = await workspace.create_session("Inspector 接收错误")
        owner = {"session_id": session_id, "thread_id": workspace.resolver.main_thread_id(session_id)}
        (root / "entry.mjs").write_text("setInterval(() => {}, 1000);\n")
        store = NodeDebugSessionStore(workspace.resolver)
        services: list[NodeDebugService] = []

        def service() -> NodeDebugService:
            created = NodeDebugService(
                workspace_root=root, file_paths=node_debug_file_paths(root),
                config_service=ConfigService(workspace_root=root), session_store=store,
                session_admission=permissive_node_debug_session_admission(),
                external_resource_leases=ExternalResourceLeaseLedger(),
            )
            services.append(created)
            return created

        app = FastAPI()
        app.add_exception_handler(HTTPException, workspace_http_exception_handler)
        app.add_middleware(TraceMiddleware)
        app.include_router(router, prefix="/api/v1")
        current = service()
        app.dependency_overrides[get_node_debug_service] = lambda: current
        app.dependency_overrides[verify_local_token] = lambda: "local"
        async def reopen(close_current: bool) -> None:
            nonlocal current
            if close_current:
                await current.close()
            else:
                # 冷 owner 模拟旧 Backend 已消失；不能让旧 monitor 与新 owner 并写 claim。
                # 这里只结束测试观察任务和连接，外部 Node 及持久记录保持原样。
                runtimes = tuple(current._runtimes.values())
                observers = tuple(
                    task for runtime in runtimes
                    for task in (runtime.process_task, runtime.stdout_task, runtime.stderr_task,
                                 runtime.inspector.receiver_task, runtime.inspector.variable_hydration_task)
                    if task is not None
                )
                for task in observers:
                    if not task.done():
                        task.cancel()
                for result in await asyncio.gather(*observers, return_exceptions=True):
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                        raise result
                for runtime in runtimes:
                    if runtime.inspector.socket is not None:
                        await runtime.inspector.socket.close()
            current = service()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            try:
                yield client, store, owner, root, reopen
            finally:
                for created in reversed(services):
                    await created.close()



@pytest.mark.asyncio
async def test_handshake_receive_failure_keeps_native_cause_private_and_persists_actions(
    debug_receive_client: DebugReceiveClient,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    client, store, owner, root, reopen = debug_receive_client
    native_error = PermissionError(errno.EACCES, "Inspector 接收边界故障", str(root / "inspector-receive-secret"))
    original_connect = websockets.connect
    opened: list[ClientConnection] = []

    class FailingReceiver:
        def __init__(self, socket: ClientConnection) -> None:
            self.socket = socket
            self.failure: PermissionError | None = native_error

        async def send(self, message: str) -> None:
            await self.socket.send(message)

        async def close(self) -> None:
            await self.socket.close()

        def __aiter__(self) -> AsyncIterator[str | bytes]:
            async def messages() -> AsyncIterator[str | bytes]:
                async for message in self.socket:
                    if self.failure is not None:
                        error = self.failure
                        self.failure = None
                        raise error
                    yield message
            return messages()

    async def connect(*args: object, **kwargs: object) -> FailingReceiver:
        socket = await original_connect(*args, **kwargs)
        opened.append(socket)
        return FailingReceiver(socket)

    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "原方案接收失败后重试", "script_path": "entry.mjs",
    })
    assert configured.status_code == 200, configured.text
    configuration_id = configured.json()["data"]["active_configuration_id"]
    payload = {**owner, "configuration_id": configuration_id, "path": "entry.mjs"}
    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR, logger=NodeDebugInspector.__module__):
        patch.setattr(websockets, "connect", connect)
        failed = await client.post("/api/v1/debug/node/start", json=payload)
    assert failed.status_code == 409, failed.text
    assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
    assert "PermissionError" in failed.text and "errno=13" in failed.text
    assert owner["session_id"] in failed.text and owner["thread_id"] in failed.text
    assert "inspector-receive-secret" not in failed.text
    assert any(record.exc_info and record.exc_info[1] is native_error for record in caplog.records)
    assert len(opened) == 1 and opened[0].close_code is not None
    state = await client.get("/api/v1/debug/node", params=owner)
    assert state.status_code == 200, state.text
    data = state.json()["data"]
    assert data["status"] == "failed" and isinstance(data["pid"], int)
    assert probe_process_identity(data["pid"]) is None
    assert data["error_message"] == failed.json()["detail"]
    failures = [action for action in data["actions"] if action["action"] == "start_failed"]
    assert len(failures) == 1 and failures[0]["message"] == data["error_message"]
    manifest_bytes, manifest = store.read_manifest_payload(**owner)
    assert manifest_bytes is not None and manifest is not None
    assert "inspector-receive-secret" not in manifest_bytes.decode()
    assert [action.model_dump(mode="json") for action in manifest.actions] == data["actions"]
    claim = store.read_launch_claim(**owner)
    assert claim is not None and claim.phase == "settled" and claim.pid == data["pid"]

    await reopen(True)
    cold = await client.get("/api/v1/debug/node", params=owner)
    assert cold.status_code == 200, cold.text
    restored = cold.json()["data"]
    assert restored["status"] == "idle" and restored["pid"] is None
    assert restored["active_configuration_id"] == configuration_id
    assert restored["actions"] == data["actions"]
    assert "inspector-receive-secret" not in cold.text

    retried = await client.post("/api/v1/debug/node/start", json=payload)
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["status"] == "running"
    assert retried.json()["data"]["active_configuration_id"] == configuration_id
    stopped = await client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["data"]["status"] == "exited"
    assert [action for action in stopped.json()["data"]["actions"] if action["action"] == "start_failed"] == failures
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    (context.artifacts_dir / "receive-errors.json").write_text(json.dumps({
        "failed": failed.json(), "live": state.json(), "manifest": manifest.model_dump(mode="json"),
        "cold": cold.json(), "retried": retried.json(), "stopped": stopped.json(),
    }, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
@pytest.mark.skipif(process_identity.sys.platform != "linux", reason="真实 /proc 读取边界只在 Linux 验证")
@pytest.mark.parametrize(("failure", "sink_fails"), [
    ("stat_format", False), ("stat_fields", False), ("empty_boot_id", False),
    ("stat_permission", False), ("missing_boot_id", False), ("boot_id_permission", False),
    ("stat_permission", True), ("missing_boot_id", True), ("boot_id_permission", True),
])
async def test_process_identity_rejection_keeps_failed_start_private_and_retries(
    debug_receive_client: DebugReceiveClient, failure: str, sink_fails: bool,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    client, store, owner, root, reopen = debug_receive_client
    original_read = Path.read_text
    observed: list[Path] = []
    private = str(root / "identity-secret")
    native_error = OSError(errno.ENOENT if failure == "missing_boot_id" else errno.EACCES, "身份读取失败", private)
    sink_error = OSError("日志输出失败 /private/launch-log-secret")
    original_call_handlers = logging.Logger.callHandlers
    sink_errors: list[BaseException] = []

    def call_handlers(logger: logging.Logger, record: logging.LogRecord) -> None:
        original_call_handlers(logger, record)
        if sink_fails and record.getMessage().startswith("启动 Node Inspector 失败:"):
            assert record.exc_info is not None and record.exc_info[1] is not None
            sink_errors.append(record.exc_info[1])
            raise sink_error

    def read_identity(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        raw = original_read(path, encoding=encoding, errors=errors)
        if observed:
            return raw
        if failure.startswith("stat_") and path.name == "stat" and path.parent.parent == Path("/proc"):
            observed.append(path)
            if failure == "stat_permission":
                raise native_error
            return private if failure == "stat_format" else f"{path.parent.name} ({private}) S 0"
        if failure in {"empty_boot_id", "missing_boot_id", "boot_id_permission"} and path == Path("/proc/sys/kernel/random/boot_id"):
            observed.append(path)
            if failure != "empty_boot_id":
                raise native_error
            return "\n"
        return raw

    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "身份读取拒绝后重试", "script_path": "entry.mjs",
    })
    assert configured.status_code == 200, configured.text
    configuration_id = configured.json()["data"]["active_configuration_id"]
    payload = {**owner, "configuration_id": configuration_id, "path": "entry.mjs"}
    process_identity._read_linux_boot_id.cache_clear()
    try:
        with monkeypatch.context() as patch, caplog.at_level(logging.ERROR, logger=process_identity.__name__):
            patch.setattr(Path, "read_text", read_identity)
            patch.setattr(logging.Logger, "callHandlers", call_handlers)
            failed = await client.post("/api/v1/debug/node/start", json=payload)
    finally:
        process_identity._read_linux_boot_id.cache_clear()
    assert failed.status_code == 409, failed.text
    assert len(observed) == 1
    assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
    assert str(observed[0]) not in failed.text and "identity-secret" not in failed.text
    if failure in {"stat_permission", "missing_boot_id", "boot_id_permission"}:
        assert any(record.exc_info and record.exc_info[1] is native_error for record in caplog.records)
        assert str(native_error) in caplog.text
        assert f"error={type(native_error).__name__}" in failed.text and f"errno={native_error.errno}" in failed.text
    else:
        assert "source=linux_proc_stat" in failed.text
        assert str(observed[0]) in caplog.text
    if failure in {"stat_format", "stat_fields"}:
        assert private in caplog.text
    if sink_fails:
        assert len(sink_errors) == 1
        assert sink_errors[0].__cause__ is native_error
        assert any(str(sink_error) in note for note in sink_errors[0].__notes__)
        assert "diagnostic_sink_error=OSError" in failed.text
    else:
        assert not sink_errors and "diagnostic_sink_error" not in failed.text
    assert "launch-log-secret" not in failed.text

    state = await client.get("/api/v1/debug/node", params=owner)
    assert state.status_code == 200, state.text
    data = state.json()["data"]
    assert data["status"] == "failed" and isinstance(data["pid"], int)
    assert probe_process_identity(data["pid"]) is None
    assert data["error_message"] == failed.json()["detail"]
    assert not any(action["action"] == "start" for action in data["actions"])
    failures = [action for action in data["actions"] if action["action"] == "start_failed"]
    assert len(failures) == 1 and failures[0]["message"] == data["error_message"]
    manifest_bytes, manifest = store.read_manifest_payload(**owner)
    assert manifest_bytes is not None and manifest is not None
    assert [action.model_dump(mode="json") for action in manifest.actions] == data["actions"]
    for text in (state.text, manifest_bytes.decode()):
        assert str(observed[0]) not in text and "identity-secret" not in text
        assert "launch-log-secret" not in text
    claim = store.read_launch_claim(**owner)
    assert claim is not None and claim.phase == "settled" and claim.pid is None

    await reopen(True)
    cold = await client.get("/api/v1/debug/node", params=owner)
    assert cold.status_code == 200, cold.text
    restored = cold.json()["data"]
    assert restored["status"] == "idle" and restored["pid"] is None
    assert restored["actions"] == data["actions"] and restored["active_configuration_id"] == configuration_id
    assert str(observed[0]) not in cold.text and "identity-secret" not in cold.text
    assert "launch-log-secret" not in cold.text

    retried = await client.post("/api/v1/debug/node/start", json=payload)
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["status"] == "running"
    assert retried.json()["data"]["active_configuration_id"] == configuration_id
    stopped = await client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["data"]["status"] == "exited"
    assert [action for action in stopped.json()["data"]["actions"] if action["action"] == "start_failed"] == failures
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    log_mode = "log-failed" if sink_fails else "log-ok"
    (context.artifacts_dir / f"identity-{failure}-{log_mode}.json").write_text(json.dumps({
        "failed": failed.json(), "live": state.json(), "manifest": manifest.model_dump(mode="json"),
        "cold": cold.json(), "retried": retried.json(), "stopped": stopped.json(),
    }, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
@pytest.mark.skipif(process_identity.sys.platform != "linux", reason="真实 /proc 读取边界只在 Linux 验证")
async def test_cold_identity_io_failure_preserves_product_claim_and_retries(
    debug_receive_client: DebugReceiveClient,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    client, store, owner, root, reopen = debug_receive_client
    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "冷身份读取失败后重试", "script_path": "entry.mjs",
    })
    assert configured.status_code == 200, configured.text
    configuration_id = configured.json()["data"]["active_configuration_id"]
    started = await client.post("/api/v1/debug/node/start", json={
        **owner, "configuration_id": configuration_id, "path": "entry.mjs",
    })
    assert started.status_code == 200, started.text
    pid = started.json()["data"]["pid"]
    claim = store.read_launch_claim(**owner)
    manifest_bytes, _manifest = store.read_manifest_payload(**owner)
    assert claim is not None and claim.phase == "running" and claim.pid == pid
    # 新服务实例只读产品入口生成的登记；原 owner 保留真实进程与清理责任，未模拟硬重启。
    await reopen(False)
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    native_error = FileNotFoundError(errno.ENOENT, "身份读取失败", str(root / "identity-secret/boot_id"))
    read_text = Path.read_text
    observed: list[Path] = []

    def read_identity(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == boot_id_path:
            observed.append(path)
            raise native_error
        return read_text(path, encoding=encoding, errors=errors)

    process_identity._read_linux_boot_id.cache_clear()
    try:
        with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
            patch.setattr(Path, "read_text", read_identity)
            failed = await client.get("/api/v1/debug/node", params=owner)
    finally:
        process_identity._read_linux_boot_id.cache_clear()
    assert failed.status_code == 404, failed.text
    assert observed == [boot_id_path]
    assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
    assert "identity-secret" not in failed.text and str(boot_id_path) not in failed.text
    assert "stage=boot_id_read" in failed.text and "FileNotFoundError" in failed.text and "errno=2" in failed.text
    assert f"source={process_identity.IDENTITY_SOURCE_LINUX_PROC}" in failed.text
    assert any(record.exc_info and record.exc_info[1] is native_error for record in caplog.records)
    assert str(native_error) in caplog.text
    assert store.read_launch_claim(**owner) == claim
    assert store.read_manifest_payload(**owner)[0] == manifest_bytes
    identity = probe_process_identity(pid)
    assert identity is not None
    assert identity.compare(recorded_source=claim.process_identity_source, recorded_start_marker=claim.process_start_marker) == "match"

    retried = await client.get("/api/v1/debug/node", params=owner)
    assert retried.status_code == 200, retried.text
    state = retried.json()["data"]
    assert state["status"] == "idle" and state["pid"] is None
    assert state["active_configuration_id"] == configuration_id
    assert state["actions"][:-1] == started.json()["data"]["actions"]
    assert probe_process_identity(pid) is None
    settled = store.read_launch_claim(**owner)
    assert settled is not None and settled.phase == "settled" and settled.process_instance_id == claim.process_instance_id
    assert len([action for action in state["actions"] if action["action"] == "reconcile_settled"]) == 1
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    (context.artifacts_dir / "cold-identity-io.json").write_text(json.dumps({
        "started": started.json(), "failed": failed.json(), "retried": retried.json(),
        "claim": claim.model_dump(mode="json"), "settled": settled.model_dump(mode="json"),
    }, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
async def test_variable_receive_failure_is_private_and_next_product_pause_recovers(
    debug_receive_client: DebugReceiveClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, store, owner, root, reopen = debug_receive_client
    native_error = PermissionError(
        errno.EACCES, "变量命令发送故障", str(root / "variables-secret")
    )
    original_connect = websockets.connect
    armed = False
    failed_sends: list[dict[str, object]] = []

    class VariableSocket:
        def __init__(self, socket: ClientConnection) -> None:
            self.socket = socket

        async def send(self, message: str) -> None:
            payload = json.loads(message)
            if (
                armed
                and not failed_sends
                and payload.get("method") == "Runtime.getProperties"
            ):
                failed_sends.append(payload)
                raise native_error
            await self.socket.send(message)

        async def close(self) -> None:
            await self.socket.close()

        def __aiter__(self) -> AsyncIterator[str | bytes]:
            return self.socket.__aiter__()

    async def connect(uri: str, *, ping_interval: float | None) -> VariableSocket:
        return VariableSocket(await original_connect(uri, ping_interval=ping_interval))

    configured = await client.post(
        "/api/v1/debug/node/configurations",
        json={
            **owner,
            "name": "变量读取失败后原进程恢复",
            "script_path": "entry.mjs",
        },
    )
    assert configured.status_code == 200, configured.text
    configuration_id = configured.json()["data"]["active_configuration_id"]
    with monkeypatch.context() as patch:
        patch.setattr(websockets, "connect", connect)
        started = await client.post(
            "/api/v1/debug/node/start",
            json={
                **owner,
                "configuration_id": configuration_id,
                "path": "entry.mjs",
            },
        )
        assert started.status_code == 200, started.text
        assert started.json()["data"]["status"] == "running"
        pid = started.json()["data"]["pid"]
        armed = True
        paused = await client.post(
            "/api/v1/debug/node/action", json={**owner, "action": "pause"}
        )
        assert paused.status_code == 200, paused.text
        assert len(failed_sends) == 1
        state = paused.json()["data"]
        assert state["status"] == "paused" and state["pid"] == pid
        expected = (
            f"读取局部变量失败: session_id={owner['session_id']}, thread_id={owner['thread_id']}: "
            "stage=node-debug-variables, error=PermissionError, errno=13"
        )
        assert state["error_message"] == expected
        assert state["call_stack"] and state["call_stack"][0]["variables"] == []
        assert paused.json()["request_id"] == paused.headers["X-Request-ID"]
        assert any(
            record.exc_info and record.exc_info[1] is native_error
            for record in caplog.records
        )
        assert str(native_error) in caplog.text
        live = await client.get("/api/v1/debug/node", params=owner)
        assert live.status_code == 200, live.text
        assert live.json()["data"]["error_message"] == expected
        manifest_bytes, manifest = store.read_manifest_payload(**owner)
        assert manifest_bytes is not None and manifest is not None
        assert manifest.actions[-1].action == "pause"
        for text in (paused.text, live.text, manifest_bytes.decode()):
            assert "variables-secret" not in text

        continued = await client.post(
            "/api/v1/debug/node/action", json={**owner, "action": "continue"}
        )
        assert continued.status_code == 200, continued.text
        assert continued.json()["data"]["status"] == "running"
        retried = await client.post(
            "/api/v1/debug/node/action", json={**owner, "action": "pause"}
        )
        assert retried.status_code == 200, retried.text
        restored = retried.json()["data"]
        assert restored["pid"] == pid and restored["status"] == "paused"
        assert restored["error_message"] is None
        assert restored["call_stack"] and restored["call_stack"][0]["variables"]
        assert len(failed_sends) == 1
        stopped = await client.post(
            "/api/v1/debug/node/action", json={**owner, "action": "stop"}
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["data"]["status"] == "exited"
        assert probe_process_identity(pid) is None
    await reopen(True)
    cold = await client.get("/api/v1/debug/node", params=owner)
    assert cold.status_code == 200, cold.text
    assert cold.json()["data"]["active_configuration_id"] == configuration_id
    assert cold.json()["data"]["actions"] == stopped.json()["data"]["actions"]
    assert "variables-secret" not in cold.text
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    (context.artifacts_dir / "variable-read-errors.json").write_text(
        json.dumps(
            {
                "started": started.json(),
                "paused": paused.json(),
                "live": live.json(),
                "manifest": manifest.model_dump(mode="json"),
                "continued": continued.json(),
                "retried": retried.json(),
                "stopped": stopped.json(),
                "cold": cold.json(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
