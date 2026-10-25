"""验证 LiteLLM 包装层不截断 reasoning_content。

问题描述:
    当使用 opencode.ai (deepseek-v4-flash 后端) 时，模型会生成 reasoning tokens
    (放在 reasoning_content 字段)。LiteLLM 包装层必须把这些 tokens 转成
    LangChain 标准 content blocks。

    这导致:
    1. 新会话出现 "LLM 空响应" — 当模型所有 token budget 用于 reasoning 时
    2. 前端无法显示推理过程 — 即使 reasoning 内容丰富

测试环境要求:
    - 环境变量 OPENCODE_ZEN_API_KEY 必须设置
    - 网络可以访问 opencode.ai
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio

from app.agents.agent_factory import build_model_from_provider
from app.services.infrastructure.config_service import ConfigService

# 标记：真实 API 测试必须显式开启，避免本机残留凭据让单元测试访问网络。
pytestmark = pytest.mark.skipif(
    os.environ.get("BOXTEAM_RUN_LIVE_MODEL_TESTS") != "1"
    or os.environ.get("OPENCODE_ZEN_API_KEY") is None,
    reason="需要 BOXTEAM_RUN_LIVE_MODEL_TESTS=1 和 OPENCODE_ZEN_API_KEY",
)


@pytest.fixture(scope="module")
def config_service():
    """使用测试配置创建 ConfigService"""
    from tests.conftest import CONFIGS_DIR

    test_config_path = os.path.join(
        CONFIGS_DIR, "tests", "workspace", "default.jsonc"
    )
    yield ConfigService(
        config_dir=Path(CONFIGS_DIR),
        config_path=Path(test_config_path),
    )


@pytest.fixture(scope="module")
def live_reasoning_model(config_service):
    """真实 Provider 直连测试由构建入口显式标记用途，不伪造 Agent binding。"""
    runtime_config = config_service.get_agent_runtime_config("default")
    return build_model_from_provider(
        runtime_config["providers"][0], runtime_config, invocation_purpose="tool_test",
    )


@pytest.fixture(scope="module")
def live_reasoning_provider_config(live_reasoning_model):
    """返回模型配置信息"""
    model = live_reasoning_model
    model_name = model.model
    if isinstance(model_name, str) and model_name.startswith("openai/"):
        model_name = model_name.removeprefix("openai/")
    return {
        "model": model_name,
        "base_url": model.api_base or "",
        "api_key": model.api_key or "",
    }


# ---------------------------------------------------------------------------
# 测试：LiteLLM 包装层 astream 暴露 reasoning_content
# ---------------------------------------------------------------------------


async def _astream_chunks(model, messages):
    """收集 astream 的所有 chunk 和元数据"""
    chunks = []
    async for chunk in model.astream(messages):
        chunks.append(
            {
                "content": chunk.content,
                "content_blocks": chunk.content_blocks,
                "additional_kwargs": dict(chunk.additional_kwargs),
                "response_metadata": dict(chunk.response_metadata)
                if chunk.response_metadata
                else {},
            }
        )
    return chunks


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live_reasoning_stream_chunks(live_reasoning_model):
    """同一输入的流式检查共享一次真实 Provider 响应，避免重复请求。"""
    return await _astream_chunks(
        live_reasoning_model, [{"role": "user", "content": "你好"}]
    )


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content else ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _blocks_text(chunks, block_type: str, field: str) -> str:
    parts: list[str] = []
    for chunk in chunks:
        for block in chunk["content_blocks"]:
            if block.get("type") == block_type and isinstance(block.get(field), str):
                parts.append(block[field])
    return "".join(parts)


@pytest.mark.asyncio
async def test_astream_exposes_reasoning_content(live_reasoning_stream_chunks):
    """OpenAI-compatible provider 应通过 LiteLLM 包装层拿到 reasoning_content。"""
    chunks = live_reasoning_stream_chunks

    total = len(chunks)
    reasoning_chunks = [
        c
        for c in chunks
        if any(block.get("type") == "reasoning" for block in c["content_blocks"])
    ]
    text_chunks = [
        c
        for c in chunks
        if any(block.get("type") == "text" for block in c["content_blocks"])
    ]

    # 使用 LiteLLM 包装层后，应该能拿到标准 reasoning content block。
    assert len(reasoning_chunks) > 0, (
        f"BoxteamLiteLLMChatModel 应该暴露 reasoning，"
        f"但 0 个 chunk 含 type='reasoning'。总计 {total} 个 chunk"
    )

    # 有 reasoning 的 chunk 数量应该接近原始 API 的 reasoning chunk 数量
    print(f"\n[astream] 总 chunks: {total}")
    print(f"[astream] reasoning content blocks: {len(reasoning_chunks)}")
    print(f"[astream] text content blocks: {len(text_chunks)}")
    if text_chunks:
        combined = "".join(
            block.get("text", "")
            for c in text_chunks
            for block in c["content_blocks"]
            if block.get("type") == "text"
        )
        print(f"[astream] 合并后的 text content: {combined[:100]}...")
    if reasoning_chunks:
        reasoning_combined = "".join(
            block.get("reasoning", "")
            for c in reasoning_chunks
            for block in c["content_blocks"]
            if block.get("type") == "reasoning"
        )
        print(f"[astream] 合并后的 reasoning: {reasoning_combined[:100]}...")


# ---------------------------------------------------------------------------
# 测试：原始 OpenAI 客户端能正确获取 reasoning_content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_openai_client_exposes_reasoning(live_reasoning_provider_config):
    """验证：原始 OpenAI 客户端可以正确获取 reasoning_content

    这证明 API 本身返回了 reasoning，问题出在 LangChain 的封装上。
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=live_reasoning_provider_config["api_key"],
        base_url=live_reasoning_provider_config["base_url"],
    )

    messages = [{"role": "user", "content": "你好"}]

    stream = await client.chat.completions.create(
        model=live_reasoning_provider_config["model"],
        messages=messages,
        stream=True,
    )

    reasoning_parts = []
    content_parts = []
    total_chunks = 0

    async for chunk in stream:
        total_chunks += 1
        delta = chunk.choices[0].delta if chunk.choices else None
        if not delta:
            continue

        if getattr(delta, "reasoning_content", None):
            reasoning_parts.append(delta.reasoning_content)
        if getattr(delta, "content", None):
            content_parts.append(delta.content)

    # 断言：原始客户端能拿到 reasoning_content
    combined_reasoning = "".join(reasoning_parts)
    assert len(combined_reasoning) > 0, (
        "原始 OpenAI 客户端应该能拿到 reasoning_content，"
        "但返回了空字符串。请检查 API 是否支持 reasoning。"
    )

    # 断言：reasoning_content 有实质性内容（不只是空字符串）
    assert len(combined_reasoning) > 10, (
        f"reasoning_content 内容过短 ({len(combined_reasoning)} 字符)，"
        f"可能 API 行为已改变。content: {combined_reasoning!r}"
    )

    # 断言：也拿到了 content
    combined_content = "".join(content_parts)
    assert len(combined_content) > 0, "原始客户端也应该能拿到 content"

    print(f"\n[raw] 总 chunks: {total_chunks}")
    print(f"[raw] reasoning_content 长度: {len(combined_reasoning)}")
    print(f"[raw] reasoning_content 前100字: {combined_reasoning[:100]}")
    print(f"[raw] content 长度: {len(combined_content)}")
    print(f"[raw] content 前100字: {combined_content[:100]}")


