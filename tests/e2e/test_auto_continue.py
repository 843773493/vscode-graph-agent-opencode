#!/usr/bin/env python3
"""会话自动继续端到端测试。"""
from __future__ import annotations

import httpx
import pytest

from tests.e2e.utils import wait_for_job_done


@pytest.mark.asyncio
async def test_session_auto_continue_start_and_stop(client: httpx.AsyncClient):
    """开启自动继续后会自动发送“继续”，关闭后停止。"""
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Session Auto Continue Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    start_response = await client.post(
        f"/api/v1/sessions/{session_id}/auto-continue/start",
        json={"poll_interval_seconds": 0.2},
    )
    assert start_response.status_code == 200
    start_data = start_response.json()["data"]
    assert start_data["enabled"] is True
    assert start_data["task_status"] in {"pending", "running"}

    trigger_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": "请只回复：自动继续测试"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert trigger_response.status_code == 200
    trigger_job_id = trigger_response.json()["data"]["job_id"]
    await wait_for_job_done(client, trigger_job_id)

    stop_response = await client.post(f"/api/v1/sessions/{session_id}/auto-continue/stop")
    assert stop_response.status_code == 200
    stop_data = stop_response.json()["data"]
    assert stop_data["enabled"] is False
