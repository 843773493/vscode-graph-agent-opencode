from __future__ import annotations

import asyncio

import httpx
import pytest


async def wait_for_job_done(client: httpx.AsyncClient, job_id: str, max_attempts: int = 60) -> dict:
    """按轮查询 Job；max_attempts 是查询轮数，只有两轮之间等待一秒。"""
    for attempt in range(max_attempts):
        response = await client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        data = response.json()["data"]
        status = data["status"]

        if status in {"completed", "succeeded"}:
            return data
        if status in {"failed", "cancelled", "timed_out"}:
            pytest.fail(f"Job {job_id} 执行失败: {data.get('error_message')}")

        if attempt + 1 < max_attempts:
            await asyncio.sleep(1)

    pytest.fail(f"Job {job_id} 超时未完成")


async def wait_for_jobs_done_concurrently(
    client: httpx.AsyncClient,
    job_ids: list[str],
    max_attempts: int = 60,
) -> dict[str, dict]:
    """等待并发运行的多个 Job，按轮查询未完成项；本函数不调度 Job。"""
    pending = set(job_ids)
    results: dict[str, dict] = {}

    for attempt in range(max_attempts):
        for job_id in list(pending):
            response = await client.get(f"/api/v1/jobs/{job_id}")
            assert response.status_code == 200
            data = response.json()["data"]
            status = data["status"]

            if status in {"completed", "succeeded"}:
                results[job_id] = data
                pending.remove(job_id)
                continue
            if status in {"failed", "cancelled", "timed_out"}:
                pytest.fail(f"Job {job_id} 执行失败: {data.get('error_message')}")

        if not pending:
            return results
        if attempt + 1 < max_attempts:
            await asyncio.sleep(1)

    if pending:
        pytest.fail(f"Jobs 超时未完成: {sorted(pending)}")

    return results

