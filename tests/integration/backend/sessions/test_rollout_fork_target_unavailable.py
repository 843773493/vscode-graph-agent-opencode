"""真实跨 Gateway 目标断连必须显式失败，不能改用本地目标。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3

import pytest

from app.core.resource_uri import parse_resource_uri, resource_display_uri
from tests.integration.backend.sessions import test_rollout_fork_transfer as transfer
from tests.integration.backend.sessions.test_rollout_fork_transfer import (
    transfer_triad as transfer_triad,  # noqa: PLC0414 - 复用真实三端 SSH/Gateway fixture
)
from tests.support.api_helpers import create_session
from tests.support.communication_federation import CommunicationNode
from tests.support.gateway_processes import close_gateway_process


def _catalog_facts(node: CommunicationNode) -> dict[str, object]:
    database = node.workspace / ".boxteam/navigation/session-catalog.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("nodes", "session_creation_records", "fork_source_captures")
        }


@pytest.mark.asyncio
async def test_authorized_remote_target_disconnect_has_no_local_fallback(transfer_triad):
    nodes, model, artifacts, _control, deadlines = transfer_triad
    source, target = nodes["b"], nodes["c"]
    model.response_callback = lambda payload: {"content": "用于验证固定远端目标的真实历史"}
    source_session = await create_session(source.client, "远端目标不可达来源")
    await transfer._send(source, source_session, "目标断连后不能复制到本地工作区")
    point_response = await source.client.get(
        f"/api/v1/sessions/{source_session}/fork-source-point", params={"mode": "full_rollout_copy"},
    )
    assert point_response.status_code == 200, point_response.text
    point = point_response.json()["data"]
    address = parse_resource_uri(point["source_uri"])
    request = {
        "idempotency_key": "authorized-target-before-disconnect",
        "source_uri": resource_display_uri(
            scope=address.scope, scope_id=address.scope_id, kind=address.kind,
            tail_segments=address.tail_segments, gateway_authority=source.gateway_id,
        ),
        "target_container_uri": resource_display_uri(
            scope="workspace", scope_id=target.workspace_id, kind="session",
            tail_segments=(), gateway_authority=target.gateway_id,
        ),
        "creation_kind": "new_session_main", "mode": "full_rollout_copy",
        "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
    }
    # 先通过真实 B→A→C 发布，证明同一来源/目标及权限在断连前实际可用。
    published_response = await transfer._post_fork(
        source, source_session, request, transfer._fork_deadline(target, request, deadlines),
        stage="prove_target_authorization_before_disconnect",
    )
    assert published_response.status_code == 200, published_response.text
    published = published_response.json()["data"]
    assert published["target_gateway_id"] == target.gateway_id
    assert published["session"]["workspace_id"] == target.workspace_id
    assert published["export_release"] == {"state": "confirmed", "error": None}
    before = {name: _catalog_facts(node) for name, node in nodes.items()}
    source_inventory = await source.client.get("/api/v1/session-catalog/export")
    assert source_inventory.status_code == 200, source_inventory.text
    provider_requests = len(model.requests)
    assert target.gateway is not None
    await asyncio.to_thread(close_gateway_process, target.gateway)
    assert target.gateway.process.poll() is not None
    assert target.backend.process.poll() is None
    # 观察真实 channel owner 已移除目标，而非靠固定等待猜测断连完成。
    async with asyncio.timeout(10):
        while any(peer == target.gateway_id for peer, _direction in nodes["a"].channels()):
            await asyncio.sleep(0.05)

    request = request | {"idempotency_key": "authorized-target-after-disconnect"}
    key = "fork-" + hashlib.sha256(request["idempotency_key"].encode()).hexdigest()
    deadline = transfer._fork_deadline(target, request, deadlines)
    response = await transfer._post_fork(
        source, source_session, request, deadline, stage="reject_disconnected_explicit_target",
    )
    (artifacts / "disconnected-target-response.json").write_text(response.text)
    assert response.status_code == 502, response.text
    assert "federation-channel-closed" in response.text
    assert target.gateway_id in response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    after = {name: _catalog_facts(node) for name, node in nodes.items()}
    assert after == before
    for node in nodes.values():
        assert transfer._capture_record(node, key) is None
        assert transfer._attachment_claims(node, key) == []
    after_inventory = await source.client.get("/api/v1/session-catalog/export")
    assert after_inventory.status_code == 200, after_inventory.text
    assert after_inventory.json()["data"] == source_inventory.json()["data"]
    assert len(model.requests) == provider_requests
    (artifacts / "disconnected-target-owner-facts.json").write_text(json.dumps({
        "request": request, "before": before, "after": after,
        "target_gateway_exit": target.gateway.process.returncode,
        "provider_request_count": provider_requests,
    }, ensure_ascii=False, indent=2) + "\n")
    # 已通过具体断连错误与三端落盘事实证明零副作用，才结清本次绝对期限。
    deadline.complete()
