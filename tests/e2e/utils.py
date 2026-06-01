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


async def wait_for_jobs_done_concurrently(
    client: httpx.AsyncClient,
    job_ids: list[str],
    max_attempts: int = 60,
) -> dict[str, dict]:
    pending = set(job_ids)
    results: dict[str, dict] = {}

    for _ in range(max_attempts):
        if not pending:
            return results

        finished_in_round: set[str] = set()
        for job_id in list(pending):
            response = await client.get(f"/api/v1/jobs/{job_id}")
            assert response.status_code == 200
            data = response.json()["data"]
            status = data["status"]

            if status in {"completed", "succeeded"}:
                results[job_id] = data
                finished_in_round.add(job_id)
                continue
            if status in {"failed", "cancelled", "timed_out"}:
                pytest.fail(f"Job {job_id} 执行失败: {data.get('error_message')}")

        pending -= finished_in_round
        await asyncio.sleep(1)

    if pending:
        pytest.fail(f"Jobs 超时未完成: {sorted(pending)}")

    return results


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


