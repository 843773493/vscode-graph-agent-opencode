"""POST /workspace/files/events SSE 适配层单元测试。

沿用 tests/unit/api 既有规范：直接调用公开处理函数隔离依赖，
不启动真实 Workspace 后端进程。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.api import workspace as workspace_api
from app.api.workspace import stream_workspace_file_events
from app.schemas.internal_v2.workspace import WorkspaceFileWatchRequest
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileChange,
    WorkspaceFileChangeBatch,
)
from app.services.infrastructure.workspace_service import WorkspaceService
from tests.support.workspaces import prepare_default_test_workspace


class _Source:
    """先产出固定批次、随后阻塞的订阅源，并在关闭时记录状态。"""

    def __init__(self, batches: list[WorkspaceFileChangeBatch]) -> None:
        self._batches = list(batches)
        self.closed = False

    def __aiter__(self) -> _Source:
        return self

    async def __anext__(self) -> WorkspaceFileChangeBatch:
        if self._batches:
            return self._batches.pop(0)
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


class _WatchService:
    """只实现端点需要的根解析与订阅入口。"""

    def __init__(self, source: _Source, root: Path) -> None:
        self._source = source
        self._root = root

    def resolve_watch_roots(self, extra_paths: list[str]) -> tuple[Path, ...]:
        return (self._root,)

    def subscribe_roots(
        self,
        roots: tuple[Path, ...],
        *,
        include_internal_paths: bool = False,
    ) -> _Source:
        return self._source


async def _open_stream(
    monkeypatch: pytest.MonkeyPatch,
    source: _Source,
    workspace_service: WorkspaceService,
) -> tuple[object, _Source]:
    # 端点内联使用模块级常量，这里缩短间隔以观测空闲窗口。
    monkeypatch.setattr(workspace_api, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    response = await stream_workspace_file_events(
        WorkspaceFileWatchRequest(paths=[]),
        "local-dev-token",
        watch_service=_WatchService(source, Path(workspace_service.root_path)),
        workspace_service=workspace_service,
    )
    return response.body_iterator, source


@pytest.mark.asyncio
async def test_workspace_file_sse_uses_shared_no_cache_headers(
    monkeypatch: pytest.MonkeyPatch,
    workspace_service: WorkspaceService,
) -> None:
    """文件流与其它 SSE 端点必须发出同一份反缓存响应头。"""
    monkeypatch.setattr(workspace_api, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    response = await stream_workspace_file_events(
        WorkspaceFileWatchRequest(paths=[]),
        "local-dev-token",
        watch_service=_WatchService(_Source([]), Path(workspace_service.root_path)),
        workspace_service=workspace_service,
    )

    try:
        assert response.headers["Cache-Control"] == "no-cache"
        assert response.headers["X-Accel-Buffering"] == "no"
        assert response.media_type == "text/event-stream"
    finally:
        await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_workspace_file_sse_emits_heartbeat_while_idle(
    monkeypatch: pytest.MonkeyPatch,
    workspace_service: WorkspaceService,
) -> None:
    """无文件变更时在心跳间隔后收到 SSE 注释心跳。"""
    stream, source = await _open_stream(monkeypatch, _Source([]), workspace_service)

    frame = await asyncio.wait_for(anext(stream), timeout=0.5)

    assert frame == ": heartbeat\n\n"
    # 心跳必须是 SSE 注释，不得伪装成业务事件。
    assert "event:" not in frame
    assert "data:" not in frame
    await stream.aclose()
    assert source.closed is True


@pytest.mark.asyncio
async def test_workspace_file_sse_emits_changes_frame(
    monkeypatch: pytest.MonkeyPatch,
    workspace_service: WorkspaceService,
) -> None:
    """有变更时输出完整 changes 事件帧，且不掺入心跳。"""
    source = _Source(
        [
            WorkspaceFileChangeBatch(
                changes=(WorkspaceFileChange(kind="create", path=str(Path(workspace_service.root_path) / "a.txt")),),
            )
        ]
    )
    stream, _ = await _open_stream(monkeypatch, source, workspace_service)

    frame = await asyncio.wait_for(anext(stream), timeout=0.5)

    assert frame.startswith("event: changes\ndata: ")
    assert '"kind":"create"' in frame
    assert frame.endswith("\n\n")
    assert ": heartbeat" not in frame
    await stream.aclose()


@pytest.mark.asyncio
async def test_workspace_file_sse_error_frame_closes_source(
    monkeypatch: pytest.MonkeyPatch,
    workspace_service: WorkspaceService,
) -> None:
    """监听错误产出 error 帧后收尾，并额外关闭源订阅。"""
    source = _Source([WorkspaceFileChangeBatch(error="监听失败")])
    stream, _ = await _open_stream(monkeypatch, source, workspace_service)

    frame = await asyncio.wait_for(anext(stream), timeout=0.5)

    assert frame.startswith("event: error\ndata: ")
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=0.5)
    # workspace 流与 trace/message 流的差异：收尾必须主动 aclose 源迭代器。
    assert source.closed is True


@pytest.mark.asyncio
async def test_workspace_file_sse_closes_source_on_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
    workspace_service: WorkspaceService,
) -> None:
    """源耗尽时停止并关闭源订阅，不泄漏任务。"""

    class _Exhausted(_Source):
        async def __anext__(self) -> WorkspaceFileChangeBatch:
            raise StopAsyncIteration

    source = _Exhausted([])
    stream, _ = await _open_stream(monkeypatch, source, workspace_service)

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=0.5)
    assert source.closed is True


@pytest.fixture
async def workspace_service(tmp_path: Path):
    root = prepare_default_test_workspace(
        workspace_root=tmp_path / "workspace",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    config = Mock(spec=ConfigService)
    config.get_workspace_file_default_limit.return_value = 500
    service = WorkspaceService(config_service=config, workspace_root=root)
    await service.start()
    yield service
    await service.stop()
    service.close()


@pytest.mark.asyncio
async def test_real_watch_failure_keeps_native_path_in_log_only(
    monkeypatch: pytest.MonkeyPatch, workspace_service: WorkspaceService, caplog: pytest.LogCaptureFixture,
) -> None:
    import app.services.infrastructure.workspace_file_watch_service as watch_module

    root = Path(workspace_service.root_path)
    async def failing_watch(*_roots, **_options):
        yield set()
        raise PermissionError(13, "Permission denied", str(root / "private.txt"))

    monkeypatch.setattr(watch_module, "awatch", failing_watch)
    watcher = watch_module.WorkspaceFileWatchService(workspace_root=root)
    response = await stream_workspace_file_events(
        WorkspaceFileWatchRequest(paths=[]), "local-dev-token",
        watch_service=watcher, workspace_service=workspace_service,
    )
    try:
        frame = await asyncio.wait_for(anext(response.body_iterator), timeout=3)
        assert frame.startswith("event: error\ndata: ")
        assert "PermissionError" in frame and "errno=13" in frame
        assert str(root) not in frame
        assert str(root / "private.txt") in caplog.text
    finally:
        await response.body_iterator.aclose()
        await watcher.shutdown()
