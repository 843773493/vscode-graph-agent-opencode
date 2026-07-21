"""真实验证配置中的 Chat Completions 与 Responses 多轮 Prompt Cache。"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    message_chunk_to_message,
)

from app.agents.agent_factory import build_model_from_provider
from app.services.infrastructure.config_service import ConfigService


CONVERSATION_ROUNDS = 3


def _cacheable_system_prompt() -> str:
    stable_fact = (
        "缓存测试固定事实：BoxTeam 是运行在本地工作区中的 AI 编程助手，"
        "本段内容在每一轮请求中必须保持完全一致。"
    )
    return "\n".join([stable_fact] * 100)


def _round_prompt(round_number: int) -> str:
    cache_growth_material = "，".join(
        f"第{round_number}轮资料-{index:03d}"
        for index in range(180)
    )
    return (
        f"这是第 {round_number} 轮缓存测试。请先在内部逐步检查新增资料的编号是否连续，"
        f"并计算校验值 {137 + round_number} * {249 + round_number} - 86；"
        f"记住资料后只回复 ROUND-{round_number}。资料：{cache_growth_material}"
    )


def _test_image_data_url() -> str:
    image_path = Path.cwd() / "asset" / "default_test_workspace" / "assets" / "test.jpg"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _cache_read_tokens(message: AIMessageChunk) -> tuple[int, int]:
    usage = message.usage_metadata
    if usage is None:
        raise AssertionError("响应缺少 usage_metadata，无法计算缓存命中率")
    details = usage.get("input_token_details") or {}
    return usage["input_tokens"], details.get("cache_read", 0)


async def _invoke(
    model: Any,
    messages: list[BaseMessage],
) -> tuple[AIMessage, int, int]:
    combined: AIMessageChunk | None = None
    async for chunk in model.astream(messages):
        if not isinstance(chunk, AIMessageChunk):
            raise TypeError(f"模型流必须返回 AIMessageChunk: {type(chunk).__name__}")
        combined = chunk if combined is None else combined + chunk
    if combined is None:
        raise AssertionError("模型流没有返回 chunk")
    input_tokens, cache_read = _cache_read_tokens(combined)
    message = message_chunk_to_message(combined)
    if not isinstance(message, AIMessage):
        raise TypeError(f"合并结果不是 AIMessage: {type(message).__name__}")
    return message, input_tokens, cache_read


async def _run_three_rounds(
    provider: dict[str, Any],
    *,
    image_data_url: str | None = None,
) -> tuple[list[tuple[int, int]], list[AIMessage]]:
    model = build_model_from_provider(
        provider,
        {},
        prompt_cache_key=f"boxteam-cache-test:{provider['id']}",
    )
    messages: list[BaseMessage] = [SystemMessage(content=_cacheable_system_prompt())]
    statistics: list[tuple[int, int]] = []
    responses: list[AIMessage] = []
    for round_number in range(1, CONVERSATION_ROUNDS + 1):
        prompt = _round_prompt(round_number)
        if round_number == 1 and image_data_url is not None:
            messages.append(
                HumanMessage(
                    content=[
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": image_data_url},
                        },
                    ]
                )
            )
        else:
            messages.append(HumanMessage(content=prompt))
        response, input_tokens, cache_read = await _invoke(model, messages)
        statistics.append((input_tokens, cache_read))
        responses.append(response)
        messages.append(response)
    return statistics, responses


def _assert_cache_hit(label: str, statistics: list[tuple[int, int]]) -> None:
    rounds = [
        {
            "round": index,
            "input_tokens": input_tokens,
            "cache_read_tokens": cache_read,
            "cache_hit_rate": cache_read / input_tokens,
        }
        for index, (input_tokens, cache_read) in enumerate(statistics, start=1)
    ]
    print(f"\n[{label}] rounds={rounds}")
    assert statistics[0][0] >= 1024
    assert any(cache_read > 0 for _, cache_read in statistics[1:]), (
        f"{label} 后续两轮均未命中 Prompt Cache: {rounds}"
    )


@pytest.fixture
def config_service() -> ConfigService:
    return ConfigService()


@pytest.mark.skipif(
    os.environ.get("CCTQ_API_KEY") is None,
    reason="需要 CCTQ_API_KEY 才能运行 Luna 真实缓存测试",
)
@pytest.mark.asyncio
async def test_luna_responses_multiturn_prompt_cache_hit_rate(
    config_service: ConfigService,
) -> None:
    provider = config_service.get_llm_provider("backup_3")
    assert provider["api_mode"] == "responses"
    statistics, responses = await _run_three_rounds(
        provider,
        image_data_url=_test_image_data_url(),
    )
    _assert_cache_hit("Luna Responses", statistics)
    assert statistics[2][1] > statistics[1][1], statistics

    reasoning_items = [
        block["extras"]["response_item"]
        for response in responses
        for block in response.content
        if isinstance(block, dict)
        and block.get("type") == "reasoning"
        and isinstance(block.get("extras"), dict)
        and isinstance(block["extras"].get("response_item"), dict)
    ]
    print(f"\n[Luna Responses] encrypted_reasoning_items={len(reasoning_items)}")
    assert reasoning_items, "Luna 三轮响应均未返回 encrypted reasoning item"
    assert all(item.get("encrypted_content") for item in reasoning_items)


@pytest.mark.skipif(
    os.environ.get("OPENCODE_ZEN_API_KEY") is None,
    reason="需要 OPENCODE_ZEN_API_KEY 才能运行 big-pickle 真实缓存测试",
)
@pytest.mark.asyncio
async def test_big_pickle_chat_completions_multiturn_prompt_cache_hit_rate(
    config_service: ConfigService,
) -> None:
    provider = config_service.get_llm_provider("primary")
    assert provider["model"] == "big-pickle"
    assert provider["api_mode"] == "chat_completions"
    statistics, _ = await _run_three_rounds(provider)
    _assert_cache_hit("big-pickle Chat Completions", statistics)
