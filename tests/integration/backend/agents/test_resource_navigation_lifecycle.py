"""真实 Gateway 的激活、channel/owner 重连及权限变化不能重选文件目标。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import commentjson
import pytest

from app.core.resource_uri import resource_display_uri
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    _prepare_workspace,
)
from tests.support.communication.federation import open_communication_triad


@pytest.fixture
async def navigation_lifecycle(request):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir / request.node.name / uuid4().hex
    artifacts.mkdir(parents=True)
    async with open_communication_triad(context, _prepare_workspace,
        backend_asgi_app="tests.integration.backend.agents.resource_navigation_lifecycle_backend:app",
        backend_environment={"BOXTEAM_TEST_NAVIGATION_ARTIFACTS": str(artifacts)}) as nodes:
        try:
            yield nodes, artifacts
        finally:
            (artifacts / "release").touch()


async def _wait(predicate, seconds=15):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.02)


def _channel(node, gateway_id):
    with closing(sqlite3.connect((node.gateway_root / "federation/control.sqlite").as_uri() + "?mode=ro")) as database:
        return database.execute(
            "SELECT channel_instance_id FROM federation_channel_registry WHERE peer_gateway_id=?", (gateway_id,),
        ).fetchall()


async def test_navigation_keeps_target_across_activation_reconnect_and_read_failures(navigation_lifecycle):
    nodes, artifacts = navigation_lifecycle
    source, other, target = nodes["a"], nodes["b"], nodes["c"]
    names = ("a.txt", "b.txt", "lifecycle-probe.txt")
    for name, node in nodes.items():
        directory = node.workspace / "src/lifecycle"
        directory.mkdir()
        for filename in names:
            (directory / filename).write_text(name + " OWNER " + filename, encoding="utf-8")
    root = resource_display_uri(scope="workspace", scope_id=target.workspace_id, kind="files",
        tail_segments=("root", "src", "lifecycle"), gateway_authority=target.gateway_id)
    uri = root + "/lifecycle-probe.txt"
    evidence = []

    async def navigate(node, address, operation="read", status=200, **options):
        response = await node.client.post("/api/v1/resources/navigation", json={
            "uri": address, "operation": operation, **options})
        assert response.status_code == status, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        for member in nodes.values():
            assert str(member.workspace) not in response.text and str(member.gateway_home) not in response.text
        assert "local-dev-token" not in response.text and "federation_token" not in response.text
        body = response.json()
        evidence.append({"source": node.gateway_id, "status": status, "body": body})
        return body["data"] if status == 200 else body["detail"]

    first = await navigate(source, root, "list", limit=1)
    assert first["truncated"] and first["next_cursor"]
    cursor = first["next_cursor"]
    assert first["entries"][0]["uri"] == root + "/a.txt"
    pending = asyncio.create_task(navigate(source, uri))
    try:
        entered = artifacts / (target.workspace_id + "-entered.json")
        await _wait(entered.is_file)
        observed = json.loads(entered.read_text(encoding="utf-8"))
        assert observed == {"uri": uri.replace(target.gateway_id + "/", ""),
            "gateway_id": target.gateway_id, "workspace_id": target.workspace_id}
        registry = await source.client.get("/api/gateway/workspaces")
        assert registry.status_code == 200, registry.text
        routes = {row["backend_workspace_id"]: row["workspace_id"] for row in registry.json()["data"]["items"]}
        switched = await source.client.post(f"/api/gateway/workspaces/{routes[other.workspace_id]}/activate")
        assert switched.status_code == 200, switched.text
        assert switched.json()["data"]["active_workspace_id"] == routes[other.workspace_id]
        active = await source.client.get("/api/v1/workspace")
        assert active.status_code == 200 and active.json()["data"]["workspace_id"] == other.workspace_id
    finally:
        (artifacts / "release").touch()
        result = await pending
    assert result["node"]["uri"] == uri and result["content"] == "c OWNER lifecycle-probe.txt"

    # 新 channel 仍属于原 Gateway；旧 cursor 继续同一目录，而非当前激活的 B。
    channel_before = _channel(source, target.gateway_id)
    assert len(channel_before) == 1
    reconnected = await source.client.post(f"/api/gateway/workspaces/{routes[target.workspace_id]}/reconnect")
    assert reconnected.status_code == 200, reconnected.text
    await _wait(lambda: len(_channel(source, target.gateway_id)) == 1
        and _channel(source, target.gateway_id) != channel_before)
    second = await navigate(source, root, "list", limit=1, cursor=cursor)
    assert second["entries"][0]["uri"] == root + "/b.txt" and second["next_cursor"]
    other_root = root.replace(target.gateway_id, other.gateway_id).replace(target.workspace_id, other.workspace_id)
    rejected = await navigate(source, other_root, "list", status=409, limit=1, cursor=cursor)
    assert rejected["code"] == "federation-target-owner-rejected"
    assert rejected["detail"]["reason"] == "invalid_cursor"

    # 目标 backend 不可达时保留明确失败；原 owner 重开后原 URI/cursor 仍可用。
    await asyncio.to_thread(target.restart_backend, "kill")
    unavailable = await navigate(source, uri, status=409)
    assert unavailable["code"] == "federation-session-main-unavailable"
    await asyncio.to_thread(target.restart_backend, "start")
    third = await navigate(source, root, "list", limit=1, cursor=second["next_cursor"])
    assert third["entries"][0]["uri"] == uri and not third["truncated"]
    assert (await navigate(other, uri))["content"] == "c OWNER lifecycle-probe.txt"
    unsupported = await navigate(other, uri, "list", status=409)
    assert unsupported["code"] == "target_not_resolvable"

    config_path = target.gateway_home / "config/gateway.jsonc"
    original = config_path.read_text(encoding="utf-8")
    before = await target.client.get("/api/gateway/config/reload-status")
    assert before.status_code == 200, before.text
    config = commentjson.loads(original)
    config.setdefault("permissions", {}).setdefault("federation", {}).setdefault("rules", []).append({
        "capability": "read", "effect": "deny", "origin_gateway_id": other.gateway_id,
    })
    try:
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        async with asyncio.timeout(15):
            while True:
                reload = await target.client.get("/api/gateway/config/reload-status")
                assert reload.status_code == 200, reload.text
                current = reload.json()["data"]
                assert current["last_error"] is None, current
                if current["active_revision"] != before.json()["data"]["active_revision"]:
                    break
                await asyncio.sleep(0.1)
        revoked = await navigate(other, uri, status=409)
        assert revoked["code"] == "target_not_resolvable"
        assert (await navigate(source, uri))["content"] == "c OWNER lifecycle-probe.txt"
        assert len(_channel(other, source.gateway_id)) == 1
    finally:
        config_path.write_text(original, encoding="utf-8")
    for name, node in nodes.items():
        for filename in names:
            assert (node.workspace / "src/lifecycle" / filename).read_text(encoding="utf-8") == name + " OWNER " + filename
    (artifacts / "navigation-lifecycle.json").write_text(json.dumps({
        "requests": evidence, "entered": observed, "channel_before": channel_before,
        "channel_after": _channel(source, target.gateway_id),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
