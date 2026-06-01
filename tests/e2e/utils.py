from __future__ import annotations

import asyncio
import os

import httpx
import pytest


async def wait_for_job_done(client: httpx.AsyncClient, job_id: str, max_attempts: int = 60) -> dict:
    for _ in range(max_attempts):
        response = await client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        data = response.json()["data"]
        status = data["status"]

        if status in {"completed", "succeeded"}:
            return data
        if status in {"failed", "cancelled", "timed_out"}:
            pytest.fail(f"Job {job_id} 执行失败: {data.get('error_message')}")

        await asyncio.sleep(1)

    pytest.fail(f"Job {job_id} 超时未完成")


def last_assistant_message(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if content:
                return content
    pytest.fail("未找到非空 assistant 消息")


def normalize_text(text: str) -> str:
    normalized = text.strip()
    normalized = normalized.strip('"\'`。.!?！？：: ')
    return " ".join(normalized.split())


def requires_real_model() -> None:
    """真实模型依赖检查。

    这些 e2e 测试需要真实的模型/密钥环境；如果未配置，则直接跳过，
    避免 pytest discovery 阶段因为环境缺失而失败。
    """

    # TODO: 若后续项目统一了真实模型开关配置，这里改为读取统一配置来源。
    enabled = os.getenv("KILO_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("REAL_MODEL_TESTS")
    if enabled:
        return

    pytest.skip("未配置真实模型环境，跳过需要真实模型的 e2e 测试")
