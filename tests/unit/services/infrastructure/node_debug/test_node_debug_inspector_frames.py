"""Inspector 真实帧解析保留源码目标，输出规范资源引用。"""

import asyncio
import json
import threading
from pathlib import Path

import pytest

from app.core.exceptions import ForbiddenError
from app.core.resource_uri import ParsedResourceUri, resource_display_uri
from app.services.infrastructure.node_debug.process.inspector import NodeDebugInspector
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from tests.support.node_debug_dependencies import TEST_THREAD_ID, node_debug_file_paths


class DirectoryBindings:
    def __init__(self) -> None:
        self.roots: list[Path] = []
        self.uri = resource_display_uri(
            scope="gateway", scope_id="gateway_" + "1" * 32,
            kind="files", tail_segments=("00000000000070008000000000000002",),
        )

    def bind(self, root: Path) -> str:
        self.roots.append(root)
        return self.uri

    def resolve(self, parsed: ParsedResourceUri) -> Path:
        assert parsed.scope_id == "gateway_" + "1" * 32
        return self.roots[0]


@pytest.fixture
def bindings() -> DirectoryBindings:
    return DirectoryBindings()


@pytest.fixture
def file_paths(tmp_path: Path, bindings: DirectoryBindings) -> WorkspaceFilePaths:
    root = tmp_path / "workspace"
    root.mkdir()
    owner = node_debug_file_paths(root)
    owner.bindings = bindings
    return owner


@pytest.fixture
def inspector(file_paths: WorkspaceFilePaths) -> NodeDebugInspector:
    return NodeDebugInspector(
        workspace_root=file_paths.workspace,
        file_paths=file_paths,
        append_action=lambda *args, **kwargs: None,
        clear_stop_snapshot=lambda runtime: None,
    )


@pytest.fixture
def runtime(file_paths: WorkspaceFilePaths) -> NodeDebugRuntime:
    return NodeDebugRuntime(
        session_id="ses_019bf141964978208ce9100774167f90",
        thread_id=TEST_THREAD_ID,
        configuration_id="dbgcfg_" + "1" * 32,
        workspace_root=file_paths.workspace,
        script_path=file_paths.workspace / "entry.mjs",
        relative_script_path="entry.mjs",
    )


def frames(url: str) -> list[dict[str, object]]:
    return [{
        "callFrameId": "frame-1", "functionName": "f", "url": url,
        "location": {"scriptId": "script-1", "lineNumber": 4, "columnNumber": 2},
        "scopeChain": [{"type": "local", "name": "local"}],
    }]


@pytest.mark.parametrize("from_script_cache", [False, True])
@pytest.mark.parametrize("source_form", ["native", "file-uri"])
def test_workspace_source_uses_uri_and_keeps_breakpoint_location(
    inspector: NodeDebugInspector, runtime: NodeDebugRuntime,
    file_paths: WorkspaceFilePaths, from_script_cache: bool, source_form: str,
) -> None:
    source = file_paths.workspace / "中文 100%.mjs"
    source.write_text("debugger;\n", encoding="utf-8")
    url = str(source) if source_form == "native" else source.as_uri()
    # 模块选择 query 不改变 file URL 源码；原生路径中的百分号保持原义。
    runtime.inspector.script_urls["script-1"] = url + ("?module=1" if source_form == "file-uri" else "")
    frame = inspector._parse_call_frames(
        runtime, frames("" if from_script_cache else url),
    )[0]
    assert frame.url == file_paths.reference(source)
    assert frame.path == source.name
    assert (frame.line, frame.column, frame.scope_names) == (5, 3, ["local"])
    assert str(file_paths.workspace) not in frame.model_dump_json()
    assert "file:" not in frame.model_dump_json()
    assert file_paths.native(frame.url) == source


@pytest.mark.parametrize("source_form", ["native", "file-uri"])
def test_external_source_uses_target_gateway_binding(
    inspector: NodeDebugInspector, runtime: NodeDebugRuntime,
    file_paths: WorkspaceFilePaths, bindings: DirectoryBindings, tmp_path: Path,
    source_form: str,
) -> None:
    source = tmp_path / "外部 源码.mjs"
    source.write_text("debugger;\n", encoding="utf-8")
    url = str(source) if source_form == "native" else source.as_uri()
    frame = inspector._parse_call_frames(runtime, frames(url))[0]
    assert frame.path is None
    assert frame.url.startswith(bindings.uri + "/")
    assert bindings.roots == [source.parent]
    assert file_paths.native(frame.url) == source
    assert str(source) not in frame.model_dump_json()
    assert "file:" not in frame.model_dump_json()


