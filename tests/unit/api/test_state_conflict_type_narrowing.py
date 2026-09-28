"""冻结「类型化状态冲突」按**类型**分流：类A 落 409，裸异常放行为 5xx。

背景：API 适配层原先用 `except RuntimeError` 全捕获，把两种语义完全不同的失败
都落成 409：

- 类A「客户端可触发的状态冲突」（生命周期状态不允许该动作、幂等键撞上既有运行等）：
  客户端重试/修正时序即可消除，落 409 正确；
- 类B「服务端完整性故障」（内部不变量被破坏、持久化记录损坏、DI 缺装配等）：
  按 AGENTS.md「故障必须透明」应落 5xx 并保留完整服务端日志，不该伪装成 409。

机制：领域异常 :class:`ClientStateConflictError`（继承 `RuntimeError`）标记类A。
适配层的唯一实现 `state_conflict_error` 只接受该类型；类B 是裸 `RuntimeError`，
路由不再兜底捕获，由 `TraceMiddleware` 落 5xx。

本文件以 runtime 生命周期守卫这一条**已完全迁移**的垂直链路作为可覆盖范围：
用依赖覆盖注入两种异常，断言精确状态码。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.abstractions.state_conflict import ClientStateConflictError
from app.api.deps import get_runtime_service
from app.api.errors import state_conflict_error, state_conflict_http_error
from app.main import app
from app.services.infrastructure.runtime_service import RuntimeService


def _client_with_service(service: MagicMock) -> TestClient:
    app.dependency_overrides[get_runtime_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


def _runtime_service_raising(error: Exception) -> MagicMock:
    service = MagicMock(spec=RuntimeService)
    service.begin_drain = AsyncMock(side_effect=error)
    service.cancel_drain = AsyncMock(side_effect=error)
    service.force_interrupt = AsyncMock(side_effect=error)
    return service


# --- 映射层单元：类型即契约 ---------------------------------------------------


def test_client_state_conflict_is_runtime_error_subclass() -> None:
    """继承 `RuntimeError` 是类型细化后的向后兼容基类，不是双轨。

    历史调用方 `except RuntimeError` 仍能捕获类型化异常；新调用方可按更精确的
    类型分流。两层同时成立。
    """
    assert issubclass(ClientStateConflictError, RuntimeError)


def test_state_conflict_error_maps_client_conflict_to_409() -> None:
    mapped = state_conflict_error(ClientStateConflictError("只剩 draining 状态可以取消"))

    assert mapped.status_code == 409
    assert mapped.detail == "只剩 draining 状态可以取消"


def test_both_conflict_entrypoints_share_one_implementation() -> None:
    """过渡期入口与类型化入口共用同一份状态码与文本抽取实现（非双轨）。"""
    error = ClientStateConflictError("同一幂等键存在未完成的生成记录")

    typed = state_conflict_error(error)
    legacy = state_conflict_http_error(error)

    assert typed.status_code == legacy.status_code == 409
    assert typed.detail == legacy.detail == str(error)


# --- 真实 HTTP 封套：类A → 409，类B → 5xx ------------------------------------


def test_client_state_conflict_stays_409_over_real_http() -> None:
    """类A 零回归：类型化状态冲突仍落 409 + 纯文本 detail。"""
    message = "只有 draining 状态可以取消排空，当前状态: ready"
    client = _client_with_service(
        _runtime_service_raising(ClientStateConflictError(message))
    )
    try:
        response = client.post(
            "/api/v1/runtime/drain/cancel",
            headers={
                "X-Local-Token": "local-dev-token",
                "X-Request-ID": "req_typed_conflict",
            },
        )
    finally:
        app.dependency_overrides.pop(get_runtime_service, None)

    assert response.status_code == 409
    assert response.headers["X-Request-ID"] == "req_typed_conflict"
    assert response.json() == {
        "detail": message,
        "request_id": "req_typed_conflict",
    }


def test_server_integrity_runtime_error_is_not_masqueraded_as_409() -> None:
    """类B：裸 `RuntimeError`（服务端完整性故障）不再伪装成 409，落 5xx。

    这是本轮语义修复的核心：路由不再用 `except RuntimeError` 全捕获，异常冒泡到
    `TraceMiddleware` 落 500，并在服务端日志保留完整堆栈（`logger.exception`）。
    """
    client = _client_with_service(
        _runtime_service_raising(RuntimeError("内部不变量被破坏: drain 账本损坏"))
    )
    try:
        response = client.post(
            "/api/v1/runtime/drain",
            headers={
                "X-Local-Token": "local-dev-token",
                "X-Request-ID": "req_integrity_failure",
            },
        )
    finally:
        app.dependency_overrides.pop(get_runtime_service, None)

    assert response.status_code == 500
    assert response.status_code != 409
    assert response.headers["X-Request-ID"] == "req_integrity_failure"
    # 5xx 响应体是 TraceMiddleware 的通用封套，携带服务端诊断信息。
    assert "内部不变量被破坏: drain 账本损坏" in response.text
