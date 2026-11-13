from pathlib import Path

import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.schemas.internal_v2.goal import goal_public_view
from app.services.infrastructure.persistent_tasks.store import PersistentTaskStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from tests.support.goal_control import goal_control_payload


@pytest.mark.asyncio
async def test_goal_api_persistence_replace_events_and_clear(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    created = await client.post("/api/v1/sessions", json={"title": "Goal E2E"})
    session_id = created.json()["data"]["session_id"]
    main = created.json()["data"]["thread_id"]

    oversized = await client.put(
        f"/api/v1/sessions/{session_id}/goal",
        json=await goal_control_payload(client, session_id, {"objective": "x" * 4_001}),
    )
    assert oversized.status_code == 422

    response = await client.put(
        f"/api/v1/sessions/{session_id}/goal",
        json=await goal_control_payload(client, session_id, {"objective": "第一个目标", "status": "paused", "token_budget": 100}),
    )
    assert response.status_code == 200
    first = response.json()["data"]["goal"]
    assert first["status"] == "paused"
    assert first["thread_id"] == main
    assert (await client.get(f"/api/v1/sessions/{session_id}/goal")).json()["data"]["goal"] == first
    goal_path = (
        get_session_path_resolver(
            Path(e2e_workspace_root_path) / ".boxteam" / "sessions"
        ).resolve_session_node(session_id)
        / "goal.json"
    )
    assert not goal_path.exists()
    with WorkspaceStateStore(workspace_root=Path(e2e_workspace_root_path)) as state:
        resolver = get_session_path_resolver(Path(e2e_workspace_root_path) / ".boxteam" / "sessions")
        tasks = PersistentTaskStore(state, workspace_id=resolver.workspace_id, paths=resolver)
        assert goal_public_view(tasks.goal_read(session_id)).model_dump(mode="json") == first

    edited = await client.put(
        f"/api/v1/sessions/{session_id}/goal",
        json=await goal_control_payload(client, session_id, {"objective": "编辑后的目标"}),
    )
    assert edited.json()["data"]["goal"]["goal_id"] == first["goal_id"]

    replaced = await client.put(
        f"/api/v1/sessions/{session_id}/goal",
        json=await goal_control_payload(client, session_id, {"objective": "替换目标", "status": "paused", "replace": True}),
    )
    assert replaced.json()["data"]["goal"]["goal_id"] != first["goal_id"]
    assert replaced.json()["data"]["goal"]["tokens_used"] == 0

    traces = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert any(
        item["type"] == "goal_updated"
        for item in traces.json()["data"]["items"]
    )
    updates = [item for item in traces.json()["data"]["items"] if item["type"] == "goal_updated"]
    assert all(item["thread_id"] == item["raw"]["payload"]["thread_id"] == item["raw"]["payload"]["goal"]["thread_id"] == main for item in updates)

    messages = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages.json()["data"]["items"] == []

    cleared = await client.request("DELETE", f"/api/v1/sessions/{session_id}/goal", json=await goal_control_payload(client, session_id))
    assert cleared.json()["data"]["status"] == "applied"
    assert cleared.json()["data"]["thread_id"] == main
    assert (await client.get(f"/api/v1/sessions/{session_id}/goal")).json()[
        "data"
    ]["goal"] is None
    traces = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert any(
        item["type"] == "goal_cleared"
        for item in traces.json()["data"]["items"]
    )
    clears = [item for item in traces.json()["data"]["items"] if item["type"] == "goal_cleared"]
    assert all(item["thread_id"] == item["raw"]["payload"]["thread_id"] == main for item in clears)
