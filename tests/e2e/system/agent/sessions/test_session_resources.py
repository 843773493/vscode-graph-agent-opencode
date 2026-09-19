from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.support.api_waiters import wait_for_job_done


async def wait_for_background_task_resource(
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
                and resource["name"] == "emit_system_time_messages"
            ):
                return resource
        await asyncio.sleep(1)

    pytest.fail("未在后台连接中看到 emit_system_time_messages 后台任务")


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
        "请直接调用 emit_system_time_messages 工具创建一个持续后台任务。"
        "不要调用 python_exec，不要调用 test_tool，不要解释。"
        "工具参数必须是：interval_seconds=2, message_count=60。"
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

    task_resource = await wait_for_background_task_resource(client, session_id)
    assert task_resource["session_id"] == session_id
    assert task_resource["status"] in {"pending", "running"}
    assert "cancel" in task_resource["available_actions"]
    assert "delete" in task_resource["available_actions"]

    task_id = task_resource["resource_id"]
    cancel_response = await client.post(
        f"/api/v1/sessions/{session_id}/resources/background_task/{task_id}/control",
        json={"action": "cancel"},
    )
    assert cancel_response.status_code == 200
    cancel_data = cancel_response.json()["data"]
    assert cancel_data["status"] == "cancelled"
    assert cancel_data["resource"]["status"] == "cancelled"

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
