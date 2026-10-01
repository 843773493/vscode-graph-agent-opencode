"""SessionService.list 的 cursor 分页契约。

缺陷背景：`list` 曾接受 `cursor`/`skip` 参数却完全忽略 `cursor`，且恒定返回
`cursor=None`；同时路由声明 `response_model=APIResponse[CursorPage[SessionDTO]]`，
FastAPI 按响应模型裁剪字段，`total` 被静默丢弃、真实游标字段也不存在。结果是
前端只能拿到第一页，永远无法翻页——契约与实际行为不一致。

本文件锁定：cursor 可继续翻页、cursor 与目录 revision 绑定（会话集合变化后
旧 cursor 显式失效）、以及 HTTP 封套真实透出 `next_cursor`/`has_more`。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_session_service
from app.api.sessions import router
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.unit.core.catalog_workspace_helper import build_catalog_workspace

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


def _build_service(workspace) -> SessionService:
    return SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=workspace.sessions_root),
        workspace_id=WORKSPACE_ID,
        path_resolver=workspace.resolver,
        creation_service=workspace.creation_service,
    )


@pytest.fixture()
def workspace(tmp_path: Path):
    context = build_catalog_workspace(tmp_path, workspace_id=WORKSPACE_ID)
    try:
        yield context
    finally:
        context.close()


@pytest.mark.asyncio
async def test_list_cursor_walks_all_pages_without_overlap(workspace) -> None:
    service = _build_service(workspace)
    for index in range(5):
        await service.create(SessionCreateRequest(title=f"会话-{index:02d}"))
        await asyncio.sleep(0.001)

    first = await service.list(limit=2)
    assert [item.title for item in first.items] == ["会话-04", "会话-03"]
    assert first.total == 5
    assert first.has_more is True
    assert first.next_cursor is not None

    second = await service.list(limit=2, cursor=first.next_cursor)
    assert [item.title for item in second.items] == ["会话-02", "会话-01"]
    assert second.has_more is True
    assert second.next_cursor is not None

    third = await service.list(limit=2, cursor=second.next_cursor)
    assert [item.title for item in third.items] == ["会话-00"]
    assert third.has_more is False
    assert third.next_cursor is None

    seen = [
        item.session_id
        for page in (first, second, third)
        for item in page.items
    ]
    assert len(seen) == len(set(seen)) == 5


@pytest.mark.asyncio
async def test_list_cursor_is_invalidated_when_catalog_changes(workspace) -> None:
    service = _build_service(workspace)
    for index in range(4):
        await service.create(SessionCreateRequest(title=f"会话-{index:02d}"))
        await asyncio.sleep(0.001)

    first = await service.list(limit=2)
    assert first.next_cursor is not None

    await service.create(SessionCreateRequest(title="后来居上"))

    with pytest.raises(ValueError, match="会话列表已更新"):
        await service.list(limit=2, cursor=first.next_cursor)


@pytest.mark.asyncio
async def test_list_rejects_malformed_cursor(workspace) -> None:
    service = _build_service(workspace)
    await service.create(SessionCreateRequest(title="唯一会话"))

    with pytest.raises(ValueError, match="cursor 格式无效"):
        await service.list(limit=2, cursor="not-a-valid-cursor")


@pytest.mark.asyncio
async def test_list_empty_workspace_has_no_cursor(workspace) -> None:
    service = _build_service(workspace)

    result = await service.list(limit=5)

    assert result.items == []
    assert result.total == 0
    assert result.has_more is False
    assert result.next_cursor is None


def test_list_sessions_http_envelope_exposes_real_pagination(tmp_path: Path) -> None:
    """HTTP 封套必须透出真实游标字段，且能据此翻到第二页。"""

    async def build():
        context = build_catalog_workspace(tmp_path, workspace_id=WORKSPACE_ID)
        service = _build_service(context)
        for index in range(3):
            await service.create(SessionCreateRequest(title=f"页-{index:02d}"))
        return service, context

    service, context = asyncio.run(build())
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session_service] = lambda: service
    try:
        with TestClient(app) as client:
            first = client.get(
                "/api/v1/sessions?limit=2",
                headers={"X-Local-Token": "local-dev-token"},
            )
            assert first.status_code == 200, first.text
            page = first.json()["data"]
            assert len(page["items"]) == 2
            assert page["has_more"] is True
            assert isinstance(page["next_cursor"], str) and page["next_cursor"]

            second = client.get(
                f"/api/v1/sessions?limit=2&cursor={page['next_cursor']}",
                headers={"X-Local-Token": "local-dev-token"},
            )
            assert second.status_code == 200, second.text
            tail = second.json()["data"]
            assert len(tail["items"]) == 1
            assert tail["has_more"] is False
            assert tail["next_cursor"] is None
            assert {item["session_id"] for item in page["items"]}.isdisjoint(
                {item["session_id"] for item in tail["items"]}
            )

            bad = client.get(
                "/api/v1/sessions?cursor=not-a-valid-cursor",
                headers={"X-Local-Token": "local-dev-token"},
            )
            assert bad.status_code == 409, bad.text
    finally:
        app.dependency_overrides.clear()
        context.close()


@pytest.mark.asyncio
async def test_list_rejects_non_positive_limit(workspace) -> None:
    """非正 limit 必须 fail-closed，不能返回永不前进的空页。

    缺陷背景：``list(limit=0)`` 返回空页却仍带 ``has_more=True`` 与和请求同
    offset 的 cursor；调用方按契约继续翻页时永远拿到空页且 ``next_cursor`` 不
    前进（死循环）。公开查询参数必须显式拒绝。
    """
    service = _build_service(workspace)
    for index in range(3):
        await service.create(SessionCreateRequest(title=f"页-{index:02d}"))

    with pytest.raises(ValueError, match="limit 必须大于 0"):
        await service.list(limit=0)
    with pytest.raises(ValueError, match="limit 必须大于 0"):
        await service.list(limit=-1)


def test_list_sessions_http_rejects_zero_limit(tmp_path: Path) -> None:
    """HTTP 入口对 limit=0/负数 fail-closed 落 409，而不是 200 + 不可收敛的空页。"""

    async def build():
        context = build_catalog_workspace(tmp_path, workspace_id=WORKSPACE_ID)
        service = _build_service(context)
        await service.create(SessionCreateRequest(title="唯一"))
        return service, context

    service, context = asyncio.run(build())
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session_service] = lambda: service
    try:
        with TestClient(app) as client:
            for bad in ("0", "-1"):
                response = client.get(
                    f"/api/v1/sessions?limit={bad}",
                    headers={"X-Local-Token": "local-dev-token"},
                )
                # 路由沿用既有 409 状态冲突映射（不新增 422 契约），detail 为
                # 纯文本消息本体。改前这里是 200 + 空 items + 重复 cursor。
                assert response.status_code == 409, response.text
                assert "limit 必须大于 0" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()
        context.close()
