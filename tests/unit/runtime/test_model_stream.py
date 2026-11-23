"""模型 transport 配置和未使用的关闭路径不得导入 SDK。"""

from __future__ import annotations

import builtins
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.runtime.model_stream import (
    ModelStreamTransportController,
    configure_model_stream_from_environment,
    ensure_model_stream_installed,
)
from app.testing.model_stream.config import (
    MODEL_STREAM_CONFIG_ENV,
    ModelStreamConfig,
    load_model_stream_config,
)
from app.testing.model_stream.errors import ModelStreamError


@pytest.fixture(scope="module")
def model_stream_config() -> ModelStreamConfig:
    return load_model_stream_config(
        Path.cwd() / "configs/tests/model_stream/model_stream_chat_basic.jsonc",
    )


@pytest.fixture
def sdk_import_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def import_without_sdk(name: str, *args: object, **kwargs: object):
        if name == "litellm" or name.startswith("litellm."):
            raise AssertionError("未请求 Provider 时不得导入 LiteLLM")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_sdk)


def test_absent_and_off_configuration_skip_sdk(
    model_stream_config: ModelStreamConfig,
    sdk_import_guard: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MODEL_STREAM_CONFIG_ENV, raising=False)
    assert configure_model_stream_from_environment() is None
    off_config = replace(
        model_stream_config,
        transport=replace(model_stream_config.transport, mode="off"),
    )
    assert ModelStreamTransportController.configure(off_config) is None
    ensure_model_stream_installed()


@pytest.mark.asyncio
async def test_unused_configuration_closes_without_sdk(
    model_stream_config: ModelStreamConfig,
    sdk_import_guard: None,
) -> None:
    controller = ModelStreamTransportController.configure(model_stream_config)
    assert controller is not None
    try:
        assert not controller.installed
        with pytest.raises(ModelStreamError, match="未关闭的配置 owner"):
            ModelStreamTransportController.configure(model_stream_config)
    finally:
        await controller.aclose()
    await controller.aclose()
    ensure_model_stream_installed()


@pytest.mark.asyncio
async def test_installed_transport_lifetime_does_not_require_sdk(
    model_stream_config: ModelStreamConfig, sdk_import_guard: None,
) -> None:
    controller = ModelStreamTransportController.configure(model_stream_config)
    assert controller is not None
    try:
        controller.ensure_installed()
        transport = controller.transport
        controller.ensure_installed()
        assert controller.transport is transport
        assert controller.installed
    finally:
        await controller.aclose()
    assert not controller.installed
    with pytest.raises(ModelStreamError, match="已关闭"):
        controller.ensure_installed()


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_pool", ["async", "sync"])
async def test_failed_close_keeps_owner_and_releases_the_other_pool(
    model_stream_config: ModelStreamConfig,
    monkeypatch: pytest.MonkeyPatch,
    failing_pool: str,
) -> None:
    config = replace(
        model_stream_config,
        transport=replace(model_stream_config.transport, mode="record"),
    )
    controller = ModelStreamTransportController.configure(config)
    assert controller is not None
    controller.ensure_installed()
    transport = controller.transport
    async_pool, sync_pool = httpx.AsyncHTTPTransport(), httpx.HTTPTransport()
    monkeypatch.setattr(transport, "_live_transport", async_pool)
    monkeypatch.setattr(transport, "_sync_live_transport", sync_pool)
    close_async, close_sync = async_pool.aclose, sync_pool.close
    calls = {"async": 0, "sync": 0}

    async def close_async_once() -> None:
        calls["async"] += 1
        if failing_pool == "async" and calls["async"] == 1:
            raise OSError("controlled async close failure")
        await close_async()

    def close_sync_once() -> None:
        calls["sync"] += 1
        if failing_pool == "sync" and calls["sync"] == 1:
            raise OSError("controlled sync close failure")
        close_sync()

    monkeypatch.setattr(async_pool, "aclose", close_async_once)
    monkeypatch.setattr(sync_pool, "close", close_sync_once)
    try:
        with pytest.raises(OSError, match=f"controlled {failing_pool} close failure"):
            await controller.aclose()
        assert calls == {"async": 1, "sync": 1}
        assert controller.installed
        with pytest.raises(ModelStreamError, match="未关闭的配置 owner"):
            ModelStreamTransportController.configure(config)
        await controller.aclose()
        await controller.aclose()
        assert not controller.installed
        assert calls == {"async": 2 if failing_pool == "async" else 1,
                         "sync": 2 if failing_pool == "sync" else 1}
    finally:
        await controller.aclose()
        await close_async()
        close_sync()

    replacement = ModelStreamTransportController.configure(config)
    assert replacement is not None
    await replacement.aclose()
