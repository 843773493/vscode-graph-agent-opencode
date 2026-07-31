from pathlib import Path

import httpx
import pytest

from app.core.path_utils import get_session_path_resolver


@pytest.mark.asyncio
async def test_goal_api_persistence_replace_events_and_clear(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    created = await client.post("/api/v1/sessions", json={"title": "Goal E2E"})
    session_id = created.json()["data"]["session_id"]

    oversized = await client.put(
        f"/api/v1/sessions/{session_id}/goal",
        json={"objective": "x" * 4_001},
    )
    assert oversized.status_code == 422

    response = await client.put(
        f"/api/v1/sessions/{session_id}/goal",
        json={"objective": "第一个目标", "status": "paused", "token_budget": 100},
    )
    assert response.status_code == 200
    first = response.json()["data"]
    assert first["status"] == "paused"
    goal_path = (
        get_session_path_resolver(
            Path(e2e_workspace_root_path) / ".boxteam" / "sessions"
        ).resolve_session_node(session_id)
        / "goal.json"
    )
    assert goal_path.is_file()

    edited = await client.put(
        f"/api/v1/sessions/{session_id}/goal",
        json={"objective": "编辑后的目标"},
    )
    assert edited.json()["data"]["goal_id"] == first["goal_id"]

    replaced = await client.put(
        f"/api/v1/sessions/{session_id}/goal",
        json={"objective": "替换目标", "status": "paused", "replace": True},
    )
    assert replaced.json()["data"]["goal_id"] != first["goal_id"]
    assert replaced.json()["data"]["tokens_used"] == 0

    traces = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert any(
        item["type"] == "goal_updated"
        for item in traces.json()["data"]["items"]
    )

    messages = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages.json()["data"]["items"] == []

    cleared = await client.delete(f"/api/v1/sessions/{session_id}/goal")
    assert cleared.json()["data"]["cleared"] is True
    assert (await client.get(f"/api/v1/sessions/{session_id}/goal")).json()[
        "data"
    ] is None
    traces = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert any(
        item["type"] == "goal_cleared"
        for item in traces.json()["data"]["items"]
    )
