from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Generator, Iterator
from pathlib import Path

import commentjson
import httpx
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.support.api_helpers import create_session
from tests.support.model_stream_config import ModelStreamReplay, model_stream_replay
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import (
    close_backend_process,
    start_backend_process,
    workspace_backend_id_for_root,
)


@pytest.fixture(scope="module")
def activity_replay(integration_workspace_root_path: str) -> Iterator[ModelStreamReplay]:
    output = Path(integration_workspace_root_path).parent
    with model_stream_replay(output, runtime_root=output / "runtime", business_assertion="session-activity") as replay:
        yield replay


@pytest.fixture(scope="module")
def activity_backend(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    activity_replay: ModelStreamReplay,
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
            "endpoint": "https://opencode.ai/zen/v1",
            "model": "e2e-stub-model",
            "api_key": "e2e-local-model-key",
            "custom_llm_provider": "openai",
        }
    )
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    backend = start_backend_process(
        workspace_root=str(workspace_root),
        port=port_block.port(0),
        log_name="session-activity-backend",
        asgi_app="tests.support.section8_backend:app",
        env_overrides=activity_replay.environment,
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
        headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": workspace_backend_id_for_root(str(activity_backend[1]))},
        timeout=30,
    ) as client:
        yield client


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
    activity_replay: ModelStreamReplay,
) -> None:
    completed_session_id = await create_session(activity_client, "未打开会话完成")
    failed_session_id = await create_session(activity_client, "未打开会话失败")
    session = await activity_client.get(f"/api/v1/sessions/{completed_session_id}/main-thread")
    assert session.status_code == 200, session.text
    thread_id = session.json()["data"]["thread_id"]
    cassette = build_cassette(
        asset_id="session-activity", model="e2e-stub-model",
        url="https://opencode.ai/zen/v1/chat/completions",
        frames=chat_completion_frames(
            response_id="chatcmpl-activity", model="e2e-stub-model",
            deltas=({"role": "assistant", "content": "activity completed"},),
            finish_reason="stop",
        ),
    )
    (call_id,) = activity_replay.control.plan_model_calls(completed_session_id, thread_id, cassette.raw["interactions"])
    completed_job = await _run_message(
        activity_client,
        completed_session_id,
        "ACTIVITY_COMPLETE_PRIVATE_MESSAGE",
    )
    assert completed_job["status"] in {"completed", "succeeded"}
    recorded = json.loads((activity_replay.artifacts / f"{call_id}.json").read_text(encoding="utf-8"))
    assert recorded["identity"]["session_id"] == completed_session_id
    assert recorded["identity"]["thread_id"] == thread_id

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
