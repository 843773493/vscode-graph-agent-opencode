"""冻结 GET /api/v1/sessions 查询参数的公开校验契约。

limit 是该端点唯一的分页参数。改前它用裸默认值 limit: int = 20 声明，没有
ge/le 边界：limit=0 / limit=-5 会一路进到业务层才抛 ValueError，被适配层统一
落成 409（状态冲突），而同族分页入口（list_messages / list_trace_events）一律在
FastAPI 参数层落 422。两个形态都是 4xx，但同一族的「非法分页参数」给出两种
状态码属于错误分类漂移。

本用例锁定：非法 limit 必须在请求参数校验层 fail-closed 为 422，且不再落到服务层。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import sessions as sessions_api
from app.api.deps import get_session_service, verify_local_token
from app.core.trace_middleware import TraceMiddleware


class _RecordingSessionService:
    """记录服务层调用，证明非法 limit 不会穿透到业务层。"""

    def __init__(self) -> None:
        self.limits: list[int] = []

    async def list(self, *, limit: int, cursor: str | None):
        self.limits.append(limit)
        raise AssertionError("非法 limit 不应到达服务层")


@pytest.fixture
def session_list_client() -> Iterator[tuple[TestClient, _RecordingSessionService]]:
    service = _RecordingSessionService()
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(sessions_api.router, prefix="/api/v1")
    application.dependency_overrides[verify_local_token] = lambda: "local"
    application.dependency_overrides[get_session_service] = lambda: service
    with TestClient(application) as client:
        yield client, service


@pytest.mark.parametrize("limit", ("0", "-5", "201"))
def test_session_list_rejects_out_of_range_limit_with_422(
    session_list_client: tuple[TestClient, _RecordingSessionService],
    limit: str,
) -> None:
    client, service = session_list_client

    response = client.get("/api/v1/sessions", params={"limit": limit})

    assert response.status_code == 422
    assert service.limits == []
