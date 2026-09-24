"""冻结 deps 依赖提供者在容器缺失时的显式报错契约。

35 个服务提供者曾各自复制一份「getattr + isinstance + RuntimeError」样板，
现收敛为唯一 ``_require_service``。收敛必须保持外部可观察行为不变：每个
提供者在容器未装配时都要抛出带自身文案的 RuntimeError，不能返回虚假默认值。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import Request

from app.api import deps
from app.services.business.agent_service import AgentService
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService


def _request_with_container(container: object | None) -> Request:
    app = MagicMock()
    app.state.container = container
    request = MagicMock(spec=Request)
    request.app = app
    return request


def test_provider_raises_when_container_missing() -> None:
    with pytest.raises(RuntimeError, match="应用容器尚未初始化"):
        deps.get_session_service(_request_with_container(None))


def test_config_provider_keeps_its_own_message() -> None:
    # 容器缺失也要报 ConfigService 自身文案，与收敛前逐字一致。
    with pytest.raises(RuntimeError, match="ConfigService 尚未在应用启动阶段初始化"):
        deps.get_config_service(_request_with_container(None))


def test_provider_raises_when_service_not_initialized() -> None:
    with pytest.raises(RuntimeError, match="SessionService 尚未在应用启动阶段初始化"):
        deps.get_session_service(_request_with_container(MagicMock()))


def test_provider_rejects_wrong_service_type() -> None:
    container = MagicMock()
    container.session_service = object()
    with pytest.raises(RuntimeError, match="SessionService 尚未在应用启动阶段初始化"):
        deps.get_session_service(_request_with_container(container))


def test_provider_returns_matching_instance() -> None:
    container = MagicMock()
    service = MagicMock(spec=SessionService)
    container.session_service = service

    assert deps.get_session_service(_request_with_container(container)) is service


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

        assert isinstance(provider(_request_with_container(container)), expected_type)
