"""POST /workspace/files/events SSE 适配层单元测试。

沿用 tests/unit/api 既有规范：直接调用公开处理函数隔离依赖，
不启动真实 Workspace 后端进程。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.api import workspace as workspace_api
from app.api.workspace import stream_workspace_file_events
from app.schemas.internal_v2.workspace import WorkspaceFileWatchRequest
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileChange,
    WorkspaceFileChangeBatch,
)


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

    def __init__(self, source: _Source) -> None:
        self._source = source

    def resolve_watch_roots(self, extra_paths: list[str]) -> tuple[Path, ...]:
        return (Path("/tmp/boxteam-sse-test"),)

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
) -> tuple[object, _Source]:
    # 端点内联使用模块级常量，这里缩短间隔以观测空闲窗口。
    monkeypatch.setattr(workspace_api, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    response = await stream_workspace_file_events(
        WorkspaceFileWatchRequest(paths=[]),
        "local-dev-token",
        watch_service=_WatchService(source),
    )
    return response.body_iterator, source


@pytest.mark.asyncio
async def test_workspace_file_sse_emits_heartbeat_while_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无文件变更时在心跳间隔后收到 SSE 注释心跳。"""
    stream, source = await _open_stream(monkeypatch, _Source([]))

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
) -> None:
    """有变更时输出完整 changes 事件帧，且不掺入心跳。"""
    source = _Source(
        [
            WorkspaceFileChangeBatch(
                changes=(WorkspaceFileChange(kind="create", path="/tmp/a.txt"),),
            )
        ]
    )
    stream, _ = await _open_stream(monkeypatch, source)

    frame = await asyncio.wait_for(anext(stream), timeout=0.5)

    assert frame.startswith("event: changes\ndata: ")
    assert '"kind":"create"' in frame
    assert frame.endswith("\n\n")
    assert ": heartbeat" not in frame
    await stream.aclose()


@pytest.mark.asyncio
async def test_workspace_file_sse_error_frame_closes_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """监听错误产出 error 帧后收尾，并额外关闭源订阅。"""
    source = _Source([WorkspaceFileChangeBatch(error="监听失败")])
    stream, _ = await _open_stream(monkeypatch, source)

    frame = await asyncio.wait_for(anext(stream), timeout=0.5)

    assert frame.startswith("event: error\ndata: ")
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=0.5)
    # workspace 流与 trace/message 流的差异：收尾必须主动 aclose 源迭代器。
    assert source.closed is True


@pytest.mark.asyncio
async def test_workspace_file_sse_closes_source_on_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源耗尽时停止并关闭源订阅，不泄漏任务。"""

    class _Exhausted(_Source):
        async def __anext__(self) -> WorkspaceFileChangeBatch:
            raise StopAsyncIteration

    source = _Exhausted([])
    stream, _ = await _open_stream(monkeypatch, source)

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=0.5)
    assert source.closed is True