@pytest.mark.parametrize("source_form", ["native", "file-uri"])
def test_external_source_without_binding_fails_explicitly(
    inspector: NodeDebugInspector, runtime: NodeDebugRuntime,
    file_paths: WorkspaceFilePaths, tmp_path: Path, source_form: str,
) -> None:
    file_paths.bindings = None
    source = tmp_path / "external.mjs"
    url = str(source) if source_form == "native" else source.as_uri()
    with pytest.raises(RuntimeError, match="尚未装配"):
        inspector._parse_call_frames(runtime, frames(url))


@pytest.mark.parametrize("source_form", ["native", "file-uri"])
def test_private_source_cannot_be_published(
    inspector: NodeDebugInspector, runtime: NodeDebugRuntime, file_paths: WorkspaceFilePaths,
    source_form: str,
) -> None:
    source = file_paths.workspace / ".boxteam/private.mjs"
    url = str(source) if source_form == "native" else source.as_uri()
    with pytest.raises(ForbiddenError, match="私有存储"):
        inspector._parse_call_frames(runtime, frames(url))


def test_builtin_and_anonymous_sources_keep_inspector_labels(
    inspector: NodeDebugInspector, runtime: NodeDebugRuntime,
) -> None:
    for url in ["node:internal/modules/run_main", "evalmachine.<anonymous>", "https://example.test/source.js", ""]:
        frame = inspector._parse_call_frames(runtime, frames(url))[0]
        assert frame.url == url
        assert frame.path is None


