"""冻结显式配置，在第一次 Provider 请求前装配模型 stream SDK。"""

from __future__ import annotations

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
        self.client: httpx.AsyncClient | None = None
        self._previous_async_client_factory: object = None
        self._closed = False

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
            if self.client is not None:
                return
            import litellm
            import litellm.llms.custom_httpx.llm_http_handler as litellm_http_handler
            from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

            if litellm.aclient_session is not None:
                raise ModelStreamError(
                    "无法安装 model stream transport：LiteLLM async client 已经存在，"
                    "必须在第一次模型请求前安装"
                )
            transport = ModelStreamHTTPTransport(
                scenario=self.scenario,
                mode=self.config.transport.mode,
                matching_policy=self.config.transport.matching_policy,
                replay_policy=self.config.transport.replay_policy,
                artifact_root=self.config.transport.artifact_root,
            )
            client = httpx.AsyncClient(transport=transport, follow_redirects=True)
            injected_handler = AsyncHTTPHandler.__new__(AsyncHTTPHandler)
            injected_handler.timeout = None
            injected_handler.event_hooks = None
            injected_handler.client_alias = "boxteam-model-stream"
            injected_handler.client = client
            self._previous_async_client_factory = litellm_http_handler.get_async_httpx_client

            # TODO: LiteLLM 尚未给 Responses API 暴露稳定的 AsyncClient 注入参数；
            # 待上游提供正式扩展点后，删除这个进程内 factory hook。
            def get_injected_async_client(*_args: object, **_kwargs: object) -> AsyncHTTPHandler:
                return injected_handler

            litellm_http_handler.get_async_httpx_client = get_injected_async_client
            litellm.aclient_session = client
            self._transport = transport
            self.client = client

    async def aclose(self) -> None:
        global _configured_controller
        with _controller_lock:
            if self._closed:
                return
            self._closed = True
            if _configured_controller is self:
                _configured_controller = None
            client = self.client
            if client is None:
                return
            import litellm
            import litellm.llms.custom_httpx.llm_http_handler as litellm_http_handler

            litellm_http_handler.get_async_httpx_client = self._previous_async_client_factory
            if litellm.aclient_session is client:
                litellm.aclient_session = None
        await client.aclose()


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
