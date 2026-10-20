"""Inspector 真实帧解析保留源码目标，输出规范资源引用。"""

import asyncio
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
def test_workspace_source_uses_uri_and_keeps_breakpoint_location(
    inspector: NodeDebugInspector, runtime: NodeDebugRuntime,
    file_paths: WorkspaceFilePaths, from_script_cache: bool,
) -> None:
    source = file_paths.workspace / "中文 100%.mjs"
    source.write_text("debugger;\n", encoding="utf-8")
    # 模块选择 query 不改变源码文件；URL 文件段只解码一次。
    runtime.inspector.script_urls["script-1"] = source.as_uri() + "?module=1"
    frame = inspector._parse_call_frames(
        runtime, frames("" if from_script_cache else source.as_uri()),
    )[0]
    assert frame.url == file_paths.reference(source)
    assert frame.path == source.name
    assert (frame.line, frame.column, frame.scope_names) == (5, 3, ["local"])
    assert str(file_paths.workspace) not in frame.model_dump_json()
    assert "file:" not in frame.model_dump_json()
    assert file_paths.native(frame.url) == source


def test_external_source_uses_target_gateway_binding(
    inspector: NodeDebugInspector, runtime: NodeDebugRuntime,
    file_paths: WorkspaceFilePaths, bindings: DirectoryBindings, tmp_path: Path,
) -> None:
    source = tmp_path / "外部 源码.mjs"
    source.write_text("debugger;\n", encoding="utf-8")
    frame = inspector._parse_call_frames(runtime, frames(source.as_uri()))[0]
    assert frame.path is None
    assert frame.url.startswith(bindings.uri + "/")
    assert bindings.roots == [source.parent]
    assert file_paths.native(frame.url) == source
    assert str(source) not in frame.model_dump_json()
    assert "file:" not in frame.model_dump_json()


def test_external_source_without_binding_fails_explicitly(
    inspector: NodeDebugInspector, runtime: NodeDebugRuntime,
    file_paths: WorkspaceFilePaths, tmp_path: Path,
) -> None:
    file_paths.bindings = None
    with pytest.raises(RuntimeError, match="尚未装配"):
        inspector._parse_call_frames(runtime, frames((tmp_path / "external.mjs").as_uri()))


def test_private_source_cannot_be_published(
    inspector: NodeDebugInspector, runtime: NodeDebugRuntime, file_paths: WorkspaceFilePaths,
) -> None:
    with pytest.raises(ForbiddenError, match="私有存储"):
        inspector._parse_call_frames(runtime, frames((file_paths.workspace / ".boxteam/private.mjs").as_uri()))


def test_builtin_and_anonymous_sources_keep_inspector_labels(
    inspector: NodeDebugInspector, runtime: NodeDebugRuntime,
) -> None:
    for url in ["node:internal/modules/run_main", ""]:
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
