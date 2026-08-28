from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import litellm
import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.testing.model_stream import (
    ModelStreamHTTPTransport,
    ModelStreamTransportController,
    StreamScenario,
    build_cassette,
    data_frame,
    done_frame,
    load_cassette_from_object,
    load_model_stream_config,
    load_scenario,
    replay_session,
)

CONFIG_PATH = Path.cwd() / "configs" / "tests" / "model_stream.jsonc"
CHAT_BASIC_CONFIG_PATH = Path.cwd() / "configs" / "tests" / "model_stream_chat_basic.jsonc"
RESPONSES_CONFIG_PATH = Path.cwd() / "configs" / "tests" / "model_stream_responses.jsonc"
FIXTURE_ROOT = Path.cwd() / "tests" / "fixtures" / "model_stream"


def _model() -> BoxteamLiteLLMChatModel:
    return BoxteamLiteLLMChatModel(
        model="openai/big-pickle",
        api_key="test-key",
        api_base="https://opencode.ai/zen/v1",
        custom_llm_provider="openai",
        streaming=True,
    )


def _chat_tool_model() -> BoxteamLiteLLMChatModel:
    return BoxteamLiteLLMChatModel(
        model="openai/big-pickle",
        api_key="test-key",
        api_base="https://opencode.ai/zen/v1",
        custom_llm_provider="openai",
        provider_id="primary",
        streaming=True,
    )


def _sequence_model() -> BoxteamLiteLLMChatModel:
    return BoxteamLiteLLMChatModel(
        model="openai/test-model",
        api_key="test-key",
        api_base="https://provider.example/v1",
        custom_llm_provider="openai",
        streaming=True,
    )


async def _visible_text(model: BoxteamLiteLLMChatModel, prompt: str) -> str:
    chunks = []
    async for chunk in model.astream(prompt):
        content = chunk.content
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            chunks.extend(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
    return "".join(chunks)


async def _responses_reasoning_and_tool(
    model: BoxteamOpenAIResponsesModel,
) -> tuple[str, str]:
    reasoning: list[str] = []
    tool_names: list[str] = []
    async for chunk in model.astream(
        [
            SystemMessage(content="测试系统消息"),
            HumanMessage(content="请读取 README.md"),
        ]
    ):
        content = chunk.content
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "reasoning":
                    nested = block.get("content")
                    if isinstance(nested, list):
                        reasoning.extend(
                            item["text"]
                            for item in nested
                            if isinstance(item, dict)
                            and item.get("type") in {"reasoning_text", "text"}
                            and isinstance(item.get("text"), str)
                        )
                    elif isinstance(block.get("reasoning"), str):
                        reasoning.append(block["reasoning"])
        for tool_call in chunk.tool_call_chunks:
            name = tool_call.get("name")
            if isinstance(name, str) and name not in tool_names:
                tool_names.append(name)
    return "".join(reasoning), "".join(tool_names)


def _responses_model() -> BoxteamOpenAIResponsesModel:
    return BoxteamOpenAIResponsesModel(
        model="gpt-5.6-luna",
        api_key="test-key",
        api_base="https://www.cctq.ai/v1",
        custom_llm_provider="openai",
        provider_id="backup_3",
        responses_store=False,
        streaming=True,
    )


def _sequence_scenario() -> StreamScenario:
    interactions: list[object] = []
    for step, text in enumerate(("第一轮", "第二轮")):
        cassette = build_cassette(
            asset_id=f"sequence-{step}",
            url="https://provider.example/v1/chat/completions",
            model="test-model",
            frames=(
                data_frame(
                    {
                        "id": f"sequence-{step}",
                        "object": "chat.completion.chunk",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": text},
                                "finish_reason": None,
                            }
                        ],
                    }
                ),
                data_frame(
                    {
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": "stop"}
                        ]
                    }
                ),
                done_frame(),
            ),
        )
        interaction = dict(cassette.raw["interactions"][0])
        interaction["replay"] = {"sequence_id": "tool-loop", "step": step}
        interactions.append(interaction)
    raw = {
        "schema_version": 1,
        "kind": "model_stream_cassette",
        "metadata": {
            "source": "handwritten",
            "asset_id": "sequence-test",
            "protocol": "openai_chat_sse",
        },
        "interactions": interactions,
    }
    return StreamScenario(
        scenario_id="sequence-test",
        asset_path=Path("<memory>"),
        cassette=load_cassette_from_object(raw),
        business_assertion="sequence-text",
        raw=raw,
    )


