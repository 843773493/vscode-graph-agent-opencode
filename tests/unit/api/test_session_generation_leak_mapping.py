"""冻结会话生成两条入口的裸 ``RuntimeError`` 不再泄漏成 500。

触发源唯一：``SessionGenerationService`` 的失败账本。
``execute`` 在 ``_execute_locked`` 的 ``except Exception`` 落一条 ``status=failed``
且**没有 result 键**的 ledger；``get_run_status`` 读到它 → 抛裸 ``RuntimeError``；
同幂等键再 ``execute`` 也撞上该记录 → 抛裸 ``RuntimeError``。

失败后重试、或查询失败运行的终态，都是客户端极其正常的操作，绝不能伪装成 500
并下发 ``RuntimeError:`` 内部类名。

本文件用依赖覆盖隔离服务，并以真实 HTTP 封套（TestClient + 真实 app）断言**精确**
状态码与**精确** detail 文本。按 tests/unit/api 规范：依赖注入隔离 + 不启动真实后端进程。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.api.deps import get_session_generation_service
from app.main import app
from app.services.business.session_generation import SessionGenerationService

_MISSING_RESULT = "会话生成运行记录缺少 result: /tmp/ledger.json"
_IDEMPOTENT_CONFLICT = (
    "同一幂等键存在未完成的生成记录，拒绝重复创建: "
    "idempotency_key=k1, status=failed"
)


def _client_with_service(service: MagicMock) -> TestClient:
    app.dependency_overrides[get_session_generation_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


def test_get_status_maps_missing_result_runtime_error_to_409() -> None:
    """路径 A：失败账本缺 result → 409 + 纯文本 detail（原 500 + 类名泄漏）。"""
    service = MagicMock(spec=SessionGenerationService)
    service.get_run_status = MagicMock(side_effect=RuntimeError(_MISSING_RESULT))
    client = _client_with_service(service)
    try:
        response = client.get(
            "/api/v1/session-generations/status",
            params={"generator_id": "g1", "idempotency_key": "k1"},
            headers={"X-Request-ID": "req_gen_status_conflict"},
        )
    finally:
        app.dependency_overrides.pop(get_session_generation_service, None)

    assert response.status_code == 409
    assert response.headers["X-Request-ID"] == "req_gen_status_conflict"
    assert response.json() == {
        "detail": _MISSING_RESULT,
        "request_id": "req_gen_status_conflict",
    }


def test_execute_maps_idempotency_runtime_error_to_409() -> None:
    """路径 B：同幂等键撞上既有失败运行 → 409 + 纯文本 detail（原 500）。"""
    service = MagicMock(spec=SessionGenerationService)
    service.execute = AsyncMock(side_effect=RuntimeError(_IDEMPOTENT_CONFLICT))
    client = _client_with_service(service)
    payload = {
        "run_id": "run1",
        "generator_id": "g1",
        "idempotency_key": "k1",
        "generator_type": {"type_id": "builtin.agent_prompt", "version": "1"},
        "name": "p1",
        "config": {"prompt": "hi"},
        "placement": {"kind": "workspace", "workspace_id": "ws1"},
        "context_source": {"kind": "fresh"},
        "session_strategy": {"mode": "new_per_run"},
        "title": "p1",
        "navigation_path": [],
        "execution_workspace_id": "ws1",
    }
    try:
        response = client.post(
            "/api/v1/session-generations/execute",
            json=payload,
            headers={
                "X-Local-Token": "local-dev-token",
                "X-Request-ID": "req_gen_execute_conflict",
            },
        )
    finally:
        app.dependency_overrides.pop(get_session_generation_service, None)

    assert response.status_code == 409
    assert response.headers["X-Request-ID"] == "req_gen_execute_conflict"
    assert response.json() == {
        "detail": _IDEMPOTENT_CONFLICT,
        "request_id": "req_gen_execute_conflict",
    }

