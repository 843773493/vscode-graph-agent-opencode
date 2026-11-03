from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import session_navigation as nav
from app.schemas.internal_v2.search import WorkspaceSearchRequest
from app.services.business.session_navigation.catalog_query import CatalogCursorError


class _FailingCatalogService:
    """按调用方指定的异常类型模拟会话目录读取失败。"""

    def __init__(self, error: Exception) -> None:
        self.workspace_id = "workspace-validation"
        self.error = error
        self.list_children_kwargs: dict[str, object] = {}
        self.search_kwargs: dict[str, object] = {}

    async def list_children(
        self,
        *,
        parent_node_id: str | None,
        limit: int,
        cursor: str | None,
    ):
        self.list_children_kwargs = {
            "parent_node_id": parent_node_id,
            "limit": limit,
            "cursor": cursor,
        }
        raise self.error

    async def search(self, request: WorkspaceSearchRequest):
        self.search_kwargs = request.model_dump()
        raise self.error


@pytest.mark.asyncio
async def test_children_maps_unknown_parent_node_to_404() -> None:
    """客户端传入不存在的父节点是输入错误，必须与 breadcrumb 一样返回 404。"""
    service = _FailingCatalogService(KeyError("会话目录节点不存在: missing"))

    with pytest.raises(HTTPException) as captured:
        await nav.list_session_catalog_children(
            parent_node_id="missing",
            limit=100,
            cursor=None,
            request_id="req_children",
            service=service,
        )

    assert captured.value.status_code == 404
    assert captured.value.detail == "会话目录节点不存在: missing"


@pytest.mark.asyncio
async def test_children_maps_malformed_cursor_shape_to_409() -> None:
    """cursor 载荷不是对象时是客户端输入错误，不能泄漏成 500。"""
    error = CatalogCursorError("payload", "会话目录 cursor 格式无效")
    service = _FailingCatalogService(error)

    with pytest.raises(HTTPException) as captured:
        await nav.list_session_catalog_children(
            parent_node_id=None,
            limit=100,
            cursor="MTIz",
            request_id="req_children",
            service=service,
        )

    assert captured.value.status_code == 409
    assert captured.value.detail == error.detail()


@pytest.mark.asyncio
async def test_roots_maps_malformed_cursor_shape_to_409() -> None:
    error = CatalogCursorError("payload", "会话目录 cursor 格式无效")
    service = _FailingCatalogService(error)

    with pytest.raises(HTTPException) as captured:
        await nav.list_session_catalog_roots(
            limit=100,
            cursor="MTIz",
            request_id="req_roots",
            service=service,
        )

    assert captured.value.status_code == 409
    assert captured.value.detail == error.detail()


@pytest.mark.asyncio
async def test_search_maps_malformed_cursor_shape_to_409() -> None:
    error = CatalogCursorError("payload", "会话目录 cursor 格式无效")
    service = _FailingCatalogService(error)

    with pytest.raises(HTTPException) as captured:
        await nav.search_session_catalog(
            query="a",
            limit=100,
            cursor="MTIz",
            request_id="req_search",
            service=service,
        )

    assert captured.value.status_code == 409
    assert captured.value.detail == error.detail()


@pytest.mark.asyncio
async def test_children_maps_stale_catalog_cursor_to_409() -> None:
    error = CatalogCursorError("revision", "会话目录已更新，请从第一页重新加载")
    service = _FailingCatalogService(error)

    with pytest.raises(HTTPException) as captured:
        await nav.list_session_catalog_children(
            parent_node_id=None,
            limit=100,
            cursor=None,
            request_id="req_children",
            service=service,
        )

    assert captured.value.status_code == 409
    assert captured.value.detail == error.detail()