@pytest.mark.asyncio
async def test_litellm_replay_preserves_provider_url_and_parser_chain() -> None:
    config = load_model_stream_config(CHAT_BASIC_CONFIG_PATH)
    controller = ModelStreamTransportController.install(config)
    if controller is None:
        raise RuntimeError("model stream replay controller 未安装")
    try:
        text = await _visible_text(_model(), "测试 LiteLLM replay")

        assert text == "基础回放"
        assert controller.transport.request_urls == (
            "https://opencode.ai/zen/v1/chat/completions",
        )
        assert controller.transport.hit_counts == {0: 1}
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_litellm_replay_is_safe_for_high_concurrency() -> None:
    config = load_model_stream_config(CHAT_BASIC_CONFIG_PATH)
    controller = ModelStreamTransportController.install(config)
    if controller is None:
        raise RuntimeError("model stream replay controller 未安装")
    try:
        results = await asyncio.gather(
            *(_visible_text(_model(), f"并发请求 {index}") for index in range(32))
        )

        assert results == ["基础回放"] * 32
        assert controller.transport.call_count == 32
        assert controller.transport.hit_counts == {0: 32}
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_litellm_chat_replay_parses_reasoning_and_tool_loop(
    tmp_path: Path,
) -> None:
    config = load_model_stream_config(CONFIG_PATH)
    controller = ModelStreamTransportController.install(config)
    if controller is None:
        raise RuntimeError("Chat tool model stream replay controller 未安装")
    try:
        initial_messages = [
            SystemMessage(content="测试系统消息"),
            HumanMessage(content="请读取 README.md"),
        ]
        first = await _chat_tool_model().ainvoke(initial_messages)
        assert first.tool_calls[0]["name"] == "read_file"
        assert first.tool_calls[0]["args"] == {"path": "README.md"}
        assert first.content[0]["type"] == "reasoning_content"
        assert first.content[0]["reasoning_content"] == (
            "先读取 README，再根据工具结果作答。"
        )

        second = await _chat_tool_model().ainvoke(
            [
                *initial_messages,
                first,
                ToolMessage(
                    content="README 测试结果",
                    tool_call_id=first.tool_calls[0]["id"],
                ),
            ]
        )
        assert second.content[0]["type"] == "reasoning_content"
        assert second.content[0]["reasoning_content"] == (
            "已读取 README，整理最终答复。"
        )
        assert second.content[1]["text"] == "工具调用完成"
        assert controller.transport.hit_counts == {0: 1, 1: 1}
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_litellm_responses_replay_uses_real_aresponses_parser() -> None:
    config = load_model_stream_config(RESPONSES_CONFIG_PATH)
    controller = ModelStreamTransportController.install(config)
    if controller is None:
        raise RuntimeError("Responses model stream replay controller 未安装")
    try:
        results = await asyncio.gather(
            *(
                _responses_reasoning_and_tool(_responses_model())
                for _ in range(12)
            )
        )

        assert results == [("先读取 README，再根据工具结果作答。", "read_file")] * 12
        assert controller.transport.request_urls == (
            "https://www.cctq.ai/v1/responses",
        ) * 12
        assert controller.transport.hit_counts == {0: 12}
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_different_cassettes_match_concurrently(tmp_path: Path) -> None:
    transports = [
        ModelStreamHTTPTransport(
            scenario=load_scenario(FIXTURE_ROOT, scenario_id),
            mode="replay",
            replay_policy="request_reusable",
            artifact_root=tmp_path / scenario_id,
        )
        for scenario_id in ("basic-text", "reasoning-stream")
    ]
    clients = [httpx.AsyncClient(transport=transport) for transport in transports]
    try:
        responses = await asyncio.gather(
            *(
                client.post(
                    "https://opencode.ai/zen/v1/chat/completions",
                    json={
                        "model": "big-pickle",
                        "stream": True,
                        "messages": [{"role": "user", "content": "测试"}],
                    },
                )
                for client in clients
            )
        )
        bodies = await asyncio.gather(*(response.aread() for response in responses))
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))

    assert "基础".encode() in bodies[0]
    assert "结论".encode() in bodies[1]
    assert [transport.hit_counts for transport in transports] == [{0: 1}, {0: 1}]


@pytest.mark.asyncio
async def test_transport_controller_restores_litellm_async_session() -> None:
    previous_session = litellm.aclient_session
    if previous_session is not None:
        raise RuntimeError("测试开始前 LiteLLM async session 已存在")
    config = load_model_stream_config(CONFIG_PATH)
    controller = ModelStreamTransportController.install(config)
    if controller is None:
        raise RuntimeError("model stream replay controller 未安装")

    assert litellm.aclient_session is controller.client
    await controller.aclose()
    assert litellm.aclient_session is previous_session


@pytest.mark.asyncio
async def test_litellm_session_sequence_isolated_for_concurrent_tool_loops(
    tmp_path: Path,
) -> None:
    previous_session = litellm.aclient_session
    if previous_session is not None:
        raise RuntimeError("测试开始前 LiteLLM async session 已存在")
    transport = ModelStreamHTTPTransport(
        scenario=_sequence_scenario(),
        mode="replay",
        replay_policy="session_sequence",
        artifact_root=tmp_path,
    )
    client = httpx.AsyncClient(transport=transport)
    litellm.aclient_session = client

    async def run_session(session_id: str) -> tuple[str, str]:
        with replay_session(session_id):
            model = _sequence_model()
            return (
                await _visible_text(model, "执行第一轮"),
                await _visible_text(model, "执行第二轮"),
            )

    try:
        results = await asyncio.gather(
            run_session("session-a"),
            run_session("session-b"),
        )
    finally:
        litellm.aclient_session = previous_session
        await client.aclose()

    assert results == [("第一轮", "第二轮"), ("第一轮", "第二轮")]
    assert transport.hit_counts == {0: 2, 1: 2}
