from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.integration.stubs.http_stubs import openai_chat_stub
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import close_backend_process, start_backend_process


@pytest.fixture(scope="module")
def activity_backend(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> Generator[tuple[str, Path], None, None]:
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    workspace_root = Path(integration_workspace_root_path).resolve()
    config_path = Path(integration_workspace_config_path)
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    primary_provider = next(
        provider for provider in config["llm"]["providers"] if provider["id"] == "primary"
    )
    primary_provider.update(
        {
            "endpoint": f"http://127.0.0.1:{port_block.port(10)}/v1",
            "model": "e2e-stub-model",
            "api_key": "e2e-local-model-key",
            "custom_llm_provider": "openai",
        }
    )
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with openai_chat_stub(port_block.port(10)):
        backend = start_backend_process(
            workspace_root=str(workspace_root),
            port=port_block.port(0),
            log_name="session-activity-backend",
        )
        try:
            yield f"http://127.0.0.1:{backend.port}", workspace_root
        finally:
            close_backend_process(backend)


@pytest.fixture
async def activity_client(
    activity_backend: tuple[str, Path],
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=activity_backend[0],
        headers={"X-Local-Token": "local-dev-token"},
        timeout=30,
    ) as client:
        yield client


async def _create_session(client: httpx.AsyncClient, title: str) -> str:
    response = await client.post("/api/v1/sessions", json={"title": title})
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["session_id"])


async def _run_message(
    client: httpx.AsyncClient,
    session_id: str,
    message: str,
) -> dict[str, object]:
    response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": message},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert response.status_code == 200, response.text
    return await _wait_for_job_terminal(client, str(response.json()["data"]["job_id"]))


async def _wait_for_job_terminal(
    client: httpx.AsyncClient,
    job_id: str,
) -> dict[str, object]:
    for _ in range(600):
        response = await client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        if data["status"] in {
            "completed",
            "succeeded",
            "failed",
            "cancelled",
            "timed_out",
        }:
            return data
        await asyncio.sleep(0.1)
    pytest.fail(f"Job 未在预期时间内结束: {job_id}")


@pytest.mark.asyncio
async def test_unopened_session_activity_is_recoverable_and_does_not_leak_content(
    activity_client: httpx.AsyncClient,
    activity_backend: tuple[str, Path],
) -> None:
    completed_session_id = await _create_session(activity_client, "未打开会话完成")
    failed_session_id = await _create_session(activity_client, "未打开会话失败")
    completed_job = await _run_message(
        activity_client,
        completed_session_id,
        "ACTIVITY_COMPLETE_PRIVATE_MESSAGE",
    )
    assert completed_job["status"] in {"completed", "succeeded"}

    state_db = activity_backend[1] / ".boxteam" / "state" / "workspace.sqlite"
    connection = sqlite3.connect(state_db, timeout=10)
    try:
        connection.execute(
            """
            INSERT INTO workspace_activity(
                event_id, session_id, status, summary, occurred_at
            ) VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (
                "event-session-failed",
                failed_session_id,
                "failed",
                "任务失败",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    response = await activity_client.get(
        "/api/v1/session-catalog/events",
        params={"after": 0, "limit": 100},
    )
    assert response.status_code == 200, response.text
    events = response.json()["data"]["items"]
    assert {
        completed_session_id,
        failed_session_id,
    } <= {str(event["session_id"]) for event in events}

    by_session = {str(event["session_id"]): event for event in events}
    assert by_session[completed_session_id]["status"] == "completed"
    assert by_session[failed_session_id]["status"] in {"failed", "cancelled"}
    for event in events:
        encoded = json.dumps(event, ensure_ascii=False)
        assert "PRIVATE_MESSAGE" not in encoded
        assert "tool_call" not in encoded
        assert "tool_result" not in encoded
        assert set(event) == {
            "event_seq",
            "event_id",
            "session_id",
            "status",
            "summary",
            "occurred_at",
        }

    first_cursor = int(events[0]["event_seq"])
    recovered = await activity_client.get(
        "/api/v1/session-catalog/events",
        params={"after": first_cursor, "limit": 100},
    )
    assert recovered.status_code == 200, recovered.text
    assert all(int(event["event_seq"]) > first_cursor for event in recovered.json()["data"]["items"])

    connection = sqlite3.connect(state_db)
    try:
        connection.execute("DELETE FROM workspace_activity")
        connection.commit()
    finally:
        connection.close()
    stale = await activity_client.get(
        "/api/v1/session-catalog/events",
        params={"after": first_cursor, "limit": 100},
    )
    assert stale.status_code == 410, stale.text
