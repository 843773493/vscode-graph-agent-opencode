from __future__ import annotations

import asyncio
from asyncio import gather

import httpx
import pytest


async def wait_for_job_done(client: httpx.AsyncClient, job_id: str, max_attempts: int = 60) -> dict:
    """按轮查询 Job；max_attempts 是查询轮数，只有两轮之间等待一秒。"""
    for poll_round in range(max_attempts):
        response = await client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        job_data = response.json()["data"]
        status = job_data["status"]

        if status in {"completed", "succeeded"}:
            return job_data
        if status in {"failed", "cancelled", "timed_out"}:
            pytest.fail(f"Job {job_id} 执行失败: {job_data.get('error_message')}")

        if poll_round + 1 < max_attempts:
            await asyncio.sleep(1)

    pytest.fail(f"Job {job_id} 超时未完成")


async def wait_for_jobs_done_concurrently(
    client: httpx.AsyncClient,
    job_ids: list[str],
    max_attempts: int = 60,
) -> dict[str, dict]:
    """等待并发运行的多个 Job，每轮并发查询未完成项；本函数不调度 Job。"""
    unfinished_job_ids = set(job_ids)
    completed_job_records: dict[str, dict] = {}
    if not unfinished_job_ids:
        return completed_job_records

    for attempt_index in range(max_attempts):
        poll_batch = tuple(unfinished_job_ids)
        poll_responses = await gather(
            *(client.get(f"/api/v1/jobs/{job_id}") for job_id in poll_batch),
            return_exceptions=True,
        )
        for job_id, response_or_error in zip(poll_batch, poll_responses, strict=True):
            if isinstance(response_or_error, BaseException):
                raise response_or_error
            assert response_or_error.status_code == 200
            job_record = response_or_error.json()["data"]
            job_status = job_record["status"]

            if job_status in {"completed", "succeeded"}:
                completed_job_records[job_id] = job_record
                unfinished_job_ids.remove(job_id)
                continue
            if job_status in {"failed", "cancelled", "timed_out"}:
                pytest.fail(
                    f"Job {job_id} 执行失败: {job_record.get('error_message')}"
                )

        if not unfinished_job_ids:
            return completed_job_records
        if attempt_index + 1 < max_attempts:
            await asyncio.sleep(1)

    pytest.fail(f"Jobs 超时未完成: {sorted(unfinished_job_ids)}")

