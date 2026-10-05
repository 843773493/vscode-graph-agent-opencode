"""冻结 deps 依赖提供者在工作区服务图注册表中的显式报错契约。

提供者必须按请求的 workspace_id 从 registry 取得工作区容器，并在服务缺失或类型不符时
显式报错，不能返回虚假默认值。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import Request

from app.api import deps
from app.services.business.agent_service import AgentService
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


def _registry(container: object | None) -> MagicMock:
    registry = MagicMock()
    registry.get.return_value = container
    return registry


def _request_with_registry(registry: object | None) -> Request:
    app = MagicMock()
    app.state.workspace_container_registry = registry
    request = MagicMock(spec=Request)
    request.app = app
    request.path_params = {"workspace_id": WORKSPACE_ID}
    request.headers = {}
    return request


def test_provider_raises_when_workspace_registry_missing() -> None:
    with pytest.raises(RuntimeError, match="已挂载工作区服务图注册表尚未初始化"):
        deps.get_session_service(_request_with_registry(None))


def test_config_provider_keeps_its_own_message() -> None:
    with pytest.raises(RuntimeError, match="ConfigService 尚未在应用启动阶段初始化"):
        deps.get_config_service(_request_with_registry(_registry(MagicMock())))


def test_provider_raises_when_service_not_initialized() -> None:
    with pytest.raises(RuntimeError, match="SessionService 尚未在应用启动阶段初始化"):
        deps.get_session_service(_request_with_registry(_registry(MagicMock())))


def test_provider_rejects_wrong_service_type() -> None:
    container = MagicMock()
    container.session_service = object()
    with pytest.raises(RuntimeError, match="SessionService 尚未在应用启动阶段初始化"):
        deps.get_session_service(_request_with_registry(_registry(container)))


def test_provider_returns_matching_instance() -> None:
    container = MagicMock()
    service = MagicMock(spec=SessionService)
    container.session_service = service
    registry = _registry(container)

    assert deps.get_session_service(_request_with_registry(registry)) is service
    registry.get.assert_called_once_with(WORKSPACE_ID)


def test_every_provider_is_callable_with_request() -> None:
    """逐一声明期望类型，防止收敛时漏接某个提供者或错配属性。"""
    expected = {
        "get_agent_service": AgentService,
        "get_config_service": ConfigService,
        "get_session_service": SessionService,
    }
    for name, expected_type in expected.items():
        provider = getattr(deps, name)
        container = MagicMock()
        setattr(container, name.removeprefix("get_"), MagicMock(spec=expected_type))
        request = _request_with_registry(_registry(container))

        assert isinstance(provider(request), expected_type)
