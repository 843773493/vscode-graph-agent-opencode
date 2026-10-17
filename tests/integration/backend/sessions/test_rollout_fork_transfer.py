"""真实三端 Gateway/SSH fork；仅外部模型使用受控协议桩。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.core.identifier import create_prefixed_id
from app.core.resource_uri import (
    parse_resource_uri,
    parse_session_thread_uri,
    resource_display_uri,
    thread_resource_uri,
)
from app.schemas.internal_v2.session_fork_transfer import FORK_TRANSFER_CHUNK_BYTES
from app.services.business.session_context_resource import (
    workspace_session_inventory_uri,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    _completed,
    _prepare_workspace,
)
from tests.integration.stubs.http_stubs import HTTPStubState, openai_chat_stub
from tests.support.api_helpers import create_session
from tests.support.communication_federation import (
    CommunicationNode,
    open_communication_triad,
)
from tests.support.e2e_test_control import E2ETestControlHarness
from tests.support.ports import integration_port_block_for_file
from tests.support.session_navigation_http import submit_navigation_operation


@pytest.fixture
async def transfer_triad(request: pytest.FixtureRequest) -> AsyncIterator[
    tuple[dict[str, CommunicationNode], HTTPStubState, Path, E2ETestControlHarness]
]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    run_partition = context.runtime_root_for_node(request.node.nodeid).parent.name
    artifacts = context.artifacts_dir / run_partition / request.node.name
    artifacts.mkdir(parents=True, exist_ok=True)
    ports = integration_port_block_for_file(Path(request.node.path))
    control = E2ETestControlHarness(artifacts)
    control.start()
    try:
        with openai_chat_stub(ports.port(10)) as model:
            async with open_communication_triad(
                context, _prepare_workspace, backend_environment=control.environment,
                backend_asgi_app="tests.support.section8_backend:app",
                resource_service_nodes=("b",),
            ) as nodes:
                try:
                    yield nodes, model, artifacts, control
                finally:
                    (artifacts / "provider-wire.json").write_text(json.dumps(model.requests, ensure_ascii=False, indent=2) + "\n")
    finally:
        control.close()


async def _send(
    node: CommunicationNode, session_id: str, content: str, *, thread_id: str | None = None,
    attachments: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    address = f"/api/v1/sessions/{session_id}"
    if thread_id is not None:
        address += f"/threads/{thread_id}"
    message: dict[str, object] = {"content": content}
    if attachments is not None:
        message["attachments"] = attachments
    response = await node.client.post(
        address + "/messages",
        json={"message": message, "run": {"mode": "single_agent", "agent_id": "default"}},
    )
    assert response.status_code == 200, response.text
    if thread_id is not None:
        assert response.json()["data"]["thread_id"] == thread_id
    return await _completed(node.client, response.json()["data"]["job_id"])


@pytest.mark.asyncio
async def test_debug_configuration_choice_uses_source_owner_thread_across_gateway_routes(transfer_triad):
    nodes, model, _artifacts, _control = transfer_triad
    source, remote_target = nodes["b"], nodes["c"]
    for node in nodes.values():
        (node.workspace / "app.mjs").write_text("console.log('fork debug');\n", encoding="utf-8")

    model.response_callback = lambda payload: {"content": "来源会话已准备"}
    source_session_id = await create_session(source.client, "调试方案 fork 来源")
    await _send(source, source_session_id, "建立真实来源历史")
    created = await source.client.post("/api/v1/debug/node/configurations", json={
        "session_id": source_session_id,
        "name": "跨 Gateway 调试方案",
        "script_path": "app.mjs",
        "working_directory": "",
        "launch_profile_name": "node-default",
        "breakpoints": [{"path": "app.mjs", "line": 1}],
    })
    assert created.status_code == 200, created.text
    source_configuration_id = created.json()["data"]["active_configuration_id"]
    assert isinstance(source_configuration_id, str)

    point_response = await source.client.get(
        f"/api/v1/sessions/{source_session_id}/fork-source-point",
        params={"mode": "full_rollout_copy"},
    )
    assert point_response.status_code == 200, point_response.text
    point = point_response.json()["data"]
    _, _, source_thread_id = parse_session_thread_uri(point["source_uri"])
    assert source_thread_id is not None
    source_uri_owner = thread_resource_uri(
        source.workspace_id, source_session_id, source_thread_id,
        gateway_authority=source.gateway_id,
    )
    source_choice_uri = next(
        resource["resource_uri"] for resource in point["resources"]
        if resource["resource_uri"].endswith("/debug/node/configurations/" + source_configuration_id)
    )
    assert source_choice_uri.startswith(point["source_uri"] + "/debug/node/configurations/")
    source_session_uri = resource_display_uri(
        scope="workspace", scope_id=source.workspace_id, kind="session",
        tail_segments=(source_session_id,),
    )
    explicit_source_session_uri = resource_display_uri(
        scope="workspace", scope_id=source.workspace_id, kind="session",
        tail_segments=(source_session_id,), gateway_authority=source.gateway_id,
    )

    async def fork_debug(
        origin, target, *, source_uri: str, target_uri: str, idempotency_key: str,
    ) -> dict[str, object]:
        request = {
            "idempotency_key": idempotency_key,
            "source_uri": source_uri,
            "source_view_id": point["source_view_id"],
            "source_view_revision": point["source_view_revision"],
            "mode": "full_rollout_copy",
            "target_container_uri": target_uri,
            "creation_kind": "new_session_main",
            "title": idempotency_key,
            "resources": [{"resource_uri": source_choice_uri, "action": "copy_snapshot"}],
        }
        response = await origin.client.post(
            f"/api/v1/sessions/{source_session_id}/fork-context", json=request,
        )
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        assert result["source_uri"] == source_uri_owner
        target_thread_uri = result["thread_uri"]
        target_parsed, target_session_id, target_thread_id = parse_session_thread_uri(target_thread_uri)
        assert target_session_id == result["session"]["session_id"]
        assert target_thread_id == result["thread_id"]
        assert target_parsed.gateway_authority == target.gateway_id
        assert target_parsed.scope_id == target.workspace_id

        assert len(result["resources"]) == 1
        copied = result["resources"][0]
        assert copied["resource_uri"] == source_choice_uri.replace(
            point["source_uri"], source_uri_owner, 1,
        )
        assert copied["action"] == "copy_snapshot" and copied["availability"] == "available"
        assert copied["target_uri"].startswith(target_thread_uri + "/debug/node/configurations/")
        target_configuration_id = copied["target_uri"].rsplit("/", 1)[-1]
        configuration_response = await target.client.get(
            f"/api/v1/debug/node/configurations/{target_configuration_id}",
            params={"session_id": target_session_id, "thread_id": target_thread_id},
        )
        assert configuration_response.status_code == 200, configuration_response.text
        configuration = configuration_response.json()["data"]
        assert configuration["configuration_id"] == target_configuration_id
        assert configuration["name"] == "跨 Gateway 调试方案"
        assert configuration["script_path"] == "app.mjs"
        assert configuration["launch_profile_name"] == "node-default"
        assert configuration["breakpoints"][0]["line"] == 1

        database = await target.session_node(target_session_id)
        with sqlite3.connect((database / "rollout/index.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
            journal, = connection.execute(
                "SELECT state,proof_json FROM fork_debug_snapshots",
            ).fetchall()
        proof = json.loads(journal[1])
        assert journal[0] == "published"
        assert proof["target_configuration_id_map"] == [[source_configuration_id, target_configuration_id]]
        assert [item["configuration_id"] for item in proof["snapshot"]["configuration_artifacts"]] == [
            source_configuration_id,
        ]
        operation_id = "fork-" + hashlib.sha256(idempotency_key.encode()).hexdigest()
        catalog = target.workspace / ".boxteam/navigation/session-catalog.sqlite"
        with sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True) as connection:
            creation_intent, = connection.execute(
                "SELECT request_intent_json FROM session_creation_records WHERE session_id=?",
                (target_session_id,),
            ).fetchone()
            capture_request, = connection.execute(
                "SELECT request_json FROM fork_source_captures WHERE copy_operation_id=?",
                (operation_id,),
            ).fetchone()
        assert json.loads(creation_intent)["request"]["resources"] == request["resources"]
        assert json.loads(capture_request)["resources"] == [{
            "resource_uri": copied["resource_uri"], "action": "copy_snapshot",
        }]

        retry = await origin.client.post(
            f"/api/v1/sessions/{source_session_id}/fork-context", json=request,
        )
        assert retry.status_code == 200, retry.text
        repeated = retry.json()["data"]
        assert (repeated["session"]["session_id"], repeated["thread_id"], repeated["thread_uri"]) == (
            result["session"]["session_id"], result["thread_id"], result["thread_uri"],
        )
        assert repeated["resources"] == result["resources"]
        return result

    local_workspace_uri = workspace_session_inventory_uri(remote_target.workspace_id)
    remote_target_uri = workspace_session_inventory_uri(
        remote_target.workspace_id, gateway_authority=remote_target.gateway_id,
    )

    local_to_remote = await fork_debug(
        source, remote_target, source_uri=source_session_uri, target_uri=remote_target_uri,
        idempotency_key="debug-choice-local-source-remote-target",
    )
    remote_to_local = await fork_debug(
        remote_target, remote_target, source_uri=explicit_source_session_uri, target_uri=local_workspace_uri,
        idempotency_key="debug-choice-remote-source-local-target",
    )
    same_gateway_to_local = await fork_debug(
        source, source, source_uri=explicit_source_session_uri,
        target_uri=workspace_session_inventory_uri(source.workspace_id),
        idempotency_key="debug-choice-explicit-gateway-source-local-target",
    )
    assert local_to_remote["thread_uri"] != remote_to_local["thread_uri"]
    assert same_gateway_to_local["thread_uri"] != local_to_remote["thread_uri"]

    async def visible_session_ids(node) -> set[str]:
        response = await node.client.get("/api/v1/session-catalog/export")
        assert response.status_code == 200, response.text
        return {item["node_id"] for item in response.json()["data"]["items"]}

    visible_before_rejections = await visible_session_ids(source)
    unknown_configuration = source_choice_uri.rsplit("/", 1)[0] + "/dbgcfg_" + "0" * 32
    unknown_thread_uri = thread_resource_uri(
        source.workspace_id, source_session_id, create_prefixed_id("thr"),
        gateway_authority=source.gateway_id,
    )
    wrong_thread_choice = unknown_thread_uri + "/debug/node/configurations/" + source_configuration_id
    for key, choice_uri in (
        ("debug-choice-unknown-configuration-rejected", unknown_configuration),
        ("debug-choice-wrong-thread-rejected", wrong_thread_choice),
    ):
        rejected = await source.client.post(
            f"/api/v1/sessions/{source_session_id}/fork-context",
            json={
                "idempotency_key": key,
                "source_uri": explicit_source_session_uri,
                "source_view_id": point["source_view_id"],
                "source_view_revision": point["source_view_revision"],
                "mode": "full_rollout_copy",
                "target_container_uri": workspace_session_inventory_uri(source.workspace_id),
                "creation_kind": "new_session_main",
                "resources": [{"resource_uri": choice_uri, "action": "copy_snapshot"}],
            },
        )
        assert rejected.status_code == 400, rejected.text
        assert "unknown_fork_resource" in rejected.text
    assert await visible_session_ids(source) == visible_before_rejections

    resource_suffix = source_choice_uri[len(point["source_uri"]):]
    gateway_choice_uri = source_uri_owner + resource_suffix
    local_thread_uri = thread_resource_uri(source.workspace_id, source_session_id, source_thread_id)
    local_choice_uri = local_thread_uri + resource_suffix
    assert gateway_choice_uri != local_choice_uri
    conflicting_choices = (
        (
            "debug-choice-canonical-duplicate-copy-then-omit",
            [
                {"resource_uri": gateway_choice_uri, "action": "copy_snapshot"},
                {"resource_uri": local_choice_uri, "action": "omit"},
            ],
        ),
        (
            "debug-choice-canonical-duplicate-omit-then-copy",
            [
                {"resource_uri": local_choice_uri, "action": "omit"},
                {"resource_uri": gateway_choice_uri, "action": "copy_snapshot"},
            ],
        ),
        (
            "debug-choice-canonical-duplicate-copy-twice",
            [
                {"resource_uri": gateway_choice_uri, "action": "copy_snapshot"},
                {"resource_uri": local_choice_uri, "action": "copy_snapshot"},
            ],
        ),
    )
    for key, choices in conflicting_choices:
        rejected = await source.client.post(
            f"/api/v1/sessions/{source_session_id}/fork-context",
            json={
                "idempotency_key": key,
                "source_uri": explicit_source_session_uri,
                "source_view_id": point["source_view_id"],
                "source_view_revision": point["source_view_revision"],
                "mode": "full_rollout_copy",
                "target_container_uri": workspace_session_inventory_uri(source.workspace_id),
                "creation_kind": "new_session_main",
                "resources": choices,
            },
        )
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["detail"] == "duplicate_fork_resource_after_owner_normalization"
        assert await visible_session_ids(source) == visible_before_rejections


def _inline_attachment(body: bytes) -> list[dict[str, object]]:
    return [{
        "file_id": "inline-fork-transfer-body", "name": "跨端附件.txt", "content_type": "text/plain",
        "data_url": "data:text/plain;base64," + base64.b64encode(body).decode("ascii"),
    }]


def _attachment_claims(node: CommunicationNode, key: str) -> list[dict[str, object]]:
    database = node.workspace / ".boxteam/attachments/catalog.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM fork_attachment_claims WHERE copy_operation_id=? ORDER BY claim_id", (key,),
        ).fetchall()
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_real_b_to_c_full_copy_cold_read_and_new_input_with_source_offline(transfer_triad):
    nodes, model, artifacts, _ = transfer_triad
    source, target = nodes["b"], nodes["c"]
    original = "来源 B 冻结正文：跨 Gateway 原字节历史必须独立保留。"
    successor = "目标 C 首次新输入：继续使用已经导入的本地历史。"
    model.response_callback = lambda payload: {"content": "确定性生产 Provider 回应"}
    source_session_id = await create_session(source.client, "跨端来源 B")
    attachment_body = b"b" * FORK_TRANSFER_CHUNK_BYTES + b"c"
    await _send(source, source_session_id, original, attachments=_inline_attachment(attachment_body))
    source_objective = "跨端来源 Goal：验证实际用量清零后由用户决定继续。"

    def goal_reply(payload: dict[str, object]) -> dict[str, object]:
        messages = payload["messages"]
        if not any(message.get("role") == "tool" and message.get("name") == "update_goal" for message in messages):
            return {"tool_calls": [{
                "index": 0, "id": "call_fork_source_goal_complete", "type": "function",
                "function": {"name": "update_goal", "arguments": json.dumps({"status": "complete"})},
            }]}
        return {"content": "来源 Goal 已完成，保留真实计量。"}

    model.response_callback = goal_reply
    goal_response = await source.client.put(f"/api/v1/sessions/{source_session_id}/goal", json={
        "objective": source_objective, "token_budget": 500,
    })
    assert goal_response.status_code == 200, goal_response.text
    async with asyncio.timeout(60):
        while True:
            goal_response = await source.client.get(f"/api/v1/sessions/{source_session_id}/goal")
            assert goal_response.status_code == 200, goal_response.text
            source_goal = goal_response.json()["data"]
            if source_goal["status"] == "complete" and source_goal["tokens_used"] > 0:
                break
            await asyncio.sleep(0.05)
    await _completed(source.client, source_goal["last_accounted_job_id"])
    goal_response = await source.client.put(f"/api/v1/sessions/{source_session_id}/goal", json={"status": "paused"})
    assert goal_response.status_code == 200, goal_response.text
    source_goal = goal_response.json()["data"]
    assert source_goal["tokens_used"] > 0 and source_goal["accounted_jobs"]
    assert source_goal["status"] == "paused"
    (artifacts / "source-goal.json").write_text(goal_response.text)
    model.response_callback = lambda payload: {"content": "确定性生产 Provider 回应"}
    point_response = await source.client.get(
        f"/api/v1/sessions/{source_session_id}/fork-source-point", params={"mode": "full_rollout_copy"},
    )
    (artifacts / "source-point.json").write_text(point_response.text)
    assert point_response.status_code == 200, point_response.text
    point = point_response.json()["data"]
    assert point["goal_available"] is True
    assert len(point["resources"]) == 1
    attachment_source = point["resources"][0]
    assert attachment_source["availability"] == "available" and attachment_source["required_for_context"]
    source_uri = parse_resource_uri(point["source_uri"])
    request = {
        "source_uri": resource_display_uri(
            scope=source_uri.scope, scope_id=source_uri.scope_id, kind=source_uri.kind,
            tail_segments=source_uri.tail_segments, gateway_authority=source.gateway_id,
        ),
        "target_container_uri": workspace_session_inventory_uri(
            target.workspace_id, gateway_authority=target.gateway_id,
        ),
        "creation_kind": "new_session_main", "mode": "full_rollout_copy",
        "source_view_id": point["source_view_id"],
        "source_view_revision": point["source_view_revision"],
        "idempotency_key": "real-triad-attachment-and-goal-full-copy", "title": "真实目标 C",
        "resources": [{"resource_uri": attachment_source["resource_uri"], "action": "copy_snapshot"}],
        "goal": {"token_budget": 123},
    }
    (artifacts / "fork-request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n")
    provider_requests_before_fork = len(model.requests)
    child_request = {
        **request, "idempotency_key": "real-triad-child-goal-must-reject",
        "creation_kind": "new_child_thread",
        "target_container_uri": resource_display_uri(
            scope="workspace", scope_id=source.workspace_id, kind="session", tail_segments=(source_session_id,),
            gateway_authority=source.gateway_id,
        ),
    }
    child_threads_before = await source.client.get(f"/api/v1/sessions/{source_session_id}/child-threads")
    assert child_threads_before.status_code == 200, child_threads_before.text
    rejected_child = await source.client.post(f"/api/v1/sessions/{source_session_id}/fork-context", json=child_request)
    assert rejected_child.status_code == 422, rejected_child.text
    assert "Goal 只能在显式创建新 Session main 时继承" in rejected_child.text
    child_threads_after = await source.client.get(f"/api/v1/sessions/{source_session_id}/child-threads")
    assert child_threads_after.status_code == 200, child_threads_after.text
    assert child_threads_after.json()["data"] == child_threads_before.json()["data"]
    unchanged_source_goal = await source.client.get(f"/api/v1/sessions/{source_session_id}/goal")
    assert unchanged_source_goal.status_code == 200, unchanged_source_goal.text
    assert unchanged_source_goal.json()["data"] == source_goal
    assert len(model.requests) == provider_requests_before_fork
    response = await source.client.post(f"/api/v1/sessions/{source_session_id}/fork-context", json=request)
    (artifacts / "published-response.json").write_text(response.text)
    assert response.status_code == 200, response.text
    published = response.json()["data"]
    target_session_id = published["session"]["session_id"]
    assert target_session_id != source_session_id
    assert published["session"]["workspace_id"] == target.workspace_id
    target_uri = parse_resource_uri(published["thread_uri"])
    assert target_uri.gateway_authority == target.gateway_id
    assert target_uri.scope_id == target.workspace_id
    assert target_uri.tail_segments == (target_session_id, "threads", published["thread_id"])
    assert published["export_release"] == {"state": "confirmed", "error": None}
    assert len(model.requests) == provider_requests_before_fork
    assert len(published["resources"]) == 1
    attachment_result = published["resources"][0]
    assert attachment_result["resource_uri"] == attachment_source["resource_uri"]
    assert attachment_result["action"] == "copy_snapshot" and attachment_result["availability"] == "available"
    target_file_id = attachment_result["target_uri"]
    assert target_file_id.startswith(f"boxteam-session://{target_session_id}/attachments/")
    attachment_response = await target.client.get(f"/api/v1/sessions/{target_session_id}/attachments/content", params={
        "file_id": target_file_id, "variant": "original",
    })
    assert attachment_response.status_code == 200, attachment_response.text
    assert attachment_response.content == attachment_body
    key = "fork-" + hashlib.sha256(request["idempotency_key"].encode()).hexdigest()
    assert _attachment_claims(source, key) == []
    target_claims = _attachment_claims(target, key)
    assert len(target_claims) == 1 and target_claims[0]["state"] == "committed"
    assert (target_claims[0]["target_session_id"], target_claims[0]["target_thread_id"]) == (
        target_session_id, published["thread_id"],
    )
    goal_response = await target.client.get(f"/api/v1/sessions/{target_session_id}/goal")
    assert goal_response.status_code == 200, goal_response.text
    target_goal = goal_response.json()["data"]
    assert target_goal["goal_id"] == published["goal_id"] != source_goal["goal_id"]
    assert (target_goal["session_id"], target_goal["thread_id"], target_goal["objective"], target_goal["token_budget"]) == (
        target_session_id, published["thread_id"], source_objective, 123,
    )
    assert target_goal["status"] == "paused"
    assert target_goal["tokens_used"] == target_goal["time_used_seconds"] == 0
    assert target_goal["accounted_jobs"] == {}
    assert target_goal["last_accounted_job_id"] is target_goal["last_continued_job_id"] is None
    updated_objective = "目标 C 用户已改 Goal；重放不得覆盖。"
    goal_response = await target.client.put(f"/api/v1/sessions/{target_session_id}/goal", json={"objective": updated_objective})
    assert goal_response.status_code == 200, goal_response.text
    edited_goal = goal_response.json()["data"]
    assert edited_goal["goal_id"] == target_goal["goal_id"] and edited_goal["objective"] == updated_objective
    (artifacts / "edited-target-goal.json").write_text(goal_response.text)

    # 停止实际来源后端，再重启实际目标后端；只使用 harness 持有的进程。
    source_stop = source.restart_backend("kill")
    target_restart = target.restart_backend()
    assert source.backend.process.poll() is not None
    assert target_restart["before_pid"] != target_restart["after_pid"]
    (artifacts / "backend-lifecycle.json").write_text(json.dumps({
        "source_stop": source_stop, "target_restart": target_restart,
    }, ensure_ascii=False, indent=2) + "\n")
    bootstrap = await target.client.get(f"/api/v1/sessions/{target_session_id}/bootstrap")
    (artifacts / "cold-bootstrap.json").write_text(bootstrap.text)
    assert bootstrap.status_code == 200, bootstrap.text
    messages = await target.client.get(f"/api/v1/sessions/{target_session_id}/messages")
    (artifacts / "cold-messages.json").write_text(messages.text)
    assert messages.status_code == 200, messages.text
    assert original in messages.text
    attachment_response = await target.client.get(f"/api/v1/sessions/{target_session_id}/attachments/content", params={
        "file_id": target_file_id, "variant": "original",
    })
    assert attachment_response.status_code == 200, attachment_response.text
    assert attachment_response.content == attachment_body
    (artifacts / "cold-target-attachment.txt").write_bytes(attachment_response.content)

    retry = await target.client.post(f"/api/v1/sessions/{source_session_id}/fork-context", json=request)
    (artifacts / "published-retry.json").write_text(retry.text)
    assert retry.status_code == 200, retry.text
    repeated = retry.json()["data"]
    assert repeated["session"]["session_id"] == target_session_id
    assert repeated["thread_id"] == published["thread_id"]
    assert repeated["thread_uri"] == published["thread_uri"]
    assert repeated["export_release"] == published["export_release"]
    assert repeated["resources"] == published["resources"] and repeated["goal_id"] == published["goal_id"]
    goal_response = await target.client.get(f"/api/v1/sessions/{target_session_id}/goal")
    assert goal_response.status_code == 200, goal_response.text
    assert goal_response.json()["data"] == edited_goal
    assert _attachment_claims(target, key) == target_claims
    assert len(model.requests) == provider_requests_before_fork

    await _send(target, target_session_id, successor)
    assert len(model.requests) == provider_requests_before_fork + 1
    successor_wire = json.dumps(model.requests[provider_requests_before_fork:], ensure_ascii=False)
    assert original in successor_wire
    assert successor in successor_wire
    target_session_node = await target.session_node(target_session_id)
    deletion = await submit_navigation_operation(target.client, "delete_session", target_node_id=target_session_id)
    assert deletion["state"] == "committed", deletion
    assert not target_session_node.exists()
    deleted_claims = _attachment_claims(target, key)
    assert [claim["claim_id"] for claim in deleted_claims] == [claim["claim_id"] for claim in target_claims]
    assert all(claim["state"] == "target_deleted" for claim in deleted_claims)
    database = target.workspace / ".boxteam/attachments/catalog.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT state FROM attachment_owner_refs WHERE owner_ref_id=?",
            (target_claims[0]["target_owner_ref_id"],),
        ).fetchall() == [("released",)]
    (artifacts / "target-deletion.json").write_text(json.dumps(deletion, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_real_source_child_fork_cold_history_attachment_and_successor(
    transfer_triad, creation_kind: str,
):
    nodes, model, artifacts, _control = transfer_triad
    source, target = nodes["b"], nodes["c"]
    inherited = "来源 B 主线程的共同历史。"
    child_only = "来源 B child 独有输入：目标必须继承这个 thread。"
    main_only = "来源 B main 后来的独有输入：不能混入 child 分支。"
    successor = "目标 C 沿来源 child 历史继续一次。"
    model.response_callback = lambda _payload: {"content": "来源 child 验收固定回答"}
    source_session_id = await create_session(source.client, "来源 B child 验收")
    await _send(source, source_session_id, inherited)
    main_response = await source.client.get(
        f"/api/v1/sessions/{source_session_id}/fork-source-point", params={"mode": "full_rollout_copy"},
    )
    assert main_response.status_code == 200, main_response.text
    main_point = main_response.json()["data"]
    child_response = await source.client.post(f"/api/v1/sessions/{source_session_id}/fork-context", json={
        "idempotency_key": "prepare-real-source-child",
        "source_uri": main_point["source_uri"], "target_container_uri": main_point["child_container_uri"],
        "source_view_id": main_point["source_view_id"], "source_view_revision": main_point["source_view_revision"],
        "creation_kind": "new_child_thread", "mode": "full_rollout_copy",
    })
    assert child_response.status_code == 200, child_response.text
    source_child = child_response.json()["data"]
    source_thread_id = source_child["thread_id"]
    assert source_child["session"]["session_id"] == source_session_id
    assert parse_resource_uri(source_child["thread_uri"]).tail_segments == (source_session_id, "threads", source_thread_id)
    attachment_body = b"c" * FORK_TRANSFER_CHUNK_BYTES + b"d"
    await _send(source, source_session_id, child_only, thread_id=source_thread_id, attachments=_inline_attachment(attachment_body))
    await _send(source, source_session_id, main_only)
    point_response = await source.client.get(
        f"/api/v1/sessions/{source_session_id}/fork-source-point",
        params={"mode": "full_rollout_copy", "thread_id": source_thread_id},
    )
    assert point_response.status_code == 200, point_response.text
    point = point_response.json()["data"]
    (artifacts / "source-child-point.json").write_text(point_response.text)
    source_address = parse_resource_uri(point["source_uri"])
    assert source_address.tail_segments == (source_session_id, "threads", source_thread_id)
    assert len(point["resources"]) == 1
    attachment_source = point["resources"][0]
    assert attachment_source["availability"] == "available" and attachment_source["required_for_context"]
    target_parent = await create_session(target.client, "已有目标 C child") if creation_kind == "new_child_thread" else None
    child_session_node = await target.session_node(target_parent) if target_parent is not None else None
    request = {
        "idempotency_key": f"real-source-child-{creation_kind}",
        "source_uri": resource_display_uri(
            scope=source_address.scope, scope_id=source_address.scope_id, kind=source_address.kind,
            tail_segments=source_address.tail_segments, gateway_authority=source.gateway_id,
        ),
        "target_container_uri": resource_display_uri(
            scope="workspace", scope_id=target.workspace_id,
            kind="session",
            tail_segments=(target_parent,) if target_parent is not None else (), gateway_authority=target.gateway_id,
        ),
        "creation_kind": creation_kind, "mode": "full_rollout_copy",
        "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
        "resources": [{"resource_uri": attachment_source["resource_uri"], "action": "copy_snapshot"}],
    }
    (artifacts / "source-child-fork-request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n")
    provider_requests_before_fork = len(model.requests)
    response = await target.client.post(f"/api/v1/sessions/{source_session_id}/fork-context", json=request)
    (artifacts / "source-child-fork-response.json").write_text(response.text)
    assert response.status_code == 200, response.text
    published = response.json()["data"]
    assert len(model.requests) == provider_requests_before_fork
    assert published["export_release"] == {"state": "confirmed", "error": None}
    assert published["source_uri"] == request["source_uri"]
    assert published["source_view_id"] == point["source_view_id"]
    assert published["source_view_revision"] == point["source_view_revision"]
    target_session_id, target_thread_id = published["session"]["session_id"], published["thread_id"]
    if target_parent is not None:
        assert target_session_id == target_parent
    target_address = parse_resource_uri(published["thread_uri"])
    assert (target_address.gateway_authority, target_address.scope_id, target_address.tail_segments) == (
        target.gateway_id, target.workspace_id, (target_session_id, "threads", target_thread_id),
    )
    key = "fork-" + hashlib.sha256(request["idempotency_key"].encode()).hexdigest()
    source_capture = _capture_record(source, key)
    target_capture = _capture_record(target, key)
    assert source_capture is not None and target_capture is not None
    assert source_capture["source_thread_id"] == target_capture["source_thread_id"] == source_thread_id
    assert source_capture["state"] == target_capture["state"] == "materialized"
    assert target_capture["target_thread_id"] == target_thread_id
    frozen = _creation_record(target, key, child_session_node)
    assert frozen["state"] == "published"
    claims = _attachment_claims(target, key)
    assert len(claims) == 1 and claims[0]["state"] == "committed"
    assert (claims[0]["target_session_id"], claims[0]["target_thread_id"]) == (target_session_id, target_thread_id)
    assert _attachment_claims(source, key) == []
    assert len(published["resources"]) == 1
    target_file_id = published["resources"][0]["target_uri"]
    assert target_file_id.startswith(f"boxteam-session://{target_session_id}/attachments/")
    lifecycle = {"source_offline": source.restart_backend("kill"), "target_cold_restart": target.restart_backend()}
    assert lifecycle["target_cold_restart"]["before_pid"] != lifecycle["target_cold_restart"]["after_pid"]
    (artifacts / "source-child-backend-lifecycle.json").write_text(json.dumps(lifecycle, ensure_ascii=False, indent=2) + "\n")
    bootstrap = await target.client.get(f"/api/v1/sessions/{target_session_id}/bootstrap")
    assert bootstrap.status_code == 200, bootstrap.text
    if target_parent is not None:
        history = await target.client.post(
            f"/api/v1/sessions/{target_session_id}/threads/{target_thread_id}/history", json={},
        )
    else:
        history = await target.client.get(f"/api/v1/sessions/{target_session_id}/messages")
    (artifacts / "source-child-cold-history.json").write_text(history.text)
    assert history.status_code == 200, history.text
    assert inherited in history.text and child_only in history.text and main_only not in history.text
    if target_parent is not None:
        assert history.json()["data"]["thread_id"] == target_thread_id
    attachment = await target.client.get(f"/api/v1/sessions/{target_session_id}/attachments/content", params={
        "file_id": target_file_id, "variant": "original",
    })
    assert attachment.status_code == 200, attachment.text
    assert attachment.content == attachment_body
    replay = await target.client.post(f"/api/v1/sessions/{source_session_id}/fork-context", json=request)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"] == published
    assert _creation_record(target, key, child_session_node) == frozen
    assert _attachment_claims(target, key) == claims
    assert len(model.requests) == provider_requests_before_fork
    await _send(target, target_session_id, successor, thread_id=target_thread_id)
    assert len(model.requests) == provider_requests_before_fork + 1
    successor_wire = json.dumps(model.requests[provider_requests_before_fork:], ensure_ascii=False)
    (artifacts / "source-child-successor-wire.json").write_text(successor_wire)
    assert inherited in successor_wire and child_only in successor_wire and successor in successor_wire
    assert main_only not in successor_wire


def _creation_record(node: CommunicationNode, key: str, child_session_node: Path | None) -> dict[str, object]:
    if child_session_node is None:
        database = node.workspace / ".boxteam/navigation/session-catalog.sqlite"
        table, key_column = "session_creation_records", "session_creation_idempotency_key"
    else:
        database = child_session_node / "session-control.sqlite"
        table, key_column = "thread_creation_records", "thread_creation_idempotency_key"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(f"SELECT * FROM {table} WHERE {key_column}=?", (key,)).fetchall()
    assert len(rows) == 1
    return dict(rows[0])


def _capture_record(node: CommunicationNode, key: str) -> dict[str, object] | None:
    database = node.workspace / ".boxteam/navigation/session-catalog.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM fork_source_captures WHERE copy_operation_id=?", (key,)).fetchone()
    return dict(row) if row is not None else None


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
@pytest.mark.parametrize("failure_window", ["prepared_before_capture", "source_release_reply_lost"])
async def test_real_fork_delivery_trace_survives_owner_failure(
    transfer_triad, creation_kind: str, failure_window: str,
):
    nodes, model, artifacts, control = transfer_triad
    source, target = nodes["b"], nodes["c"]
    original = "来源 B 的真实输入：恢复后正文必须继续由 C 本地读取。"
    successor = "目标 C 的新输入：使用原分支 thread 的继承历史。"
    model.response_callback = lambda payload: {"content": "确定性恢复验收回应"}
    source_session_id = await create_session(source.client, "故障验收来源 B")
    attachment_body = b"x" * FORK_TRANSFER_CHUNK_BYTES + b"y"
    await _send(source, source_session_id, original, attachments=_inline_attachment(attachment_body))
    point_response = await source.client.get(
        f"/api/v1/sessions/{source_session_id}/fork-source-point", params={"mode": "full_rollout_copy"},
    )
    assert point_response.status_code == 200, point_response.text
    (artifacts / "source-point.json").write_text(point_response.text)
    point = point_response.json()["data"]
    assert len(point["resources"]) == 1
    attachment_source = point["resources"][0]
    assert attachment_source["availability"] == "available" and attachment_source["required_for_context"]
    source_address = parse_resource_uri(point["source_uri"])
    child_session_id = None
    child_session_node = None
    if creation_kind == "new_child_thread":
        child_session_id = await create_session(target.client, "已有目标 C")
        child_session_node = await target.session_node(child_session_id)
    request = {
        "source_uri": resource_display_uri(
            scope=source_address.scope, scope_id=source_address.scope_id, kind=source_address.kind,
            tail_segments=source_address.tail_segments, gateway_authority=source.gateway_id,
        ),
        "target_container_uri": resource_display_uri(
            scope="workspace", scope_id=target.workspace_id,
            kind="session",
            tail_segments=(child_session_id,) if child_session_id is not None else (),
            gateway_authority=target.gateway_id,
        ),
        "creation_kind": creation_kind, "mode": "full_rollout_copy",
        "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
        "idempotency_key": f"trace-{failure_window}-{creation_kind}", "title": "冻结 Trace 的目标 C",
        "resources": [{"resource_uri": attachment_source["resource_uri"], "action": "copy_snapshot"}],
    }
    (artifacts / "fork-request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n")
    key = "fork-" + hashlib.sha256(request["idempotency_key"].encode()).hexdigest()
    first_request_id = f"req-first-{failure_window}-{creation_kind}"
    phase = "copy.target.prepared" if failure_window == "prepared_before_capture" else "copy.source.released"
    control.arm(key, phase)
    provider_requests_before_fork = len(model.requests)
    fork_call = asyncio.create_task(target.client.post(
        f"/api/v1/sessions/{source_session_id}/fork-context", json=request,
        headers={"X-Request-ID": first_request_id},
    ))
    reached = asyncio.create_task(asyncio.to_thread(control.acknowledged, key, phase))
    done, _ = await asyncio.wait((fork_call, reached), return_when=asyncio.FIRST_COMPLETED)
    if fork_call in done:
        early_response = await fork_call
        (artifacts / "response-before-failure-window.json").write_text(early_response.text)
        reached.cancel()
        pytest.fail(f"fork 在故障窗口前返回 HTTP {early_response.status_code}: {early_response.text}")
    await reached
    frozen = _creation_record(target, key, child_session_node)
    (artifacts / "creation-before-failure.json").write_text(json.dumps(frozen, ensure_ascii=False, indent=2) + "\n")
    assert frozen["fork_delivery_request_id"] == first_request_id
    assert frozen["request_intent_json"] is not None
    lifecycle = {}
    if failure_window == "prepared_before_capture":
        assert frozen["state"] == "preparing"
        assert _capture_record(target, key) is None
        lifecycle["target_kill"] = target.restart_backend("kill")
        response = await asyncio.wait_for(fork_call, timeout=30)
        assert response.status_code >= 400, response.text
        (artifacts / "interrupted-response.json").write_text(response.text)
        control.release(key, phase)
        lifecycle["target_start"] = target.restart_backend("start")
        assert lifecycle["target_kill"]["before_pid"] != lifecycle["target_start"]["after_pid"]
    else:
        assert frozen["state"] == "published"
        released = _capture_record(source, key)
        assert released is not None
        assert released["state"] == "materialized"
        assert not (source.workspace / ".boxteam/sessions" / released["snapshot_relative_locator"]).exists()
        (artifacts / "source-release-before-lost-reply.json").write_text(json.dumps(released, ensure_ascii=False, indent=2) + "\n")
        lifecycle["source_kill_after_release"] = source.restart_backend("kill")
        control.release(key, phase)
        response = await asyncio.wait_for(fork_call, timeout=30)
        (artifacts / "pending-release-response.json").write_text(response.text)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["export_release"]["state"] == "pending"
        assert response.json()["data"]["export_release"]["error"]
        lifecycle["source_start"] = source.restart_backend("start")
        lifecycle["target_release_recovery"] = target.restart_backend()
        assert lifecycle["target_release_recovery"]["before_pid"] != lifecycle["target_release_recovery"]["after_pid"]

    retry = await target.client.post(
        f"/api/v1/sessions/{source_session_id}/fork-context", json=request,
        headers={"X-Request-ID": "req-new-http-delivery-after-recovery"},
    )
    (artifacts / "recovered-response.json").write_text(retry.text)
    assert retry.status_code == 200, retry.text
    published = retry.json()["data"]
    assert retry.headers["x-request-id"] == "req-new-http-delivery-after-recovery"
    assert published["session"]["workspace_id"] == target.workspace_id
    assert published["export_release"] == {"state": "confirmed", "error": None}
    target_session_id = published["session"]["session_id"]
    thread_field = "child_thread_id" if child_session_id is not None else "main_thread_id"
    assert published["thread_id"] == frozen[thread_field]
    assert target_session_id == (child_session_id if child_session_id is not None else frozen["session_id"])
    address = parse_resource_uri(published["thread_uri"])
    assert address.gateway_authority == target.gateway_id
    assert address.scope_id == target.workspace_id
    assert address.tail_segments == (target_session_id, "threads", published["thread_id"])
    identity_fields = ("preimage_hash", "request_intent_json", "fork_delivery_request_id", thread_field)
    recovered = _creation_record(target, key, child_session_node)
    assert recovered["state"] == "published"
    assert {name: recovered[name] for name in identity_fields} == {name: frozen[name] for name in identity_fields}
    assert len(model.requests) == provider_requests_before_fork
    assert len(published["resources"]) == 1
    attachment_result = published["resources"][0]
    assert attachment_result["resource_uri"] == attachment_source["resource_uri"]
    assert attachment_result["action"] == "copy_snapshot" and attachment_result["availability"] == "available"
    target_file_id = attachment_result["target_uri"]
    assert target_file_id.startswith(f"boxteam-session://{target_session_id}/attachments/")
    assert _attachment_claims(source, key) == []
    target_claims = _attachment_claims(target, key)
    assert len(target_claims) == 1 and target_claims[0]["state"] == "committed"
    assert (target_claims[0]["target_session_id"], target_claims[0]["target_thread_id"]) == (
        target_session_id, published["thread_id"],
    )

    lifecycle["source_offline"] = source.restart_backend("kill")
    lifecycle["target_cold_restart"] = target.restart_backend()
    assert lifecycle["target_cold_restart"]["before_pid"] != lifecycle["target_cold_restart"]["after_pid"]
    (artifacts / "backend-lifecycle.json").write_text(json.dumps(lifecycle, ensure_ascii=False, indent=2) + "\n")
    bootstrap = await target.client.get(f"/api/v1/sessions/{target_session_id}/bootstrap")
    (artifacts / "cold-bootstrap.json").write_text(bootstrap.text)
    assert bootstrap.status_code == 200, bootstrap.text
    if child_session_id is not None:
        messages = await target.client.post(
            f"/api/v1/sessions/{target_session_id}/threads/{published['thread_id']}/history", json={},
        )
    else:
        messages = await target.client.get(f"/api/v1/sessions/{target_session_id}/messages")
    (artifacts / "cold-history.json").write_text(messages.text)
    assert messages.status_code == 200, messages.text
    if child_session_id is not None:
        assert messages.json()["data"]["thread_id"] == published["thread_id"]
    assert original in messages.text
    attachment_response = await target.client.get(f"/api/v1/sessions/{target_session_id}/attachments/content", params={
        "file_id": target_file_id, "variant": "original",
    })
    assert attachment_response.status_code == 200, attachment_response.text
    assert attachment_response.content == attachment_body
    (artifacts / "cold-target-attachment.txt").write_bytes(attachment_response.content)
    offline_retry = await target.client.post(
        f"/api/v1/sessions/{source_session_id}/fork-context", json=request,
        headers={"X-Request-ID": "req-third-http-delivery-source-offline"},
    )
    (artifacts / "offline-retry.json").write_text(offline_retry.text)
    assert offline_retry.status_code == 200, offline_retry.text
    repeated = offline_retry.json()["data"]
    assert repeated["session"]["session_id"] == target_session_id
    assert repeated["thread_id"] == published["thread_id"]
    assert repeated["thread_uri"] == published["thread_uri"]
    assert repeated["export_release"] == published["export_release"]
    assert repeated["resources"] == published["resources"]
    assert _attachment_claims(target, key) == target_claims
    cold_record = _creation_record(target, key, child_session_node)
    assert {name: cold_record[name] for name in identity_fields} == {name: frozen[name] for name in identity_fields}
    assert len(model.requests) == provider_requests_before_fork
    await _send(target, target_session_id, successor, thread_id=published["thread_id"])
    assert len(model.requests) == provider_requests_before_fork + 1
    successor_wire = json.dumps(model.requests[provider_requests_before_fork:], ensure_ascii=False)
    assert original in successor_wire
    assert successor in successor_wire
