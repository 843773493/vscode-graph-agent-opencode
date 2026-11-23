"""冻结显式录放配置；在各实例的 HTTP 传输边界共用匹配与录制状态。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Lock

import httpx

from app.testing.model_stream.assets import StreamScenario, load_scenario
from app.testing.model_stream.config import (
    ModelStreamConfig,
    load_model_stream_config_from_environment,
)
from app.testing.model_stream.errors import ModelStreamError
from app.testing.model_stream.transport import ModelStreamHTTPTransport

_controller_lock = Lock()


class ModelStreamTransportController:
    def __init__(
        self,
        *,
        config: ModelStreamConfig,
        scenario: StreamScenario,
    ) -> None:
        self.config = config
        self.scenario = scenario
        self._transport: ModelStreamHTTPTransport | None = None
        self._closed = False

    @property
    def installed(self) -> bool:
        return self._transport is not None and not self._closed

    @property
    def transport(self) -> ModelStreamHTTPTransport:
        if self._transport is None:
            raise ModelStreamError("model stream transport 尚未安装")
        return self._transport

    @classmethod
    def configure(cls, config: ModelStreamConfig) -> ModelStreamTransportController | None:
        global _configured_controller
        if config.transport.mode == "off":
            return None
        if config.transport.scenario_id is None:
            raise ModelStreamError("启用 model stream transport 时缺少 scenario_id")
        scenario = load_scenario(
            config.transport.fixture_root,
            config.transport.scenario_id,
        )
        with _controller_lock:
            if _configured_controller is not None:
                raise ModelStreamError("model stream transport 已有未关闭的配置 owner")
            controller = cls(config=config, scenario=scenario)
            _configured_controller = controller
            return controller

    def ensure_installed(self) -> None:
        with _controller_lock:
            if self._closed:
                raise ModelStreamError("model stream transport owner 已关闭")
            if self._transport is not None:
                return
            self._transport = ModelStreamHTTPTransport(
                scenario=self.scenario,
                mode=self.config.transport.mode,
                matching_policy=self.config.transport.matching_policy,
                replay_policy=self.config.transport.replay_policy,
                artifact_root=self.config.transport.artifact_root,
            )

    async def aclose(self) -> None:
        global _configured_controller
        with _controller_lock:
            if self._closed:
                return
            transport = self._transport
        if transport is not None:
            await transport.aclose()
        with _controller_lock:
            self._closed = True
            if _configured_controller is self:
                _configured_controller = None


_configured_controller: ModelStreamTransportController | None = None


def configure_model_stream_from_environment(
    *,
    project_root: Path | str | None = None,
) -> ModelStreamTransportController | None:
    config = load_model_stream_config_from_environment(project_root=project_root)
    if config is None:
        return None
    return ModelStreamTransportController.configure(config)


def ensure_model_stream_installed() -> None:
    controller = _configured_controller
    if controller is not None:
        controller.ensure_installed()


class _RecordingRoute(httpx.AsyncBaseTransport):
    """实例持有实际路由；录制状态仍归原进程级 owner。"""

    def __init__(self, recorder: ModelStreamHTTPTransport, live: httpx.AsyncBaseTransport) -> None:
        self._recorder, self._live = recorder, live

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._recorder.handle_async_request(request, live_transport=self._live)

    async def aclose(self) -> None:
        await self._live.aclose()


class _SyncRecordingRoute(httpx.BaseTransport):
    def __init__(self, recorder: ModelStreamHTTPTransport, live: httpx.BaseTransport) -> None:
        self._recorder, self._live = recorder, live

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._recorder.handle_request(request, live_transport=self._live)

    def close(self) -> None:
        self._live.close()


def model_stream_sync_transport(
    live_factory: Callable[[], httpx.BaseTransport],
) -> httpx.BaseTransport:
    controller = _configured_controller
    if controller is None:
        return live_factory()
    controller.ensure_installed()
    if controller.config.transport.mode == "record":
        return _SyncRecordingRoute(controller.transport, live_factory())
    return httpx.MockTransport(controller.transport.handle_request)


def model_stream_async_transport(
    live_factory: Callable[[], httpx.AsyncBaseTransport],
) -> httpx.AsyncBaseTransport:
    """回放不创建真实路由；录制包装本实例路由，关闭不触碰进程级 owner。"""
    controller = _configured_controller
    if controller is None:
        return live_factory()
    controller.ensure_installed()
    if controller.config.transport.mode == "record":
        return _RecordingRoute(controller.transport, live_factory())
    return httpx.MockTransport(controller.transport.handle_async_request)
