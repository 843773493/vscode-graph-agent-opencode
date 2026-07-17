from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from tests.e2e.utils import wait_for_job_done


async def wait_for_monitor_resource(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    max_attempts: int = 30,
) -> dict:
    for _ in range(max_attempts):
        response = await client.get(f"/api/v1/sessions/{session_id}/resources")
        assert response.status_code == 200
        resources = response.json()["data"]["items"]
        for resource in resources:
            if (
                resource["kind"] == "background_task"
                and resource["name"] == "monitor_session_agent_end"
            ):
                return resource
        await asyncio.sleep(1)

    pytest.fail("未在后台连接中看到 monitor_session_agent_end 后台任务")


async def load_agent_state_records(
    client: httpx.AsyncClient,
    session_id: str,
) -> list[dict]:
    response = await client.get(f"/api/v1/sessions/{session_id}/agent-state/messages")
    assert response.status_code == 200
    jsonl = response.json()["data"]["jsonl"]
    return [json.loads(line) for line in jsonl.splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_session_resource_api_lists_and_controls_model_created_background_task(
    client: httpx.AsyncClient,
):
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Session Resource Background Task Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    prompt = (
        "请直接调用 monitor_session_agent_end 工具创建一个持续后台监控任务。"
        "不要调用 python_exec，不要调用 test_tool，不要解释。"
        "工具参数必须是："
        f'target_session_id="{session_id}", '
        "timeout_seconds=120, poll_interval_seconds=0.2, max_events=null。"
        "工具调用完成后，只回复返回结果里的 task_id。"
    )
    message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": prompt},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert message_response.status_code == 200
    job_id = message_response.json()["data"]["job_id"]
    await wait_for_job_done(client, job_id, max_attempts=90)

    resources_response = await client.get(f"/api/v1/sessions/{session_id}/resources")
    assert resources_response.status_code == 200
    resources = resources_response.json()["data"]["items"]
    transient_job_resources = [
        resource
        for resource in resources
        if resource["kind"] == "job" and resource["resource_id"] == job_id
    ]
    assert not transient_job_resources, f"后台连接不应展示一次性 agent job: {resources}"

    monitor_resource = await wait_for_monitor_resource(client, session_id)
    assert monitor_resource["session_id"] == session_id
    assert monitor_resource["status"] in {"pending", "running"}
    assert "cancel" in monitor_resource["available_actions"]
    assert "delete" in monitor_resource["available_actions"]
    assert monitor_resource["metadata"]["target_session_id"] == session_id

    task_id = monitor_resource["resource_id"]
    cancel_response = await client.post(
        f"/api/v1/sessions/{session_id}/resources/background_task/{task_id}/control",
        json={"action": "cancel"},
    )
    assert cancel_response.status_code == 200
    cancel_data = cancel_response.json()["data"]
    assert cancel_data["status"] == "cancelled"
    assert cancel_data["resource"]["status"] == "cancelled"

    records = await load_agent_state_records(client, session_id)
    reminder_records = [
        record
        for record in records
        if record.get("role") == "user"
        and record.get("type") == "human"
        and "<system_reminder>" in str(record.get("content", ""))
    ]
    assert reminder_records, f"Agent State 未持久化后台连接取消 system_reminder: {records}"
    reminder = reminder_records[-1]
    assert "monitor_session_agent_end" in str(reminder.get("content", ""))
    assert task_id in str(reminder.get("content", ""))
    assert session_id in str(reminder.get("content", ""))
    metadata = reminder.get("response_metadata")
    assert isinstance(metadata, dict)
    assert metadata["source"] == "resource_cancel"
    assert metadata["user_initiated"] is True
    assert metadata["task_id"] == task_id
    assert metadata["task_name"] == "monitor_session_agent_end"

    delete_response = await client.post(
        f"/api/v1/sessions/{session_id}/resources/background_task/{task_id}/control",
        json={"action": "delete"},
    )
    assert delete_response.status_code == 200
    delete_data = delete_response.json()["data"]
    assert delete_data["status"] == "deleted"

    after_delete_response = await client.get(f"/api/v1/sessions/{session_id}/resources")
    assert after_delete_response.status_code == 200
    remaining_resources = after_delete_response.json()["data"]["items"]
    assert all(resource["resource_id"] != task_id for resource in remaining_resources)


@pytest.mark.asyncio
async def test_delete_session_cleans_background_tasks(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Session Delete Cleanup Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    start_response = await client.post(
        f"/api/v1/sessions/{session_id}/auto-continue/start",
        json={"poll_interval_seconds": 0.2},
    )
    assert start_response.status_code == 200

    resources_response = await client.get(f"/api/v1/sessions/{session_id}/resources")
    assert resources_response.status_code == 200
    resources = resources_response.json()["data"]["items"]
    assert any(resource["kind"] == "background_task" for resource in resources)

    delete_response = await client.delete(f"/api/v1/sessions/{session_id}")
    assert delete_response.status_code == 200
    delete_data = delete_response.json()["data"]
    assert delete_data["status"] == "deleted"
    assert delete_data["cleaned_background_tasks"] >= 1
    assert not (
        Path(e2e_workspace_root_path) / ".boxteam" / "sessions" / session_id
    ).exists()

    list_response = await client.get("/api/v1/sessions")
    assert list_response.status_code == 200
    sessions = list_response.json()["data"]["items"]
    assert all(session["session_id"] != session_id for session in sessions)