@pytest.mark.asyncio
async def test_paused_event_does_not_block_loop_during_gateway_binding(
    inspector: NodeDebugInspector, runtime: NodeDebugRuntime,
    bindings: DirectoryBindings, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    released = threading.Event()
    bind = bindings.bind

    def slow_bind(root: Path) -> str:
        entered.set()
        if not released.wait(timeout=2):
            raise RuntimeError("测试 Gateway 绑定未被事件循环释放")
        return bind(root)

    monkeypatch.setattr(bindings, "bind", slow_bind)
    event = asyncio.create_task(inspector._handle_event(runtime, "Debugger.paused", {
        "callFrames": frames((tmp_path / "external.mjs").as_uri()),
    }))
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        assert not event.done()
    finally:
        released.set()
        await event
        if runtime.inspector.variable_hydration_task is not None:
            await runtime.inspector.variable_hydration_task
    assert runtime.status == "paused"
    assert runtime.call_stack[0].url.startswith(bindings.uri + "/")


class HydrationSocket:
    def __init__(self, runtime: NodeDebugRuntime) -> None:
        self.runtime = runtime
        self.error: BaseException | None = None
        self.protocol_error: str | None = None
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.methods: list[str] = []

    async def send(self, message: str) -> None:
        payload = json.loads(message)
        self.methods.append(payload["method"])
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        if self.protocol_error is not None:
            self.runtime.inspector.pending_commands[payload["id"]].set_result(
                {"error": {"code": -32000, "message": self.protocol_error}}
            )
            return
        self.runtime.inspector.pending_commands[payload["id"]].set_result(
            {
                "result": {
                    "result": [
                        {"name": "local", "value": {"type": "number", "value": 42}}
                    ]
                },
            }
        )


@pytest.fixture
def hydration_socket(runtime: NodeDebugRuntime) -> HydrationSocket:
    socket = HydrationSocket(runtime)
    runtime.inspector.socket = socket  # type: ignore[assignment]
    return socket


@pytest.fixture
def paused_event(runtime: NodeDebugRuntime) -> dict[str, object]:
    call_frames = frames(runtime.script_path.as_uri())
    call_frames[0]["scopeChain"] = [
        {"type": "local", "object": {"objectId": "object-1"}}
    ]
    return {"callFrames": call_frames, "reason": "other"}


@pytest.fixture(
    params=[
        "/private/variables-secret",
        r"C:\private\variables-secret",
        r"\\server\private\variables-secret",
    ],
    ids=["posix", "drive", "unc"],
)
def hydration_error(request: pytest.FixtureRequest) -> PermissionError:
    return PermissionError(13, "变量读取故障" + "完整诊断" * 80, request.param)


@pytest.mark.parametrize("sink_fails", [False, True])
@pytest.mark.asyncio
async def test_variable_failure_keeps_facts_and_next_pause_recovers(
    inspector: NodeDebugInspector,
    runtime: NodeDebugRuntime,
    hydration_socket: HydrationSocket,
    paused_event: dict[str, object],
    hydration_error: PermissionError,
    sink_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hydration_socket.error = hydration_error
    runtime.logpoint_error_message = "已有的日志点说明"
    if sink_fails:
        from app.core import failures

        def fail_sink(*args: object, **kwargs: object) -> None:
            raise OSError("日志 sink 故障 /private/variables-log")

        monkeypatch.setattr(failures.logger, "error", fail_sink)
    await inspector._handle_event(runtime, "Debugger.paused", paused_event)
    task = runtime.inspector.variable_hydration_task
    assert task is not None
    await asyncio.wait_for(task, timeout=2)
    expected = (
        f"读取局部变量失败: session_id={runtime.session_id}, thread_id={runtime.thread_id}: "
        "stage=node-debug-variables, error=PermissionError, errno=13"
    )
    assert runtime.error_message == expected + (
        ", diagnostic_sink_error=OSError" if sink_fails else ""
    )
    assert (
        runtime.status == "paused" and runtime.call_stack[0].call_frame_id == "frame-1"
    )
    assert runtime.call_stack[0].variables == []
    assert hydration_socket.methods == ["Runtime.getProperties"]
    assert runtime.inspector.pending_commands == {}
    if sink_fails:
        assert any(
            "/private/variables-log" in note for note in hydration_error.__notes__
        )
    else:
        assert any(
            record.exc_info and record.exc_info[1] is hydration_error
            for record in caplog.records
        )
        assert str(hydration_error) in caplog.text
    await inspector._handle_event(runtime, "Debugger.paused", paused_event)
    task = runtime.inspector.variable_hydration_task
    assert task is not None
    await asyncio.wait_for(task, timeout=2)
    assert [
        (item.name, item.value, item.scope) for item in runtime.call_stack[0].variables
    ] == [("local", "42", "local")]
    assert runtime.error_message == "已有的日志点说明"
    assert hydration_socket.methods == [
        "Runtime.getProperties",
        "Runtime.getProperties",
    ]
    assert runtime.inspector.pending_commands == {}


@pytest.mark.parametrize("changed", ["running", "frame"])
@pytest.mark.asyncio
async def test_late_variable_failure_keeps_current_state_and_diagnostic(
    inspector: NodeDebugInspector,
    runtime: NodeDebugRuntime,
    hydration_socket: HydrationSocket,
    paused_event: dict[str, object],
    changed: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original = RuntimeError("失效的变量响应 /private/late-variables-secret")
    hydration_socket.error = original
    hydration_socket.release = asyncio.Event()
    await inspector._handle_event(runtime, "Debugger.paused", paused_event)
    task = runtime.inspector.variable_hydration_task
    assert task is not None
    try:
        await asyncio.wait_for(hydration_socket.entered.wait(), timeout=2)
        runtime.error_message = "当前暂停的错误"
        if changed == "running":
            runtime.status = "running"
        else:
            runtime.call_stack[0] = runtime.call_stack[0].model_copy(
                update={"call_frame_id": "frame-2"}
            )
        hydration_socket.release.set()
        await asyncio.wait_for(task, timeout=2)
        assert runtime.error_message == "当前暂停的错误"
        assert runtime.status == ("running" if changed == "running" else "paused")
        assert runtime.call_stack[0].call_frame_id == (
            "frame-1" if changed == "running" else "frame-2"
        )
        assert any(
            record.exc_info and record.exc_info[1] is original
            for record in caplog.records
        )
        assert str(original) in caplog.text
        assert runtime.inspector.pending_commands == {}
    finally:
        hydration_socket.release.set()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.parametrize(
    "message",
    ["Cannot find context with specified id", "Could not find object with given id"],
)
@pytest.mark.asyncio
async def test_expired_variable_context_preserves_existing_semantics(
    inspector: NodeDebugInspector,
    runtime: NodeDebugRuntime,
    hydration_socket: HydrationSocket,
    paused_event: dict[str, object],
    message: str,
) -> None:
    hydration_socket.protocol_error = message
    runtime.logpoint_error_message = "已有的日志点说明"
    await inspector._handle_event(runtime, "Debugger.paused", paused_event)
    task = runtime.inspector.variable_hydration_task
    assert task is not None
    await asyncio.wait_for(task, timeout=2)
    assert runtime.error_message == (
        "已有的日志点说明"
        if message.startswith("Cannot")
        else "读取局部变量失败：暂停期间 Inspector 变量对象已经失效"
    )
    assert runtime.status == "paused" and runtime.call_stack[0].variables == []
    assert runtime.inspector.pending_commands == {}


@pytest.mark.asyncio
async def test_variable_fetch_preserves_original_cancellation(
    inspector: NodeDebugInspector,
    runtime: NodeDebugRuntime,
    hydration_socket: HydrationSocket,
    paused_event: dict[str, object],
) -> None:
    cancellation = asyncio.CancelledError("取消变量读取")
    hydration_socket.error = cancellation
    runtime.logpoint_error_message = "已有的日志点说明"
    await inspector._handle_event(runtime, "Debugger.paused", paused_event)
    task = runtime.inspector.variable_hydration_task
    assert task is not None
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, timeout=2)
    assert caught.value is cancellation
    assert runtime.error_message == "已有的日志点说明"
    assert runtime.inspector.pending_commands == {}
