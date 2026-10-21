"""workspace 附件 content-addressed blob store 的分层测试（8.3 附件链）。

覆盖点：日期分桶物理定位、逻辑引用、去重与跨 Session 复用、权限隔离、
缩略图派生、损坏图片诊断、digest 唯一 claim、blob identity conflict、
pin/fence 发布复核、上传与删除竞态、定点恢复与引用感知 GC。

全部使用 tmp_path 隔离，不依赖网络或真实 LLM。
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, message_to_dict
from langgraph.checkpoint.base import empty_checkpoint
from PIL import Image

from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
    compute_capability_profile_hash,
)
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.resource_uri import parse_session_artifact_uri, session_artifact_uri
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_catalog_store.contracts import ForkSourceCaptureRecord
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.core.session_thread_context import session_thread_owner_scope
from app.core.thread_creation import ThreadCreationService
from app.core.trace_middleware import (
    get_current_gateway_id,
    reset_current_gateway_id,
    set_current_gateway_id,
)
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import user_input_payload
from app.domain.itemized.request_plan import ContextRequestPlan
from app.schemas.internal_v2.message import AttachmentRef
from app.schemas.internal_v2.session import SessionForkRequest
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.business.user_content_builder import UserContentBuilder
from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    compute_blob_identity,
    date_bucket_relative_locator,
    validate_blob_relative_locator,
)
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentBlobCatalog,
    BlobIdentityConflictError,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    ingest_staging_relative_locator,
    resolve_blob_path,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
    parse_logical_file_id,
)
from app.services.infrastructure.attachment_content_service import (
    AttachmentContentService,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from tests.support.rollout_view_mutations import mutate_view_members

SESSION_A = "ses_019b781159d47374805bc4427d081a14"
SESSION_B = "ses_019b9cdd1ad5758788a18bb227323523"


def _data_url(content_type: str, data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def _attach(name: str, content_type: str, data: bytes) -> AttachmentRef:
    return AttachmentRef(
        file_id=f"inline:{name}",
        name=name,
        content_type=content_type,
        data_url=_data_url(content_type, data),
    )


def _blob_files(workspace_root: Path) -> list[Path]:
    return sorted(
        path
        for path in (workspace_root / ".boxteam" / "attachments").rglob("blb_*")
        if path.is_file()
    )


def _store(tmp_path: Path, session_bundle_factory, *session_ids: str) -> AttachmentBlobStore:
    for session_id in session_ids:
        session_bundle_factory(tmp_path / ".boxteam" / "sessions", session_id)
    return AttachmentBlobStore(tmp_path)


@pytest.fixture
def fork_capture(tmp_path: Path, session_bundle_factory):
    """由合法 Session bundle 和真实 ingest 创建冻结附件输入。"""
    store = _store(tmp_path, session_bundle_factory, SESSION_A, SESSION_B)
    resolver = store._path_resolver
    record = ForkSourceCaptureRecord(
        copy_operation_id="attachment-copy", creation_key="attachment-copy",
        source_session_id=SESSION_A, source_thread_id=resolver.main_thread_id(SESSION_A),
        target_session_id=SESSION_B, target_thread_id=resolver.main_thread_id(SESSION_B),
        snapshot_id="1" * 32, snapshot_relative_locator="fork-source-captures/" + "1" * 32,
        fork_id="2" * 32, materialization_id="3" * 32,
        request_json=json.dumps({"resources": [], "creation_kind": "new_session_main", "target_gateway_id": get_current_gateway_id(),
                                 "target_main_thread_id": resolver.main_thread_id(SESSION_B),
                                 "source_storage_domain_id": resolver.catalog_store.get_node(SESSION_A).workspace_id,
                                 "target_storage_domain_id": resolver.catalog_store.get_node(SESSION_B).workspace_id}),
        preimage_hash="4" * 64, state="preparing",
        snapshot_sha256=None, marker_sha256=None, abort_reason=None,
    )
    yield store, record
    store.close()


@pytest.fixture
async def attachment_tool_owner(fork_capture, tmp_path, frozen_provider_wire, frozen_provider_activation):
    """由真实 ingress、prepare/seal 和 canonical tool-call 建立原工具 capability。"""
    store, record = fork_capture
    ref = (await store.persist_inline(SESSION_A, [_attach("input.txt", "text/plain", b"first\noriginal attachment\n")], thread_id=record.source_thread_id))[0]
    saver = RolloutCheckpointSaver(tmp_path / ".boxteam" / "sessions", attachment_store=store)
    content = UserContentBuilder(attachment_service=AttachmentContentService(workspace_root=tmp_path, attachment_store=store)).build("读取本次附件", [ref])
    accepted = saver.accept_turn(SESSION_A, thread_id=record.source_thread_id,
        accepted_ingress_id="attachment-ingress", acceptance_idempotency_key="attachment-accept",
        job_id="attachment-job", admission_idempotency_key="attachment-admission",
        payload=user_input_payload(content.content, [ref.model_dump(mode="json", exclude={"data_url"})]), payload_kind="structured_content")
    execution = accepted["initial_execution_id"]
    admission = saver.execution_admission(SESSION_A, thread_id=record.source_thread_id, execution_id=execution)
    owner = MutationIntentOwner(SESSION_A, record.source_thread_id)
    manager = ContextSourceManager(owner=ContextSourceOwnerKey(SESSION_A, record.source_thread_id), control_state_port=saver)
    selected = ToolSetSnapshot(selection_revision=sha256_jcs({"read_file": True}), tools=({"name": "read_file", "parameters": {"type": "object"}},))
    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id), execution_admission_scope(admission, langgraph_thread_id=owner.thread_id):
        activation = frozen_provider_activation(saver, plan_creation_idempotency_key="attachment-create")
        prepared = saver.prepare_context_for_provider(SESSION_A, owner.thread_id,
            turn_id=accepted["turn_id"], execution_id=execution,
            plan_creation_idempotency_key="attachment-create", seal_idempotency_key="attachment-seal",
            context_source_manager=manager, tool_set_supplier=lambda: selected,
            **frozen_provider_wire("attachment-model", "chat_completions"), **activation)
        model_call_id = activation["model_call_id"]
        saver.register_model_call(SESSION_A, thread_id=owner.thread_id, execution_id=execution,
            model_call_id=model_call_id, attempt=1, provider="attachment-model", assembly_id=prepared["assembly_id"])
        call_id = "read_attachment"
        saver.ensure_request_items(SESSION_A, owner.thread_id, turn_id=accepted["turn_id"], execution_id=execution,
            messages=[AIMessage(content="", id="lc_run--" + model_call_id, tool_calls=[{"id": call_id, "name": "read_file", "args": {"path": ref.file_id}, "type": "tool_call"}])])
    return SimpleNamespace(store=store, saver=saver, owner=owner, ref=ref, model_call_id=model_call_id, call_id=call_id, assembly_id=prepared["assembly_id"], turn_id=accepted["turn_id"])


@pytest.fixture
def attachment_sibling_factory(attachment_tool_owner):
    """通过真实 Thread 创建和 ingress 建立与 owner 同 ID 的 sibling item。"""
    case = attachment_tool_owner
    sessions = case.saver._storage.storage.sessions_dir
    catalog = SessionCatalogStore(sessions.parent / "navigation/session-catalog.sqlite", sessions)
    control = SessionControlStore(case.store._path_resolver.resolve_session_node(SESSION_A) / "session-control.sqlite")
    creator = ThreadCreationService(
        store=catalog, control_store=control, sessions_root=sessions,
        workspace_id=catalog.get_node(SESSION_A).workspace_id,
        compute_capability_profile_hash=compute_capability_profile_hash,
    )

    async def create(payload: object) -> ContextRef:
        child = await creator.create(
            idempotency_key="attachment-sibling", session_id=SESSION_A, thread_id=None,
            delegation_id=None, initial_state="idle", artifact_manifest={},
            session_metadata={
                "graph_binding": {
                    "graph_id": DEEP_AGENT_GRAPH_BINDING.graph_id,
                    "graph_revision": DEEP_AGENT_GRAPH_BINDING.graph_revision,
                    "graph_schema_hash": DEEP_AGENT_GRAPH_BINDING.graph_schema_hash,
                    "capability_profile_hash": DEEP_AGENT_GRAPH_BINDING.capability_profile_hash,
                }, "capability_profile": dict(DEEP_AGENT_CAPABILITY_PROFILE),
                "task_seed": None, "task_reference": None,
            },
        )
        snapshot = case.saver.get_context_assembly(SESSION_A, thread_id=case.owner.thread_id, assembly_id=case.assembly_id)
        root = next(ref for ref in snapshot.refs if ref.semantic_kind == "user_input")
        case.saver.accept_turn(
            SESSION_A, thread_id=child.child_thread_id, root_item_id=root.ref_id,
            accepted_ingress_id="sibling-ingress", acceptance_idempotency_key="sibling-accept",
            job_id="sibling-job", admission_idempotency_key="sibling-admit",
            payload=payload, payload_kind="structured_content" if isinstance(payload, dict) else "text",
        )
        item = case.saver._storage.storage.read_items(SESSION_A, child.child_thread_id, item_ids=(root.ref_id,))[0]
        return ContextRef.canonical_item(item, session_id=SESSION_A, thread_id=child.child_thread_id)

    try:
        yield create
    finally:
        control.close()
        catalog.close()


@pytest.fixture
def fork_source_point_service(attachment_tool_owner):
    """外层 Session/Goal 只提供展示 metadata；附件必须由真实 ingress 索引取得。"""
    owner = attachment_tool_owner.owner

    async def get_session(session_id):
        assert session_id == owner.session_id
        return SimpleNamespace(
            workspace_id="wksp_019b781159d47374805bc4427d081a14", title="来源会话",
            uri=f"boxteam://workspace/wksp_019b781159d47374805bc4427d081a14/resources/session/{session_id}",
        )

    async def get_goal(session_id):
        assert session_id == owner.session_id

    service = SessionContextForkService(
        session_service=SimpleNamespace(get=get_session),
        checkpointer=attachment_tool_owner.saver,
        thread_creation_factory=SimpleNamespace(), goal_service=SimpleNamespace(get=get_goal),
        terminal_resource_owner=SimpleNamespace(list_resources=AsyncMock(return_value=[])),
        browser_resource_owner=SimpleNamespace(list_resources=AsyncMock(return_value=[])),
    )
    token = set_current_gateway_id("gateway_332f92ee6191880e797569a47e22922b")
    try:
        yield service
    finally:
        reset_current_gateway_id(token)


async def test_source_point_projects_ingress_attachment_with_frozen_scope(
    attachment_tool_owner, fork_source_point_service, monkeypatch,
):
    owner = attachment_tool_owner.owner
    display = attachment_tool_owner.saver.load_history(
        owner.session_id, owner.thread_id,
        TurnHistoryLoadRequest(turn_ids=[attachment_tool_owner.turn_id], include=["user"]),
    ).items[0]

    def unexpected_body_read(*args, **kwargs):
        raise AssertionError("SourcePoint 展示不能解析 canonical 或附件正文")

    monkeypatch.setattr(attachment_tool_owner.saver._storage.storage, "_read_v2_item_at", unexpected_body_read)
    monkeypatch.setattr(attachment_tool_owner.store, "_verify_fork_attachment", unexpected_body_read)
    point = await fork_source_point_service.source_point(
        session_id=owner.session_id, thread_id=owner.thread_id, turn_id=attachment_tool_owner.turn_id,
        source_view_id=display.source_view_id, source_view_revision=display.source_view_revision,
    )
    assert point.resource_mode == "view_prefix_fork"
    assert point.resource_anchor_mode == "inclusive"
    assert [resource.model_dump() for resource in point.resources] == [{
        "resource_uri": attachment_tool_owner.ref.file_id, "name": "input.txt",
        "content_type": "text/plain", "availability": "available", "required_for_context": True,
    }]
    frozen = {
        "session_id": owner.session_id, "thread_id": owner.thread_id,
        "item_id": point.anchor_item_id, "source_view_id": point.source_view_id,
        "source_view_revision": point.source_view_revision,
    }
    before = await fork_source_point_service.source_point(**frozen, anchor_mode="before")
    assert before.resources == []
    assert before.anchor_item_id == point.anchor_item_id
    assert before.source_view_revision == point.source_view_revision
    with pytest.raises(ValueError, match="revision 已变化"):
        await fork_source_point_service.source_point(**{**frozen, "source_view_revision": point.source_view_revision + 1})


@pytest.fixture
def fork_attachment_storage(fork_capture, tmp_path, monkeypatch):
    """复用真实 Saver 的 storage 接线，仅观察真实附件投影调用次数。"""
    store, _ = fork_capture
    calls: list[tuple[str, str, str]] = []
    original = AttachmentBlobStore.project_attachment

    def project(self: AttachmentBlobStore, session_id: str, thread_id: str, file_id: str) -> dict[str, str]:
        calls.append((session_id, thread_id, file_id))
        return original(self, session_id, thread_id, file_id)

    monkeypatch.setattr(AttachmentBlobStore, "project_attachment", project)
    saver = RolloutCheckpointSaver(tmp_path / ".boxteam" / "sessions", attachment_store=store)
    return saver._storage.storage, calls


@pytest.mark.asyncio
async def test_attachment_capability_uses_original_model_call_after_new_ingress(attachment_tool_owner):
    case = attachment_tool_owner
    read = {"thread_id": case.owner.thread_id, "tool_call_id": case.call_id, "model_call_id": case.model_call_id}
    assert case.saver.read_tool_call_attachment(SESSION_A, file_id=case.ref.file_id, **read).data == b"first\noriginal attachment\n"
    late = (await case.store.persist_inline(SESSION_A, [_attach("late.txt", "text/plain", b"queued secret")], thread_id=case.owner.thread_id))[0]
    case.saver.accept_turn(SESSION_A, thread_id=case.owner.thread_id,
        accepted_ingress_id="late-ingress", acceptance_idempotency_key="late-accept",
        job_id="late-job", admission_idempotency_key="late-admission",
        payload={"content": "后继输入", "attachments": [late.model_dump(mode="json", exclude={"data_url"})]}, payload_kind="structured_content")
    with pytest.raises(PermissionError, match="原始 assembly"):
        case.saver.read_tool_call_attachment(SESSION_A, file_id=late.file_id, **read)
    restarted = RolloutCheckpointSaver(case.saver._storage.storage.sessions_dir, attachment_store=case.store)
    assert restarted.read_tool_call_attachment(SESSION_A, file_id=case.ref.file_id, **read).data == b"first\noriginal attachment\n"
    with pytest.raises(PermissionError, match="原始 assembly"):
        restarted.read_tool_call_attachment(SESSION_A, file_id=late.file_id, **read)


@pytest.mark.asyncio
async def test_attachment_capability_rejects_other_session_even_with_same_blob(attachment_tool_owner):
    case = attachment_tool_owner
    peer = (await case.store.persist_inline(SESSION_B, [_attach("peer.txt", "text/plain", b"first\noriginal attachment\n")], thread_id=case.store._path_resolver.main_thread_id(SESSION_B)))[0]
    assert len(_blob_files(case.store._workspace_root)) == 1
    with pytest.raises(PermissionError, match="原始 assembly"):
        case.saver.read_tool_call_attachment(SESSION_A, thread_id=case.owner.thread_id,
            tool_call_id=case.call_id, model_call_id=case.model_call_id, file_id=peer.file_id)
    with pytest.raises(PermissionError, match="SessionThread"):
        case.store.read(SESSION_A, case.ref.file_id, owner_thread_id=case.store._path_resolver.main_thread_id(SESSION_B))
    with pytest.raises(RuntimeError, match="identity 缺失或歧义"):
        case.saver.read_tool_call_attachment(SESSION_A, thread_id=case.owner.thread_id,
            tool_call_id="unissued-call", model_call_id=case.model_call_id, file_id=case.ref.file_id)


@pytest.mark.asyncio
async def test_cold_attachment_capability_does_not_alias_same_item_id_in_sibling(
    attachment_tool_owner, attachment_sibling_factory,
):
    case = attachment_tool_owner
    sessions = case.saver._storage.storage.sessions_dir
    ref = await attachment_sibling_factory("该 sibling item 没有 owner 的附件")
    owner_snapshot = case.saver.get_context_assembly(SESSION_A, thread_id=case.owner.thread_id, assembly_id=case.assembly_id)
    plan = case.saver.create_context_plan(SESSION_A, ContextRequestPlan(
        session_id=SESSION_A, thread_id=case.owner.thread_id, plan_id="sibling-attachment-plan",
        plan_creation_idempotency_key="sibling-attachment-create", refs=(ref,),
    )).draft
    execution = owner_snapshot.execution_id
    model_call_id = create_uuid_hex()
    snapshot = ContextPlanComposer().assembly(
        plan=plan, assembly_id="sibling-attachment-assembly", session_id=SESSION_A,
        thread_id=case.owner.thread_id, turn_id=case.turn_id,
        execution_id=execution, provider_version="attachment-sibling-test", model_call_id=model_call_id,
    )
    case.saver.seal_context_assembly(SESSION_A, case.owner.thread_id, snapshot,
        seal_idempotency_key="sibling-attachment-seal", seal_input_hash=sha256_jcs("sibling-input"))
    admission = case.saver.execution_admission(SESSION_A, thread_id=case.owner.thread_id, execution_id=execution)
    with session_thread_owner_scope(case.owner, langgraph_thread_id=case.owner.thread_id), execution_admission_scope(admission, langgraph_thread_id=case.owner.thread_id):
        case.saver.register_model_call(SESSION_A, thread_id=case.owner.thread_id, execution_id=execution,
            model_call_id=model_call_id, attempt=2, provider="attachment-sibling-test", assembly_id=snapshot.assembly_id)
        case.saver.ensure_request_items(SESSION_A, case.owner.thread_id, turn_id=case.turn_id, execution_id=execution,
            messages=[AIMessage(content="", id="lc_run--" + model_call_id,
                tool_calls=[{"id": "sibling-read", "name": "read_file", "args": {}, "type": "tool_call"}])])
    cold = RolloutCheckpointSaver(sessions, attachment_store=case.store)
    assert cold.read_tool_call_attachment(SESSION_A, thread_id=case.owner.thread_id,
        tool_call_id=case.call_id, model_call_id=case.model_call_id, file_id=case.ref.file_id).data == b"first\noriginal attachment\n"
    with pytest.raises(PermissionError, match="原始 assembly"):
        cold.read_tool_call_attachment(SESSION_A, thread_id=case.owner.thread_id,
            tool_call_id="sibling-read", model_call_id=model_call_id, file_id=case.ref.file_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("ref_scope", ["owner", "sibling"])
async def test_fork_source_attachment_candidates_require_frozen_ref_thread(
    attachment_tool_owner, attachment_sibling_factory, fork_source_point_service, ref_scope, monkeypatch,
):
    case = attachment_tool_owner
    storage = case.saver._storage.storage
    root = next(item for item in storage.read_items(SESSION_A, case.owner.thread_id) if item.semantic_kind == "user_input")
    owner_ref = ContextRef.canonical_item(root, session_id=SESSION_A, thread_id=case.owner.thread_id)
    sibling_ref = await attachment_sibling_factory(root.payload)
    # 正文和所有本地 JOIN 字段相同，只有冻结 thread 不同；拒绝不能借助 hash 不匹配。
    assert sibling_ref.ref_id == owner_ref.ref_id and sibling_ref.thread_id != owner_ref.thread_id
    assert (sibling_ref.content_hash, sibling_ref.content_length, sibling_ref.source_revision, sibling_ref.item_sequence) == (
        owner_ref.content_hash, owner_ref.content_length, owner_ref.source_revision, owner_ref.item_sequence,
    )
    accepted = case.saver.accept_turn(SESSION_A, thread_id=case.owner.thread_id,
        accepted_ingress_id="fork-visible-ingress", acceptance_idempotency_key="fork-visible-accept",
        job_id="fork-visible-job", admission_idempotency_key="fork-visible-admit",
        payload="后继 Turn 自身没有附件", payload_kind="text")
    new_root = storage.read_items(SESSION_A, case.owner.thread_id, item_ids=(accepted["root_input_item_id"],))[0]
    assert new_root.projection_identity is not None
    execution = accepted["initial_execution_id"]
    ref = owner_ref if ref_scope == "owner" else sibling_ref
    plan = case.saver.create_context_plan(SESSION_A, ContextRequestPlan(
        session_id=SESSION_A, thread_id=case.owner.thread_id, plan_id="fork-scope-plan",
        plan_creation_idempotency_key="fork-scope-create", refs=(ref,),
    )).draft
    model_call_id = create_uuid_hex()
    snapshot = ContextPlanComposer().assembly(
        plan=plan, assembly_id="fork-scope-assembly", session_id=SESSION_A, thread_id=case.owner.thread_id,
        turn_id=accepted["turn_id"], execution_id=execution, provider_version="fork-scope-test", model_call_id=model_call_id,
    )
    case.saver.seal_context_assembly(SESSION_A, case.owner.thread_id, snapshot,
        seal_idempotency_key="fork-scope-seal", seal_input_hash=sha256_jcs("fork-scope-input"))
    admission = case.saver.execution_admission(SESSION_A, thread_id=case.owner.thread_id, execution_id=execution)
    with session_thread_owner_scope(case.owner, langgraph_thread_id=case.owner.thread_id), execution_admission_scope(admission, langgraph_thread_id=case.owner.thread_id):
        case.saver.register_model_call(SESSION_A, thread_id=case.owner.thread_id, execution_id=execution,
            model_call_id=model_call_id, attempt=1, provider="fork-scope-test", assembly_id=snapshot.assembly_id)
        checkpoint = empty_checkpoint()
        checkpoint["id"] = "fork-scope-checkpoint"
        checkpoint["channel_values"] = {"messages": [
            HumanMessage(content=new_root.payload, id=new_root.projection_identity.projection_message_id,
                         response_metadata={"turn_id": accepted["turn_id"]}),
            AIMessage(content="仅 producer manifest 引用前一条附件", id="lc_run--" + model_call_id,
                      response_metadata={"model_call_id": model_call_id}),
        ]}
        checkpoint["channel_versions"] = {"messages": "1"}
        case.saver.put(build_checkpoint_config(case.owner.thread_id), checkpoint, {"source": "loop"}, {"messages": "1"})
    answer = next(item for item in storage.read_items(SESSION_A, case.owner.thread_id)
                  if item.semantic_kind == "assistant_output" and item.turn_id == accepted["turn_id"])
    with storage._connect(case.owner.thread_id, "", session_id=SESSION_A) as connection:
        view, revision = connection.execute(
            "SELECT branch.head_view_id,meta.history_view_revision FROM checkpoint_namespace_state namespace "
            "JOIN branches branch ON branch.branch_id=namespace.active_branch_id "
            "JOIN database_meta meta ON meta.singleton_id=1 WHERE namespace.checkpoint_ns=''",
        ).fetchone()
        # 收窄查询 fixture 的有效 view；前一个 Turn 只保留为 producer 正文依赖。
        mutate_view_members(connection,
            "UPDATE context_view_items SET visible=0,context_selected=0,context_ordinal=NULL "
            "WHERE view_id=? AND item_id IN (SELECT item_id FROM item_catalog WHERE turn_id=?)",
            (view, case.turn_id),
        )
        assert connection.execute(
            "SELECT 1 FROM context_view_items WHERE view_id=? AND item_id=? AND visible=1 AND context_selected=1",
            (view, root.item_id),
        ).fetchall() == []
        assert connection.execute(
            "SELECT ref_thread_id FROM context_assembly_selections WHERE assembly_id=? AND ref_type='canonical_item'",
            (snapshot.assembly_id,),
        ).fetchall() == [(ref.thread_id,)]
        assert connection.execute(
            "SELECT 1 FROM history_namespace_bindings WHERE session_id=? AND thread_id=?",
            (SESSION_A, sibling_ref.thread_id),
        ).fetchall() == []
        assert connection.execute("SELECT resource_uri FROM item_resource_references WHERE item_id=?", (root.item_id,)).fetchall() == [(case.ref.file_id,)]
    observed = []
    project = case.saver.forks.source.project_display_resources

    async def project_resources(**kwargs):
        observed.append((kwargs["source_owner"], kwargs["source_view_id"], kwargs["source_view_revision"]))
        return await project(**kwargs)

    monkeypatch.setattr(case.saver.forks.source, "project_display_resources", project_resources)
    assert get_current_gateway_id() == "gateway_332f92ee6191880e797569a47e22922b"
    point = await fork_source_point_service.source_point(
        session_id=SESSION_A, thread_id=case.owner.thread_id, item_id=answer.item_id,
        source_view_id=view, source_view_revision=revision,
    )
    assert observed == [(case.owner, view, revision)]
    assert point.anchor_item_id == answer.item_id and point.source_view_revision == revision
    assert [(resource.resource_uri, resource.required_for_context) for resource in point.resources] == (
        [(case.ref.file_id, True)] if ref_scope == "owner" else []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["text/plain", "image/png"])
async def test_fork_projects_generated_attachment_blocks_without_mutating_user_text(fork_capture, fork_attachment_storage, tmp_path, content_type: str):
    store, record = fork_capture
    buffer = BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, format="PNG")
    ref = (await store.persist_inline(SESSION_A, [_attach("input", content_type, buffer.getvalue() if content_type == "image/png" else b"fork input")], thread_id=record.source_thread_id))[0]
    user_text = "用户文字中的 URI 必须保持原样 " + ref.file_id
    built = UserContentBuilder(attachment_service=AttachmentContentService(workspace_root=tmp_path, attachment_store=store)).build(user_text, [ref])
    snapshots = store.capture_fork_attachments(record, same_storage_owner=True, references=((ref.file_id, True),))
    captured = snapshots[0]
    await store.reserve_copy_attachments(record, None, snapshots)
    assert store.project_attachment(SESSION_B, record.target_thread_id, ref.file_id)["availability"] == "unavailable"
    with pytest.raises(FileNotFoundError, match="尚未完成发布"):
        store.read(SESSION_B, captured.target_file_id)
    await store.finalize_copy_attachments(record, publication_preimage_hash=record.preimage_hash)
    storage, calls = fork_attachment_storage
    message = message_to_dict(HumanMessage(content=built.content, response_metadata={"attachments": [ref.model_dump(mode="json", exclude={"data_url"})]}))
    original = json.dumps(message, sort_keys=True)
    calls.clear()
    projected = storage.project_message_attachments(SESSION_B, record.target_thread_id, message)
    target = captured.target_file_id
    assert projected["data"]["content"][0]["text"] == user_text
    manifest = projected["data"]["content"][1]
    assert manifest["metadata"]["file_id"] == manifest["metadata"]["path"] == target
    assert f'id="{target}" path="{target}"' in manifest["text"]
    for block in projected["data"]["content"][2:]:
        assert block["metadata"]["file_id"] == block["metadata"]["path"] == target
    assert projected["data"]["response_metadata"]["attachments"][0]["file_id"] == target
    assert calls == [(SESSION_B, record.target_thread_id, ref.file_id)]
    assert json.dumps(message, sort_keys=True) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
async def test_fork_attachment_missing_bytes_respects_required(fork_capture, required: bool) -> None:
    store, record = fork_capture
    ref = (await store.persist_inline(SESSION_A, [_attach("capture.txt", "text/plain", b"fork-body")], thread_id=record.source_thread_id))[0]
    attachment_id = parse_logical_file_id(SESSION_A, ref.file_id)
    source = store._catalog.get_owner_ref(attachment_id)
    blob = store._catalog.get_blob(source.blob_id)
    path = resolve_blob_path(store._attachments_root, blob.relative_locator)
    path.unlink()
    if required:
        with pytest.raises(FileNotFoundError):
            store.capture_fork_attachments(record, same_storage_owner=True, references=((ref.file_id, True),))
        assert store._catalog.fork_attachment_claim_ids(record.copy_operation_id) == ()
        assert store.project_attachment(SESSION_B, record.target_thread_id, ref.file_id) == {"file_id": ref.file_id, "availability": "unavailable"}
    else:
        snapshots = store.capture_fork_attachments(record, same_storage_owner=True, references=((ref.file_id, False),))
        assert len(snapshots) == 1
        assert snapshots[0].availability == "unavailable"
        assert snapshots[0].claim_id is None
        assert store._catalog.fork_attachment_claim_ids(record.copy_operation_id) == ()
        projection = store.project_attachment(SESSION_B, record.target_thread_id, ref.file_id)
        assert projection == {"file_id": snapshots[0].target_file_id, "availability": "unavailable"}
        mappings = store._catalog._connected().execute("SELECT source_file_id,snapshot_json FROM fork_attachment_mappings WHERE copy_operation_id=?", (record.copy_operation_id,)).fetchall()
        assert {row[0] for row in mappings} == {ref.file_id, projection["file_id"]}
        assert all(json.loads(row[1])["availability"] == "unavailable" for row in mappings)
    assert store._catalog.get_owner_ref(attachment_id) == source
    assert store._catalog.get_blob(source.blob_id) == blob


@pytest.mark.asyncio
async def test_fork_attachment_same_domain_different_owner_rejects_before_claim(fork_capture) -> None:
    store, record = fork_capture
    ref = (await store.persist_inline(SESSION_A, [_attach("owner.txt", "text/plain", b"same-domain")], thread_id=record.source_thread_id))[0]
    source = store._catalog.get_owner_ref(parse_logical_file_id(SESSION_A, ref.file_id))
    with pytest.raises(ValueError, match="fork_owner_overlap"):
        store.capture_fork_attachments(record, references=((ref.file_id, True),), same_storage_owner=False)
    assert store._catalog.fork_attachment_claim_ids(record.copy_operation_id) == ()
    assert store._catalog.fork_attachment_snapshots(record.copy_operation_id, target_session_id=record.target_session_id,
                                                  target_thread_id=record.target_thread_id) == ()
    assert store._catalog.get_owner_ref(source.attachment_id) == source


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["hash", "symlink"])
async def test_optional_fork_attachment_damage_is_not_unavailable(fork_capture, damage: str) -> None:
    store, record = fork_capture
    ref = (await store.persist_inline(SESSION_A, [_attach("capture.txt", "text/plain", b"fork-body")], thread_id=record.source_thread_id))[0]
    source = store._catalog.get_owner_ref(parse_logical_file_id(SESSION_A, ref.file_id))
    blob = store._catalog.get_blob(source.blob_id)
    path = resolve_blob_path(store._attachments_root, blob.relative_locator)
    if damage == "hash":
        path.write_bytes(b"changed-bytes")
        error = "hash/length"
    else:
        path.unlink()
        path.symlink_to(path.with_name("missing-body"))
        error = "符号链接"
    with pytest.raises(RuntimeError, match=error):
        store.capture_fork_attachments(record, same_storage_owner=True, references=((ref.file_id, False),))
    assert store._catalog.fork_attachment_claim_ids(record.copy_operation_id) == ()
    assert store.project_attachment(SESSION_B, record.target_thread_id, ref.file_id) == {"file_id": ref.file_id, "availability": "unavailable"}


# ----------------------------------------------------------------------
# 身份与受检 locator
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [False, True])
async def test_fork_attachment_same_length_damage_never_reuses_stat_cache(fork_capture, warm: bool) -> None:
    """冷读与已验证文件均拒绝同长度损坏，即使调用方把 mtime 恢复为原值。"""
    store, record = fork_capture
    ref = (await store.persist_inline(SESSION_A, [_attach("capture.txt", "text/plain", b"fork-body")], thread_id=record.source_thread_id))[0]
    source = store._catalog.get_owner_ref(parse_logical_file_id(SESSION_A, ref.file_id))
    blob = store._catalog.get_blob(source.blob_id)
    path = resolve_blob_path(store._attachments_root, blob.relative_locator)
    metadata = path.stat()
    if warm:
        store.capture_fork_attachments(record, same_storage_owner=True, references=((ref.file_id, True),))
    claims = store._catalog.fork_attachment_claim_ids(record.copy_operation_id)
    path.write_bytes(b"bad-bytes")
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    with pytest.raises(RuntimeError, match="hash/length"):
        store.capture_fork_attachments(record, same_storage_owner=True, references=((ref.file_id, True),))
    assert store._catalog.fork_attachment_claim_ids(record.copy_operation_id) == claims
    assert store._catalog.get_owner_ref(source.attachment_id) == source
    assert store._catalog.get_blob(blob.blob_id) == blob


def test_blob_identity_is_pure_sha256_of_bytes() -> None:
    identity = compute_blob_identity(b"hello")
    assert identity.blob_id == "blb_" + compute_blob_identity(b"hello").blob_id[4:]
    assert identity.digest == "sha256:" + identity.blob_id[4:]
    assert identity.length == 5


def test_date_bucket_locator_has_no_extra_shard() -> None:
    locator = date_bucket_relative_locator(
        "blb_" + "a" * 64, datetime(2026, 3, 4, tzinfo=UTC).date()
    )
    assert locator == "2026/03/04/blb_" + "a" * 64
    validate_blob_relative_locator(locator)


@pytest.mark.parametrize(
    "locator",
    [
        "2026/03/04/blb_" + "A" * 64,
        "2026/03/04/blb_" + "a" * 63,
        "../2026/03/04/blb_" + "a" * 64,
        "/2026/03/04/blb_" + "a" * 64,
        "2026/03/04/blb_" + "a" * 64 + "/x",
        "blb_" + "a" * 64,
    ],
)
def test_validate_blob_relative_locator_rejects_non_canonical(locator: str) -> None:
    with pytest.raises(ValueError):
        validate_blob_relative_locator(locator)


def test_resolve_blob_path_rejects_caller_supplied_escape(tmp_path: Path) -> None:
    """resolver 只接受受检 locator：调用方字符串路径必须失败。"""
    with pytest.raises(ValueError):
        resolve_blob_path(tmp_path / "attachments", "../../etc/passwd")


@pytest.mark.parametrize(
    "file_id",
    [
        f"boxteam://workspace/test/resources/session/{SESSION_A}/attachments/../../etc/passwd",
        f"boxteam://workspace/test/resources/session/{SESSION_A}/attachments/att_1/../x",
        f"boxteam://workspace/test/resources/session/{SESSION_A}/other/att_1",
        f"boxteam://workspace/test/resources/session/{SESSION_A}/attachments",
        f"boxteam://workspace/test/resources/session/{SESSION_A}/attachments/att_{'1' * 32}?owner=other",
        f"boxteam://workspace/test/resources/session/{SESSION_A}/attachments/att_{'1' * 32}#other",
        f"boxteam-session://{SESSION_A}/attachments/att_{'1' * 32}",
        "/etc/passwd",
    ],
)
def test_parse_logical_file_id_rejects_traversal_and_foreign(file_id: str) -> None:
    with pytest.raises(ValueError):
        parse_logical_file_id(SESSION_A, file_id)


def test_parse_logical_file_id_rejects_other_session() -> None:
    with pytest.raises(ValueError, match="不属于指定会话"):
        parse_logical_file_id(SESSION_B, session_artifact_uri("019d1b4a-1111-7000-8000-000000000001", SESSION_A, "attachments", "att_" + "1" * 32))


# ----------------------------------------------------------------------
# 写入与读取
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_inline_writes_date_bucketed_blob_and_reads_back(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    payload = b"\x89PNG\r\n\x1a\nimage-data"

    stored = (
        await store.persist_inline(SESSION_A, [_attach("example.png", "image/png", payload)], thread_id=store._path_resolver.main_thread_id(SESSION_A))
    )[0]

    assert stored.data_url is None
    assert parse_session_artifact_uri(stored.file_id).tail_segments[:2] == (SESSION_A, "attachments")
    blobs = _blob_files(tmp_path)
    assert len(blobs) == 1
    identity = compute_blob_identity(payload)
    assert blobs[0].name == identity.blob_id
    assert blobs[0].parent.parent.parent.name == str(datetime.now(UTC).year)

    content = store.read(SESSION_A, stored.file_id)
    assert content.data == payload
    assert content.content_type == "image/png"
    store.close()


@pytest.mark.asyncio
async def test_persist_inline_deduplicates_and_reuses_first_locator(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    attachment = _attach("duplicate.png", "image/png", b"same-image")

    first = (await store.persist_inline(SESSION_A, [attachment], thread_id=store._path_resolver.main_thread_id(SESSION_A)))[0]
    second = (await store.persist_inline(SESSION_A, [attachment], thread_id=store._path_resolver.main_thread_id(SESSION_A)))[0]

    assert first.file_id == second.file_id
    assert len(_blob_files(tmp_path)) == 1
    store.close()


@pytest.mark.asyncio
async def test_same_digest_across_sessions_reuses_single_physical_blob(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A, SESSION_B)
    payload = b"shared-bytes"

    ref_a = (await store.persist_inline(SESSION_A, [_attach("a.bin", "application/pdf", payload)], thread_id=store._path_resolver.main_thread_id(SESSION_A)))[0]
    ref_b = (await store.persist_inline(SESSION_B, [_attach("b.bin", "application/pdf", payload)], thread_id=store._path_resolver.main_thread_id(SESSION_B)))[0]

    assert len(_blob_files(tmp_path)) == 1
    assert ref_a.file_id != ref_b.file_id
    assert store.read(SESSION_A, ref_a.file_id).data == payload
    assert store.read(SESSION_B, ref_b.file_id).data == payload
    store.close()


@pytest.mark.asyncio
async def test_generic_mime_and_name_suffix_are_preserved(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)

    stored = (
        await store.persist_inline(
            SESSION_A,
            [_attach("document.custom", "application/x-custom-document", b"custom")],
            thread_id=store._path_resolver.main_thread_id(SESSION_A),
        )
    )[0]

    assert store.read(SESSION_A, stored.file_id).data == b"custom"
    assert store.read(SESSION_A, stored.file_id).content_type == "application/x-custom-document"
    store.close()


@pytest.mark.asyncio
async def test_read_rejects_attachment_from_another_session(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A, SESSION_B)
    ref_a = (await store.persist_inline(SESSION_A, [_attach("a.png", "image/png", b"a")], thread_id=store._path_resolver.main_thread_id(SESSION_A)))[0]
    ref_b = (await store.persist_inline(SESSION_B, [_attach("b.png", "image/png", b"b")], thread_id=store._path_resolver.main_thread_id(SESSION_B)))[0]

    with pytest.raises(ValueError, match="不属于指定会话"):
        store.read(SESSION_B, ref_a.file_id)
    assert store.read(SESSION_B, ref_b.file_id).data == b"b"
    store.close()


@pytest.mark.asyncio
async def test_persist_rejects_mismatched_content_type(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    attachment = AttachmentRef(
        file_id="inline:image.png",
        content_type="image/jpeg",
        data_url=_data_url("image/png", b"image"),
    )

    with pytest.raises(ValueError, match="MIME 不一致"):
        await store.persist_inline(SESSION_A, [attachment], thread_id=store._path_resolver.main_thread_id(SESSION_A))
    assert _blob_files(tmp_path) == []
    store.close()


@pytest.mark.asyncio
async def test_relative_path_exposes_logical_ref_not_physical_locator(
    tmp_path, session_bundle_factory
) -> None:
    """模型/canonical content 只拿逻辑引用，绝不暴露 blob 物理 locator。"""
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    stored = (await store.persist_inline(SESSION_A, [_attach("a.png", "image/png", b"a")], thread_id=store._path_resolver.main_thread_id(SESSION_A)))[0]

    relative = store.relative_path(SESSION_A, stored.file_id)
    assert relative == stored.file_id
    assert parse_session_artifact_uri(relative).tail_segments[:2] == (SESSION_A, "attachments")
    assert ".boxteam/attachments" not in relative
    store.close()


# ----------------------------------------------------------------------
# 缩略图
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_thumbnail_generates_bounded_cached_webp(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    buffer = BytesIO()
    Image.new("RGB", (1600, 900), color=(245, 210, 80)).save(buffer, format="PNG")
    stored = (
        await store.persist_inline(SESSION_A, [_attach("large.png", "image/png", buffer.getvalue())], thread_id=store._path_resolver.main_thread_id(SESSION_A))
    )[0]

    thumbnail = store.read_thumbnail(SESSION_A, stored.file_id)

    assert thumbnail.content_type == "image/webp"
    with Image.open(BytesIO(thumbnail.data)) as image:
        assert max(image.size) == 512
    assert store.read_thumbnail(SESSION_A, stored.file_id).data == thumbnail.data
    store.close()


@pytest.mark.asyncio
async def test_read_thumbnail_reports_corrupt_image_as_value_error(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    truncated = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    stored = (
        await store.persist_inline(SESSION_A, [_attach("bad.png", "image/png", truncated)], thread_id=store._path_resolver.main_thread_id(SESSION_A))
    )[0]

    with pytest.raises(ValueError, match="已损坏或不是有效图片"):
        store.read_thumbnail(SESSION_A, stored.file_id)
    assert store.read(SESSION_A, stored.file_id).data == truncated
    store.close()


@pytest.mark.asyncio
async def test_read_thumbnail_rejects_non_image(tmp_path, session_bundle_factory) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    stored = (
        await store.persist_inline(SESSION_A, [_attach("doc.pdf", "application/pdf", b"%PDF-1.7")], thread_id=store._path_resolver.main_thread_id(SESSION_A))
    )[0]

    with pytest.raises(ValueError, match="不是图片"):
        store.read_thumbnail(SESSION_A, stored.file_id)
    store.close()


# ----------------------------------------------------------------------
# digest 唯一 claim 与 identity conflict（catalog 层，确定性）
# ----------------------------------------------------------------------


def _catalog(tmp_path: Path) -> AttachmentBlobCatalog:
    return AttachmentBlobCatalog(
        tmp_path / ".boxteam" / "attachments" / "catalog.sqlite"
    )


def test_mark_hashed_claims_single_locator_per_digest(tmp_path: Path) -> None:
    """并发相同 digest：唯一 claim 冻结首次日期与 locator，不产生第二 locator。"""
    catalog = _catalog(tmp_path)
    identity = compute_blob_identity(b"same")
    ingest_ids = {
        "first": "a7937b64b8caa58f03721bb6bacf5c78",
        "second": "16367aacb67a4a017c8da8ab95682ccb",
    }
    for index, ingest_hex in ingest_ids.items():
        catalog.create_or_get_ingest_record(
            ingest_idempotency_key=f"attkey_{index}",
            ingest_id=f"ing_{ingest_hex}",
            owner_session_id=SESSION_A,
            owner_thread_id="thr_" + "a" * 32,
            pin_lease_id=f"lease_{index}",
            pin_fencing_token=1,
            pin_captured_generation=1,
            preimage_hash="b" * 64,
            staging_relative_locator=ingest_staging_relative_locator(
                f"ing_{ingest_hex}"
            ),
            max_bytes=1024,
        )

    _first_record, first_claim = catalog.mark_ingest_hashed(
        ingest_idempotency_key="attkey_first", identity=identity
    )
    _second_record, second_claim = catalog.mark_ingest_hashed(
        ingest_idempotency_key="attkey_second", identity=identity
    )

    assert first_claim.winning_ingest_id == "ing_a7937b64b8caa58f03721bb6bacf5c78"
    assert second_claim.final_relative_locator == first_claim.final_relative_locator
    assert second_claim.winning_ingest_id == "ing_a7937b64b8caa58f03721bb6bacf5c78"
    assert len(catalog.list_non_terminal_claims()) == 1
    catalog.close()


def test_publish_rejects_same_blob_id_with_different_length(tmp_path: Path) -> None:
    """同一 blob-id 不同 length → blob-identity-conflict，不覆盖。"""
    catalog = _catalog(tmp_path)
    payload = b"correct-bytes"
    identity = compute_blob_identity(payload)
    tampered = type(identity)(blob_id=identity.blob_id, digest=identity.digest, length=99)

    catalog.create_or_get_ingest_record(
        ingest_idempotency_key="attkey_x",
        ingest_id="ing_2d711642b726b04401627ca9fbac32f5",
        owner_session_id=SESSION_A,
        owner_thread_id="thr_" + "a" * 32,
        pin_lease_id="lease_x",
        pin_fencing_token=1,
        pin_captured_generation=1,
        preimage_hash="c" * 64,
        staging_relative_locator=ingest_staging_relative_locator("ing_2d711642b726b04401627ca9fbac32f5"),
        max_bytes=1024,
    )
    catalog.publish_blob_and_owner_ref(
        identity=identity,
        relative_locator=date_bucket_relative_locator(identity.blob_id, datetime.now(UTC).date()),
        ingest_idempotency_key="attkey_x",
        attachment_id="att_first",
        owner_session_id=SESSION_A,
        owner_thread_id="thr_" + "a" * 32,
        file_name="a.bin",
        mime_type="application/pdf",
    )

    with pytest.raises(BlobIdentityConflictError):
        catalog.publish_blob_and_owner_ref(
            identity=tampered,
            relative_locator=date_bucket_relative_locator(
                identity.blob_id, datetime.now(UTC).date()
            ),
            ingest_idempotency_key="attkey_x",
            attachment_id="att_second",
            owner_session_id=SESSION_A,
            owner_thread_id="thr_" + "a" * 32,
            file_name="b.bin",
            mime_type="application/pdf",
        )
    catalog.close()


def test_ingest_record_rejects_preimage_conflict(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    kwargs = {
        "ingest_idempotency_key": "attkey_k",
        "ingest_id": "ing_8254c329a92850f6d539dd376f4816ee",
        "owner_session_id": SESSION_A,
        "owner_thread_id": "thr_" + "a" * 32,
        "pin_lease_id": "lease_k",
        "pin_fencing_token": 1,
        "pin_captured_generation": 1,
        "staging_relative_locator": ingest_staging_relative_locator("ing_8254c329a92850f6d539dd376f4816ee"),
        "max_bytes": 1024,
    }
    catalog.create_or_get_ingest_record(preimage_hash="a" * 64, **kwargs)

    with pytest.raises(RuntimeError, match="preimage 冲突"):
        catalog.create_or_get_ingest_record(preimage_hash="b" * 64, **kwargs)
    catalog.close()


@pytest.mark.parametrize("future", [False, True])
def test_released_owner_retention_blocks_gc_until_deadline(tmp_path: Path, future: bool) -> None:
    """引用释放不提前撤销持久 retention；候选与原子 tombstone 使用同一截止时间。"""
    catalog = _catalog(tmp_path)
    identity = compute_blob_identity(b"retained-owner")
    ingest_id = "ing_2d711642b726b04401627ca9fbac32f5"
    catalog.create_or_get_ingest_record(
        ingest_idempotency_key="retained-owner", ingest_id=ingest_id,
        owner_session_id=SESSION_A, owner_thread_id="thr_" + "a" * 32,
        pin_lease_id="lease_retained", pin_fencing_token=1,
        pin_captured_generation=1, preimage_hash="a" * 64,
        staging_relative_locator=ingest_staging_relative_locator(ingest_id), max_bytes=1024,
    )
    now = datetime.now(UTC)
    deadline = now + timedelta(days=1 if future else -1)
    catalog.publish_blob_and_owner_ref(
        identity=identity, relative_locator=date_bucket_relative_locator(identity.blob_id, now.date()),
        ingest_idempotency_key="retained-owner", attachment_id="att_retained",
        owner_session_id=SESSION_A, owner_thread_id="thr_" + "a" * 32,
        file_name="retained.bin", mime_type="application/octet-stream",
        retention_until=deadline.isoformat(),
    )
    released = catalog.release_owner_refs_for_session(owner_session_id=SESSION_A, reason="删除 owner")
    assert released[0].retention_until == deadline.isoformat()
    assert bool(catalog.list_gc_candidates(before=now + timedelta(seconds=1))) is not future
    if future:
        with pytest.raises(RuntimeError, match="owner retention"):
            catalog.tombstone_blob(identity.blob_id)
        assert catalog.get_blob(identity.blob_id).availability == "available"
    else:
        assert catalog.tombstone_blob(identity.blob_id).availability == "tombstoned"
    catalog.close()


# ----------------------------------------------------------------------
# pin / fence 发布复核 与 上传-删除竞态
# ----------------------------------------------------------------------


def _flip_fence_to_deleting(tmp_path: Path) -> None:
    sessions_root = tmp_path / ".boxteam" / "sessions"
    session_dir = next(
        path
        for path in sessions_root.rglob(SESSION_A)
        if path.is_dir()
    )
    control = SessionControlStore(session_dir / "session-control.sqlite")
    try:
        assert control.cas_fence_transition(1, "deleting") is True
    finally:
        control.close()


@pytest.mark.asyncio
async def test_publish_rechecks_active_session_under_same_gate(
    tmp_path, session_bundle_factory, monkeypatch
) -> None:
    """rename 后若 Session 已进入 deleting，发布必须被拒绝且不提交 owner ref。"""
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    original = store._commit_staged_blob

    def _rename_then_delete(**kwargs):
        original(**kwargs)
        _flip_fence_to_deleting(tmp_path)

    monkeypatch.setattr(store, "_commit_staged_blob", _rename_then_delete)

    with pytest.raises(SessionDeletionPendingError):
        await store.persist_inline(
            SESSION_A, [_attach("race.png", "image/png", b"race-bytes")],
            thread_id=store._path_resolver.main_thread_id(SESSION_A),
        )

    assert store._catalog.list_owner_refs_for_session(SESSION_A, active_only=True) == ()
    store.close()


@pytest.mark.asyncio
async def test_publish_rejects_when_pin_token_invalidated(
    tmp_path, session_bundle_factory, monkeypatch
) -> None:
    """pin 未生效（token 被接管/失效）时不得发布 owner reference。"""
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    original = store._commit_staged_blob
    captured: dict[str, object] = {}

    def _rename_then_takeover(**kwargs):
        original(**kwargs)
        sessions_root = tmp_path / ".boxteam" / "sessions"
        session_dir = next(path for path in sessions_root.rglob(SESSION_A) if path.is_dir())
        control = SessionControlStore(session_dir / "session-control.sqlite")
        try:
            lease = control.list_non_terminal_leases()[0]
            captured["lease_id"] = lease.lease_id
            control.takeover_lease(
                lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token
            )
        finally:
            control.close()

    monkeypatch.setattr(store, "_commit_staged_blob", _rename_then_takeover)

    with pytest.raises(SessionDeletionPendingError, match="pin 已失效"):
        await store.persist_inline(
            SESSION_A, [_attach("pin.png", "image/png", b"pin-bytes")],
            thread_id=store._path_resolver.main_thread_id(SESSION_A),
        )

    assert captured["lease_id"] is not None
    assert store._catalog.list_owner_refs_for_session(SESSION_A, active_only=True) == ()
    store.close()


@pytest.mark.asyncio
async def test_persist_rejected_when_session_already_deleting(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    _flip_fence_to_deleting(tmp_path)

    with pytest.raises(SessionDeletionPendingError):
        await store.persist_inline(SESSION_A, [_attach("x.png", "image/png", b"x")], thread_id=store._path_resolver.main_thread_id(SESSION_A))
    assert _blob_files(tmp_path) == []
    assert store._catalog.list_owner_refs_for_session(SESSION_A) == ()
    store.close()


# ----------------------------------------------------------------------
# 定点恢复 / 引用释放 / GC
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_session_references_keeps_other_session_blob(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A, SESSION_B)
    payload = b"shared"
    ref_a = (await store.persist_inline(SESSION_A, [_attach("a.bin", "application/pdf", payload)], thread_id=store._path_resolver.main_thread_id(SESSION_A)))[0]
    ref_b = (await store.persist_inline(SESSION_B, [_attach("b.bin", "application/pdf", payload)], thread_id=store._path_resolver.main_thread_id(SESSION_B)))[0]

    released = store.release_session_references(SESSION_A)

    assert ref_a.file_id.rsplit("/", 1)[-1] in "".join(released)
    with pytest.raises(FileNotFoundError):
        store.read(SESSION_A, ref_a.file_id)
    assert store.read(SESSION_B, ref_b.file_id).data == payload
    # 另有 session 的 active reference，GC 不得回收该正文。
    store.collect_garbage(retention=timedelta(0))
    assert len(_blob_files(tmp_path)) == 1
    store.close()


@pytest.mark.asyncio
async def test_collect_garbage_tombstones_then_deletes_unreferenced_blob(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    ref = (await store.persist_inline(SESSION_A, [_attach("a.bin", "application/pdf", b"gone")], thread_id=store._path_resolver.main_thread_id(SESSION_A)))[0]

    store.release_session_references(SESSION_A)
    collected = store.collect_garbage(retention=timedelta(0))

    assert collected
    assert _blob_files(tmp_path) == []
    with pytest.raises(FileNotFoundError):
        store.read(SESSION_A, ref.file_id)
    store.close()


@pytest.mark.asyncio
async def test_recover_session_cleans_orphan_staging_from_record(
    tmp_path, session_bundle_factory
) -> None:
    """无 record 文件不得被吸收；有 record 的 staging 按 record 定点清理。"""
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    staging_root = tmp_path / ".boxteam" / "attachments" / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    orphan = staging_root / "ing_88f6811ab5d8fc6d3177f9b7609ae0fc"
    orphan.write_bytes(b"orphan-without-record")

    assert store.recover_session(SESSION_A) == ()
    assert orphan.is_file()

    thread_id = store._path_resolver.main_thread_id(SESSION_A)
    control = store._open_control_store(SESSION_A)
    pin = await store._acquire_pin(
        session_id=SESSION_A, thread_id=thread_id, control=control,
        operation_identity="attkey_orphan", preimage_hash="d" * 64,
    )
    control.close()
    store._catalog.create_or_get_ingest_record(
        ingest_idempotency_key="attkey_orphan",
        ingest_id="ing_88f6811ab5d8fc6d3177f9b7609ae0fc",
        owner_session_id=SESSION_A,
        owner_thread_id=thread_id,
        pin_lease_id=pin.lease_id,
        pin_fencing_token=pin.fencing_token,
        pin_captured_generation=pin.captured_lifecycle_generation,
        preimage_hash="d" * 64,
        staging_relative_locator=ingest_staging_relative_locator("ing_88f6811ab5d8fc6d3177f9b7609ae0fc"),
        max_bytes=1024,
    )

    actions = store.recover_session(SESSION_A)
    assert "aborted-ingest" in actions
    assert not orphan.exists()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage_existing", [False, True], ids=["reuse", "reject-damaged"])
async def test_cross_domain_fork_import_reuses_verified_target_blob_without_streaming(
    fork_capture, tmp_path: Path, session_bundle_factory, damage_existing: bool,
) -> None:
    source, initial = fork_capture
    body = b"same-content-across-domains"
    source_ref = (await source.persist_inline(SESSION_A, [_attach("source.txt", "text/plain", body)],
                                             thread_id=initial.source_thread_id))[0]
    target_root = tmp_path / "target"
    target_root.mkdir()
    target = _store(target_root, session_bundle_factory, SESSION_B)
    try:
        target_thread = target._path_resolver.main_thread_id(SESSION_B)
        existing_ref = (await target.persist_inline(SESSION_B, [_attach("target.txt", "text/plain", body)],
                                                    thread_id=target_thread))[0]
        source_domain = source._path_resolver.catalog_store.get_node(SESSION_A).workspace_id
        target_domain = target._path_resolver.catalog_store.get_node(SESSION_B).workspace_id
        assert source_domain != target_domain
        request = SessionForkRequest(
            idempotency_key="existing-content-import",
            source_uri=f"boxteam://workspace/{source_domain}/resources/session/{SESSION_A}/threads/{initial.source_thread_id}",
            source_view_id="frozen-source-view", source_view_revision=0, mode="context_fork",
            target_container_uri=f"boxteam://workspace/{target_domain}/resources/workspace_sessions",
            creation_kind="new_session_main",
            resources=[{"resource_uri": source_ref.file_id, "action": "copy_snapshot"}],
        )
        record = replace(initial, target_thread_id=target_thread, request_json=json.dumps({
            **request.model_dump(mode="json"), "target_main_thread_id": target_thread,
            "source_storage_domain_id": source_domain, "target_storage_domain_id": target_domain, "target_gateway_id": get_current_gateway_id(),
        }))
        snapshots = source.capture_fork_attachments(
            record, same_storage_owner=False, references=((source_ref.file_id, True),),
        )
        assert len(snapshots) == 1 and snapshots[0].target_action == "copy_snapshot"
        existing_owner = target._catalog.get_owner_ref(parse_logical_file_id(SESSION_B, existing_ref.file_id))
        blob = target._catalog.get_blob(existing_owner.blob_id)
        path = resolve_blob_path(target._attachments_root, blob.relative_locator)
        before = path.stat()
        if damage_existing:
            path.write_bytes(b"x" * len(body))
        transport = SimpleNamespace(fork_transfer=AsyncMock(side_effect=AssertionError("目标已有正文不得请求传输 chunk")))
        if damage_existing:
            with pytest.raises(RuntimeError, match="hash/length"):
                await target.accept_fork_import(record, snapshots, transport=transport, request=request)
        else:
            await target.accept_fork_import(record, snapshots, transport=transport, request=request)
            await target.reserve_copy_attachments(record, None, snapshots)
            await target.finalize_copy_attachments(record, publication_preimage_hash=record.preimage_hash)
            projection = target.project_attachment(SESSION_B, target_thread, source_ref.file_id)
            assert projection["availability"] == "available"
            assert target.read(SESSION_B, projection["file_id"]).data == body
            assert path.read_bytes() == body
            after = path.stat()
            assert (after.st_ino, after.st_mtime_ns, after.st_ctime_ns) == (
                before.st_ino, before.st_mtime_ns, before.st_ctime_ns,
            )
            assert _blob_files(target_root) == [path]
        transport.fork_transfer.assert_not_awaited()
    finally:
        target.close()


@pytest.mark.asyncio
async def test_model_attachment_capability_normalizes_only_current_gateway(attachment_tool_owner):
    case = attachment_tool_owner
    gateway = "gateway_332f92ee6191880e797569a47e22922b"
    parsed = parse_session_artifact_uri(case.ref.file_id)
    read = {"thread_id": case.owner.thread_id, "tool_call_id": case.call_id, "model_call_id": case.model_call_id}
    token = set_current_gateway_id(gateway)
    try:
        qualified = session_artifact_uri(parsed.scope_id, SESSION_A, "attachments", parsed.tail_segments[-1], gateway_authority=gateway)
        assert case.saver.read_tool_call_attachment(SESSION_A, file_id=qualified, **read).data == b"first\noriginal attachment\n"
        for foreign in (
            qualified.replace(gateway, "gateway_ce27c875d842690955440795c9de8a35"),
            qualified.replace(parsed.scope_id, "019d1b4a-1111-7000-8000-000000000001"),
        ):
            with pytest.raises(PermissionError, match="原始 assembly"):
                case.saver.read_tool_call_attachment(SESSION_A, file_id=foreign, **read)
    finally:
        reset_current_gateway_id(token)


@pytest.mark.asyncio
async def test_preview_cache_io_error_keeps_uri_and_errno_in_generated_diagnostics(
    tmp_path, session_bundle_factory, monkeypatch,
) -> None:
    import errno

    from app.services.infrastructure.attachment_blob_catalog import (
        store as store_module,
    )

    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    buffer = BytesIO()
    Image.new("RGB", (64, 64)).save(buffer, format="PNG")
    attachment = (await store.persist_inline(
        SESSION_A, [_attach("preview.png", "image/png", buffer.getvalue())],
        thread_id=store._path_resolver.main_thread_id(SESSION_A),
    ))[0]

    def denied_write(target, data):
        raise PermissionError(errno.EACCES, "Permission denied", str(target))

    monkeypatch.setattr(store_module, "_write_once", denied_write)
    try:
        built = UserContentBuilder(attachment_service=AttachmentContentService(
            workspace_root=tmp_path, attachment_store=store,
        )).build("读取附件", [attachment])
        assert built.diagnostics[0]["file_id"] == attachment.file_id
        assert "Errno 13" in built.diagnostics[0]["detail"]
        assert attachment.file_id in built.diagnostics[0]["detail"]
        assert str(tmp_path) not in str(built)
        assert built.content[1]["metadata"]["preview_status"] == "failed"
    finally:
        store.close()
