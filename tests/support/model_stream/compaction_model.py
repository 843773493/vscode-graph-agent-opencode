"""用于真实候选生成链路的确定性本地摘要模型。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from langchain_core.language_models import BaseChatModel

from app.runtime.model_stream import ModelStreamTransportController
from app.runtime.network_proxy import ProxyRoutes
from app.runtime.provider_model import build_model_from_provider
from app.testing.model_stream import load_model_stream_config, load_scenario
from configs.network import DirectProxy
from tests.harness.python.run_context import TestRunContext


@asynccontextmanager
async def replay_compaction_model(artifacts_dir: Path) -> AsyncIterator[BaseChatModel]:
    """场景和静态历史生成器共用同一 cassette 及模型生命周期。"""
    config = load_model_stream_config(Path.cwd() / "configs/tests/model_stream/model_stream_chat_basic.jsonc")
    config = replace(
        config,
        transport=replace(
            config.transport,
            scenario_id="basic-text",
            artifact_root=artifacts_dir,
        ),
    )
    scenario = load_scenario(config.transport.fixture_root, "basic-text")
    provider_request = scenario.cassette.interactions[0].request
    async with AsyncExitStack() as cleanup:
        controller = ModelStreamTransportController.configure(config)
        cleanup.push_async_callback(controller.aclose)
        model = build_model_from_provider(
            provider={
                "provider_id": "summary",
                "model_id": "summary",
                "model": provider_request.match["model"],
                "endpoint": provider_request.url.removesuffix("/chat/completions"),
                "api_key": "test-key",
                "custom_llm_provider": "openai",
                "api_mode": {"protocol": "chat_completions", "model_info": {}, "supports_reasoning": {}},
                "proxy_routes": ProxyRoutes.from_configuration(DirectProxy()),
            },
            runtime_config={},
            invocation_purpose="compaction",
        )
        cleanup.push_async_callback(model.aclose)
        yield model


@pytest.fixture
async def compaction_summary_model(request, monkeypatch) -> AsyncIterator[BaseChatModel]:
    """从固定 cassette 创建真实 compaction provider 模型，不访问外网。"""
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()

    def forbidden_transport(*_args, **_kwargs):
        raise AssertionError("候选生成回放不得创建真实 HTTP transport")

    async with replay_compaction_model(context.artifacts_dir) as model:
        monkeypatch.setattr(httpx, "AsyncHTTPTransport", forbidden_transport)
        monkeypatch.setattr(httpx, "HTTPTransport", forbidden_transport)
        yield model
