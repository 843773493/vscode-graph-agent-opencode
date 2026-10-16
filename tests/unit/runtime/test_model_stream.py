"""模型 transport 配置和未使用的关闭路径不得导入 SDK。"""
from __future__ import annotations

import builtins
from dataclasses import replace
from pathlib import Path

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


@pytest.fixture
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
        assert controller.client is None
        with pytest.raises(ModelStreamError, match="未关闭的配置 owner"):
            ModelStreamTransportController.configure(model_stream_config)
    finally:
        await controller.aclose()
    await controller.aclose()
    ensure_model_stream_installed()
