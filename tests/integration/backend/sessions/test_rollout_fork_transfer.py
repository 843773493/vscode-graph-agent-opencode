"""真实三端 Gateway/SSH fork；模型使用按产品 owner 精确匹配的共享回放。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import commentjson
import httpx
import pytest
from pydantic import ValidationError

from app.core.identifier import create_prefixed_id
from app.core.resource_uri import (
    parse_resource_uri,
    parse_session_artifact_uri,
    parse_session_thread_uri,
    resource_display_uri,
    thread_resource_uri,
)
from app.schemas.internal_v2.session import SessionForkRequest
from app.schemas.internal_v2.session_fork_transfer import FORK_TRANSFER_CHUNK_BYTES
from app.services.business.session_context_resource import (
    workspace_session_inventory_uri,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    fork_import_ingest_id,
    ingest_staging_relative_locator,
    resolve_staging_path,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    _completed,
    _prepare_workspace,
)
from tests.support.api_helpers import create_session
from tests.support.communication_federation import (
    CommunicationNode,
    open_communication_triad,
)
from tests.support.e2e_control.harness import E2ETestControlHarness
from tests.support.fork_deadline import ForkDeadlineRegistry, ForkOperationDeadline
from tests.support.model_stream_config import ModelStreamReplay, model_stream_replay
from tests.support.processes.gateway import close_gateway_process
from tests.support.session_navigation_http import submit_navigation_operation


@pytest.fixture
async def transfer_triad(request: pytest.FixtureRequest) -> AsyncIterator[
    tuple[dict[str, CommunicationNode], ModelStreamReplay, Path, E2ETestControlHarness, ForkDeadlineRegistry]
]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    run_partition = context.runtime_root_for_node(request.node.nodeid).parent.name
    artifacts = context.artifacts_dir / run_partition / request.node.name
    artifacts.mkdir(parents=True, exist_ok=True)
    same_backend_routes = bool(getattr(request, "param", {}).get("same_backend_routes"))
    fork_deadlines = ForkDeadlineRegistry(artifacts)
    try:
        with model_stream_replay(
            context.artifacts_dir.parent, runtime_root=context.runtime_root_for_node(request.node.nodeid),
            business_assertion="cross-gateway-fork-transfer",
        ) as model:
            control = model.control
            async with open_communication_triad(
                context, _prepare_workspace, backend_environment=model.environment,
                backend_asgi_app="tests.support.e2e_control.backend_app:app",
                # 共享 Backend 时真实 owner 是 A，辅助服务必须跟随该 owner 启动。
                resource_service_nodes=("a" if same_backend_routes else "b",),
                on_sshd_started=fork_deadlines.watchdog.register_c3_sshd,
                same_backend_routes=same_backend_routes,
            ) as nodes:
                try:
                    yield nodes, model, artifacts, control, fork_deadlines
                finally:
                    (artifacts / "provider-wire.json").write_text(json.dumps({"provider_call_count": _provider_call_count(model), "recordings": _provider_requests(model)}, ensure_ascii=False, indent=2) + "\n")
    finally:
        fork_deadlines.close()


def _provider_call_count(model: ModelStreamReplay) -> int:
    # 在 transport 前创建的真实 ModelCall 也计入，未匹配回放不能伪装成零请求。
    with model.control._condition:
        return sum(count for namespace, count in model.control._counters.items() if namespace.startswith("model_call/"))


def _provider_requests(model: ModelStreamReplay) -> list[dict[str, object]]:
    events = model.control._dispatch({"action": "events"})
    records = []
    for event in events:
        if event["phase"] != "model.response_ready":
            continue
        call_id = event["operation_id"]
        record = json.loads((model.artifacts / f"{call_id}.json").read_text(encoding="utf-8"))
        assert record["identity"]["model_call_id"] == call_id
        records.append(record["request"])
    return records


async def _plan_replies(
    node: CommunicationNode, model: ModelStreamReplay, session_id: str,
    responses: list[dict[str, object]], *, thread_id: str | None = None,
) -> None:
    if thread_id is None:
        main = await node.client.get(f"/api/v1/sessions/{session_id}/main-thread")
        assert main.status_code == 200, main.text
        thread_id = main.json()["data"]["thread_id"]
    config = commentjson.loads((node.workspace / ".boxteam/workspace.jsonc").read_text())
    endpoint = next(provider["endpoint"] for provider in config["llm"]["providers"] if provider["id"] == "primary")
    interactions = [build_cassette(
        asset_id="fork-transfer", model="e2e-stub-model",
        url=endpoint + "/chat/completions",
        frames=chat_completion_frames(
            response_id="chatcmpl-fork-transfer", model="e2e-stub-model",
            deltas=({"role": "assistant", **response},),
            finish_reason="tool_calls" if "tool_calls" in response else "stop",
            usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
        ),
    ).raw["interactions"][0] for response in responses]
    model.control.plan_model_calls(session_id, thread_id, interactions)


async def _send(
    node: CommunicationNode, session_id: str, content: str, *, model: ModelStreamReplay,
    reply: str, thread_id: str | None = None,
    attachments: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    address = f"/api/v1/sessions/{session_id}"
    if thread_id is not None:
        address += f"/threads/{thread_id}"
    message: dict[str, object] = {"content": content}
    if attachments is not None:
        message["attachments"] = attachments
    await _plan_replies(node, model, session_id, [{"content": reply}], thread_id=thread_id)
    response = await node.client.post(
        address + "/messages",
        json={"message": message, "run": {"mode": "single_agent", "agent_id": "default"}},
    )
    assert response.status_code == 200, response.text
    if thread_id is not None:
        assert response.json()["data"]["thread_id"] == thread_id
    return await _completed(node.client, response.json()["data"]["job_id"])


def _fork_deadline(
    target: CommunicationNode, request: dict[str, object], deadlines: ForkDeadlineRegistry,
) -> ForkOperationDeadline:
    return deadlines.for_operation(
        target=f"{target.gateway_id}/{target.workspace_id}",
        operation_key=str(request["idempotency_key"]),
    )


async def _post_fork(
    target: CommunicationNode,
    source_session_id: str,
    request: dict[str, object],
    deadline: ForkOperationDeadline,
    *,
    stage: str,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    response = await deadline.wait(
        target.client.post(
            f"/api/v1/sessions/{source_session_id}/fork-context", json=request, headers=headers,
        ),
        stage,
    )
    if response.status_code == 200:
        release = response.json()["data"].get("export_release")
        if release is None or release["state"] == "confirmed":
            deadline.complete()
    elif 400 <= response.status_code < 500:
        deadline.complete()
    return response


@pytest.mark.asyncio
async def test_debug_configuration_choice_uses_source_owner_thread_across_gateway_routes(transfer_triad):
    nodes, model, _artifacts, _control, fork_deadlines = transfer_triad
    source, remote_target = nodes["b"], nodes["c"]
    for node in nodes.values():
        (node.workspace / "app.mjs").write_text("console.log('fork debug');\n", encoding="utf-8")

    reply_text = "来源会话已准备"
    source_session_id = await create_session(source.client, "调试方案 fork 来源")
    await _send(source, source_session_id, "建立真实来源历史", model=model, reply=reply_text)
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
        deadline = _fork_deadline(target, request, fork_deadlines)
        response = await _post_fork(
            origin, source_session_id, request, deadline, stage="fork_http_response",
        )
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        assert result["target_gateway_id"] == target.gateway_id
        assert result["export_release"] == ({"state": "confirmed", "error": None} if target is not source else None)
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
        target_capture = _capture_record(target, operation_id)
        source_capture = _capture_record(source, operation_id)
        assert target_capture is not None and source_capture is not None
        assert target_capture["state"] == source_capture["state"] == "materialized"
        assert target_capture["snapshot_sha256"] == source_capture["snapshot_sha256"] is not None
        assert target_capture["marker_sha256"] == source_capture["marker_sha256"] is not None
        assert target_capture["request_json"] == source_capture["request_json"]
        if target is not source:
            assert target_capture["export_release_state"] == "confirmed"
            receipt = json.loads(target_capture["export_release_receipt_json"])
            assert receipt == {field: value for field, value in source_capture.items() if not field.startswith("export_release_")}
        else:
            assert target_capture["export_release_state"] is None
        for node, capture in ((source, source_capture), (target, target_capture)):
            root = node.workspace / ".boxteam/navigation" / capture["snapshot_relative_locator"]
            assert not root.exists() and not root.is_symlink()

        retry = await _post_fork(
            origin, source_session_id, request, _fork_deadline(target, request, fork_deadlines),
            stage="completed_idempotency_replay",
        )
        assert retry.status_code == 200, retry.text
        repeated = retry.json()["data"]
        assert (repeated["session"]["session_id"], repeated["thread_id"], repeated["thread_uri"]) == (
            result["session"]["session_id"], result["thread_id"], result["thread_uri"],
        )
        assert repeated["resources"] == result["resources"]
        assert repeated["export_release"] == result["export_release"]
        assert _capture_record(target, operation_id) == target_capture
        assert _capture_record(source, operation_id) == source_capture
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
        rejected_request = {
            "idempotency_key": key,
            "source_uri": explicit_source_session_uri,
            "source_view_id": point["source_view_id"],
            "source_view_revision": point["source_view_revision"],
            "mode": "full_rollout_copy",
            "target_container_uri": workspace_session_inventory_uri(source.workspace_id),
            "creation_kind": "new_session_main",
            "resources": [{"resource_uri": choice_uri, "action": "copy_snapshot"}],
        }
        rejected = await _post_fork(
            source, source_session_id, rejected_request,
            _fork_deadline(source, rejected_request, fork_deadlines), stage="rejected_resource_http_response",
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
        rejected_request = {
            "idempotency_key": key,
            "source_uri": explicit_source_session_uri,
            "source_view_id": point["source_view_id"],
            "source_view_revision": point["source_view_revision"],
            "mode": "full_rollout_copy",
            "target_container_uri": workspace_session_inventory_uri(source.workspace_id),
            "creation_kind": "new_session_main",
            "resources": choices,
        }
        rejected = await _post_fork(
            source, source_session_id, rejected_request,
            _fork_deadline(source, rejected_request, fork_deadlines), stage="rejected_resource_http_response",
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
@pytest.mark.parametrize("transfer_triad", [{"same_backend_routes": True}], indirect=True)
@pytest.mark.parametrize(("creation_kind", "source_reference"), [
    ("new_child_thread", "qualified"), ("new_session_main", "qualified"),
    ("new_child_thread", "local"), ("new_session_main", "local"),
])
async def test_same_backend_two_gateway_routes_fork_and_cold_replay(
    transfer_triad, creation_kind: str, source_reference: str,
):
    nodes, model, artifacts, control, fork_deadlines = transfer_triad
    target, source = nodes["a"], nodes["b"]
    assert target.backend.process is source.backend.process
    assert target.workspace == source.workspace and target.workspace_id == source.workspace_id
    assert target.gateway_id != source.gateway_id and target.gateway_home != source.gateway_home
    assert target.gateway is not None and source.gateway is not None
    assert target.gateway.process.pid != source.gateway.process.pid
    topology = {}
    for name, node in nodes.items():
        health = await node.client.get("/api/gateway/health")
        inventory = await node.client.get("/api/gateway/workspaces")
        workspace = await node.client.get("/api/v1/workspace")
        assert health.status_code == inventory.status_code == workspace.status_code == 200
        for response in (health, inventory, workspace):
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
        assert health.json()["data"]["gateway_id"] == node.gateway_id
        assert workspace.json()["data"]["workspace_id"] == node.workspace_id
        assert "root_path" not in workspace.json()["data"]
        assert "root" not in workspace.json()["data"]["git"]
        topology[name] = {"health": health.json(), "inventory": inventory.json(), "workspace": workspace.json(),
                          "backend_pid": node.backend.process.pid, "gateway_pid": node.gateway.process.pid, "gateway_home": str(node.gateway_home)}
    (artifacts / "same-owner-topology.json").write_text(json.dumps(topology, ensure_ascii=False, indent=2) + "\n")
    reply_text = "同 owner 来源已准备"
    # 本地来源 URI 仍可能引用上传时带 Gateway authority 的附件。
    history_node = target if source_reference == "local" else source
    session_id = await create_session(history_node.client, "同 Backend 双 Gateway")
    body = b"same-owner-attachment" * 4096
    await _send(history_node, session_id, "同 owner 主线程附件历史", attachments=_inline_attachment(body), model=model, reply=reply_text)
    point_response = await history_node.client.get(f"/api/v1/sessions/{session_id}/fork-source-point", params={"mode": "full_rollout_copy"})
    assert point_response.status_code == 200, point_response.text
    point = point_response.json()["data"]
    _, _, main_thread_id = parse_session_thread_uri(point["source_uri"])
    request = {
        "idempotency_key": "same-backend-two-gateway-" + creation_kind,
        "source_uri": thread_resource_uri(source.workspace_id, session_id, main_thread_id,
            gateway_authority=source.gateway_id if source_reference == "qualified" else None),
        "target_container_uri": resource_display_uri(scope="workspace", scope_id=target.workspace_id, kind="session",
            tail_segments=(session_id,) if creation_kind == "new_child_thread" else (), gateway_authority=target.gateway_id),
        "creation_kind": creation_kind, "mode": "full_rollout_copy",
        "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
    }
    (artifacts / "same-owner-source-point.json").write_text(point_response.text)
    (artifacts / "same-owner-request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n")
    source_database = source.workspace / ".boxteam/attachments/catalog.sqlite"
    with sqlite3.connect(source_database.as_uri() + "?mode=ro", uri=True) as connection:
        source_blobs = connection.execute("SELECT * FROM attachment_blobs ORDER BY blob_id").fetchall()
        source_refs = connection.execute("SELECT * FROM attachment_owner_refs ORDER BY owner_ref_id").fetchall()
    key = "fork-" + hashlib.sha256(request["idempotency_key"].encode()).hexdigest()
    session_node = await target.session_node(session_id)
    child_node = session_node if creation_kind == "new_child_thread" else None
    deadline = _fork_deadline(target, request, fork_deadlines)
    control.arm(key, "copy.target.prepared")
    fork_call = asyncio.create_task(target.client.post(f"/api/v1/sessions/{session_id}/fork-context", json=request))
    reached = asyncio.create_task(asyncio.to_thread(control.acknowledged, key, "copy.target.prepared"))
    try:
        done, _ = await deadline.wait(asyncio.wait((fork_call, reached), return_when=asyncio.FIRST_COMPLETED), "same_owner_pre_capture")
        if fork_call in done:
            early = await fork_call
            pytest.fail(f"same-owner 在 preparing 故障窗口前返回: {early.status_code}: {early.text}")
        await reached
        preparing = _creation_record(target, key, child_node)
        assert preparing["state"] == "preparing" and _capture_record(target, key) is None
        assert "source_owner_instance_id" not in preparing["request_intent_json"]
        assert "source_target_creation_preimage_hash" not in preparing["request_intent_json"]
        assert json.loads(preparing["request_intent_json"])["capture_decision"] == {
            "source_storage_domain_id": source.workspace_id, "target_storage_domain_id": target.workspace_id,
            "capture_owner": "local",
        }
        assert _attachment_claims(target, key) == []
        lifecycle = {"pre_capture_kill": target.restart_backend("kill")}
        control.release(key, "copy.target.prepared")
        interrupted = await deadline.wait(fork_call, "same_owner_interrupted_http")
        assert interrupted.status_code >= 400, interrupted.text
        # 首次真实握手已随 creation 固定；重启捕获不得依赖尚未监听的 Backend 或来源 Gateway。
        close_gateway_process(source.gateway)
        assert source.gateway.process.poll() is not None
        lifecycle["pre_capture_start"] = target.restart_backend("start")
    finally:
        control.release(key, "copy.target.prepared")
        for task in (fork_call, reached):
            if not task.done():
                task.cancel()
        await asyncio.gather(fork_call, reached, return_exceptions=True)
    response = await _post_fork(target, session_id, request, deadline, stage="same_owner_fork")
    (artifacts / "same-owner-response.json").write_text(response.text)
    assert response.status_code == 200, response.text
    published = response.json()["data"]
    target_thread_id = published["thread_id"]
    target_session_id = published["session"]["session_id"]
    assert target_thread_id != main_thread_id
    assert (target_session_id == session_id) == (creation_kind == "new_child_thread")
    assert published["source_uri"] == thread_resource_uri(
        history_node.workspace_id, session_id, main_thread_id, gateway_authority=history_node.gateway_id,
    )
    assert published["export_release"] is None
    assert len(published["resources"]) == 1
    resource = published["resources"][0]
    assert resource["action"] == ("reuse_reference" if creation_kind == "new_child_thread" else "retain_reference")
    assert resource["availability"] == "available"
    capture = _capture_record(target, key)
    assert capture is not None and capture["state"] == "materialized"
    frozen = json.loads(capture["request_json"])
    assert frozen["source_uri"] == published["source_uri"]
    assert frozen["source_storage_domain_id"] == frozen["target_storage_domain_id"] == target.workspace_id
    assert "source_owner_instance_id" not in capture["request_json"]
    assert "source_target_creation_preimage_hash" not in capture["request_json"]
    assert capture["export_release_state"] is None
    claims = _attachment_claims(target, key)
    if creation_kind == "new_child_thread":
        assert claims == []
    else:
        assert len(claims) == 1 and claims[0]["state"] == "committed"
    assert not (target.workspace / ".boxteam/navigation" / capture["snapshot_relative_locator"]).exists()
    creation = _creation_record(target, key, child_node)
    with sqlite3.connect(source_database.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT * FROM attachment_blobs ORDER BY blob_id").fetchall() == source_blobs
        refs = connection.execute("SELECT * FROM attachment_owner_refs ORDER BY owner_ref_id").fetchall()
    assert all(row in refs for row in source_refs)
    assert len(refs) == len(source_refs) + (creation_kind == "new_session_main")
    if creation_kind == "new_child_thread":
        with sqlite3.connect((session_node / "session-control.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
            assert connection.execute("SELECT * FROM copy_attachment_settlements WHERE copy_operation_id=?", (key,)).fetchall() == []
    assert creation["state"] == "published"
    assert creation["request_intent_json"] == preparing["request_intent_json"]
    assert creation["preimage_hash"] == preparing["preimage_hash"]
    assert creation["fork_delivery_request_id"] == preparing["fork_delivery_request_id"]
    assert "source_owner_instance_id" not in creation["request_intent_json"]
    assert "source_target_creation_preimage_hash" not in creation["request_intent_json"]
    phases = [event for event in control.events if event.get("operation_id") == key]
    source_phases = [event for event in phases if event["phase"] == "copy.source.export_authorized"]
    assert source_phases == []
    local_phases = [event for event in phases if event["phase"] == "copy.source.local_capture_authorized"]
    assert len(local_phases) == 1
    assert local_phases[0]["details"] == {
        "gateway_id": target.gateway_id, "source_gateway_id": history_node.gateway_id, "same_storage_owner": True,
    }
    assert all(event["details"]["gateway_id"] == target.gateway_id for event in phases if event["phase"] == "copy.target.prepared")
    (artifacts / "same-owner-capture.json").write_text(json.dumps({"capture": capture, "creation": creation, "phases": phases}, ensure_ascii=False, indent=2) + "\n")
    before_requests = _provider_call_count(model)
    assert source.gateway.process.poll() is not None
    lifecycle["cold_restart"] = target.restart_backend()
    (artifacts / "same-owner-backend-restart.json").write_text(json.dumps(lifecycle, indent=2) + "\n")
    replay = await _post_fork(target, session_id, request, _fork_deadline(target, request, fork_deadlines), stage="same_owner_cold_replay")
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"] == published
    history = await target.client.post(f"/api/v1/sessions/{target_session_id}/threads/{target_thread_id}/history", json={})
    assert history.status_code == 200 and "同 owner 主线程附件历史" in history.text, history.text
    attachment = await target.client.get(f"/api/v1/sessions/{target_session_id}/attachments/content", params={"file_id": resource["target_uri"], "variant": "original"})
    assert attachment.status_code == 200 and attachment.content == body
    (artifacts / "same-owner-cold-history.json").write_text(history.text)
    await _send(target, target_session_id, "同 owner 目标后继输入", thread_id=target_thread_id, model=model, reply=reply_text)
    wire = json.dumps(_provider_requests(model)[before_requests:], ensure_ascii=False)
    assert "同 owner 主线程附件历史" in wire and "同 owner 目标后继输入" in wire
    assert _capture_record(target, key) == capture and _attachment_claims(target, key) == claims
    (artifacts / "same-owner-successor-wire.json").write_text(wire)


@pytest.mark.asyncio
async def test_real_b_to_c_full_copy_cold_read_and_new_input_with_source_offline(transfer_triad):
    nodes, model, artifacts, control, fork_deadlines = transfer_triad
    source, target = nodes["b"], nodes["c"]
    original = "来源 B 冻结正文：跨 Gateway 原字节历史必须独立保留。"
    successor = "目标 C 首次新输入：继续使用已经导入的本地历史。"
    reply_text = "确定性生产 Provider 回应"
    source_session_id = await create_session(source.client, "跨端来源 B")
    attachment_body = b"b" * FORK_TRANSFER_CHUNK_BYTES + b"c"
    await _send(source, source_session_id, original, attachments=_inline_attachment(attachment_body), model=model, reply=reply_text)
    source_objective = "跨端来源 Goal：验证实际用量清零后由用户决定继续。"

    await _plan_replies(source, model, source_session_id, [
        {"tool_calls": [{
            "index": 0, "id": "call_fork_source_goal_complete", "type": "function",
            "function": {"name": "update_goal", "arguments": json.dumps({"status": "complete"})},
        }]},
        {"content": "来源 Goal 已完成，保留真实计量。"},
    ])
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
    provider_requests_before_fork = _provider_call_count(model)
    child_request = {
        **request, "idempotency_key": "real-triad-child-goal-must-reject",
        "creation_kind": "new_child_thread",
        "target_container_uri": resource_display_uri(
            scope="workspace", scope_id=source.workspace_id, kind="session", tail_segments=(source_session_id,),
            gateway_authority=source.gateway_id,
        ),
    }
    with pytest.raises(ValidationError, match="Goal 只能在显式创建新 Session main 时继承"):
        SessionForkRequest.model_validate(child_request)
    child_threads_before = await source.client.get(f"/api/v1/sessions/{source_session_id}/child-threads")
    assert child_threads_before.status_code == 200, child_threads_before.text
    rejected_child = await _post_fork(
        source, source_session_id, child_request,
        _fork_deadline(source, child_request, fork_deadlines), stage="rejected_goal_http_response",
    )
    assert rejected_child.status_code == 422, rejected_child.text
    assert rejected_child.json()["detail"] == [
        {"loc": ["body"], "type": "value_error", "msg": "请求参数校验失败"},
    ]
    child_threads_after = await source.client.get(f"/api/v1/sessions/{source_session_id}/child-threads")
    assert child_threads_after.status_code == 200, child_threads_after.text
    assert child_threads_after.json()["data"] == child_threads_before.json()["data"]
    unchanged_source_goal = await source.client.get(f"/api/v1/sessions/{source_session_id}/goal")
    assert unchanged_source_goal.status_code == 200, unchanged_source_goal.text
    assert unchanged_source_goal.json()["data"] == source_goal
    assert _provider_call_count(model) == provider_requests_before_fork
    key = "fork-" + hashlib.sha256(request["idempotency_key"].encode()).hexdigest()
    phase = "copy.source.marker_published"
    control.arm(key, phase)
    deadline = _fork_deadline(target, request, fork_deadlines)
    fork_call = asyncio.create_task(_post_fork(
        source, source_session_id, request, deadline, stage="fork_http_response",
    ))
    reached = asyncio.create_task(asyncio.to_thread(control.acknowledged, key, phase))
    hidden = live = None
    try:
        done, _ = await deadline.wait(
            asyncio.wait((fork_call, reached), return_when=asyncio.FIRST_COMPLETED),
            "wait_for_frozen_attachment_or_http_response",
        )
        if fork_call in done:
            early = await fork_call
            pytest.fail(f"fork 未到冻结文件验证窗口即返回: {early.status_code}: {early.text}")
        await deadline.wait(reached, "source_attachment_frozen")
        source_capture = _capture_record(source, key)
        capture_root = source.workspace / ".boxteam/navigation" / source_capture["snapshot_relative_locator"]
        marker = json.loads((capture_root / "captured.json").read_bytes())
        assert marker["schema"] == "source-copy-snapshot:v3"
        (snapshot,) = marker["attachments"]
        frozen_file = capture_root / source_session_id / "attachment-files" / snapshot["blob_id"]
        assert frozen_file.read_bytes() == attachment_body
        live = source.workspace / ".boxteam/attachments" / snapshot["relative_locator"]
        assert frozen_file.stat().st_ino != live.stat().st_ino
        # 关键状态由真实上传/执行/fork 生成；在 marker 发布后注入来源原件丢失故障。
        hidden = live.with_name(live.name + ".fault-hidden")
        live.rename(hidden)
        control.release(key, phase)
        response = await deadline.wait(fork_call, "fork_from_frozen_attachment")
    finally:
        control.release(key, phase)
        if hidden is not None and hidden.exists():
            hidden.rename(live)
        if not fork_call.done():
            fork_call.cancel()
        await asyncio.gather(fork_call, return_exceptions=True)
        await asyncio.gather(reached, return_exceptions=True)
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
    assert _provider_call_count(model) == provider_requests_before_fork
    assert len(published["resources"]) == 1
    attachment_result = published["resources"][0]
    assert attachment_result["resource_uri"] == attachment_source["resource_uri"]
    assert attachment_result["action"] == "copy_snapshot" and attachment_result["availability"] == "available"
    target_file_id = attachment_result["target_uri"]
    assert parse_session_artifact_uri(target_file_id).tail_segments[:2] == (target_session_id, "attachments")
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

    retry = await _post_fork(
        target, source_session_id, request, _fork_deadline(target, request, fork_deadlines),
        stage="completed_idempotency_replay",
    )
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
    assert _provider_call_count(model) == provider_requests_before_fork

    await _send(target, target_session_id, successor, model=model, reply=reply_text)
    assert _provider_call_count(model) == provider_requests_before_fork + 1
    successor_wire = json.dumps(_provider_requests(model)[provider_requests_before_fork:], ensure_ascii=False)
    assert original in successor_wire
    assert successor in successor_wire
    target_session_node = await target.session_node(target_session_id)
    deletion = await submit_navigation_operation(
        target.client,
        "delete_session",
        target_node_id=target_session_id,
        wait_for_settlement=True,
    )
    assert deletion["state"] == "committed", deletion
    assert deletion["settlement"] == "settled", deletion
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
    nodes, model, artifacts, control, fork_deadlines = transfer_triad
    hub, source, target = nodes["a"], nodes["b"], nodes["c"]
    for node in (source, target):
        inventory = await node.client.get("/api/gateway/workspaces")
        assert inventory.status_code == 200, inventory.text
        own = next(item for item in inventory.json()["data"]["items"]
                   if item["backend_workspace_id"] == node.workspace_id and item["connection_kind"] == "local")
        node.client.headers["X-BoxTeam-Workspace-Id"] = own["workspace_id"]
    hub_inventory = await hub.client.get("/api/gateway/workspaces")
    assert hub_inventory.status_code == 200, hub_inventory.text
    hub_items = hub_inventory.json()["data"]["items"]
    source_route = next(item for item in hub_items if (item.get("remote") or {}).get("gateway_id") == source.gateway_id)
    hub_local = next(item for item in hub_items
                     if item["backend_workspace_id"] == hub.workspace_id and item["connection_kind"] == "local")
    inherited = "来源 B 主线程的共同历史。"
    child_only = "来源 B child 独有输入：目标必须继承这个 thread。"
    main_only = "来源 B main 后来的独有输入：不能混入 child 分支。"
    successor = "目标 C 沿来源 child 历史继续一次。"
    reply_text = "来源 child 验收固定回答"
    source_session_id = await create_session(source.client, "来源 B child 验收")
    await _send(source, source_session_id, inherited, model=model, reply=reply_text)
    main_response = await source.client.get(
        f"/api/v1/sessions/{source_session_id}/fork-source-point", params={"mode": "full_rollout_copy"},
    )
    assert main_response.status_code == 200, main_response.text
    main_point = main_response.json()["data"]
    child_request = {
        "idempotency_key": "prepare-real-source-child",
        "source_uri": main_point["source_uri"], "target_container_uri": main_point["child_container_uri"],
        "source_view_id": main_point["source_view_id"], "source_view_revision": main_point["source_view_revision"],
        "creation_kind": "new_child_thread", "mode": "full_rollout_copy",
    }
    child_response = await _post_fork(
        source, source_session_id, child_request, _fork_deadline(source, child_request, fork_deadlines),
        stage="source_child_setup_response",
    )
    assert child_response.status_code == 200, child_response.text
    source_child = child_response.json()["data"]
    source_thread_id = source_child["thread_id"]
    assert source_child["session"]["session_id"] == source_session_id
    assert parse_resource_uri(source_child["thread_uri"]).tail_segments == (source_session_id, "threads", source_thread_id)
    attachment_body = b"c" * FORK_TRANSFER_CHUNK_BYTES + b"d"
    await _send(source, source_session_id, child_only, thread_id=source_thread_id, attachments=_inline_attachment(attachment_body), model=model, reply=reply_text)
    await _send(source, source_session_id, main_only, model=model, reply=reply_text)
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
    target_container_thread = None
    if target_parent is not None:
        target_main = await target.client.get(f"/api/v1/sessions/{target_parent}/main-thread")
        assert target_main.status_code == 200, target_main.text
        target_container_thread = target_main.json()["data"]["thread_id"]
    request = {
        "idempotency_key": f"real-source-child-{creation_kind}",
        "source_uri": resource_display_uri(
            scope=source_address.scope, scope_id=source_address.scope_id, kind=source_address.kind,
            tail_segments=source_address.tail_segments, gateway_authority=source.gateway_id,
        ),
        "target_container_uri": resource_display_uri(
            scope="workspace", scope_id=target.workspace_id,
            kind="session",
            tail_segments=(target_parent, "threads", target_container_thread) if target_parent is not None else (),
            gateway_authority=target.gateway_id,
        ),
        "creation_kind": creation_kind, "mode": "full_rollout_copy",
        "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
        "resources": [{"resource_uri": attachment_source["resource_uri"], "action": "copy_snapshot"}],
    }
    (artifacts / "source-child-fork-request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n")
    before_rejections = await target.client.get("/api/v1/session-catalog/export")
    assert before_rejections.status_code == 200, before_rejections.text
    missing_request = {
        **request, "idempotency_key": f"missing-target-session-{creation_kind}", "creation_kind": "new_child_thread",
        "target_container_uri": resource_display_uri(
            scope="workspace", scope_id=target.workspace_id, kind="session",
            tail_segments=("ses_00000000400070008000000000000001",), gateway_authority=target.gateway_id,
        ),
    }
    missing = await _post_fork(
        target, source_session_id, missing_request,
        _fork_deadline(target, missing_request, fork_deadlines), stage="missing_target_http_response",
    )
    assert missing.status_code == 404, missing.text
    rejections = {"missing_session": missing.json()}
    rejected_keys = [missing_request["idempotency_key"]]
    for case, gateway_id, workspace_id, expected_code in (
        ("unknown_gateway", "gateway_unknown", target.workspace_id, "federation-unknown-peer"),
        ("missing_workspace", target.gateway_id, "00000000-0000-4000-8000-000000000099", "target_not_resolvable"),
    ):
        target_uri = workspace_session_inventory_uri(workspace_id, gateway_authority=gateway_id)
        rejected_key = f"source-child-{case}-{creation_kind}"
        rejected_keys.append(rejected_key)
        fork_target = {"gateway_id": gateway_id, "uri": target_uri, "fork_role": "target"}
        rejected = await hub.client.post("/api/gateway/federation/relay", json={
            "operation": "fork_target", "fork_target": fork_target,
            "body": {
                "source_gateway_id": hub.gateway_id, "fork_target": fork_target, "action": "create",
                "request": {**request, "idempotency_key": rejected_key, "creation_kind": "new_session_main", "target_container_uri": target_uri},
            },
        })
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == expected_code, rejected.text
        rejections[case] = rejected.json()
    after_rejections = await target.client.get("/api/v1/session-catalog/export")
    assert after_rejections.status_code == 200, after_rejections.text
    assert after_rejections.json()["data"] == before_rejections.json()["data"]
    for rejected_key in rejected_keys:
        rejected_operation = "fork-" + hashlib.sha256(rejected_key.encode()).hexdigest()
        assert _capture_record(source, rejected_operation) is None
        assert _capture_record(target, rejected_operation) is None
    (artifacts / "source-child-rejections.json").write_text(json.dumps(rejections, ensure_ascii=False, indent=2) + "\n")
    provider_requests_before_fork = _provider_call_count(model)
    key = "fork-" + hashlib.sha256(request["idempotency_key"].encode()).hexdigest()
    phase = "copy.target.prepared"
    activation = await hub.client.post(f"/api/gateway/workspaces/{source_route['workspace_id']}/activate")
    assert activation.status_code == 200, activation.text
    control.arm(key, phase)
    fork_call = asyncio.create_task(hub.client.post(
        f"/api/v1/sessions/{source_session_id}/fork-context", json=request,
        headers={"X-BoxTeam-Workspace-Id": source_route["workspace_id"]},
    ))
    reached = asyncio.create_task(asyncio.to_thread(control.acknowledged, key, phase))
    deadline = _fork_deadline(target, request, fork_deadlines)
    try:
        done, _ = await deadline.wait(
            asyncio.wait((fork_call, reached), return_when=asyncio.FIRST_COMPLETED),
            "wait_for_phase_or_http_response",
        )
        if fork_call in done:
            early_response = await deadline.wait(fork_call, "http_response_before_phase")
            reached.cancel()
            pytest.fail(f"fork 在固定目标准备屏障前返回 HTTP {early_response.status_code}: {early_response.text}")
        await deadline.wait(reached, "phase_acknowledged")
        prepared = _creation_record(target, key, child_session_node)
        assert prepared["state"] == "preparing"
        switched = await hub.client.post(f"/api/gateway/workspaces/{hub_local['workspace_id']}/activate")
        assert switched.status_code == 200, switched.text
        health = await hub.client.get("/api/gateway/health")
        assert health.status_code == 200, health.text
        assert health.json()["data"]["active_workspace_id"] == hub_local["workspace_id"]
        (artifacts / "source-child-workspace-switch.json").write_text(json.dumps({
            "fixed_source_route": source_route["workspace_id"], "activated_workspace": hub_local["workspace_id"],
            "prepared": prepared, "health": health.json(),
        }, ensure_ascii=False, indent=2) + "\n")
        deadline.set_stage("release_phase_barrier")
        control.release(key, phase)
        response = await deadline.wait(fork_call, "http_response_after_phase_release")
    finally:
        deadline.set_stage("cleanup_phase_barrier_and_client_task")
        control.release(key, phase)
        for task in (fork_call, reached):
            if not task.done():
                task.cancel()
        await asyncio.gather(fork_call, reached, return_exceptions=True)
    (artifacts / "source-child-fork-response.json").write_text(response.text)
    assert response.status_code == 200, response.text
    published = response.json()["data"]
    assert _provider_call_count(model) == provider_requests_before_fork
    assert published["export_release"] == {"state": "confirmed", "error": None}
    fork_deadlines.complete(deadline)
    assert published["source_uri"] == request["source_uri"]
    assert published["source_view_id"] == point["source_view_id"]
    assert published["source_view_revision"] == point["source_view_revision"]
    target_session_id, target_thread_id = published["session"]["session_id"], published["thread_id"]
    if target_parent is not None:
        assert target_session_id == target_parent
        assert target_thread_id not in {source_thread_id, target_container_thread}
    target_address = parse_resource_uri(published["thread_uri"])
    assert (target_address.gateway_authority, target_address.scope_id, target_address.tail_segments) == (
        target.gateway_id, target.workspace_id, (target_session_id, "threads", target_thread_id),
    )
    source_capture = _capture_record(source, key)
    target_capture = _capture_record(target, key)
    assert source_capture is not None and target_capture is not None
    assert source_capture["source_thread_id"] == target_capture["source_thread_id"] == source_thread_id
    assert source_capture["state"] == target_capture["state"] == "materialized"
    assert target_capture["target_thread_id"] == target_thread_id
    frozen = _creation_record(target, key, child_session_node)
    assert frozen["state"] == "published"
    assert (frozen["preimage_hash"], frozen["request_intent_json"]) == (prepared["preimage_hash"], prepared["request_intent_json"])
    claims = _attachment_claims(target, key)
    assert len(claims) == 1 and claims[0]["state"] == "committed"
    assert (claims[0]["target_session_id"], claims[0]["target_thread_id"]) == (target_session_id, target_thread_id)
    assert _attachment_claims(source, key) == []
    assert len(published["resources"]) == 1
    target_file_id = published["resources"][0]["target_uri"]
    assert parse_session_artifact_uri(target_file_id).tail_segments[:2] == (target_session_id, "attachments")
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
    target_container = parse_resource_uri(request["target_container_uri"])
    alias_request = {**request, "target_container_uri": resource_display_uri(
        scope=target_container.scope, scope_id=target_container.scope_id,
        kind=target_container.kind, tail_segments=target_container.tail_segments,
    )}
    replay = await _post_fork(
        target, source_session_id, alias_request, _fork_deadline(target, alias_request, fork_deadlines),
        stage="completed_alias_idempotency_replay",
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"] == published
    assert _creation_record(target, key, child_session_node) == frozen
    assert _attachment_claims(target, key) == claims
    assert _provider_call_count(model) == provider_requests_before_fork
    await _send(target, target_session_id, successor, thread_id=target_thread_id, model=model, reply=reply_text)
    assert _provider_call_count(model) == provider_requests_before_fork + 1
    successor_wire = json.dumps(_provider_requests(model)[provider_requests_before_fork:], ensure_ascii=False)
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
@pytest.mark.parametrize(("creation_kind", "source_recovery"), [
    ("new_session_main", "incomplete"),
    ("new_child_thread", "incomplete"),
    ("new_session_main", "transport_unavailable"),
])
async def test_real_partial_source_capture_recovery_preserves_owner_error(
    transfer_triad, creation_kind: str, source_recovery: str,
):
    nodes, model, artifacts, control, fork_deadlines = transfer_triad
    source, target = nodes["b"], nodes["c"]
    reply_text = "来源已持久化，等待真实跨 Gateway fork"
    source_session_id = await create_session(source.client, "未完成来源 capture")
    await _send(source, source_session_id, "来源正文必须保持可读", attachments=_inline_attachment(b"partial-source"), model=model, reply=reply_text)
    point_response = await source.client.get(
        f"/api/v1/sessions/{source_session_id}/fork-source-point", params={"mode": "full_rollout_copy"},
    )
    assert point_response.status_code == 200, point_response.text
    point = point_response.json()["data"]
    source_address = parse_resource_uri(point["source_uri"])
    child_session_id = None
    child_session_node = None
    if creation_kind == "new_child_thread":
        child_session_id = await create_session(target.client, "保留既有目标 Session")
        child_session_node = await target.session_node(child_session_id)
    request = {
        "source_uri": resource_display_uri(
            scope=source_address.scope, scope_id=source_address.scope_id, kind=source_address.kind,
            tail_segments=source_address.tail_segments, gateway_authority=source.gateway_id,
        ),
        "target_container_uri": resource_display_uri(
            scope="workspace", scope_id=target.workspace_id, kind="session",
            tail_segments=(child_session_id,) if child_session_id is not None else (), gateway_authority=target.gateway_id,
        ),
        "creation_kind": creation_kind, "mode": "full_rollout_copy",
        "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
        "idempotency_key": f"partial-{creation_kind}-{source_recovery}",
        "resources": [{"resource_uri": item["resource_uri"], "action": "copy_snapshot"} for item in point["resources"]],
    }
    (artifacts / "fork-request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n")
    key = "fork-" + hashlib.sha256(request["idempotency_key"].encode()).hexdigest()
    first_request_id = f"req-partial-{creation_kind}-{source_recovery}"
    phase = "copy.source.partial"
    control.arm(key, phase)
    deadline = _fork_deadline(target, request, fork_deadlines)
    provider_requests = _provider_call_count(model)
    fork_call = asyncio.create_task(target.client.post(
        f"/api/v1/sessions/{source_session_id}/fork-context", json=request, headers={"X-Request-ID": first_request_id},
    ))
    reached = asyncio.create_task(asyncio.to_thread(control.acknowledged, key, phase))
    lifecycle = {}
    stderr_path = Path(target.backend.stderr_file.name)
    try:
        done, _ = await deadline.wait(
            asyncio.wait((fork_call, reached), return_when=asyncio.FIRST_COMPLETED), "wait_for_partial_source_capture",
        )
        if fork_call in done:
            response = await deadline.wait(fork_call, "unexpected_response_before_partial_capture")
            pytest.fail(f"fork 未到来源 partial 窗口即返回 HTTP {response.status_code}: {response.text}")
        await deadline.wait(reached, "partial_source_capture_acknowledged")
        frozen = _creation_record(target, key, child_session_node)
        target_before = _capture_record(target, key)
        source_before = _capture_record(source, key)
        assert frozen["state"] == "preparing" and frozen["fork_delivery_request_id"] == first_request_id
        assert target_before is not None and source_before is not None
        for capture in (source_before, target_before):
            assert capture["state"] == "preparing" and capture["snapshot_sha256"] is None and capture["marker_sha256"] is None
        assert target_before["export_release_state"] == "pending"
        source_root = source.workspace / ".boxteam/navigation" / source_before["snapshot_relative_locator"]
        target_root = target.workspace / ".boxteam/navigation" / target_before["snapshot_relative_locator"]
        assert (source_root / source_session_id / "rollout").is_dir()
        assert not (source_root / "captured.json").exists() and not target_root.exists()
        (artifacts / "frozen-before-interruption.json").write_text(json.dumps({
            "creation": frozen, "source": source_before, "target": target_before,
        }, ensure_ascii=False, indent=2) + "\n")

        # 两端只中断本 fixture 的真实 Popen；私有 artifact 保留原 owner preparing 事实。
        deadline.set_stage("interrupt_target_before_source_http_failure")
        lifecycle["target_kill"] = target.restart_backend("kill")
        deadline.set_stage("interrupt_source_before_captured_marker")
        lifecycle["source_kill"] = source.restart_backend("kill")
        control.release(key, phase)
        interrupted = await deadline.wait(fork_call, "interrupted_fork_http_response")
        (artifacts / "interrupted-response.json").write_text(interrupted.text)
        assert interrupted.status_code >= 400, interrupted.text
        assert _creation_record(target, key, child_session_node) == frozen

        if source_recovery == "incomplete":
            lifecycle["source_start"] = await deadline.wait(asyncio.to_thread(source.restart_backend, "start"), "restart_partial_source_owner")
        stderr_offset = stderr_path.stat().st_size
        if source_recovery == "transport_unavailable":
            with pytest.raises(RuntimeError, match="后端进程提前退出"):
                await deadline.wait(asyncio.to_thread(target.restart_backend, "start"), "reject_unknown_source_transport_during_startup")
            failed_startup = stderr_path.read_bytes()[stderr_offset:].decode()
            (artifacts / "unknown-transport-startup.stderr.log").write_text(failed_startup)
            assert "Gateway 上下文查询失败" in failed_startup and "path=/api/gateway/federation/relay" in failed_startup
            assert "fork startup 已中止未完成 capture" not in failed_startup
            aborted_pending = _capture_record(target, key)
            assert aborted_pending["state"] == "aborted" and aborted_pending["export_release_state"] == "pending"
            assert aborted_pending["export_release_error"]
            lifecycle["source_start"] = await deadline.wait(asyncio.to_thread(source.restart_backend, "start"), "restore_source_after_loud_transport_failure")
        lifecycle["target_start"] = await deadline.wait(asyncio.to_thread(target.restart_backend, "start"), "recover_target_through_real_gateway_relay")
        if source_recovery == "incomplete":
            startup_log = stderr_path.read_bytes()[stderr_offset:].decode()
            (artifacts / "incomplete-source-startup.stderr.log").write_text(startup_log)
            assert "fork startup 已中止未完成 capture" in startup_log
        else:
            # 原失败已明确中止 creation；重试只确认真实来源释放，不重新创建目标。
            replay = await deadline.wait(target.client.post(
                f"/api/v1/sessions/{source_session_id}/fork-context", json=request,
            ), "confirm_aborted_operation_export_release")
            (artifacts / "aborted-operation-replay.json").write_text(replay.text)
            assert replay.status_code == 500, replay.text

        aborted = _creation_record(target, key, child_session_node)
        target_after = _capture_record(target, key)
        source_after = _capture_record(source, key)
        assert aborted["state"] == "aborted"
        identity_fields = ("preimage_hash", "request_intent_json", "fork_delivery_request_id",
                           "main_thread_id" if child_session_node is None else "child_thread_id")
        assert {field: aborted[field] for field in identity_fields} == {field: frozen[field] for field in identity_fields}
        assert source_after["state"] == target_after["state"] == "aborted"
        assert target_after["export_release_state"] == "confirmed" and target_after["export_release_error"] is None
        receipt = json.loads(target_after["export_release_receipt_json"])
        assert receipt == {field: value for field, value in source_after.items() if not field.startswith("export_release_")}
        for before, after in ((source_before, source_after), (target_before, target_after)):
            assert {field: after[field] for field in ("copy_operation_id", "creation_key", "request_json", "preimage_hash", "snapshot_id", "snapshot_relative_locator")} == {
                field: before[field] for field in ("copy_operation_id", "creation_key", "request_json", "preimage_hash", "snapshot_id", "snapshot_relative_locator")
            }
            assert after["snapshot_sha256"] is None and after["marker_sha256"] is None
        assert not source_root.exists() and not target_root.exists()
        assert _attachment_claims(source, key) == _attachment_claims(target, key) == []
        if child_session_node is None:
            visible = await deadline.wait(target.client.get("/api/v1/session-catalog/export"), "verify_aborted_session_invisible")
            assert visible.status_code == 200, visible.text
            assert target_after["target_session_id"] not in {item["node_id"] for item in visible.json()["data"]["items"]}
        else:
            with sqlite3.connect((child_session_node / "session-control.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
                assert connection.execute("SELECT thread_id FROM thread_catalog WHERE thread_id=?", (target_after["target_thread_id"],)).fetchall() == []

        lifecycle["source_offline"] = source.restart_backend("kill")
        lifecycle["target_second_start"] = await deadline.wait(asyncio.to_thread(target.restart_backend), "idempotent_aborted_startup_source_offline")
        assert _creation_record(target, key, child_session_node) == aborted
        assert _capture_record(target, key) == target_after and _capture_record(source, key) == source_after
        assert _provider_call_count(model) == provider_requests
        (artifacts / "terminal-owner-records.json").write_text(json.dumps({
            "creation": aborted, "source": source_after, "target": target_after,
        }, ensure_ascii=False, indent=2) + "\n")
        deadline.complete()
    finally:
        control.release(key, phase)
        for task in (fork_call, reached):
            if not task.done():
                task.cancel()
        await asyncio.gather(fork_call, reached, return_exceptions=True)
        (artifacts / "backend-lifecycle.json").write_text(json.dumps(lifecycle, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
@pytest.mark.parametrize("failure_window", ["prepared_before_capture", "source_release_reply_lost", "attachment_staged_marker_lost"])
async def test_real_fork_delivery_trace_survives_owner_failure(
    transfer_triad, creation_kind: str, failure_window: str,
):
    nodes, model, artifacts, control, fork_deadlines = transfer_triad
    source, target = nodes["b"], nodes["c"]
    original = "来源 B 的真实输入：恢复后正文必须继续由 C 本地读取。"
    successor = "目标 C 的新输入：使用原分支 thread 的继承历史。"
    reply_text = "确定性恢复验收回应"
    source_session_id = await create_session(source.client, "故障验收来源 B")
    attachment_body = b"x" * FORK_TRANSFER_CHUNK_BYTES + b"y"
    await _send(source, source_session_id, original, attachments=_inline_attachment(attachment_body), model=model, reply=reply_text)
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
    phase = {"prepared_before_capture": "copy.target.prepared",
             "source_release_reply_lost": "copy.source.released",
             "attachment_staged_marker_lost": "copy.attachments.import_staged"}[failure_window]
    control.arm(key, phase)
    deadline = _fork_deadline(target, request, fork_deadlines)
    provider_requests_before_fork = _provider_call_count(model)
    fork_call = asyncio.create_task(target.client.post(
        f"/api/v1/sessions/{source_session_id}/fork-context", json=request,
        headers={"X-Request-ID": first_request_id},
    ))
    reached = asyncio.create_task(asyncio.to_thread(control.acknowledged, key, phase))
    try:
        done, _ = await deadline.wait(
            asyncio.wait((fork_call, reached), return_when=asyncio.FIRST_COMPLETED),
            "wait_for_failure_phase_or_http_response",
        )
        if fork_call in done:
            early_response = await deadline.wait(fork_call, "http_response_before_failure_phase")
            (artifacts / "response-before-failure-window.json").write_text(early_response.text)
            pytest.fail(f"fork 在故障窗口前返回 HTTP {early_response.status_code}: {early_response.text}")
        await deadline.wait(reached, "failure_phase_acknowledged")
        frozen = _creation_record(target, key, child_session_node)
        (artifacts / "creation-before-failure.json").write_text(json.dumps(frozen, ensure_ascii=False, indent=2) + "\n")
        assert frozen["fork_delivery_request_id"] == first_request_id
        assert frozen["request_intent_json"] is not None
        lifecycle = {}
        if failure_window in {"prepared_before_capture", "attachment_staged_marker_lost"}:
            assert frozen["state"] == "preparing"
            if failure_window == "prepared_before_capture":
                assert _capture_record(target, key) is None
            else:
                target_capture = _capture_record(target, key)
                (staged_claim,) = _attachment_claims(target, key)
                assert staged_claim["state"] == "preparing"
                snapshot = json.loads(staged_claim["snapshot_json"])
                staged = resolve_staging_path(target.workspace / ".boxteam/attachments", ingest_staging_relative_locator(
                    fork_import_ingest_id(key, snapshot["blob_id"])))
                assert staged.read_bytes() == attachment_body
                assert target_capture["snapshot_sha256"] is None and target_capture["marker_sha256"] is None
            deadline.set_stage("kill_target_backend_before_capture")
            lifecycle["target_kill"] = target.restart_backend("kill")
            deadline.set_stage("release_phase_barrier")
            control.release(key, phase)
            response = await deadline.wait(fork_call, "interrupted_http_response")
            assert response.status_code >= 400, response.text
            (artifacts / "interrupted-response.json").write_text(response.text)
            if failure_window == "attachment_staged_marker_lost":
                # 关键状态均由真实 POST 创建；只在进程终止后注入 marker 丢失故障。
                assert staged.read_bytes() == attachment_body
                marker = target.workspace / ".boxteam/navigation" / target_capture["snapshot_relative_locator"] / "captured.json"
                marker.unlink()
            deadline.set_stage("restart_target_backend_for_recovery")
            lifecycle["target_start"] = target.restart_backend("start")
            assert lifecycle["target_kill"]["before_pid"] != lifecycle["target_start"]["after_pid"]
        else:
            assert frozen["state"] == "published"
            released = _capture_record(source, key)
            assert released is not None
            assert released["state"] == "materialized"
            assert not (source.workspace / ".boxteam/sessions" / released["snapshot_relative_locator"]).exists()
            (artifacts / "source-release-before-lost-reply.json").write_text(json.dumps(released, ensure_ascii=False, indent=2) + "\n")
            deadline.set_stage("kill_source_backend_after_release")
            lifecycle["source_kill_after_release"] = source.restart_backend("kill")
            deadline.set_stage("release_phase_barrier")
            control.release(key, phase)
            response = await deadline.wait(fork_call, "pending_release_http_response")
            (artifacts / "pending-release-response.json").write_text(response.text)
            assert response.status_code == 200, response.text
            assert response.json()["data"]["export_release"]["state"] == "pending"
            result = response.json()["data"]
            detail = result["export_release"]["error"]
            assert all(value in detail for value in (
                f"operation_id={key}", f"session_id={result['session']['session_id']}",
                f"thread_id={result['thread_id']}", "stage=source_export_release",
                "error=WorkspaceSessionContextAccessError",
            ))
            assert all(str(node.workspace) not in response.text for node in nodes.values())
            pending_capture = _capture_record(target, key)
            assert pending_capture["export_release_state"] == "pending"
            assert pending_capture["export_release_error"] == detail
            deadline.set_stage("restart_source_backend_for_release_recovery")
            lifecycle["source_start"] = source.restart_backend("start")
            deadline.set_stage("restart_target_backend_for_release_recovery")
            lifecycle["target_release_recovery"] = target.restart_backend()
            assert lifecycle["target_release_recovery"]["before_pid"] != lifecycle["target_release_recovery"]["after_pid"]
    finally:
        deadline.set_stage("cleanup_phase_barrier_and_client_task")
        control.release(key, phase)
        for task in (fork_call, reached):
            if not task.done():
                task.cancel()
        await asyncio.gather(fork_call, reached, return_exceptions=True)

    if failure_window == "attachment_staged_marker_lost":
        aborted = _creation_record(target, key, child_session_node)
        assert aborted["state"] == "aborted"
        thread_field = "child_thread_id" if child_session_id is not None else "main_thread_id"
        for field in ("preimage_hash", "request_intent_json", "fork_delivery_request_id", thread_field):
            assert aborted[field] == frozen[field]
        (settled_claim,) = _attachment_claims(target, key)
        assert settled_claim == {**staged_claim, "state": "aborted"}
        assert not staged.exists()
        assert _capture_record(target, key)["state"] == "aborted"
        assert _attachment_claims(source, key) == []
        assert _provider_call_count(model) == provider_requests_before_fork
        lifecycle["target_second_start"] = await deadline.wait(asyncio.to_thread(target.restart_backend), "retry_terminal_staging_cleanup")
        assert _creation_record(target, key, child_session_node) == aborted
        assert _attachment_claims(target, key) == [settled_claim] and not staged.exists()
        original_content = await deadline.wait(source.client.get(
            f"/api/v1/sessions/{source_session_id}/attachments/content",
            params={"file_id": attachment_source["resource_uri"], "variant": "original"},
        ), "source_attachment_survives_target_abort")
        assert original_content.status_code == 200 and original_content.content == attachment_body
        (artifacts / "aborted-import-staging.json").write_text(json.dumps({
            "creation": aborted, "claim": settled_claim, "staging": str(staged), "staging_exists": staged.exists(),
        }, ensure_ascii=False, indent=2) + "\n")
        (artifacts / "backend-lifecycle.json").write_text(json.dumps(lifecycle, ensure_ascii=False, indent=2) + "\n")
        deadline.complete()
        return

    retry = await _post_fork(
        target, source_session_id, request, deadline, stage="same_operation_recovery_retry",
        headers={"X-Request-ID": "req-new-http-delivery-after-recovery"},
    )
    (artifacts / "recovered-response.json").write_text(retry.text)
    assert retry.status_code == 200, retry.text
    published = retry.json()["data"]
    assert retry.headers["x-request-id"] == "req-new-http-delivery-after-recovery"
    assert published["session"]["workspace_id"] == target.workspace_id
    assert published["export_release"] == {"state": "confirmed", "error": None}
    deadline.last_stage = "operation_completed"
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
    assert _provider_call_count(model) == provider_requests_before_fork
    assert len(published["resources"]) == 1
    attachment_result = published["resources"][0]
    assert attachment_result["resource_uri"] == attachment_source["resource_uri"]
    assert attachment_result["action"] == "copy_snapshot" and attachment_result["availability"] == "available"
    target_file_id = attachment_result["target_uri"]
    assert parse_session_artifact_uri(target_file_id).tail_segments[:2] == (target_session_id, "attachments")
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
    offline_retry = await _post_fork(
        target, source_session_id, request, _fork_deadline(target, request, fork_deadlines),
        stage="completed_idempotency_replay_source_offline",
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
    assert _provider_call_count(model) == provider_requests_before_fork
    await _send(target, target_session_id, successor, thread_id=published["thread_id"], model=model, reply=reply_text)
    assert _provider_call_count(model) == provider_requests_before_fork + 1
    successor_wire = json.dumps(_provider_requests(model)[provider_requests_before_fork:], ensure_ascii=False)
    assert original in successor_wire
    assert successor in successor_wire
