"""upstream attempt 记录不得随进程内调用次数累积。

历史缺陷：trace 回调挂在 LiteLLM 全局回调表上，而该表只增不减，导致第 N 次
模型调用的日志里出现 N 条 attempt（见 ses_a307c20260f4417fa7ad41aad1b9b319）。
"""
from __future__ import annotations

from pathlib import Path

import litellm
import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.agents.upstream_request_trace import (
    begin_upstream_capture,
    end_upstream_capture,
)
from app.testing.model_stream import (
    ModelStreamTransportController,
    load_model_stream_config,
)

CHAT_CONFIG_PATH = (
    Path.cwd() / "configs" / "tests" / "model_stream" / "model_stream_chat_basic.jsonc"
)
RESPONSES_CONFIG_PATH = (
    Path.cwd() / "configs" / "tests" / "model_stream" / "model_stream_responses.jsonc"
)


async def _capture(model, messages) -> list[dict]:
    token = begin_upstream_capture()
    async for _chunk in model.astream(messages):
        pass
    return end_upstream_capture(token)


@pytest.mark.asyncio
async def test_chat_attempts_do_not_accumulate_across_calls() -> None:
    config = load_model_stream_config(CHAT_CONFIG_PATH)
    controller = ModelStreamTransportController.install(config)
    if controller is None:
        raise RuntimeError("model stream replay controller 未安装")
    try:
        model = BoxteamLiteLLMChatModel(
            model="openai/big-pickle",
            api_key="test-key",
            api_base="https://opencode.ai/zen/v1",
            custom_llm_provider="openai",
            provider_id="primary",
            streaming=True,
        )
        for round_index in range(4):
            attempts = await _capture(
                model,
                [SystemMessage(content="s"), HumanMessage(content=f"第 {round_index} 轮")],
            )
            assert len(attempts) == 1, f"第 {round_index} 轮 attempts={len(attempts)}"
            assert attempts[0]["call_type"] == "acompletion"
            assert attempts[0]["api_base"] == "https://opencode.ai/zen/v1"
            assert attempts[0]["response"] is not None
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_responses_attempts_do_not_accumulate_across_calls() -> None:
    config = load_model_stream_config(RESPONSES_CONFIG_PATH)
    controller = ModelStreamTransportController.install(config)
    if controller is None:
        raise RuntimeError("model stream replay controller 未安装")
    try:
        model = BoxteamOpenAIResponsesModel(
            model="gpt-5.6-luna",
            api_key="test-key",
            api_base="https://www.cctq.ai/v1",
            custom_llm_provider="openai",
            provider_id="backup_3",
            responses_store=False,
            streaming=True,
        )
        for round_index in range(4):
            attempts = await _capture(
                model,
                [SystemMessage(content="s"), HumanMessage(content="请读取 README.md")],
            )
            assert len(attempts) == 1, f"第 {round_index} 轮 attempts={len(attempts)}"
            assert attempts[0]["call_type"] == "aresponses"
            assert attempts[0]["api_base"] == "https://www.cctq.ai/v1/responses"
            assert attempts[0]["response"] is not None
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_model_calls_leave_litellm_callback_tables_untouched() -> None:
    config = load_model_stream_config(CHAT_CONFIG_PATH)
    controller = ModelStreamTransportController.install(config)
    if controller is None:
        raise RuntimeError("model stream replay controller 未安装")
    try:
        model = BoxteamLiteLLMChatModel(
            model="openai/big-pickle",
            api_key="test-key",
            api_base="https://opencode.ai/zen/v1",
            custom_llm_provider="openai",
            streaming=True,
        )
        for round_index in range(3):
            await _capture(
                model,
                [SystemMessage(content="s"), HumanMessage(content=f"第 {round_index} 轮")],
            )
        assert litellm.input_callback == []
        assert litellm.success_callback == []
        assert litellm.failure_callback == []
    finally:
        await controller.aclose()
