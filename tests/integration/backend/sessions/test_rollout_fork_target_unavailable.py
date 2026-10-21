"""真实跨 Gateway 授权和断连必须显式失败，不能改用本地目标。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3

import commentjson
import pytest

from app.core.resource_uri import parse_resource_uri, resource_display_uri
from tests.integration.backend.sessions import test_rollout_fork_transfer as transfer
from tests.integration.backend.sessions.test_rollout_fork_transfer import (
    transfer_triad as transfer_triad,  # noqa: PLC0414 - 复用真实三端 SSH/Gateway fixture
)
from tests.support.api_helpers import create_session
from tests.support.communication_federation import CommunicationNode
from tests.support.processes.gateway import close_gateway_process


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
    source_session = await create_session(source.client, "远端目标不可达来源")
    await transfer._send(
        source, source_session, "目标断连后不能复制到本地工作区",
        model=model, reply="用于验证固定远端目标的真实历史",
    )
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
    provider_requests = transfer._provider_call_count(model)
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
    assert transfer._provider_call_count(model) == provider_requests
    (artifacts / "disconnected-target-owner-facts.json").write_text(json.dumps({
        "request": request, "before": before, "after": after,
        "target_gateway_exit": target.gateway.process.returncode,
        "provider_request_count": provider_requests,
    }, ensure_ascii=False, indent=2) + "\n")
    # 已通过具体断连错误与三端落盘事实证明零副作用，才结清本次绝对期限。
    deadline.complete()


async def _publish_fork_policy(node: CommunicationNode, policy: dict[str, object]) -> None:
    before = await node.client.get("/api/gateway/config/reload-status")
    assert before.status_code == 200, before.text
    assert node.gateway_home is not None
    config_path = node.gateway_home / "config/gateway.jsonc"
    config = commentjson.loads(config_path.read_text())
    config.setdefault("permissions", {})["federation"] = policy
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    async with asyncio.timeout(15):
        while True:
            response = await node.client.get("/api/gateway/config/reload-status")
            assert response.status_code == 200, response.text
            status = response.json()["data"]
            assert status["last_error"] is None, status
            if status["active_revision"] != before.json()["data"]["active_revision"]:
                return
            await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_remote_fork_rechecks_both_owner_permissions_without_business_side_effects(transfer_triad):
    nodes, model, artifacts, _control, deadlines = transfer_triad
    source, target = nodes["b"], nodes["c"]
    source_session = await create_session(source.client, "两端 fork 权限")
    await transfer._send(
        source, source_session, "仅有 read 权限不能授权 fork",
        model=model, reply="两端授权验收来源",
    )
    point_response = await source.client.get(
        f"/api/v1/sessions/{source_session}/fork-source-point", params={"mode": "full_rollout_copy"},
    )
    assert point_response.status_code == 200, point_response.text
    point = point_response.json()["data"]
    source_address = parse_resource_uri(point["source_uri"])
    request = {
        "source_uri": resource_display_uri(
            scope=source_address.scope, scope_id=source_address.scope_id, kind=source_address.kind,
            tail_segments=source_address.tail_segments, gateway_authority=source.gateway_id,
        ),
        "target_container_uri": resource_display_uri(
            scope="workspace", scope_id=target.workspace_id, kind="session",
            tail_segments=(), gateway_authority=target.gateway_id,
        ),
        "creation_kind": "new_session_main", "mode": "full_rollout_copy",
        "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
    }
    policy = {"default_effect": "allow", "rules": [], "hardening_enabled": False}
    receipts = []
    # 同一真实通道先拒绝目标创建，再拒绝来源导出；read 保持可用。
    for owner, origin, capability in (
        (target, source, "fork_target"), (source, target, "fork_source"),
    ):
        await _publish_fork_policy(owner, {**policy, "rules": [{
            "capability": capability, "effect": "deny", "origin_gateway_id": origin.gateway_id,
        }]})
        marker = owner.workspace / "fork-permission-proof.txt"
        marker.write_text(capability, encoding="utf-8")
        readable = await origin.client.post("/api/v1/resources/navigation", json={
            "uri": resource_display_uri(
                scope="workspace", scope_id=owner.workspace_id, kind="files",
                tail_segments=("root", marker.name), gateway_authority=owner.gateway_id,
            ),
            "operation": "read",
        })
        assert readable.status_code == 200, readable.text
        assert readable.json()["data"]["content"] == capability
        frozen = request | {"idempotency_key": f"reject-and-retry-{capability}"}
        key = "fork-" + hashlib.sha256(frozen["idempotency_key"].encode()).hexdigest()
        before = {name: _catalog_facts(node) for name, node in nodes.items()}
        provider_requests = transfer._provider_call_count(model)
        channels = {name: node.channels() for name, node in nodes.items()}
        deadline = transfer._fork_deadline(target, frozen, deadlines)
        denied = await transfer._post_fork(
            source, source_session, frozen, deadline, stage=f"reject_{capability}",
        )
        receipts.append({"capability": capability, "status": denied.status_code, "body": denied.json()})
        assert denied.status_code == 502, denied.text
        assert "authorization-revoked" in denied.text and capability in denied.text
        assert denied.headers["X-Request-ID"] == denied.json()["request_id"]
        after = {name: _catalog_facts(node) for name, node in nodes.items()}
        assert after == before
        assert transfer._provider_call_count(model) == provider_requests
        for node in nodes.values():
            assert transfer._capture_record(node, key) is None
            assert transfer._attachment_claims(node, key) == []
        assert {name: node.channels() for name, node in nodes.items()} == channels
        deadline.complete()
        await _publish_fork_policy(owner, policy)
        # 撤销恢复不重建 channel；原 key 可以从零副作用拒绝后正常发布。
        published = await transfer._post_fork(
            source, source_session, frozen, transfer._fork_deadline(target, frozen, deadlines),
            stage=f"retry_restored_{capability}",
        )
        assert published.status_code == 200, published.text
        result = published.json()["data"]
        assert result["target_gateway_id"] == target.gateway_id
        assert result["session"]["workspace_id"] == target.workspace_id
        assert result["export_release"] == {"state": "confirmed", "error": None}
        assert transfer._provider_call_count(model) == provider_requests
        receipts[-1].update({"before": before, "after": after, "restored_result": result})
    (artifacts / "fork-both-owner-permissions.json").write_text(
        json.dumps(receipts, ensure_ascii=False, indent=2) + "\n",
    )
