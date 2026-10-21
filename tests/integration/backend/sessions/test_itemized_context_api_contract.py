"""真实 Workspace HTTP 检查面读取 Saver sealed selection；不代表 Web UI 验收。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.refs import ContextRef, selection_ref_identity
from app.domain.itemized.records import CanonicalItemRecord
from app.schemas.internal_v2.session_context import SessionContextReadResultDTO
from app.services.business.session_context_resource import (
    context_assembly_identity,
    session_resource_uri,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
    detail_relative_path,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)
from app.services.mapping.itemized.resource_activation import (
    project_sealed_resource_refs,
)
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    EMPTY_TOOL_SET,
    dispatch_execution_scope,
    inspection_resource_registry,
    invoke_provider_dispatch,
    native_http_server,
    seed_dispatch_history,
    seed_dispatch_overlay,
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    register_projection_draft,
)
from tests.support.process_scope import unregister_test_process
from tests.support.processes import close_backend_process, start_backend_process

__all__ = ["inspection_resource_registry", "native_http_server"]


@pytest.fixture(scope="module")
def context_backend(
    integration_workspace_root_path,
    integration_workspace_config_path,
    integration_backend_port,
):
    assert Path(integration_workspace_config_path).is_file()
    handles = [
        start_backend_process(
            workspace_root=integration_workspace_root_path,
            port=integration_backend_port,
            log_name="itemized-context-api",
        )
    ]
    try:
        yield handles
    finally:
        close_backend_process(handles[0])


@pytest.fixture(scope="module")
def integration_backend_process(context_backend):
    """同一正式端口可重启，沿用正式 integration_client 的 HTTP/认证 fixture。"""
    return context_backend[0].process


@pytest.fixture
async def context_source(integration_client, integration_workspace_root_path, inspection_resource_registry):
    response = await integration_client.post(
        "/api/v1/sessions", json={"title": "assembly inspection contract"}
    )
    assert response.status_code == 200, response.text
    session = response.json()["data"]
    session_id = session["session_id"]
    sessions = Path(integration_workspace_root_path) / ".boxteam/sessions"
    with RolloutCheckpointSaver(sessions) as saver:
        turn_id = seed_dispatch_history(saver, session_id, registry=inspection_resource_registry)
        seed_dispatch_overlay(saver, session_id)
        with dispatch_execution_scope(saver, session_id, turn_id):
            yield saver, session, turn_id


def _resource(session, snapshot=None):
    resource = {key: session[key] for key in ("resource_identity", "uri", "scope")}
    if snapshot is not None:
        resource["assembly_ref"] = context_assembly_identity(
            session["resource_identity"], snapshot.assembly_id
        )
    return resource


async def _read_pages(client, resource, artifacts, *, max_chars=12000, limit=2):
    """校验线上 DTO 字符预算并还原既有分页器的 item chunk。"""
    items, pages = [], []
    chunk_text = ""
    cursor = None
    seen_cursors = set()
    for _ in range(100):
        response = await client.post(
            "/api/v1/context/read",
            json={
                **resource,
                "view": "assembly",
                "limit": limit,
                "max_chars": max_chars,
                "cursor": cursor,
                "include": ["visible_text", "tool_summary"],
            },
        )
        assert response.status_code == 200, response.text
        envelope = response.json()
        assert envelope["request_id"] == response.headers["X-Request-ID"]
        page = envelope["data"]
        assert {key: page[key] for key in resource} == resource
        assert page["returned_chars"] <= max_chars
        assert (
            len(SessionContextReadResultDTO.model_validate(page).model_dump_json())
            == page["returned_chars"]
        )
        assert len(page["items"]) <= limit
        if pages:
            assert page["revision"] == pages[0]["revision"]
        pages.append(page)
        for item in page["items"]:
            if not item["kind"].endswith("_chunk"):
                assert not chunk_text
                items.append(item)
                continue
            assert item["data"]["chunk_start"] == len(chunk_text)
            chunk_text += item["text"]
            if not item["truncated"]:
                items.append(json.loads(chunk_text))
                chunk_text = ""
        cursor = page["next_cursor"]
        if not page["has_more"]:
            assert cursor is None and not chunk_text
            break
        assert cursor and cursor not in seen_cursors
        seen_cursors.add(cursor)
    else:
        pytest.fail("assembly HTTP cursor 未收敛")
    (artifacts / f"context-pages-{uuid4().hex}.json").write_text(
        json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return items, pages


def _assert_snapshot(items, snapshot, saver, session_id):
    selection = [item["data"] for item in items if item["kind"] == "selection"]
    assert selection == [entry.to_dict() for entry in snapshot.selection]
    assert [entry["plan_ordinal"] for entry in selection] == list(range(len(selection)))
    projection = saver.project_context_plan_to_history_with_diagnostics(
        session_id, saver._storage.storage.main_thread_id(session_id), snapshot.as_sealed_plan()
    )
    history, loss = projection.messages, projection.losses
    expected_resources = (
        [resource.model_dump(mode="json") for resource in project_sealed_resource_refs(projection.resource_activation)]
        if projection.resource_activation is not None else []
    )
    assert items[0]["resource_refs"] == expected_resources
    assert all(not item["resource_refs"] for item in items[1:])
    for resource in expected_resources:
        assert set(resource) == {"display_uri", "resource_kind", "owner_scope", "facet", "revision", "availability", "provenance_ref"}
        assert set(resource["provenance_ref"]) == {"kind", "token"}
        assert resource["provenance_ref"]["kind"] == "sealed_resource"
    for private in ("INSPECTION_PRIVATE_REVISION", "INSPECTION_PRIVATE_RESOURCE_BODY", "INSPECTION_PRIVATE_LINEAGE"):
        assert private not in json.dumps(items)
    messages = [item for item in items if item["kind"] == "message"]
    assert [item["data"]["message_id"] for item in messages] == [
        message.id for message in history
    ]
    assert [item["text"] for item in messages] == [message.text for message in history]
    assert [
        item["data"]["loss"]
        for item in items
        if item["kind"] == "loss" and item["data"]["projection"] == "history"
    ] == list(loss)
    assert [
        item["data"]["loss"]
        for item in items
        if item["kind"] == "loss" and item["data"]["projection"] == "assembly"
    ] == list(snapshot.loss)
    assert items[0]["data"]["selection_count"] == len(selection)
    assert items[0]["data"]["history_loss_count"] == len(loss)
    assert items[0]["data"]["assembly_loss_count"] == len(snapshot.loss)
    assert "HTTP overlay" not in json.dumps(items, ensure_ascii=False)
    return messages


@pytest.mark.asyncio
async def test_frozen_selection_http_and_active_history_survive_backend_restart(
    context_source,
    integration_client,
    native_http_server,
    context_backend,
    inspection_resource_registry,
    monkeypatch,
):
    saver, session, turn_id = context_source
    session_id = session["session_id"]
    state, endpoint = native_http_server
    await invoke_provider_dispatch(saver, session_id, turn_id, endpoint, state.api_key, target_format="responses")
    (snapshot,) = saver.list_context_assemblies(
        session_id, saver._storage.storage.main_thread_id(session_id)
    )
    resource = _resource(session, snapshot)
    # 真实读取只开一个权威快照，assembly/activation/items均消费同一对象。
    reads = []
    activation_reads = []
    item_reads = []
    storage = saver._storage.storage
    open_snapshot = storage.open_read_snapshot
    load_activation = saver.load_sealed_resource_activation
    read_ref_items = storage.read_ref_items
    def observe_open(*args, **kwargs):
        value = open_snapshot(*args, **kwargs)
        reads.append(value)
        return value
    def observe_activation(*args, **kwargs):
        activation_reads.append(kwargs["snapshot"])
        return load_activation(*args, **kwargs)
    def observe_items(*args, **kwargs):
        item_reads.append(kwargs["snapshot"])
        return read_ref_items(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(storage, "open_read_snapshot", observe_open)
        patch.setattr(saver, "load_sealed_resource_activation", observe_activation)
        patch.setattr(storage, "read_ref_items", observe_items)
        projected = saver.project_context_plan_to_history_with_diagnostics(
            session_id, saver._storage.storage.main_thread_id(session_id), snapshot.as_sealed_plan()
        )
    assert len(reads) == len(activation_reads) == len(item_reads) == 1
    assert activation_reads[0] is item_reads[0]
    assert projected.assembly == snapshot
    assert len(projected.resource_activation.bindings) == 2
    canonical_before = saver.read_canonical_items(session_id)
    # 当前发布内容前进，旧assembly仍使用冻结值；重启后也不能读当前来源回填。
    for current in inspection_resource_registry.snapshots():
        inspection_resource_registry.publish(replace(
            current, revision="CURRENT_RESOURCE_REVISION", payload={"instruction": "CURRENT_RESOURCE_BODY"}, generation=8,
        ))
    items, pages = await _read_pages(integration_client, resource, state.artifacts)
    messages = _assert_snapshot(items, snapshot, saver, session_id)
    assert len(pages) > 1
    assert saver.read_canonical_items(session_id) == canonical_before
    assert {ref["display_uri"] for ref in items[0]["resource_refs"]} == {
        "boxteam://workspace/test/resources/skills/inspection/SKILL.md", None,
    }
    assert len({ref["provenance_ref"]["token"] for ref in items[0]["resource_refs"]}) == 2
    assert "CURRENT_RESOURCE" not in json.dumps(items)
    chunked, chunk_pages = await _read_pages(
        integration_client, resource, state.artifacts, max_chars=2400, limit=1,
    )
    assert any(item["kind"] == "assembly_chunk" for page in chunk_pages for item in page["items"])
    assert chunked == items
    assert any(ref["display_uri"] is None for ref in chunked[0]["resource_refs"])
    assert "native-http-result" not in json.dumps(messages)
    native = saver.project_context_plan_to_native(
        session_id, saver._storage.storage.main_thread_id(session_id), snapshot.as_sealed_plan()
    )
    assert state.requests[0]["body"]["input"] == native["request"]["input"]
    assert native["selection"] == [entry.to_dict() for entry in snapshot.selection]
    assert state.requests[0]["authorization"] == f"Bearer {state.api_key}"
    assert state.requests[0]["path"] == "/v1/responses"

    # 普通消息/聊天 history 继续覆盖 active view，包括 seal 之后新生成的输出。
    for path, payload in (
        (
            "/api/v1/context/read",
            {**_resource(session), "view": "messages"},
        ),
        (
            f"/api/v1/sessions/{session_id}/history",
            {"turn_ids": [turn_id], "include": ["user", "assistant_text", "metadata"]},
        ),
    ):
        response = await integration_client.post(path, json=payload)
        assert response.status_code == 200, response.text
        assert "native-http-result" in response.text
        assert "HTTP overlay" not in response.text

    previous_handle = context_backend[0]
    previous_pid = previous_handle.process.pid
    await asyncio.to_thread(close_backend_process, previous_handle)
    context_backend[0] = await asyncio.to_thread(
        start_backend_process,
        workspace_root=previous_handle.workspace_root,
        port=previous_handle.port,
        log_name="itemized-context-api-restarted",
    )
    # 新进程归模块fixture持有；不能让首个用例的function scope提前回收它。
    unregister_test_process(context_backend[0])
    assert context_backend[0].process.pid != previous_pid
    (state.artifacts / "backend-restart.json").write_text(
        json.dumps(
            {
                "before_pid": previous_pid,
                "after_pid": context_backend[0].process.pid,
                "port": previous_handle.port,
                "workspace": previous_handle.workspace_root,
                **resource,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    restored, restored_pages = await _read_pages(
        integration_client, resource, state.artifacts
    )
    assert restored == items
    assert restored_pages[0]["revision"] == pages[0]["revision"]

    # 新 request 使 history revision 前进，旧 assembly 的分页游标仍绑定旧快照。
    await invoke_provider_dispatch(saver, session_id, turn_id, endpoint, state.api_key, target_format="responses")
    second = next(
        value
        for value in saver.list_context_assemblies(
            session_id, saver._storage.storage.main_thread_id(session_id)
        )
        if value.assembly_id != snapshot.assembly_id
    )
    assert second.history_view_revision > snapshot.history_view_revision
    response = await integration_client.post(
        "/api/v1/context/read",
        json={
            **resource,
            "view": "assembly",
            "limit": 2,
            "include": ["visible_text", "tool_summary"],
            "max_chars": 12000,
            "cursor": pages[0]["next_cursor"],
            "revision": pages[0]["revision"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"] == pages[1]
    current, _ = await _read_pages(
        integration_client, _resource(session, second), state.artifacts
    )
    _assert_snapshot(current, second, saver, session_id)
    assert "native-http-result" in json.dumps(current)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "omission", ["none", "canonical_item", "request_only", "tool_set", "all"]
)
async def test_http_selection_preserves_order_omission_toolset_and_typed_details(
    context_source,
    integration_client,
    native_http_server,
    omission,
):
    saver, session, turn_id = context_source
    session_id = session["session_id"]
    state, _ = native_http_server
    draft = saver.compose_committed_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan_id=f"api-plan-{uuid4().hex}",
        tool_set=ToolSetSnapshot(
            selection_revision=sha256_jcs({"inspect_file": True}),
            tools=(
                {
                    "tool_id": "inspect_file",
                    "name": "inspect_file",
                    "parameters": {"type": "object", "properties": {}},
                },
            ),
        ),
    )
    canonical = [ref for ref in draft.refs if ref.ref_type == "canonical_item"]
    overlay = [ref for ref in draft.refs if ref.ref_type == "request_only"]
    plain_body = [{"type": "text", "text": "private request-only API body"}]
    plain = ContextRef.request_only_ref(
        "plain-api",
        session_id=session_id,
        thread_id=draft.thread_id,
        plan_id=draft.plan_id,
        source_revision="api-v1",
        source_ref="source:plain-api",
        payload_kind="structured_content",
        content=plain_body,
    )
    draft = replace(draft, refs=(canonical[1], *overlay, plain, canonical[0]))
    omitted = tuple(
        selection_ref_identity(ref)
        for ref in (*draft.refs, *draft.tool_set_refs)
        if omission in (ref.ref_type, "all")
    )
    draft = register_projection_draft(saver, session_id, draft)
    snapshot = saver.seal_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        draft,
        wire_profile=None, wire_params=None, seal_idempotency_key=f"seal:{draft.plan_id}",
        turn_id=turn_id,
        execution_id=saver.execution_for_turn(
            session_id,
            turn_id=turn_id,
            thread_id=saver._storage.storage.main_thread_id(session_id),
        ),
        provider_version="api-contract",
        omitted_ref_identities=omitted,
        request_only_content={"plain-api": plain_body},
    )
    items, _ = await _read_pages(
        integration_client, _resource(session, snapshot), state.artifacts
    )
    _assert_snapshot(items, snapshot, saver, session_id)
    assert [
        selection_ref_identity(entry.ref) for entry in snapshot.selection if not entry.included
    ] == list(omitted)
    assert {entry.ref.ref_type for entry in snapshot.selection} == {
        "canonical_item",
        "request_only",
        "tool_set",
    }
    assert "private request-only API body" not in json.dumps(items)
    included = [
        entry
        for entry in snapshot.selection
        if entry.included and entry.ref.ref_type == "request_only"
    ]
    for entry in included:
        assert entry.detail_ref.session_id == session_id
        assert entry.detail_ref.assembly_id == snapshot.assembly_id
        assert (
            entry.contribution_id is None
            if entry.ref.ref_id == "plain-api"
            else entry.contribution_id is not None
        )
    if omission != "none":
        assert any(item["kind"] == "loss" for item in items)


@pytest.mark.asyncio
async def test_context_http_rejects_invalid_selectors_and_cross_owner_cursors(
    context_source,
    integration_client,
    native_http_server,
):
    saver, session, turn_id = context_source
    session_id = session["session_id"]
    state, _ = native_http_server
    snapshots = []
    for _ in range(2):
        plan = saver.compose_committed_context_plan(
            session_id,
            saver._storage.storage.main_thread_id(session_id),
            plan_id=f"api-{uuid4().hex}",
            tool_set=EMPTY_TOOL_SET,
        )
        plan = register_projection_draft(saver, session_id, plan)
        snapshots.append(
            saver.seal_context_plan(
                session_id,
                saver._storage.storage.main_thread_id(session_id),
                plan,
                wire_profile=None, wire_params=None, seal_idempotency_key=f"seal:{plan.plan_id}",
                turn_id=turn_id,
                execution_id=saver.execution_for_turn(
                    session_id,
                    turn_id=turn_id,
                    thread_id=saver._storage.storage.main_thread_id(session_id),
                ),
                provider_version="api-contract",
            )
        )
    resource = _resource(session, snapshots[0])
    _, pages = await _read_pages(integration_client, resource, state.artifacts)
    other = await integration_client.post(
        "/api/v1/sessions", json={"title": "other owner"}
    )
    assert other.status_code == 200, other.text
    other_id = other.json()["data"]["session_id"]
    base = {**resource, "view": "assembly"}
    cases = [
        ({"view": "messages"}, 400),
        ({"assembly_ref": None}, 400),
        ({"assembly_ref": ""}, 422),
        ({"assembly_ref": "bad/path"}, 422),
        (
            {
                "assembly_ref": context_assembly_identity(
                    session["resource_identity"], "missing"
                )
            },
            404,
        ),
        (_resource(other.json()["data"], snapshots[0]), 404),
        ({"uri": session_resource_uri("wrong", session_id)}, 400),
        ({"uri": session["uri"] + "#assembly=private-selector"}, 400),
        ({"revision": "stale"}, 409),
        ({"cursor": "invalid-base64"}, 400),
        ({"cursor": pages[0]["next_cursor"], "include": ["reasoning"]}, 409),
        ({"cursor": pages[0]["next_cursor"], **_resource(session, snapshots[1])}, 409),
    ]
    errors = []
    for overrides, status in cases:
        response = await integration_client.post(
            "/api/v1/context/read", json={**base, **overrides}
        )
        assert response.status_code == status, (overrides, response.text)
        assert "HTTP canonical" not in response.text
        errors.append(
            {
                "request": {**base, **overrides},
                "status": status,
                "response": response.json(),
            }
        )
    (state.artifacts / "context-errors.json").write_text(
        json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assert saver.list_context_assemblies(
        session_id, saver._storage.storage.main_thread_id(session_id)
    ) == tuple(snapshots)
    other_thread = saver._storage.storage.main_thread_id(other_id)
    other_rollout = saver._storage.storage.root(other_id, thread_id=other_thread)
    assert not other_rollout.exists()
    with pytest.raises(RolloutNotMaterializedError):
        saver.list_context_assemblies(other_id, other_thread)
    response = await integration_client.post(
        "/api/v1/context/read",
        json={**_resource(other.json()["data"]), "view": "assemblies"},
    )
    assert response.status_code == 404, response.text
    assert "rollout 尚未物化" in response.json()["detail"]
    assert other_id in response.json()["detail"]
    assert other_thread in response.json()["detail"]
    assert str(other_rollout) not in response.text
    assert str(saver._storage.storage.sessions_dir) not in response.text
    assert not other_rollout.exists()


@pytest.mark.asyncio
async def test_context_http_selection_chunks_are_bounded_and_lossless(
    context_source,
    integration_client,
    native_http_server,
):
    saver, session, turn_id = context_source
    session_id = session["session_id"]
    state, _ = native_http_server
    plan = saver.compose_committed_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan_id=f"api-{uuid4().hex}",
        tool_set=EMPTY_TOOL_SET,
    )
    plan = register_projection_draft(saver, session_id, plan)
    snapshot = saver.seal_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan,
        wire_profile=None, wire_params=None, seal_idempotency_key=f"seal:{plan.plan_id}",
        turn_id=turn_id,
        execution_id=saver.execution_for_turn(
            session_id,
            turn_id=turn_id,
            thread_id=saver._storage.storage.main_thread_id(session_id),
        ),
        provider_version="api-contract",
    )
    items, pages = await _read_pages(
        integration_client,
        _resource(session, snapshot),
        state.artifacts,
        max_chars=2400,
        limit=1,
    )
    assert any(
        item["kind"] == "selection_chunk" for page in pages for item in page["items"]
    )
    _assert_snapshot(items, snapshot, saver, session_id)


@pytest.mark.asyncio
async def test_context_http_reports_protected_reasoning_capability_loss(
    context_source,
    integration_client,
    native_http_server,
):
    saver, session, turn_id = context_source
    session_id = session["session_id"]
    state, _ = native_http_server
    opaque = CanonicalItemRecord.create(
        item_id="api-opaque",
        item_sequence=3,
        semantic_kind="reasoning",
        payload_kind="opaque",
        status="completed",
        producer_ref={"producer_kind": "provider", "producer_id": "api-contract"},
        payload={
            "encoding": "base64",
            "wire_type": "encrypted_reasoning",
            "schema_version": "v1",
            "value": "c2VjcmV0",
            "protection": {"encrypted": True},
        },
        turn_id=turn_id,
        turn_scope="turn_member",
    )
    saver.append_items(
        session_id, (opaque,), thread_id=saver._storage.storage.main_thread_id(session_id)
    )
    plan = saver.compose_committed_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan_id=f"api-{uuid4().hex}",
        tool_set=EMPTY_TOOL_SET,
    )
    plan = register_projection_draft(saver, session_id, plan)
    snapshot = saver.seal_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan,
        wire_profile=None, wire_params=None, seal_idempotency_key=f"seal:{plan.plan_id}",
        turn_id=turn_id,
        execution_id=saver.execution_for_turn(
            session_id,
            turn_id=turn_id,
            thread_id=saver._storage.storage.main_thread_id(session_id),
        ),
        provider_version="api-contract",
    )
    items, _ = await _read_pages(
        integration_client, _resource(session, snapshot), state.artifacts
    )
    _assert_snapshot(items, snapshot, saver, session_id)
    _, langchain_losses = saver.project_context_plan_with_diagnostics(
        session_id, saver._storage.storage.main_thread_id(session_id), snapshot.as_sealed_plan()
    )
    native = saver.project_context_plan_to_native(
        session_id, saver._storage.storage.main_thread_id(session_id), snapshot.as_sealed_plan()
    )
    losses = [
        item["data"]["loss"]
        for item in items
        if item["kind"] == "loss" and item["data"]["projection"] == "history"
    ]
    assert (
        losses
        == list(langchain_losses)
        == native["losses"]
        == ["api-opaque:reasoning/opaque"]
    )
    assert "c2VjcmV0" not in json.dumps(items)
    # 即使请求原始字段也不从 canonical opaque payload 绕过 history protection。
    response = await integration_client.post(
        "/api/v1/context/read",
        json={
            **_resource(session, snapshot),
            "view": "assembly",
            "include": ["visible_text", "reasoning", "raw_record"],
            "limit": 200,
        },
    )
    assert response.status_code == 200, response.text
    assert "c2VjcmV0" not in response.text


@pytest.mark.asyncio
async def test_context_http_reads_selection_without_request_source_body(
    context_source,
    integration_client,
    integration_workspace_root_path,
    native_http_server,
):
    saver, session, turn_id = context_source
    session_id = session["session_id"]
    state, _ = native_http_server
    plan = saver.compose_committed_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan_id=f"api-{uuid4().hex}",
        tool_set=EMPTY_TOOL_SET,
    )
    plan = register_projection_draft(saver, session_id, plan)
    snapshot = saver.seal_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan,
        wire_profile=None, wire_params=None, seal_idempotency_key=f"seal:{plan.plan_id}",
        turn_id=turn_id,
        execution_id=saver.execution_for_turn(
            session_id,
            turn_id=turn_id,
            thread_id=saver._storage.storage.main_thread_id(session_id),
        ),
        provider_version="api-contract",
    )
    source = next(
        entry for entry in snapshot.selection if entry.base_delta_role == "base"
    )
    sessions = Path(integration_workspace_root_path) / ".boxteam/sessions"
    thread_root = get_session_path_resolver(sessions).resolve_thread_node(
        session_id, source.detail_ref.thread_id
    )
    path = thread_root / detail_relative_path(source.detail_ref)
    retained = state.artifacts / f"retained-{source.detail_ref.detail_id}"
    path.rename(retained)
    try:
        items, _ = await _read_pages(
            integration_client, _resource(session, snapshot), state.artifacts
        )
        _assert_snapshot(items, snapshot, saver, session_id)
        with pytest.raises(DetailUnavailableError) as failure:
            saver.project_context_plan_to_native(
                session_id,
                saver._storage.storage.main_thread_id(session_id),
                snapshot.as_sealed_plan(),
            )
        assert isinstance(failure.value.__cause__, FileNotFoundError)
    finally:
        retained.rename(path)


@pytest.mark.asyncio
async def test_context_http_discovers_only_sealed_assemblies_and_binds_list_cursor(
    context_source,
    integration_client,
):
    saver, session, turn_id = context_source
    session_id = session["session_id"]
    snapshots = []
    for _ in range(3):
        plan = saver.compose_committed_context_plan(
            session_id,
            saver._storage.storage.main_thread_id(session_id),
            plan_id=f"list-{uuid4().hex}",
            tool_set=EMPTY_TOOL_SET,
        )
        plan = register_projection_draft(saver, session_id, plan)
        snapshots.append(
            saver.seal_context_plan(
                session_id,
                saver._storage.storage.main_thread_id(session_id),
                plan,
                wire_profile=None, wire_params=None, seal_idempotency_key=f"seal:{plan.plan_id}",
                turn_id=turn_id,
                execution_id=saver.execution_for_turn(
                    session_id,
                    turn_id=turn_id,
                    thread_id=saver._storage.storage.main_thread_id(session_id),
                ),
                provider_version="api-list-contract",
            )
        )
    # 未封存 draft 不能进入 UI 的请求列表。
    draft = saver.compose_committed_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        plan_id=f"draft-{uuid4().hex}",
        tool_set=EMPTY_TOOL_SET,
    )
    draft = register_projection_draft(saver, session_id, draft)
    request = {
        **_resource(session),
        "view": "assemblies",
        "limit": 2,
    }
    response = await integration_client.post("/api/v1/context/read", json=request)
    assert response.status_code == 200, response.text
    first = response.json()["data"]
    assert first["has_more"] and first["next_cursor"]
    assert [item["data"]["assembly_id"] for item in first["items"]] == [
        value.assembly_id for value in reversed(snapshots[1:])
    ]
    response = await integration_client.post(
        "/api/v1/context/read", json={**request, "cursor": first["next_cursor"]}
    )
    assert response.status_code == 200, response.text
    last = response.json()["data"]
    assert last["revision"] == first["revision"]
    assert not last["has_more"]
    assert [item["data"]["assembly_id"] for item in last["items"]] == [
        snapshots[0].assembly_id
    ]
    assert draft.plan_id not in json.dumps([first, last])
    saver.seal_context_plan(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        draft,
        wire_profile=None, wire_params=None, seal_idempotency_key=f"seal:{draft.plan_id}",
        turn_id=turn_id,
        execution_id=saver.execution_for_turn(
            session_id,
            turn_id=turn_id,
            thread_id=saver._storage.storage.main_thread_id(session_id),
        ),
        provider_version="api-list-contract",
    )
    changed = await integration_client.post(
        "/api/v1/context/read", json={**request, "cursor": first["next_cursor"]}
    )
    assert changed.status_code == 409, changed.text
