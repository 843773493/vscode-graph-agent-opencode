"""导航 operation HTTP 适配层的错误映射与协议单测（OpenSpec 8.1-G/8.1-H）。

只验证 handler 的参数转换与错误映射，不启动真实后端进程。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import session_navigation as nav
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
    NavigationMutationEnqueueResultDTO,
    NavigationMutationStatusPageDTO,
)
from app.services.business.session_navigation.queue_store import (
    NavigationBackpressureError,
    NavigationMutationConflictError,
)


def _enqueue_request() -> NavigationMutationEnqueueRequest:
    return NavigationMutationEnqueueRequest.model_validate(
        {
            "intents": [
                {
                    "client_operation_id": "op_" + "a" * 32,
                    "client_sequence": 1,
                    "kind": "create_folder",
                    "base_catalog_revision": 0,
                    "name": "新目录",
                }
            ]
        }
    )


class _StubService:
    """记录调用并可按需抛错的目录服务桩。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.workspace_id = "11111111-1111-4111-8111-111111111111"
        self.error = error
        self.enqueue_scope_calls = 0
        # 计数而非仅抛错：空转断言（如「某个恒真表达式」）无法区分
        # 「cursor 解码失败后短路返回」与「仍继续调用 events 服务」。
        self.decode_cursor_calls = 0
        self.navigation_events_calls = 0

    async def submit_operation_batch(self, request, scope):
        self.enqueue_scope_calls += 1
        assert scope.workspace_id == self.workspace_id
        if self.error is not None:
            raise self.error
        return NavigationMutationEnqueueResultDTO(
            workspace_id=self.workspace_id, accepted_count=0
        )

    def operation_status(self, operation_ids, scope):
        assert scope.workspace_id == self.workspace_id
        return NavigationMutationStatusPageDTO(
            workspace_id=self.workspace_id,
            catalog_revision=0,
            unknown_operation_ids=list(operation_ids),
        )

    def navigation_snapshot(self):
        raise AssertionError("本用例不应调用 snapshot")

    def navigation_events(self, *, after, limit):
        self.navigation_events_calls += 1
        raise AssertionError("本用例不应调用 events")

    def decode_navigation_events_cursor(self, cursor: str):
        self.decode_cursor_calls += 1
        raise ValueError(f"navigation 事件 cursor 无效: {cursor!r}")


@pytest.mark.asyncio
async def test_enqueue_maps_backpressure_to_retryable_503() -> None:
    """队列积压是可重试背压，必须是 503 而不是 4xx/5xx 静默丢弃。"""
    service = _StubService(NavigationBackpressureError("队列积压"))

    with pytest.raises(HTTPException) as captured:
        await nav.enqueue_session_catalog_operations(
            payload=_enqueue_request(),
            _="local-dev-token",
            request_id="req_backpressure",
            service=service,
        )

    assert captured.value.status_code == 503


@pytest.mark.asyncio
async def test_enqueue_maps_conflict_to_409() -> None:
    """同 key 异 preimage 冲突必须是 409，客户端才能保留原 outbox。"""
    service = _StubService(NavigationMutationConflictError("preimage 冲突"))

    with pytest.raises(HTTPException) as captured:
        await nav.enqueue_session_catalog_operations(
            payload=_enqueue_request(),
            _="local-dev-token",
            request_id="req_conflict",
            service=service,
        )

    assert captured.value.status_code == 409


@pytest.mark.asyncio
async def test_operation_status_reports_unknown_ids_without_error() -> None:
    """未知 operation ID 走正常 200 响应，由响应体显式列出未知 ID。"""
    service = _StubService()

    response = await nav.get_session_catalog_operation_status(
        operation_id=["op_" + "b" * 32],
        _="local-dev-token",
        request_id="req_status",
        service=service,
    )

    assert response.data is not None
    assert response.data.unknown_operation_ids == ["op_" + "b" * 32]
    assert response.request_id == "req_status"


@pytest.mark.asyncio
async def test_navigation_events_decodes_cursor_then_serves_page() -> None:
    """cursor 优先于 after 参数；解析失败映射为 409（不泄漏成 500）。"""
    service = _StubService()

    with pytest.raises(HTTPException) as captured:
        await nav.list_session_catalog_navigation_events(
            after=0,
            limit=200,
            cursor="not-a-cursor",
            _="local-dev-token",
            request_id="req_events",
            service=service,
        )

    assert captured.value.status_code == 409
    # cursor 已给出且解码失败：必须先短路返回 409，绝不继续调用 events 服务
    # （否则调用方会拿到一个基于错误 cursor 的事件页）。
    assert service.decode_cursor_calls == 1
    assert service.navigation_events_calls == 0
    assert "cursor 无效" in str(captured.value.detail)
