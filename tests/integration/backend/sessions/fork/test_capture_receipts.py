"""真实三端传输的冻结坐标与来源离线后的幂等回执，不替代大附件验收。"""

from __future__ import annotations

import hashlib
import json

from app.core.resource_uri import parse_resource_uri, resource_display_uri
from app.core.session_catalog_store.contracts import ForkCapturedPoint
from app.services.business.session_context_resource import (
    workspace_session_inventory_uri,
)
from tests.integration.backend.sessions.test_rollout_fork_transfer import (
    _capture_record,
    _fork_deadline,
    _post_fork,
    _provider_call_count,
    _send,
)
from tests.integration.backend.sessions.test_rollout_fork_transfer import (
    transfer_triad as transfer_triad,  # noqa: PLC0414
)
from tests.support.api_helpers import create_session


async def test_real_capture_receipt_survives_source_offline_and_target_restart(transfer_triad):
    nodes, model, artifacts, _control, deadlines = transfer_triad
    source, target = nodes["b"], nodes["c"]
    original = "capture-receipt-source-question"
    source_reply = "capture-receipt-source-answer"
    source_session = await create_session(source.client, "冻结点回执来源")
    await _send(source, source_session, original, model=model, reply=source_reply)
    response = await source.client.get(
        f"/api/v1/sessions/{source_session}/fork-source-point", params={"mode": "full_rollout_copy"},
    )
    assert response.status_code == 200, response.text
    point = response.json()["data"]
    source_uri = parse_resource_uri(point["source_uri"])
    request = {
        "idempotency_key": "real-capture-receipt", "mode": "full_rollout_copy",
        "source_uri": resource_display_uri(
            scope=source_uri.scope, scope_id=source_uri.scope_id, kind=source_uri.kind,
            tail_segments=source_uri.tail_segments, gateway_authority=source.gateway_id,
        ),
        "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
        "target_container_uri": workspace_session_inventory_uri(target.workspace_id, gateway_authority=target.gateway_id),
        "creation_kind": "new_session_main",
    }
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
            "anchor_item_id": None,
        } == {field: published[field] for field in ("source_view_id", "source_view_revision", "anchor_item_id")}
    assert source_capture["source_point_json"] == target_capture["source_point_json"]
    assert source_capture["marker_sha256"] == target_capture["marker_sha256"]
    assert source_capture["snapshot_sha256"] == target_capture["snapshot_sha256"]
    assert _provider_call_count(model) == calls_before

    source_stop = source.restart_backend("kill")
    target_restart = target.restart_backend()
    assert source.backend.process.poll() is not None
    assert target_restart["before_pid"] != target_restart["after_pid"]
    target_session = published["session"]["session_id"]
    response = await target.client.get(f"/api/v1/sessions/{target_session}/messages")
    assert response.status_code == 200, response.text
    assert original in response.text and source_reply in response.text
    repeated = await _post_fork(
        target, source_session, request, _fork_deadline(target, request, deadlines), stage="offline cold receipt replay",
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"] == published
    assert _capture_record(target, key) == target_capture
    assert _provider_call_count(model) == calls_before

    await _send(target, target_session, "capture-receipt-successor", model=model, reply="capture-receipt-new-answer")
    assert _provider_call_count(model) == calls_before + 1
    (artifacts / "backend-lifecycle.json").write_text(json.dumps({
        "source_stop": source_stop, "target_restart": target_restart,
        "source_capture": source_capture, "target_capture": target_capture,
    }, ensure_ascii=False, indent=2) + "\n")
