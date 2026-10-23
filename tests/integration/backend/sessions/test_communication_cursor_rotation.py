"""真实产品分页在普通重启后保留，在显式管理轮换后明确过期。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict

import httpx
import pytest

from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414 — 复用真实产品进程 fixture
)
from tests.support.api_helpers import create_session


async def _message(case: CommunicationCase, session: str, text: str) -> None:
    response = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": text}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    await _completed(case.client, response.json()["data"]["job_id"])


async def _read(
    case: CommunicationCase, source: str, arguments: dict[str, object], name: str,
) -> str:
    first, final = await case.plan(source, [
        _tool("invoke_extension_tool", {"tool_name": "read_context", "arguments": arguments}, name),
        {"content": name + "已完成"},
    ])
    await _message(case, source, name)
    request = case.request(final)
    result = [message for message in request["messages"] if message["role"] == "tool"][-1]
    assert result["tool_call_id"] == name
    database = case.main_thread_node(source) / "rollout/index.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT status FROM item_catalog WHERE semantic_kind='tool_result' AND tool_call_id=?",
            (first + ":tool-call:" + name,),
        ).fetchall()
    assert rows == [("completed",)]
    return result["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["source", "target"])
async def test_management_rotation_expires_cursor_and_restart_preserves_snapshot(
    communication_case: CommunicationCase, side: str,
) -> None:
    case = communication_case
    source = await create_session(case.client, "轮换来源")
    target = await create_session(case.client, "轮换目标")
    await case.plan(target, [{"content": "rotation-original-answer"}])
    await _message(case, target, "rotation-original-question")
    arguments = {
        "resource_identity": str(session_resource_identity(case.backend.workspace_id, target)),
        "uri": session_resource_uri(case.backend.workspace_id, target),
        "scope": "workspace", "view": "messages", "limit": 1,
    }
    first = json.loads(await _read(case, source, arguments, "rotation-first"))
    assert first["has_more"] is True and first["next_cursor"].startswith("rc2.")
    assert "rotation-original-question" in json.dumps(first["items"])
    nodes = {"source": await case.session_node(source), "target": await case.session_node(target)}
    materials = {}
    for label, node in nodes.items():
        with closing(SessionControlStore(node / CONTROL_DATABASE_NAME)) as store:
            materials[label] = store.observation_cursor_material()
    await case.hard_restart()
    for label, node in nodes.items():
        with closing(SessionControlStore(node / CONTROL_DATABASE_NAME)) as store:
            assert store.observation_cursor_material() == materials[label]
    continued = json.loads(await _read(case, source, {
        **arguments, "cursor": first["next_cursor"],
    }, "rotation-after-restart"))
    assert continued["revision"] == first["revision"]
    assert "rotation-original-answer" in json.dumps(continued["items"])

    frozen = json.loads(await _read(case, source, arguments, "rotation-before-management"))
    node = nodes[side]
    session = source if side == "source" else target
    with closing(SessionControlStore(node / CONTROL_DATABASE_NAME)) as store:
        original = store.list_remote_observations()
        pages = tuple(store.list_remote_pages(row.observation_id, side=row.side) for row in original)
        leases = tuple(tuple(row) for row in store.connection.execute("SELECT * FROM session_operation_leases ORDER BY lease_id"))
        material = store.observation_cursor_material()
    path = f"/api/v1/sessions/{session}/communication-observations/rotate-cursor-key"
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{case.backend.port}", headers={
            "X-BoxTeam-Workspace-Id": case.backend.workspace_id,
            "X-BoxTeam-Gateway-Id": case.gateway_id,
        },
    ) as unauthorized:
        denied = await unauthorized.post(path)
    assert denied.status_code == 401, denied.text
    with closing(SessionControlStore(node / CONTROL_DATABASE_NAME)) as store:
        assert store.observation_cursor_material() == material
    rotated = await case.client.post(path)
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["data"] is None
    assert rotated.headers["X-Request-ID"] == rotated.json()["request_id"]
    assert not any(word in rotated.text for word in ("root_key", "generation", "cursor_key"))
    with closing(SessionControlStore(node / CONTROL_DATABASE_NAME)) as store:
        current = store.observation_cursor_material()
        assert current.root_key == material.root_key and current.generation != material.generation
        assert store.list_remote_observations() == original
        assert tuple(store.list_remote_pages(row.observation_id, side=row.side) for row in original) == pages
        assert tuple(tuple(row) for row in store.connection.execute("SELECT * FROM session_operation_leases ORDER BY lease_id")) == leases
    if side == "target":
        old_page, = [page for group in pages for page in group if page.page_ordinal == 1]
        envelope = {**old_page.request, "terminal_replay": True}
        replayed = await case.client.post("/api/gateway/federation/relay", json={
            "operation": "read", "target": envelope["target"], "body": envelope,
        })
        assert replayed.status_code == 409, replayed.text
        assert "read-snapshot-expired" in replayed.text
        assert "rotation-original-answer" not in replayed.text
    expired = await _read(case, source, {
        **arguments, "cursor": frozen["next_cursor"],
    }, "rotation-expired")
    assert "read-snapshot-expired" in expired and "read-snapshot-invalid" not in expired
    fresh = json.loads(await _read(case, source, arguments, "rotation-fresh"))
    last = json.loads(await _read(case, source, {
        **arguments, "cursor": fresh["next_cursor"],
    }, "rotation-fresh-next"))
    assert fresh["items"] == frozen["items"] and fresh["revision"] == frozen["revision"]
    assert fresh["next_cursor"] != frozen["next_cursor"]
    assert last["revision"] == fresh["revision"]
    assert "rotation-original-answer" in json.dumps(last["items"])
    with closing(SessionControlStore(node / CONTROL_DATABASE_NAME)) as store:
        for row, old_pages in zip(original, pages, strict=True):
            assert store.get_remote_observation(row.observation_id, side=row.side) == row
            assert store.list_remote_pages(row.observation_id, side=row.side) == old_pages
    case.record("rotation-actual", {
        "side": side, "first": first, "continued": continued, "expired": expired,
        "fresh": fresh, "last": last, "rotation_response": rotated.json(),
        "original_observations": [asdict(row) for row in original],
    })
