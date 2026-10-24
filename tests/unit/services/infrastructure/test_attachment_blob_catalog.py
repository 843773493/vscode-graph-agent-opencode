"""附件目录 owner 的引用、完整性、事务发布、冻结 fork 与定点清理合同。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
from contextlib import closing
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Event
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
from app.core.resource_uri import (
    file_uri_root,
    parse_attachment_uri,
    parse_resource_uri,
    resource_display_uri,
)
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_catalog_store.contracts import ForkSourceCaptureRecord
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    SessionDeletionPendingError,
    SessionOperationLease,
)
from app.core.session_thread_context import session_thread_owner_scope
from app.core.thread_creation import ThreadCreationService
from app.core.trace_middleware import (
    get_current_gateway_id,
    reset_current_gateway_id,
    set_current_gateway_id,
)
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import user_input_payload
from app.schemas.internal_v2.message import AttachmentRef
from app.schemas.internal_v2.session import SessionForkRequest
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.business.user_content_builder import UserContentBuilder
from app.services.infrastructure.attachment_blob_catalog import (
    fork_import as fork_import_module,
)
from app.services.infrastructure.attachment_blob_catalog import (
    store as attachment_blob_store_module,
)
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentBlobCatalog,
    AttachmentDirectoryRetainedError,
)
from app.services.infrastructure.attachment_blob_catalog.fork_import import _import_id
from app.services.infrastructure.attachment_blob_catalog.locator import (
    attachment_directory_relative_locator,
    ingest_staging_relative_locator,
    resolve_attachment_directory,
    resolve_captured_directory,
    resolve_staging_path,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.attachment_content_service import (
    AttachmentContentService,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.fork.source_capture import (
    capture_directory,
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


def _original_files(workspace_root: Path) -> list[Path]:
    return sorted((workspace_root / ".boxteam/attachments").glob("*/*/*/att_*/original/content"))


def _original_path(store: AttachmentBlobStore, file_id: str) -> Path:
    return store.resolve_directory_uri(parse_resource_uri(file_id, local_gateway_id=get_current_gateway_id()))


def _store(tmp_path: Path, session_bundle_factory, *session_ids: str) -> AttachmentBlobStore:
    for session_id in session_ids:
        session_bundle_factory(tmp_path / ".boxteam" / "sessions", session_id)
    return AttachmentBlobStore(tmp_path)


async def test_upload_restricts_original_before_publication(
    tmp_path: Path, session_bundle_factory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    thread = store._path_resolver.main_thread_id(SESSION_A)
    restricted_originals: list[tuple[Path, int]] = []
    restrict = attachment_blob_store_module.restrict_private_path_access

    def restrict_and_record(path: Path, **kwargs) -> None:
        if path.name == "content":
            restricted_originals.append((path, path.stat().st_size))
        restrict(path, **kwargs)

    monkeypatch.setattr(attachment_blob_store_module, "restrict_private_path_access", restrict_and_record)
    try:
        ref, = await store.persist_inline(SESSION_A, [_attach("private.txt", "text/plain", b"private attachment")],
                                         thread_id=thread, message_id="private-upload")
        target = _original_path(store, ref.file_id)
        assert target.read_bytes() == b"private attachment"
        staged, size = restricted_originals[0]
        assert len(restricted_originals) == 1 and size == 0
        assert staged != target and not staged.exists()
        if os.name != "nt":
            assert target.stat().st_mode & 0o077 == 0
            assert target.parent.stat().st_mode & 0o077 == 0
    finally:
        store.close()


async def test_upload_permission_failure_aborts_without_publishing_directory(
    tmp_path: Path, session_bundle_factory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    thread = store._path_resolver.main_thread_id(SESSION_A)
    restrict = attachment_blob_store_module.restrict_private_path_access

    def reject_original(path: Path, **kwargs) -> None:
        if path.name == "content":
            raise PermissionError("ACL update denied")
        restrict(path, **kwargs)

    monkeypatch.setattr(attachment_blob_store_module, "restrict_private_path_access", reject_original)
    try:
        with pytest.raises(PermissionError, match="ACL update denied"):
            await store.persist_inline(SESSION_A, [_attach("private.txt", "text/plain", b"private attachment")],
                                       thread_id=thread, message_id="private-upload-failed")
        assert store._catalog.connection.execute("SELECT * FROM attachment_directories").fetchall() == []
        assert [row[0] for row in store._catalog.connection.execute("SELECT state FROM attachment_directory_ingests")] == ["aborted"]
        assert _original_files(tmp_path) == []
        assert list((tmp_path / ".boxteam/attachments/.staging").iterdir()) == []
        with closing(store._open_control_store(SESSION_A)) as control:
            assert [row[0] for row in control.connection.execute("SELECT state FROM session_operation_leases")] == ["failed"]
    finally:
        store.close()


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
    ref = (await store.persist_inline(SESSION_A, [_attach("input.txt", "text/plain", b"first\noriginal attachment\n")], thread_id=record.source_thread_id, message_id="msg_" + create_uuid_hex(namespace="message")))[0]
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
    monkeypatch.setattr(attachment_tool_owner.store, "_fork_directory_manifest", unexpected_body_read)
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
def fork_attachment_projection(fork_capture, tmp_path, monkeypatch):
    """复用真实 Saver 的附件投影接线，仅观察真实附件投影调用次数。"""
    store, _ = fork_capture
    calls: list[tuple[str, str, str]] = []
    original = AttachmentBlobStore.project_attachment

    def project(self: AttachmentBlobStore, session_id: str, thread_id: str, file_id: str) -> dict[str, str]:
        calls.append((session_id, thread_id, file_id))
        return original(self, session_id, thread_id, file_id)

    monkeypatch.setattr(AttachmentBlobStore, "project_attachment", project)
    saver = RolloutCheckpointSaver(tmp_path / ".boxteam" / "sessions", attachment_store=store)
    return saver._storage.attachment_projection, calls


@pytest.mark.asyncio
async def test_fork_capture_accepts_historical_gateway_uri_with_no_ambient_context(attachment_tool_owner, fork_capture):
    case = attachment_tool_owner
    _, record = fork_capture
    storage = case.saver._storage.storage
    with storage._connect(case.owner.thread_id, "", session_id=SESSION_A) as connection:
        canonical_refs = tuple(row[0] for row in connection.execute("SELECT DISTINCT resource_uri FROM item_resource_references WHERE resource_kind='attachment'"))
    assert canonical_refs == (case.ref.file_id,)
    parsed = parse_resource_uri(case.ref.file_id)
    historical_gateway = "gateway_6c8c19e583c1f236c73a80810503bc7e"
    historical_uri = resource_display_uri(scope="workspace", scope_id=parsed.scope_id, kind="attachments",
        tail_segments=parsed.tail_segments, gateway_authority=historical_gateway)
    token = set_current_gateway_id(None)
    try:
        with pytest.raises(ValueError, match="固定目标工作区"):
            case.store._parse_logical_file_id(SESSION_A, historical_uri)
        (snapshot,) = case.store.capture_fork_attachments(record, references=((historical_uri, True),), same_storage_owner=True)
        assert (snapshot.source_session_id, snapshot.source_thread_id, snapshot.source_attachment_id, snapshot.source_file_id) == (
            SESSION_A, case.owner.thread_id, parsed.attachment_id, historical_uri,
        )
        assert snapshot.availability == "available" and snapshot.claim_id is not None
        wrong = historical_uri.replace(parsed.scope_id, "019d1b4a-1111-7000-8000-000000000001")
        with pytest.raises(ValueError, match="固定目标工作区"):
            case.store.capture_fork_attachments(replace(record, copy_operation_id="foreign-workspace"), references=((wrong, True),), same_storage_owner=True)
        missing = historical_uri.replace(parsed.attachment_id, "att_" + "9" * 32)
        with pytest.raises(FileNotFoundError, match="context_required_resource_unavailable"):
            case.store.capture_fork_attachments(replace(record, copy_operation_id="missing-directory"), references=((missing, True),), same_storage_owner=True)
    finally:
        reset_current_gateway_id(token)


@pytest.mark.asyncio
async def test_attachment_capability_uses_original_model_call_after_new_ingress(attachment_tool_owner):
    case = attachment_tool_owner
    read = {"thread_id": case.owner.thread_id, "tool_call_id": case.call_id, "model_call_id": case.model_call_id}
    assert case.saver.read_tool_call_attachment(SESSION_A, file_id=case.ref.file_id, **read).data == b"first\noriginal attachment\n"
    late = (await case.store.persist_inline(SESSION_A, [_attach("late.txt", "text/plain", b"queued secret")], thread_id=case.owner.thread_id, message_id="msg_" + create_uuid_hex(namespace="message")))[0]
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
async def test_attachment_capability_requires_assembly_membership_despite_workspace_read_access(attachment_tool_owner):
    case = attachment_tool_owner
    peer = (await case.store.persist_inline(SESSION_B, [_attach("peer.txt", "text/plain", b"first\noriginal attachment\n")], thread_id=case.store._path_resolver.main_thread_id(SESSION_B), message_id="msg_" + create_uuid_hex(namespace="message")))[0]
    assert len(_original_files(case.store._workspace_root)) == 2
    with pytest.raises(PermissionError, match="原始 assembly"):
        case.saver.read_tool_call_attachment(SESSION_A, thread_id=case.owner.thread_id,
            tool_call_id=case.call_id, model_call_id=case.model_call_id, file_id=peer.file_id)
    assert case.store.read(SESSION_A, peer.file_id).data == b"first\noriginal attachment\n"
    assert case.store.read(SESSION_A, case.ref.file_id, owner_thread_id=case.store._path_resolver.main_thread_id(SESSION_B)).data == b"first\noriginal attachment\n"
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
async def test_fork_projects_generated_attachment_blocks_without_mutating_user_text(fork_capture, fork_attachment_projection, tmp_path, content_type: str):
    store, record = fork_capture
    buffer = BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, format="PNG")
    ref = (await store.persist_inline(SESSION_A, [_attach("input", content_type, buffer.getvalue() if content_type == "image/png" else b"fork input")], thread_id=record.source_thread_id, message_id="msg_" + create_uuid_hex(namespace="message")))[0]
    user_text = "用户文字中的 URI 必须保持原样 " + ref.file_id
    built = UserContentBuilder(attachment_service=AttachmentContentService(workspace_root=tmp_path, attachment_store=store)).build(user_text, [ref])
    snapshots = store.capture_fork_attachments(record, same_storage_owner=True, references=((ref.file_id, True),))
    captured = snapshots[0]
    await store.reserve_copy_attachments(record, None, snapshots)
    assert store.project_attachment(SESSION_B, record.target_thread_id, ref.file_id)["availability"] == "unavailable"
    with pytest.raises(FileNotFoundError, match="没有已发布的目标目录"):
        store.read(SESSION_B, captured.target_file_id, owner_thread_id=record.target_thread_id)
    await store.finalize_copy_attachments(record, publication_preimage_hash=record.preimage_hash)
    projection, calls = fork_attachment_projection
    message = message_to_dict(HumanMessage(content=built.content, response_metadata={"attachments": [ref.model_dump(mode="json", exclude={"data_url"})]}))
    original = json.dumps(message, sort_keys=True)
    calls.clear()
    projected = projection.project_message_attachments(SESSION_B, record.target_thread_id, message)
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
async def test_fork_reclaimed_directory_respects_required_without_recreating_bytes(fork_capture, required: bool) -> None:
    store, record = fork_capture
    (ref,) = await store.persist_inline(SESSION_A, [_attach("capture.txt", "text/plain", b"fork-body")],
        thread_id=record.source_thread_id, message_id="msg_reclaimed")
    attachment_id = parse_resource_uri(ref.file_id).attachment_id
    store.release_session_references(SESSION_A)
    current = datetime.now(UTC)
    store._clock = lambda: current + timedelta(days=31)
    assert await store.collect_garbage(leases=ExternalResourceLeaseLedger()) == (attachment_id,)
    before = store._catalog.get_directory(attachment_id)
    if required:
        with pytest.raises(FileNotFoundError, match="context_required_resource_unavailable"):
            store.capture_fork_attachments(record, same_storage_owner=True, references=((ref.file_id, True),))
    else:
        (snapshot,) = store.capture_fork_attachments(record, same_storage_owner=True, references=((ref.file_id, False),))
        assert snapshot.availability == "unavailable" and snapshot.claim_id is None
        assert snapshot.target_action == "omit" and snapshot.unavailable_reason
        assert store.project_attachment(SESSION_B, record.target_thread_id, ref.file_id) == {"file_id": ref.file_id, "availability": "unavailable"}
    assert store._catalog.fork_attachment_claim_ids(record.copy_operation_id) == ()
    assert store._catalog.get_directory(attachment_id) == before
    assert _original_files(store._workspace_root) == []


@pytest.mark.asyncio
async def test_fork_attachment_same_domain_different_owner_rejects_before_claim(fork_capture) -> None:
    store, record = fork_capture
    ref = (await store.persist_inline(SESSION_A, [_attach("owner.txt", "text/plain", b"same-domain")], thread_id=record.source_thread_id, message_id="msg_" + create_uuid_hex(namespace="message")))[0]
    source = store._catalog.get_directory(parse_resource_uri(ref.file_id).attachment_id)
    with pytest.raises(ValueError, match="fork_owner_overlap"):
        store.capture_fork_attachments(record, references=((ref.file_id, True),), same_storage_owner=False)
    assert store._catalog.fork_attachment_claim_ids(record.copy_operation_id) == ()
    assert store._catalog.fork_attachment_snapshots(record.copy_operation_id, target_session_id=record.target_session_id,
                                                  target_thread_id=record.target_thread_id) == ()
    assert store._catalog.get_directory(source.attachment_id) == source


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["hash", "symlink"])
async def test_optional_fork_attachment_damage_is_not_unavailable(fork_capture, damage: str) -> None:
    store, record = fork_capture
    ref = (await store.persist_inline(SESSION_A, [_attach("capture.txt", "text/plain", b"fork-body")], thread_id=record.source_thread_id, message_id="msg_" + create_uuid_hex(namespace="message")))[0]
    source = store._catalog.get_directory(parse_resource_uri(ref.file_id).attachment_id)
    path = _original_path(store, ref.file_id)
    path.chmod(0o600)
    path.parent.chmod(0o700)
    if damage == "hash":
        path.write_bytes(b"changed-bytes")
        error = "已登记版本不一致"
    else:
        path.unlink()
        path.symlink_to(path.with_name("missing-body"))
        error = "符号链接"
    with pytest.raises(RuntimeError, match=error):
        store.capture_fork_attachments(record, same_storage_owner=True, references=((ref.file_id, False),))
    assert store._catalog.fork_attachment_claim_ids(record.copy_operation_id) == ()
    assert store._catalog.fork_attachment_snapshots(record.copy_operation_id, target_session_id=SESSION_B, target_thread_id=record.target_thread_id) == ()
    assert store._catalog.get_directory(source.attachment_id) == source


# ----------------------------------------------------------------------
# 身份与受检 locator
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [False, True])
async def test_fork_attachment_same_length_damage_never_reuses_stat_cache(fork_capture, warm: bool) -> None:
    """冷读与已验证文件均拒绝同长度损坏，即使调用方把 mtime 恢复为原值。"""
    store, record = fork_capture
    ref = (await store.persist_inline(SESSION_A, [_attach("capture.txt", "text/plain", b"fork-body")], thread_id=record.source_thread_id, message_id="msg_" + create_uuid_hex(namespace="message")))[0]
    source = store._catalog.get_directory(parse_resource_uri(ref.file_id).attachment_id)
    path = _original_path(store, ref.file_id)
    path.chmod(0o600)
    path.parent.chmod(0o700)
    metadata = path.stat()
    if warm:
        store.capture_fork_attachments(record, same_storage_owner=True, references=((ref.file_id, True),))
    claims = store._catalog.fork_attachment_claim_ids(record.copy_operation_id)
    path.write_bytes(b"bad-bytes")
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    with pytest.raises(RuntimeError, match="已登记版本不一致"):
        store.capture_fork_attachments(record, same_storage_owner=True, references=((ref.file_id, True),))
    assert store._catalog.fork_attachment_claim_ids(record.copy_operation_id) == claims
    assert store._catalog.get_directory(source.attachment_id) == source














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
        parse_attachment_uri(file_id, require_file=True)


def test_directory_reference_rejects_foreign_workspace(tmp_path, session_bundle_factory) -> None:
    with closing(_store(tmp_path, session_bundle_factory, SESSION_A)) as store:
        foreign = resource_display_uri(scope="workspace", scope_id="019d1b4a-1111-7000-8000-000000000001",
            kind="attachments", tail_segments=("att_" + "1" * 32, "original", "content"))
        with pytest.raises(ValueError, match="固定目标工作区"):
            store.read(SESSION_A, foreign)


# ----------------------------------------------------------------------
# 写入与读取
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_inline_writes_independent_directory_and_reads_original(tmp_path, session_bundle_factory) -> None:
    with closing(_store(tmp_path, session_bundle_factory, SESSION_A)) as store:
        payload = b"original-image-bytes"
        (stored,) = await store.persist_inline(SESSION_A, [_attach("example.png", "image/png", payload)],
            thread_id=store._path_resolver.main_thread_id(SESSION_A), message_id="msg_" + create_uuid_hex(namespace="message"))
        parsed = parse_attachment_uri(stored.file_id, require_file=True)
        assert stored.data_url is None and parsed.kind == "attachments" and parsed.scope_id == store.workspace_id
        assert parsed.tail_segments[1:] == ("original", "content")
        assert stored.directory_uri == file_uri_root(stored.file_id)
        (original,) = _original_files(tmp_path)
        directory = store._catalog.get_directory(parsed.attachment_id)
        assert original == resolve_attachment_directory(store._attachments_root, directory.relative_locator) / "original/content"
        assert directory.relative_locator.startswith(datetime.now(UTC).strftime("%Y/%m/%d/"))
        assert (original.parent.parent / "preview").is_dir() and (original.parent.parent / "derived").is_dir()
        assert store.read(SESSION_A, stored.file_id).data == payload
        assert store.read(SESSION_A, stored.file_id).content_type == "image/png"


@pytest.mark.asyncio
async def test_independent_uploads_with_same_body_have_distinct_directory_identities(tmp_path, session_bundle_factory) -> None:
    with closing(_store(tmp_path, session_bundle_factory, SESSION_A)) as store:
        attachment = _attach("duplicate.txt", "text/plain", b"same-body")
        thread = store._path_resolver.main_thread_id(SESSION_A)
        (first,) = await store.persist_inline(SESSION_A, [attachment], thread_id=thread, message_id="msg_first")
        (second,) = await store.persist_inline(SESSION_A, [attachment], thread_id=thread, message_id="msg_second")
        assert first.file_id != second.file_id and first.directory_uri != second.directory_uri
        assert len(_original_files(tmp_path)) == 2
        (_original_path(store, first.file_id).parent.parent / "derived/result.txt").write_text("first-only")
        assert not (_original_path(store, second.file_id).parent.parent / "derived/result.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before-write", "after-write", "before-publish"])
async def test_failed_upload_can_retry_same_body_without_reopening_old_ingest(tmp_path, session_bundle_factory, monkeypatch, boundary) -> None:
    with closing(_store(tmp_path, session_bundle_factory, SESSION_A)) as store:
        thread = store._path_resolver.main_thread_id(SESSION_A)
        attachment = _attach("retry.txt", "text/plain", b"retry-identical-content")
        write = store._write_directory
        def fail_write(*args):
            if boundary == "after-write":
                write(*args)
            raise OSError("注入上传失败")
        def fail_publish(*args, **kwargs):
            raise OSError("注入上传失败")
        with monkeypatch.context() as patch:
            if boundary == "before-publish":
                patch.setattr(store._catalog, "publish_ingest", fail_publish)
            else:
                patch.setattr(store, "_write_directory", fail_write)
            with pytest.raises(OSError, match="注入上传失败"):
                await store.persist_inline(SESSION_A, [attachment], thread_id=thread, message_id="msg_failed")
        (failed,) = store._catalog.connection.execute("SELECT * FROM attachment_directory_ingests").fetchall()
        before = dict(failed)
        assert before["state"] == "aborted"
        assert not resolve_attachment_directory(store._attachments_root, before["relative_locator"]).exists()
        (result,) = await store.persist_inline(SESSION_A, [attachment], thread_id=thread, message_id="msg_retry")
        assert store.read(SESSION_A, result.file_id).data == b"retry-identical-content"
        assert parse_resource_uri(result.file_id).attachment_id != before["attachment_id"]
        assert dict(store._catalog.connection.execute("SELECT * FROM attachment_directory_ingests WHERE ingest_idempotency_key=?", (before["ingest_idempotency_key"],)).fetchone()) == before
        assert len(_original_files(tmp_path)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_same_upload_retry_rejects_damage_while_new_upload_keeps_separate_original(tmp_path, session_bundle_factory, damage) -> None:
    with closing(_store(tmp_path, session_bundle_factory, SESSION_A)) as store:
        attachment = _attach("winner.txt", "text/plain", b"winner-content")
        thread = store._path_resolver.main_thread_id(SESSION_A)
        (first,) = await store.persist_inline(SESSION_A, [attachment], thread_id=thread, message_id="msg_same")
        path = _original_path(store, first.file_id)
        path.chmod(0o600)
        if damage == "missing":
            path.parent.chmod(0o700)
            path.unlink()
        else:
            path.write_bytes(b"wrong-content!")
        with pytest.raises(FileNotFoundError if damage == "missing" else RuntimeError):
            await store.persist_inline(SESSION_A, [attachment], thread_id=thread, message_id="msg_same")
        (second,) = await store.persist_inline(SESSION_A, [attachment], thread_id=thread, message_id="msg_distinct")
        assert second.file_id != first.file_id and store.read(SESSION_A, second.file_id).data == b"winner-content"
        if damage == "corrupt":
            assert path.read_bytes() == b"wrong-content!"





@pytest.mark.asyncio
async def test_same_digest_across_sessions_uses_independent_directories(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A, SESSION_B)
    payload = b"shared-bytes"

    ref_a = (await store.persist_inline(SESSION_A, [_attach("a.bin", "application/pdf", payload)], thread_id=store._path_resolver.main_thread_id(SESSION_A), message_id="msg_" + create_uuid_hex(namespace="message")))[0]
    ref_b = (await store.persist_inline(SESSION_B, [_attach("b.bin", "application/pdf", payload)], thread_id=store._path_resolver.main_thread_id(SESSION_B), message_id="msg_" + create_uuid_hex(namespace="message")))[0]

    assert len(_original_files(tmp_path)) == 2
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
         message_id="msg_" + create_uuid_hex(namespace="message"))
    )[0]

    assert store.read(SESSION_A, stored.file_id).data == b"custom"
    assert store.read(SESSION_A, stored.file_id).content_type == "application/x-custom-document"
    store.close()


@pytest.mark.asyncio
async def test_workspace_directory_read_does_not_create_retaining_association(tmp_path, session_bundle_factory) -> None:
    with closing(_store(tmp_path, session_bundle_factory, SESSION_A, SESSION_B)) as store:
        (ref,) = await store.persist_inline(SESSION_A, [_attach("a.txt", "text/plain", b"workspace-readable")],
            thread_id=store._path_resolver.main_thread_id(SESSION_A), message_id="msg_workspace")
        before = tuple(store._catalog.connection.execute("SELECT * FROM attachment_directory_associations"))
        assert store.read(SESSION_B, ref.file_id).data == b"workspace-readable"
        assert tuple(store._catalog.connection.execute("SELECT * FROM attachment_directory_associations")) == before


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
        await store.persist_inline(SESSION_A, [attachment], thread_id=store._path_resolver.main_thread_id(SESSION_A), message_id="msg_" + create_uuid_hex(namespace="message"))
    assert _original_files(tmp_path) == []
    store.close()


@pytest.mark.asyncio
async def test_model_manifest_exposes_directory_and_file_uris_without_physical_locator(tmp_path, session_bundle_factory) -> None:
    with closing(_store(tmp_path, session_bundle_factory, SESSION_A)) as store:
        (stored,) = await store.persist_inline(SESSION_A, [_attach("a.txt", "text/plain", b"content")],
            thread_id=store._path_resolver.main_thread_id(SESSION_A), message_id="msg_manifest")
        built = UserContentBuilder(attachment_service=AttachmentContentService(workspace_root=tmp_path, attachment_store=store)).build("读取附件", [stored])
        manifest = built.content[1]
        assert manifest["metadata"]["file_id"] == stored.file_id
        assert manifest["metadata"]["directory_uri"] == stored.directory_uri
        assert stored.file_id in manifest["text"] and stored.directory_uri in manifest["text"]
        assert ".boxteam/attachments" not in str(built) and str(tmp_path) not in str(built)


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
        await store.persist_inline(SESSION_A, [_attach("large.png", "image/png", buffer.getvalue())], thread_id=store._path_resolver.main_thread_id(SESSION_A), message_id="msg_" + create_uuid_hex(namespace="message"))
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
        await store.persist_inline(SESSION_A, [_attach("bad.png", "image/png", truncated)], thread_id=store._path_resolver.main_thread_id(SESSION_A), message_id="msg_" + create_uuid_hex(namespace="message"))
    )[0]

    with pytest.raises(ValueError, match="已损坏或不是有效图片"):
        store.read_thumbnail(SESSION_A, stored.file_id)
    assert store.read(SESSION_A, stored.file_id).data == truncated
    store.close()


@pytest.mark.asyncio
async def test_preview_cache_io_error_keeps_uri_and_errno_in_generated_diagnostics(
    tmp_path, session_bundle_factory, monkeypatch,
) -> None:
    import errno

    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    buffer = BytesIO()
    Image.new("RGB", (64, 64)).save(buffer, format="PNG")
    attachment = (await store.persist_inline(
        SESSION_A, [_attach("preview.png", "image/png", buffer.getvalue())],
        thread_id=store._path_resolver.main_thread_id(SESSION_A),
     message_id="msg_" + create_uuid_hex(namespace="message")))[0]

    original_open = Path.open

    def denied_preview_open(target, mode="r", *args, **kwargs):
        if target.parts[-2:] == ("preview", "512.webp") and mode == "xb":
            raise PermissionError(errno.EACCES, "Permission denied", str(target))
        return original_open(target, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_preview_open)
    try:
        built = UserContentBuilder(attachment_service=AttachmentContentService(
            workspace_root=tmp_path, attachment_store=store,
        )).build("读取附件", [attachment])
        assert built.diagnostics[0]["file_id"] == attachment.file_id
        assert built.diagnostics[0]["detail"] == (
            f"附件预览生成失败: file_id={attachment.file_id}, "
            "stage=image_preview, error=PermissionError, errno=13"
        )
        assert attachment.file_id in built.diagnostics[0]["detail"]
        assert str(tmp_path) not in str(built)
        assert built.content[1]["metadata"]["preview_status"] == "failed"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_read_thumbnail_rejects_non_image(tmp_path, session_bundle_factory) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    stored = (
        await store.persist_inline(SESSION_A, [_attach("doc.pdf", "application/pdf", b"%PDF-1.7")], thread_id=store._path_resolver.main_thread_id(SESSION_A), message_id="msg_" + create_uuid_hex(namespace="message"))
    )[0]

    with pytest.raises(ValueError, match="不是图片"):
        store.read_thumbnail(SESSION_A, stored.file_id)
    store.close()


# ----------------------------------------------------------------------
# 目录 intent 与不可变版本冲突（catalog 层，确定性）
# ----------------------------------------------------------------------


def _catalog(tmp_path: Path) -> AttachmentBlobCatalog:
    return AttachmentBlobCatalog(tmp_path / ".boxteam/attachments/catalog.sqlite",
                                 workspace_id="a475d596-039d-48db-a14c-a0cbf7b4b58c")


def _create_ingest(catalog: AttachmentBlobCatalog, operation: str, now: datetime, **changes):
    stamp = now.isoformat()
    arguments = {
        "operation_id": operation, "ingest_id": "ing_" + hashlib.sha256(operation.encode()).hexdigest()[:32],
        "session_id": SESSION_A, "thread_id": "thr_" + "a" * 32, "message_id": "msg_" + "b" * 32,
        "pin": SessionOperationLease(lease_id="lease_" + operation, operation_kind="attachment",
            operation_identity=operation, preimage_hash="c" * 64, captured_lifecycle_generation=1,
            holder_generation=1, fencing_token=1, state="active", revision=1, recovery_ref=None,
            created_at=stamp, updated_at=stamp),
        "digest": "sha256:" + hashlib.sha256(b"same").hexdigest(), "length": 4,
        "file_name": "input.txt", "mime_type": "text/plain", "now": now,
    }
    return catalog.create_ingest(**{**arguments, **changes})


@pytest.mark.parametrize("abort_first", [False, True])
def test_independent_ingests_do_not_merge_directory_identity_or_creation_date(tmp_path: Path, abort_first: bool) -> None:
    now = datetime(2026, 10, 23, 23, 59, 59, tzinfo=UTC)
    with closing(_catalog(tmp_path)) as catalog:
        first = _create_ingest(catalog, "attkey_first", now)
        if abort_first:
            catalog.abort_ingest_record(ingest_idempotency_key=first.ingest_idempotency_key, reason="上传失败", now=now)
        before = catalog.get_ingest_record(first.ingest_idempotency_key)
        second = _create_ingest(catalog, "attkey_second", now + timedelta(seconds=2))
        assert first.digest == second.digest and first.length == second.length
        assert first.attachment_id != second.attachment_id
        assert first.relative_locator == "2026/10/23/" + first.attachment_id
        assert second.relative_locator == "2026/10/24/" + second.attachment_id
        assert catalog.get_ingest_record(first.ingest_idempotency_key) == before
        assert catalog.connection.execute("SELECT count(*) FROM attachment_directories").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [("digest", "sha256:" + "d" * 64), ("length", 99)])
def test_published_file_version_rejects_different_content_without_overwriting(tmp_path: Path, field: str, value: object) -> None:
    now = datetime.now(UTC)
    with closing(_catalog(tmp_path)) as catalog:
        ingest = _create_ingest(catalog, "attkey_version", now)
        with pytest.raises(RuntimeError, match="尚未 durable"):
            catalog.publish_ingest(ingest.ingest_idempotency_key, now=now)
        assert catalog.get_directory(ingest.attachment_id) is None
        catalog.mark_ingest_durable(ingest.ingest_idempotency_key, now=now)
        published = catalog.publish_ingest(ingest.ingest_idempotency_key, now=now)
        original = catalog.file_version(ingest.attachment_id, "original/content")
        assert original.digest == ingest.digest and original.length == ingest.length
        assert catalog.publish_ingest(ingest.ingest_idempotency_key, now=now) == published
        catalog.publish_file_version(original)
        with pytest.raises(RuntimeError, match="版本冲突"):
            catalog.publish_file_version(replace(original, **{field: value}))
        assert catalog.file_version(ingest.attachment_id, "original/content") == original
        assert catalog.connection.execute("SELECT count(*) FROM attachment_directory_files").fetchone()[0] == 1


@pytest.mark.parametrize("field,value", [
    ("digest", "sha256:" + "d" * 64), ("length", 99), ("file_name", "other.txt"),
    ("mime_type", "application/pdf"), ("session_id", SESSION_B), ("thread_id", "thr_" + "d" * 32),
    ("message_id", "msg_" + "d" * 32),
])
def test_ingest_record_rejects_frozen_input_conflict(tmp_path: Path, field: str, value: object) -> None:
    now = datetime.now(UTC)
    with closing(_catalog(tmp_path)) as catalog:
        original = _create_ingest(catalog, "attkey_conflict", now)
        assert _create_ingest(catalog, "attkey_conflict", now + timedelta(days=1)) == original
        with pytest.raises(RuntimeError, match="冻结输入冲突"):
            _create_ingest(catalog, "attkey_conflict", now, **{field: value})
        assert catalog.get_ingest_record(original.ingest_idempotency_key) == original


@pytest.mark.parametrize("future", [False, True])
def test_released_directory_retention_and_thirty_days_both_protect_deletion(tmp_path: Path, future: bool) -> None:
    now = datetime.now(UTC)
    with closing(_catalog(tmp_path)) as catalog:
        ingest = _create_ingest(catalog, "attkey_retained", now - timedelta(days=100))
        catalog.mark_ingest_durable(ingest.ingest_idempotency_key, now=now)
        catalog.publish_ingest(ingest.ingest_idempotency_key, now=now)
        catalog.release_associations(session_id=SESSION_A, now=now - timedelta(days=31))
        before = catalog.get_directory(ingest.attachment_id)
        assert before.gc_after == (now - timedelta(days=1)).isoformat()
        catalog.release_associations(session_id=SESSION_A, now=now)
        assert catalog.get_directory(ingest.attachment_id) == before
        deadline = now + timedelta(days=1 if future else -1)
        with catalog.write_transaction() as connection:
            connection.execute("UPDATE attachment_directories SET retention_until=? WHERE attachment_id=?", (deadline.isoformat(), ingest.attachment_id))
        assert bool(catalog.gc_candidates(now=now)) is not future
        if future:
            with pytest.raises(AttachmentDirectoryRetainedError, match="尚未到期"):
                catalog.begin_delete(ingest.attachment_id, now=now)
            assert catalog.get_directory(ingest.attachment_id).availability == "available"
        else:
            assert catalog.begin_delete(ingest.attachment_id, now=now).availability == "deleting"
            catalog.finish_delete(ingest.attachment_id, now=now)
            assert catalog.get_directory(ingest.attachment_id).availability == "tombstoned"


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
    original = store._write_directory

    def _rename_then_delete(*args, **kwargs):
        original(*args, **kwargs)
        _flip_fence_to_deleting(tmp_path)

    monkeypatch.setattr(store, "_write_directory", _rename_then_delete)

    with pytest.raises(SessionDeletionPendingError):
        await store.persist_inline(
            SESSION_A, [_attach("race.png", "image/png", b"race-bytes")],
            thread_id=store._path_resolver.main_thread_id(SESSION_A),
         message_id="msg_" + create_uuid_hex(namespace="message"))

    assert store._catalog.connection.execute("SELECT 1 FROM attachment_directory_associations WHERE state='active'").fetchall() == []
    store.close()


@pytest.mark.asyncio
async def test_publish_rejects_when_pin_token_invalidated(
    tmp_path, session_bundle_factory, monkeypatch
) -> None:
    """pin 未生效（token 被接管/失效）时不得发布 owner reference。"""
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    original = store._write_directory
    captured: dict[str, object] = {}

    def _rename_then_takeover(*args, **kwargs):
        original(*args, **kwargs)
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

    monkeypatch.setattr(store, "_write_directory", _rename_then_takeover)

    with pytest.raises(SessionDeletionPendingError, match="pin 或 Session 代次已失效"):
        await store.persist_inline(
            SESSION_A, [_attach("pin.png", "image/png", b"pin-bytes")],
            thread_id=store._path_resolver.main_thread_id(SESSION_A),
         message_id="msg_" + create_uuid_hex(namespace="message"))

    assert captured["lease_id"] is not None
    assert store._catalog.connection.execute("SELECT 1 FROM attachment_directory_associations WHERE state='active'").fetchall() == []
    store.close()


@pytest.mark.asyncio
async def test_persist_rejected_when_session_already_deleting(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    _flip_fence_to_deleting(tmp_path)

    with pytest.raises(SessionDeletionPendingError):
        await store.persist_inline(SESSION_A, [_attach("x.png", "image/png", b"x")], thread_id=store._path_resolver.main_thread_id(SESSION_A), message_id="msg_" + create_uuid_hex(namespace="message"))
    assert _original_files(tmp_path) == []
    assert store._catalog.connection.execute("SELECT 1 FROM attachment_directory_associations").fetchall() == []
    store.close()


# ----------------------------------------------------------------------
# 定点恢复 / 引用释放 / GC
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_session_starts_own_deadline_and_keeps_other_session_directory(tmp_path, session_bundle_factory) -> None:
    with closing(_store(tmp_path, session_bundle_factory, SESSION_A, SESSION_B)) as store:
        payload = b"shared"
        refs = []
        for session in (SESSION_A, SESSION_B):
            (ref,) = await store.persist_inline(session, [_attach("input.txt", "text/plain", payload)],
                thread_id=store._path_resolver.main_thread_id(session), message_id="msg_" + create_uuid_hex(namespace="message"))
            refs.append(ref)
        first_id = parse_resource_uri(refs[0].file_id).attachment_id
        assert first_id in store.release_session_references(SESSION_A)
        assert store.read(SESSION_A, refs[0].file_id).data == payload
        assert await store.collect_garbage(leases=ExternalResourceLeaseLedger()) == ()
        current = datetime.now(UTC)
        store._clock = lambda: current + timedelta(days=31)
        assert await store.collect_garbage(leases=ExternalResourceLeaseLedger()) == (first_id,)
        assert store.read(SESSION_B, refs[1].file_id).data == payload
        assert len(_original_files(tmp_path)) == 1


@pytest.mark.asyncio
async def test_collect_garbage_marks_deleting_before_physical_removal_then_tombstones(tmp_path, session_bundle_factory, monkeypatch) -> None:
    with closing(_store(tmp_path, session_bundle_factory, SESSION_A)) as store:
        (ref,) = await store.persist_inline(SESSION_A, [_attach("input.txt", "text/plain", b"garbage")],
            thread_id=store._path_resolver.main_thread_id(SESSION_A), message_id="msg_gc")
        attachment_id = parse_resource_uri(ref.file_id).attachment_id
        store.release_session_references(SESSION_A)
        current = datetime.now(UTC)
        store._clock = lambda: current + timedelta(days=31)
        remove = attachment_blob_store_module.remove_directory
        seen = []
        def observe(path):
            seen.append(store._catalog.get_directory(attachment_id).availability)
            remove(path)
        monkeypatch.setattr(attachment_blob_store_module, "remove_directory", observe)
        assert await store.collect_garbage(leases=ExternalResourceLeaseLedger()) == (attachment_id,)
        assert seen == ["deleting"] and store._catalog.get_directory(attachment_id).availability == "tombstoned"
        assert _original_files(tmp_path) == []
        with pytest.raises(FileNotFoundError):
            store.read(SESSION_A, ref.file_id)


@pytest.mark.asyncio
async def test_recover_session_cleans_only_staging_owned_by_original_ingest_record(tmp_path, session_bundle_factory) -> None:
    with closing(_store(tmp_path, session_bundle_factory, SESSION_A)) as store:
        staging_root = store._attachments_root / ".staging"
        unknown = staging_root / ("ing_" + "8" * 32)
        unknown.mkdir(parents=True)
        (unknown / "unknown.txt").write_text("无归属文件不得被扫描吸收或删除")
        saver = RolloutCheckpointSaver(tmp_path / ".boxteam/sessions", attachment_store=store)
        assert store.recover_session(SESSION_A, messages=saver) == ()
        now = datetime.now(UTC)
        thread = store._path_resolver.main_thread_id(SESSION_A)
        operation, message = "attkey_owned_staging", "msg_owned_staging"
        digest = "sha256:" + hashlib.sha256(b"data").hexdigest()
        preimage = hashlib.sha256(canonical_json_bytes([SESSION_A, thread, message, operation, digest, 4, "input.txt", "text/plain"])).hexdigest()
        with closing(store._open_control_store(SESSION_A)) as control:
            pin = control.create_or_get_lease(operation_kind="attachment", operation_identity=operation,
                preimage_hash=preimage, expected_generation=1, recovery_ref=f"attachment:{SESSION_A}:{thread}:{message}")
        record = store._catalog.create_ingest(operation_id=operation, ingest_id="ing_" + "9" * 32,
            session_id=SESSION_A, thread_id=thread, message_id=message, pin=pin, digest=digest, length=4,
            file_name="input.txt", mime_type="text/plain", now=now)
        owned = resolve_staging_path(store._attachments_root, record.staging_relative_locator)
        owned.mkdir()
        (owned / "partial.txt").write_bytes(b"partial")
        assert "aborted-ingest" in store.recover_session(SESSION_A, messages=saver)
        assert not owned.exists() and (unknown / "unknown.txt").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["aborted", "target_deleted"])
async def test_pending_fork_claim_retains_only_source_directory_until_original_operation_settles(fork_capture, tmp_path, terminal):
    store, record = fork_capture
    body = b"pending-fork-unaccepted-upload"
    (source,) = await store.persist_inline(SESSION_A, [_attach("source.txt", "text/plain", body)], thread_id=record.source_thread_id, message_id="msg_pending_source")
    (target,) = await store.persist_inline(SESSION_B, [_attach("target.txt", "text/plain", body)], thread_id=record.target_thread_id, message_id="msg_pending_target")
    (snapshot,) = store.capture_fork_attachments(record, same_storage_owner=True, references=((source.file_id, True),))
    assert snapshot.target_action == "reuse_reference" and snapshot.claim_id is not None
    assert snapshot.target_file_id == source.file_id and snapshot.target_file_id != target.file_id
    saver = RolloutCheckpointSaver(tmp_path / ".boxteam/sessions", attachment_store=store)
    assert await store.recover_pending_ingests(messages=saver) == 2
    with closing(store._open_control_store(SESSION_A)) as control:
        assert control.list_non_terminal_leases() == ()
    with closing(store._open_control_store(SESSION_B)) as control:
        assert control.list_non_terminal_leases() == ()
    current = datetime.now(UTC)
    store._clock = lambda: current + timedelta(days=31)
    assert await store.collect_garbage(leases=ExternalResourceLeaseLedger()) == (parse_resource_uri(target.file_id).attachment_id,)
    assert store.read(SESSION_A, source.file_id).data == body
    store._catalog.settle_fork_attachment_claims(copy_operation_id=record.copy_operation_id, claim_ids=(snapshot.claim_id,), state=terminal)
    assert await store.collect_garbage(leases=ExternalResourceLeaseLedger()) == (snapshot.source_attachment_id,)
    with pytest.raises(FileNotFoundError):
        store.read(SESSION_A, source.file_id)


@pytest.mark.asyncio
async def test_target_deletion_cleans_terminal_fork_mapping_without_rewriting_claim(fork_capture):
    """删除不重写终态 claim；冻结 alias 仍用于历史不可用投影。"""
    store, record = fork_capture
    (ref,) = await store.persist_inline(SESSION_A, [_attach("source.txt", "text/plain", b"terminal-fork")],
        thread_id=record.source_thread_id, message_id="msg_terminal_source")
    (snapshot,) = store.capture_fork_attachments(record, same_storage_owner=True, references=((ref.file_id, True),))
    assert snapshot.claim_id is not None
    # 仅此局部清理用例准备旧持久形态，真实 fork 发布/中止由产品入口测试覆盖。
    with store._catalog.write_transaction() as connection:
        connection.execute("UPDATE attachment_directory_fork_claims SET state='aborted' WHERE claim_id=?", (snapshot.claim_id,))
    source_owner = store._catalog.get_directory(snapshot.source_attachment_id)
    claim_before = tuple(store._catalog._connected().execute(
        "SELECT * FROM attachment_directory_fork_claims WHERE claim_id=?", (snapshot.claim_id,)).fetchone())
    store.release_session_references(SESSION_B)
    assert store._catalog.fork_attachment_operations_for_target(SESSION_B) == ()
    assert tuple(store._catalog._connected().execute(
        "SELECT * FROM attachment_directory_fork_claims WHERE claim_id=?", (snapshot.claim_id,)).fetchone()) == claim_before
    assert store._catalog.get_directory(snapshot.source_attachment_id) == source_owner
    assert store._catalog.fork_attachment_snapshot_for_alias(target_session_id=SESSION_B, target_thread_id=record.target_thread_id, source_file_id=ref.file_id) == snapshot
    assert store.project_attachment(SESSION_B, record.target_thread_id, ref.file_id)["availability"] == "unavailable"
    assert store.read(SESSION_A, ref.file_id).data == b"terminal-fork"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage_existing", [False, True], ids=["independent", "unrelated-damaged"])
async def test_cross_domain_fork_import_does_not_merge_unrelated_target_upload(fork_capture, tmp_path, session_bundle_factory, damage_existing):
    source, initial = fork_capture
    body = b"same-content-across-domains"
    (ref,) = await source.persist_inline(SESSION_A, [_attach("source.txt", "text/plain", body)], thread_id=initial.source_thread_id, message_id="msg_import_source")
    target_root = tmp_path / "target"
    with closing(_store(target_root, session_bundle_factory, SESSION_B)) as target:
        thread = target._path_resolver.main_thread_id(SESSION_B)
        (existing,) = await target.persist_inline(SESSION_B, [_attach("target.txt", "text/plain", body)], thread_id=thread, message_id="msg_existing")
        source_domain, target_domain = source.workspace_id, target.workspace_id
        request = SessionForkRequest(idempotency_key="independent-import", mode="context_fork", source_view_id="source-view", source_view_revision=0,
            source_uri=f"boxteam://workspace/{source_domain}/resources/session/{SESSION_A}/threads/{initial.source_thread_id}",
            target_container_uri=f"boxteam://workspace/{target_domain}/resources/workspace_sessions", creation_kind="new_session_main",
            resources=[{"resource_uri": ref.file_id, "action": "copy_snapshot"}])
        record = replace(initial, target_thread_id=thread, request_json=json.dumps({**request.model_dump(mode="json"),
            "target_main_thread_id": thread, "source_storage_domain_id": source_domain, "target_storage_domain_id": target_domain,
            "target_gateway_id": get_current_gateway_id()}))
        (snapshot,) = source.capture_fork_attachments(record, same_storage_owner=False, references=((ref.file_id, True),))
        path = _original_path(target, existing.file_id)
        if damage_existing:
            path.chmod(0o600)
            path.write_bytes(b"x" * len(body))
        before, metadata = path.read_bytes(), path.stat()
        shutil.copytree(capture_directory(source._sessions_root, record), capture_directory(target._sessions_root, record))
        await target.accept_fork_import(record, (snapshot,))
        await target.reserve_copy_attachments(record, None, (snapshot,))
        await target.finalize_copy_attachments(record, publication_preimage_hash=record.preimage_hash)
        projected = target.project_attachment(SESSION_B, thread, ref.file_id)
        assert projected["availability"] == "available" and projected["file_id"] != existing.file_id
        assert target.read(SESSION_B, projected["file_id"]).data == body
        after = path.stat()
        assert path.read_bytes() == before
        assert (after.st_ino, after.st_mtime_ns, after.st_ctime_ns) == (metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns)
        assert len(_original_files(target_root)) == 2


@pytest.mark.asyncio
async def test_model_attachment_capability_normalizes_only_current_gateway(attachment_tool_owner):
    case = attachment_tool_owner
    gateway = "gateway_332f92ee6191880e797569a47e22922b"
    parsed = parse_resource_uri(case.ref.file_id)
    read = {"thread_id": case.owner.thread_id, "tool_call_id": case.call_id, "model_call_id": case.model_call_id}
    token = set_current_gateway_id(gateway)
    try:
        qualified = resource_display_uri(scope="workspace", scope_id=parsed.scope_id, kind="attachments", tail_segments=parsed.tail_segments, gateway_authority=gateway)
        assert case.saver.read_tool_call_attachment(SESSION_A, file_id=qualified, **read).data == b"first\noriginal attachment\n"
        for foreign in (
            qualified.replace(gateway, "gateway_ce27c875d842690955440795c9de8a35"),
            qualified.replace(parsed.scope_id, "019d1b4a-1111-7000-8000-000000000001"),
        ):
            with pytest.raises(PermissionError, match="原始 assembly"):
                case.saver.read_tool_call_attachment(SESSION_A, file_id=foreign, **read)
    finally:
        reset_current_gateway_id(token)


@pytest.fixture
async def fork_import_target(fork_capture, tmp_path, session_bundle_factory):
    """局部导入/清理测试用真实上传与冻结资源决策，不伪造 claim 行。"""
    source, initial = fork_capture
    body = b"fork-import-staging-lifecycle"
    (ref,) = await source.persist_inline(SESSION_A, [_attach("source.txt", "text/plain", body)],
        thread_id=initial.source_thread_id, message_id="msg_import_staging")
    root = tmp_path / "import-target"
    root.mkdir()
    target = _store(root, session_bundle_factory, SESSION_B)
    thread = target._path_resolver.main_thread_id(SESSION_B)
    source_domain = source._path_resolver.catalog_store.get_node(SESSION_A).workspace_id
    target_domain = target._path_resolver.catalog_store.get_node(SESSION_B).workspace_id
    request = SessionForkRequest(
        idempotency_key="import-staging", mode="context_fork", source_view_id="source-view", source_view_revision=0,
        source_uri=f"boxteam://workspace/{source_domain}/resources/session/{SESSION_A}/threads/{initial.source_thread_id}",
        target_container_uri=f"boxteam://workspace/{target_domain}/resources/workspace_sessions",
        creation_kind="new_session_main", resources=[{"resource_uri": ref.file_id, "action": "copy_snapshot"}],
    )
    record = replace(initial, target_thread_id=thread, request_json=json.dumps({
        **request.model_dump(mode="json"), "target_main_thread_id": thread,
        "source_storage_domain_id": source_domain, "target_storage_domain_id": target_domain,
        "target_gateway_id": get_current_gateway_id(),
    }))
    snapshots = source.capture_fork_attachments(record, same_storage_owner=False, references=((ref.file_id, True),))
    shutil.copytree(capture_directory(source._sessions_root, record), capture_directory(target._sessions_root, record))
    try:
        yield SimpleNamespace(source=source, target=target, record=record, snapshots=snapshots, request=request, body=body)
    finally:
        target.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["prepared", "aborted", "target_deleted", "committed"])
async def test_fork_staging_gc_uses_terminal_claim_and_exact_operation(fork_import_target, terminal):
    case = fork_import_target
    target, record = case.target, case.record
    (snapshot,) = case.snapshots
    # 已存在目标正文走真实 import 复用，collector 只验证终态残留文件，不代替完整 fork。
    await target.persist_inline(SESSION_B, [_attach("target.txt", "text/plain", case.body)],
        thread_id=record.target_thread_id, message_id="msg_existing_staging")
    await target.accept_fork_import(record, case.snapshots)
    staging_id = _import_id(record.copy_operation_id, snapshot.target_attachment_id)
    staging = resolve_staging_path(target._attachments_root, ingest_staging_relative_locator(staging_id))
    other = resolve_staging_path(target._attachments_root, ingest_staging_relative_locator(
        _import_id("other-operation", snapshot.target_attachment_id)))
    staging.mkdir(parents=True)
    (staging / "partial.txt").write_bytes(b"incomplete")
    other.mkdir()
    (other / "partial.txt").write_bytes(b"other-operation-writer")
    if terminal == "committed":
        target._catalog.reserve_fork_attachment_owners(copy_operation_id=record.copy_operation_id, claim_ids=(snapshot.claim_id,))
    if terminal != "prepared":
        target._catalog.settle_fork_attachment_claims(copy_operation_id=record.copy_operation_id,
            claim_ids=(snapshot.claim_id,), state=terminal)
    # 删除 owner 可能已持 Session gate；清理不得阻塞等待 importer。
    async with target._fork_imports.lock_for(record.copy_operation_id), target._gate.exclusive(SESSION_B):
        assert await asyncio.wait_for(target.collect_fork_import_staging(), timeout=1) == ()
        assert (staging / "partial.txt").read_bytes() == b"incomplete"
    if terminal == "prepared":
        assert await target.collect_fork_import_staging() == ()
        assert staging.exists()
    else:
        assert await target.collect_fork_import_staging() == (snapshot.target_attachment_id,)
        assert not staging.exists()
        assert await target.collect_fork_import_staging() == ()
    assert (other / "partial.txt").read_bytes() == b"other-operation-writer"
    assert case.source.read(SESSION_A, snapshot.source_file_id).data == case.body


@pytest.mark.asyncio
async def test_terminal_fork_staging_removal_failure_retries_without_scanning(fork_import_target, monkeypatch):
    case = fork_import_target
    target, record = case.target, case.record
    (snapshot,) = case.snapshots
    target._catalog.prepare_fork_attachment_claims(copy_operation_id=record.copy_operation_id,
        source_snapshot_id=record.snapshot_id, operation_preimage_hash=record.preimage_hash,
        snapshots=case.snapshots)
    target._catalog.settle_fork_attachment_claims(copy_operation_id=record.copy_operation_id,
        claim_ids=(snapshot.claim_id,), state="aborted")
    staging_id = _import_id(record.copy_operation_id, snapshot.target_attachment_id)
    path = resolve_staging_path(target._attachments_root, ingest_staging_relative_locator(staging_id))
    path.mkdir(parents=True)
    (path / "partial.txt").write_bytes(b"partial")
    remove = fork_import_module.remove_directory
    def interrupted(candidate):
        if candidate == path:
            raise OSError("terminal staging deletion interrupted")
        return remove(candidate)
    with monkeypatch.context() as patch:
        patch.setattr(fork_import_module, "remove_directory", interrupted)
        with pytest.raises(OSError, match="deletion interrupted"):
            await target.collect_garbage(leases=ExternalResourceLeaseLedger())
    assert path.exists()
    await target.collect_garbage(leases=ExternalResourceLeaseLedger())
    assert not path.exists()
    assert json.dumps([asdict(snapshot) for snapshot in target._catalog.terminal_fork_attachment_snapshots(record.copy_operation_id)], sort_keys=True) == json.dumps([asdict(snapshot) for snapshot in case.snapshots], sort_keys=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_fork_import_holds_writer_gate_until_frozen_copy_exits(fork_import_target, monkeypatch, cancelled):
    case = fork_import_target
    target, record = case.target, case.record
    (snapshot,) = case.snapshots
    copying, release = Event(), Event()
    copy = fork_import_module.copy_directory_files
    def copy_frozen(source, destination, files, directories):
        destination.mkdir(parents=True)
        partial = destination / "partial.txt"
        partial.write_bytes(b"partial")
        copying.set()
        assert release.wait(timeout=10)
        if not cancelled:
            raise OSError("frozen copy interrupted after settlement")
        partial.unlink()
        return copy(source, destination, files, directories)
    monkeypatch.setattr(fork_import_module, "copy_directory_files", copy_frozen)
    importing = asyncio.create_task(target.accept_fork_import(record, case.snapshots))
    path = resolve_staging_path(target._attachments_root, ingest_staging_relative_locator(_import_id(record.copy_operation_id, snapshot.target_attachment_id)))
    try:
        assert await asyncio.to_thread(copying.wait, 3) and path.is_dir()
        target._catalog.settle_fork_attachment_claims(copy_operation_id=record.copy_operation_id, claim_ids=(snapshot.claim_id,), state="aborted")
        if cancelled:
            importing.cancel()
            await asyncio.sleep(0)
            assert not importing.done()
        assert await asyncio.wait_for(target.collect_fork_import_staging(), timeout=1) == ()
        assert path.is_dir()
        async with target._fork_imports.lock_for(record.copy_operation_id, wait=False) as holder:
            assert not holder.acquired
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError if cancelled else OSError, match=None if cancelled else "frozen copy interrupted"):
            await importing
    assert not path.exists()
    assert target._catalog.get_directory(snapshot.target_attachment_id) is None
    destination = resolve_attachment_directory(target._attachments_root, attachment_directory_relative_locator(snapshot.target_attachment_id, datetime.fromisoformat(snapshot.target_created_at)))
    assert not destination.exists()
    assert await target.collect_fork_import_staging() == ()


@pytest.mark.asyncio
async def test_fork_import_uses_frozen_file_after_live_source_changes(fork_import_target):
    case = fork_import_target
    (snapshot,) = case.snapshots
    live = _original_path(case.source, snapshot.source_file_id)
    live.chmod(0o600)
    live.write_bytes(b"changed live source")
    await case.target.accept_fork_import(case.record, case.snapshots)
    await case.target.reserve_copy_attachments(case.record, None, case.snapshots)
    await case.target.finalize_copy_attachments(case.record, publication_preimage_hash=case.record.preimage_hash)
    assert case.target.read(SESSION_B, snapshot.target_file_id).data == case.body
    frozen = resolve_captured_directory(
        capture_directory(case.target._sessions_root, case.record) / case.record.source_session_id,
        snapshot.source_attachment_id,
    ) / "original/content"
    assert frozen.read_bytes() == case.body


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "changed", "symlink"])
async def test_fork_import_rejects_damaged_frozen_file(fork_import_target, damage):
    case = fork_import_target
    (snapshot,) = case.snapshots
    frozen = resolve_captured_directory(
        capture_directory(case.target._sessions_root, case.record) / case.record.source_session_id,
        snapshot.source_attachment_id,
    ) / "original/content"
    if damage == "changed":
        frozen.write_bytes(b"corrupt frozen file")
    else:
        frozen.unlink()
        if damage == "symlink":
            frozen.symlink_to(_original_path(case.source, snapshot.source_file_id))
    with pytest.raises((OSError, RuntimeError)):
        await case.target.accept_fork_import(case.record, case.snapshots)
    assert case.target._catalog.get_directory(snapshot.target_attachment_id) is None
    assert _original_files(case.target._workspace_root) == []