# ---------------------------------------------------------------------------
# 测试：ainvoke 的完整响应中可以看到 reasoning_tokens 元数据
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ainvoke_shows_reasoning_in_metadata(live_reasoning_model):
    """ainvoke 不应依赖 provider 一定返回 usage metadata。"""
    messages = [{"role": "user", "content": "你好"}]
    result = await live_reasoning_model.ainvoke(messages)

    metadata = result.response_metadata
    token_usage = metadata.get("token_usage", {})
    completion_details = token_usage.get("completion_tokens_details", {})
    reasoning_tokens = completion_details.get("reasoning_tokens")

    custom_llm_provider = metadata.get("custom_llm_provider")
    if custom_llm_provider is not None:
        assert custom_llm_provider == "openai"
    if reasoning_tokens is not None:
        assert reasoning_tokens > 0, (
            f"reasoning_tokens 应该 > 0，但得到 {reasoning_tokens}。"
        )

    print(f"\n[ainvoke] reasoning_tokens: {reasoning_tokens}")
    print(f"[ainvoke] completion_tokens: {token_usage.get('completion_tokens')}")
    print(f"[ainvoke] total_tokens: {token_usage.get('total_tokens')}")
    print(f"[ainvoke] content: {_content_text(result.content)[:100]}")


# ---------------------------------------------------------------------------
# 测试：新会话场景 — 所有 token 被 reasoning 占用的边界情况
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_session_reasoning_only_scenario(live_reasoning_stream_chunks):
    """复现：新会话/简单问候时，所有 token 都用于 reasoning，导致空响应

    当模型在简单问候时决定把所有 token 预算用于 reasoning 时，
    astream 的所有 chunk 的 content 都是空字符串，导致前端显示 "LLM 空响应"。

    期望：即使 content 为空，也应该通过某种机制暴露 reasoning 内容
    实际：reasoning 被丢弃，content 为空，最终显示空响应
    """
    chunks = live_reasoning_stream_chunks

    combined_content = "".join(_content_text(c["content"]) for c in chunks)
    combined_reasoning = _blocks_text(chunks, "reasoning", "reasoning")

    assert combined_content.strip() or combined_reasoning.strip(), (
        "astream 至少应暴露可见正文或标准 reasoning content block，避免前端收到空响应"
    )

    print(f"\n[scenario] combined_reasoning: {combined_reasoning[:100]}")
    print(f"[scenario] combined_content: {combined_content[:100]}")


# ---------------------------------------------------------------------------
# 测试：对比实验 — 有 reasoning vs 无 reasoning 的 chunk 数量
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_vs_content_chunk_ratio(live_reasoning_stream_chunks):
    """定量分析：reasoning 被截断后，空 chunk 与有内容 chunk 的比例"""
    chunks = live_reasoning_stream_chunks

    total = len(chunks)
    empty = len([c for c in chunks if not c["content"]])
    with_content = len([c for c in chunks if c["content"]])

    # 记录比例（不硬性断言，作为诊断数据）
    ratio = empty / with_content if with_content > 0 else float("inf")

    print(f"\n[ratio] 总 chunks: {total}")
    print(f"[ratio] 空 content chunks: {empty}")
    print(f"[ratio] 有 content chunks: {with_content}")
    print(f"[ratio] 空/有内容 比例: {ratio:.2f}")
    print(f"[ratio] 空 chunk 占比: {empty / total * 100:.1f}%")

    # 这些 chunk 在 astream 中都以 content='' 的形式存在，
    # 实际上对应了 API 返回的 reasoning_content，但 LangChain 没有正确传递
