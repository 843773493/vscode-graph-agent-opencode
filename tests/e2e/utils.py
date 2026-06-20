from __future__ import annotations

import asyncio
import json
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


async def read_sse_events_until(
    response: httpx.Response,
    predicate,
    timeout_seconds: float = 30.0,
) -> list[dict]:
    """从 SSE 响应流中读取事件，直到 predicate(event) 为真或超时。

    返回的事件列表已按 SSE 顺序解析为 dict。
    """
    events: list[dict] = []
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    async for line in response.aiter_lines():
        if asyncio.get_running_loop().time() >= deadline:
            break

        line = line.strip()
        if not line or line.startswith(":") or line.startswith("event:"):
            continue

        if line.startswith("data:"):
            raw = line.removeprefix("data:").strip()
            if raw:
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue

        if events and predicate(events[-1]):
            break

    return events


def get_trace_payload(event: dict) -> dict:
    """从 TraceEventDTO 中提取 payload。

    后端 TraceEventDTO 把 payload 嵌套在 raw.payload 中。
    """
    raw = event.get("raw") or {}
    payload = raw.get("payload") or {}
    return payload if isinstance(payload, dict) else {}


