"""真实三端传输的冻结坐标与来源离线后的幂等回执，不替代大附件验收。"""

from __future__ import annotations

import hashlib
import json

from app.core.resource_uri import parse_resource_uri, resource_display_uri
from app.core.session_catalog_store.contracts import ForkCapturedPoint
from app.domain.itemized.serialization import normalize_wire_request
from app.services.business.session_context_resource import (
    workspace_session_inventory_uri,
)
from app.services.infrastructure.mcp.catalog_activation import (
    MCP_TOOL_CATALOG_RESOURCE_ID,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.test_rollout_fork_transfer import (
    _capture_record,
    _fork_deadline,
    _post_fork,
    _provider_call_count,
    _provider_requests,
    _send,
)
from tests.integration.backend.sessions.test_rollout_fork_transfer import (
    transfer_triad as transfer_triad,  # noqa: PLC0414
)
from tests.support.api_helpers import create_session
from tests.support.communication.federation import CommunicationNode
from tests.support.session_navigation.http_operations import submit_navigation_operation


def _sealed_aggregates(
    node: CommunicationNode, session: str, thread: str, *,
    assembly_ids: tuple[str, ...] | None = None, wire: object | None = None,
) -> dict[str, dict[str, object]]:
    """只消费产品封存结果，验证原请求、空地址和正文不依赖来源进程。"""
    aggregates = {"skill-entry:workspace:catalog:catalog", MCP_TOOL_CATALOG_RESOURCE_ID}
    result = {}
    with RolloutCheckpointSaver(node.workspace / ".boxteam/sessions") as saver:
        assemblies = saver.list_context_assemblies(session, thread) if assembly_ids is None else tuple(
            saver.get_context_assembly(session, thread, assembly_id=identity) for identity in assembly_ids
        )
        assert assemblies
        for assembly in assemblies:
            assembly.validate_hashes()
            if wire is not None:
                assert assembly.request_hash_preimage == normalize_wire_request(wire)
            activation = saver.load_sealed_resource_activation(
                session, thread_id=thread, assembly_id=assembly.assembly_id,
                plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
            )
            bindings = {binding.resource_id: binding for binding in activation.bindings}
            assert aggregates <= bindings.keys()
            assert all(bindings[identity].to_dict()["display_uri"] is None for identity in aggregates)
            result[assembly.assembly_id] = {
                "assembly": assembly.to_dict(), "activation": activation.to_dict(),
                "payloads": {binding.resource_id: saver.read_resource_activation_payload(
                    activation, binding, storage_owner=(session, thread),
                ) for binding in activation.bindings},
            }
        with saver._storage.storage.read_connection(session, thread) as connection:
            assert connection.execute(
                "SELECT DISTINCT display_uri FROM resource_activation_bindings WHERE resource_id IN (?, ?)",
                tuple(sorted(aggregates)),
            ).fetchall() == [(None,)]
    return result


async def test_real_capture_receipt_survives_source_offline_and_target_restart(transfer_triad):
    await _capture_receipt(transfer_triad, prefix=False)


async def test_real_item_only_receipt_survives_source_offline_and_target_restart(transfer_triad):
    await _capture_receipt(transfer_triad, prefix=True)


async def _capture_receipt(transfer_triad, *, prefix: bool):
    nodes, model, artifacts, _control, deadlines = transfer_triad
    source, target = nodes["b"], nodes["c"]
    original = "capture-receipt-source-question"
    source_reply = "capture-receipt-source-answer"
    source_session = await create_session(source.client, "冻结点回执来源")
    source_job = await _send(source, source_session, original, model=model, reply=source_reply)
    source_node = await source.session_node(source_session)
    source_thread = source_job["thread_id"]
    assert isinstance(source_thread, str)
    sealed = _sealed_aggregates(
        source, source_session, source_thread, wire=_provider_requests(model)[0],
    )
    assert len(sealed) == 1
    (artifacts / "sealed-aggregates.json").write_text(json.dumps(sealed, ensure_ascii=False, indent=2) + "\n")
    mode = "view_prefix_fork" if prefix else "full_rollout_copy"
    response = await source.client.get(
        f"/api/v1/sessions/{source_session}/fork-source-point", params={"mode": mode},
    )
    assert response.status_code == 200, response.text
    point = response.json()["data"]
    source_uri = parse_resource_uri(point["source_uri"])
    request = {
        "idempotency_key": "real-prefix-receipt" if prefix else "real-capture-receipt", "mode": mode,
        "source_uri": resource_display_uri(
            scope=source_uri.scope, scope_id=source_uri.scope_id, kind=source_uri.kind,
            tail_segments=source_uri.tail_segments, gateway_authority=source.gateway_id,
        ),
        "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
        "target_container_uri": workspace_session_inventory_uri(target.workspace_id, gateway_authority=target.gateway_id),
        "creation_kind": "new_session_main",
    }
    if prefix:
        del request["source_view_id"]
        request["anchor_item_id"] = point["anchor_item_id"]
        await _send(source, source_session, "capture-receipt-excluded-tail", model=model, reply="capture-receipt-excluded-answer")
        response = await source.client.get(f"/api/v1/sessions/{source_session}/fork-source-point", params={
            "mode": mode, "item_id": point["anchor_item_id"],
        })
        assert response.status_code == 200, response.text
        point = response.json()["data"]
        assert point["source_view_revision"] > request["source_view_revision"]
    calls_before = _provider_call_count(model)
    response = await _post_fork(
        source, source_session, request, _fork_deadline(target, request, deadlines), stage="real capture transfer",
    )
    (artifacts / "published-response.json").write_text(response.text)
    assert response.status_code == 200, response.text
    published = response.json()["data"]
    assert published["export_release"] == {"state": "confirmed", "error": None}
    key = "fork-" + hashlib.sha256(request["idempotency_key"].encode()).hexdigest()
    source_capture, target_capture = (_capture_record(node, key) for node in (source, target))
    for capture in (source_capture, target_capture):
        frozen = ForkCapturedPoint.from_frozen_json(
            capture["source_point_json"], preimage_hash=capture["preimage_hash"],
            snapshot_sha256=capture["snapshot_sha256"], marker_sha256=capture["marker_sha256"],
        )
        assert frozen.model_dump() == {
            "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
            "anchor_item_id": point["anchor_item_id"] if prefix else None,
        } == {field: published[field] for field in ("source_view_id", "source_view_revision", "anchor_item_id")}
    assert source_capture["source_point_json"] == target_capture["source_point_json"]
    assert source_capture["marker_sha256"] == target_capture["marker_sha256"]
    assert source_capture["snapshot_sha256"] == target_capture["snapshot_sha256"]
    assert _provider_call_count(model) == calls_before
    target_session, target_thread = published["session"]["session_id"], published["thread_id"]
    assert _sealed_aggregates(target, target_session, target_thread, assembly_ids=tuple(sealed)) == sealed

    deletion = await submit_navigation_operation(
        source.client, "delete_session", target_node_id=source_session, wait_for_settlement=True,
    )
    assert deletion["state"] == "committed" and deletion["settlement"] == "settled", deletion
    assert not source_node.exists()

    source_stop = source.restart_backend("kill")
    target_restart = target.restart_backend()
    assert source.backend.process.poll() is not None
    assert target_restart["before_pid"] != target_restart["after_pid"]
    assert _sealed_aggregates(target, target_session, target_thread, assembly_ids=tuple(sealed)) == sealed
    response = await target.client.get(f"/api/v1/sessions/{target_session}/messages")
    assert response.status_code == 200, response.text
    assert original in response.text and source_reply in response.text
    assert "capture-receipt-excluded" not in response.text
    repeated = await _post_fork(
        target, source_session, request, _fork_deadline(target, request, deadlines), stage="offline cold receipt replay",
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"] == published
    assert _capture_record(target, key) == target_capture
    assert _provider_call_count(model) == calls_before

    await _send(target, target_session, "capture-receipt-successor", model=model, reply="capture-receipt-new-answer")
    assert _provider_call_count(model) == calls_before + 1
    assert _sealed_aggregates(target, target_session, target_thread, assembly_ids=tuple(sealed)) == sealed
    (artifacts / "backend-lifecycle.json").write_text(json.dumps({
        "source_stop": source_stop, "target_restart": target_restart,
        "source_capture": source_capture, "target_capture": target_capture, "source_deletion": deletion,
    }, ensure_ascii=False, indent=2) + "\n")
