"""消息准备窗口内，真实附件发布和 canonical acceptance 必须先于线程删除。"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langgraph.graph import END, START, MessagesState, StateGraph
from PIL import Image

from app.agents.providers.wire_finalize import finalize_provider_wire
from app.api import messages as messages_api
from app.api.deps import get_session_orchestrator, verify_local_token
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_prefixed_id, create_uuid_hex
from app.core.job_event_bus import JobEventBus
from app.core.phase_observer import observe_phase_sync
from app.core.resource_uri import resource_display_uri
from app.core.session_thread_context import session_thread_owner_scope
from app.core.trace_middleware import (
    TraceMiddleware,
    get_current_gateway_id,
    reset_current_gateway_id,
    set_current_gateway_id,
)
from app.core.workspace_identity import read_workspace_id
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.execution_admission import (
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.domain.itemized.hashing import canonical_json_bytes, content_hash, sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import RuntimeNoticePayload
from app.domain.itemized.serialization import normalize_wire_request
from app.prompting import PromptSection, internal_message_factory
from app.runtime.session_orchestrator import SessionOrchestrator
from app.schemas.internal_v2.job import JobDispatchSnapshotDTO
from app.schemas.internal_v2.message import (
    AttachmentRef,
    MessageCreateRequest,
    MessageRunRequest,
    RunOptions,
)
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.business.message_service import MessageService
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader
from app.services.mapping.itemized.canonical_langchain import validate_summary_source
from app.services.mapping.itemized.provider_request import project_user_message_content

BODY = b"attachment survives until canonical acceptance"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["generated_session_result", "session_message"])
@pytest.mark.parametrize("media_type", ["text/plain", "image/png"])
@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
async def test_internal_attachment_admission_cold_read_and_provider_request(
    message_lifetime_case, integration_workspace_root_path, frozen_provider_wire,
    frozen_provider_activation, monkeypatch, kind, media_type, target_format,
):
    """经真实消息准备和 Job 准入生成 notice，再冷读、重试及封存请求。"""
    case = message_lifetime_case
    owner = case.owner
    private = "内部私有控制与附件处理正文"
    prepared = internal_message_factory.build(
        kind=kind, control="处理附件并保留原始请求。",
        sections=(PromptSection("control_context", {"source_session_id": owner.session_id}),
                  PromptSection(kind, private)),
        **({"display_content": "生成分支已结束，正在处理结果。"} if kind == "generated_session_result" else {}),
    )
    body = BODY
    if media_type == "image/png":
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), color=(240, 30, 20)).save(buffer, format="PNG")
        body = buffer.getvalue()
    message = await case.messages.create(owner.session_id, MessageCreateRequest(
        content=prepared.content, metadata=prepared.metadata,
        attachments=[AttachmentRef(file_id="inline:internal", name="input", content_type=media_type,
            data_url=f"data:{media_type};base64," + base64.b64encode(body).decode())],
    ), thread_id=owner.thread_id)
    intent = InternalExecutionIntent(
        message_id=message.message_id, execution_id=create_uuid_hex(namespace="execution"),
        admission_idempotency_key=create_prefixed_id("adm"),
    )
    job_id = create_prefixed_id("job")
    job = SimpleNamespace(
        session_id=owner.session_id, thread_id=owner.thread_id, job_id=job_id,
        execution_binding=intent, message_id=message.message_id, message=message.content,
        attachments=message.attachments, message_metadata=message.metadata,
        message_created_at=message.created_at.isoformat(),
    )
    case.messages.accept_job_input(job, queue_input=ExecutionQueueInput(execution_binding=job.execution_binding, message_id=job.message_id, agent_id="default", provider_id=None, delivery_policy="after_turn", gateway_id=None, request_id=None, created_at=job.message_created_at))
    (notice,) = case.saver.read_canonical_items(owner.session_id, owner.thread_id)
    payload = RuntimeNoticePayload.from_dict(notice.payload)
    assert payload.prompt == prepared.content
    assert private not in canonical_json_bytes(payload.attachment_blocks).decode()
    assert notice.turn_id is None and notice.turn_scope == "pending_next_turn"
    assert notice.content_hash == content_hash("structured_content", notice.payload)
    assert payload.attachments == tuple(a.model_dump(exclude_none=True) for a in message.attachments)
    with case.saver._storage.storage.read_connection(owner.session_id, owner.thread_id) as connection:
        assert connection.execute("SELECT item_id,reference_ordinal,resource_uri FROM item_resource_references").fetchall() == [
            (notice.item_id, 0, message.attachments[0].file_id),
        ]
        assert connection.execute("SELECT count(*) FROM turns").fetchone() == (0,)
    frozen = case.saver.accepted_internal_message(
        owner.session_id, thread_id=owner.thread_id, job_id=job_id,
        message_id=message.message_id, intent=intent,
    )
    assert frozen.content == prepared.content
    assert frozen.response_metadata["attachments"] == list(payload.attachments)
    assert frozen.response_metadata["attachment_blocks"] == list(payload.attachment_blocks)
    validate_summary_source(frozen, (notice,))
    visible = case.messages._message_to_dto(owner.session_id, owner.thread_id, 0, frozen)
    assert private not in visible.model_dump_json()
    assert visible.content == ("生成分支已结束，正在处理结果。" if kind == "generated_session_result" else "")
    assert visible.attachments == message.attachments
    # 重入只消费冻结的 canonical 内容，不重新读取附件或生成媒体预览。
    def refuse_rebuild(*_args, **_kwargs):
        raise AssertionError("已接受的内部消息不得重新生成附件内容")
    with monkeypatch.context() as patch:
        patch.setattr("app.services.business.message_service.UserContentBuilder.build", refuse_rebuild)
        queued_input = SimpleNamespace(**{**vars(job), "message_metadata": frozen.response_metadata})
        case.messages.accept_job_input(queued_input, queue_input=ExecutionQueueInput(execution_binding=queued_input.execution_binding, message_id=queued_input.message_id, agent_id="default", provider_id=None, delivery_policy="after_turn", gateway_id=None, request_id=None, created_at=queued_input.message_created_at))
        with pytest.raises(ValueError, match="附件.*不一致"):
            queued_input = SimpleNamespace(**{
                **vars(job), "message_metadata": frozen.response_metadata, "attachments": [],
            })
            case.messages.accept_job_input(queued_input, queue_input=ExecutionQueueInput(execution_binding=queued_input.execution_binding, message_id=queued_input.message_id, agent_id="default", provider_id=None, delivery_policy="after_turn", gateway_id=None, request_id=None, created_at=queued_input.message_created_at))
        with pytest.raises(ValueError):
            queued_input = SimpleNamespace(**{
                **vars(job), "message_metadata": frozen.response_metadata, "message": prepared.content + "漂移",
            })
            case.messages.accept_job_input(queued_input, queue_input=ExecutionQueueInput(execution_binding=queued_input.execution_binding, message_id=queued_input.message_id, agent_id="default", provider_id=None, delivery_policy="after_turn", gateway_id=None, request_id=None, created_at=queued_input.message_created_at))
    assert case.saver.read_canonical_items(owner.session_id, owner.thread_id) == (notice,)
    with RolloutCheckpointSaver(
        Path(integration_workspace_root_path) / ".boxteam" / "sessions", attachment_store=case.store,
    ) as cold:
        assert cold.accepted_internal_message(
            owner.session_id, thread_id=owner.thread_id, job_id=job_id,
            message_id=message.message_id, intent=intent,
        ) == frozen
        admission = cold.execution_admission_for_job(owner.session_id, thread_id=owner.thread_id, job_id=job_id)
        manager = ContextSourceManager(owner=ContextSourceOwnerKey(owner.session_id, owner.thread_id), control_state_port=cold)
        key = f"internal-attachment:{target_format}"
        with (session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
              execution_admission_scope(admission, langgraph_thread_id=owner.thread_id)):
            # 使用真实 LangGraph checkpoint 协议消费已准入输入；这里不执行模型。
            graph = StateGraph(MessagesState)
            graph.add_node("ingress", lambda _state: {})
            graph.add_edge(START, "ingress")
            graph.add_edge("ingress", END)
            await graph.compile(checkpointer=cold).ainvoke(
                {"messages": [frozen]}, build_checkpoint_config(owner.thread_id),
            )
            result = cold.prepare_context_for_provider(
                owner.session_id, owner.thread_id, turn_id=None, execution_id=intent.execution_id,
                plan_creation_idempotency_key=key, seal_idempotency_key="seal:" + key,
                context_source_manager=manager,
                tool_set_supplier=lambda: ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
                **frozen_provider_wire("attachment-test", target_format),
                **frozen_provider_activation(cold, plan_creation_idempotency_key=key),
            )
        selected = [entry for entry in result["snapshot"].selection if entry.included and entry.ref.ref_id == notice.item_id]
        assert len(selected) == 1 and selected[0].content_hash == notice.content_hash
        if target_format == "responses":
            wire = result["native_projection"]["request"]["input"]
            candidates = [entry["content"] for entry in wire if any(
                block.get("text") == prepared.content for block in entry.get("content", [])
            )]
            assert len(candidates) == 1, wire
            blocks = candidates[0]
        else:
            candidates = [entry for entry in result["messages"] if isinstance(entry.content, list)
                          and entry.content and entry.content[0].get("text") == prepared.content]
            assert len(candidates) == 1, result["messages"]
            carrier = candidates[0]
            projection = project_user_message_content(carrier.content, target_format=target_format, image_input=True)
            assert projection["diagnostics"] == []
            blocks = projection["content"]
        assert isinstance(blocks, list)
        text_type = "input_text" if target_format == "responses" else "text"
        assert blocks[0] == {"type": text_type, "text": prepared.content}
        assert message.attachments[0].file_id in blocks[1]["text"]
        images = [block for block in blocks if block["type"] in {"image_url", "input_image"}]
        assert len(images) == int(media_type == "image/png")
        if images:
            url = images[0]["image_url"]
            assert (url["url"] if isinstance(url, dict) else url).startswith("data:image/")
        assert cold.read_canonical_items(owner.session_id, owner.thread_id) == (notice,)
        wire = frozen_provider_wire("attachment-test", target_format)
        native = result.get("native_projection")
        assert normalize_wire_request(finalize_provider_wire(
            messages=result["messages"], tools=result["tools"],
            native_request=None if native is None else native["request"],
            profile=wire["wire_profile"], params=wire["wire_params"],
        )) == result["snapshot"].request_hash_preimage
        history = RolloutHistoryReader(cold._context_reader).load(
            owner.session_id, owner.thread_id,
            TurnHistoryLoadRequest(execution_id=intent.execution_id, include=["internal"]),
        )
        assert history.items == []
        assert private not in history.model_dump_json()
        assert "data:image/" not in history.model_dump_json()
        if kind == "generated_session_result":
            assert [part.item_id for execution in history.internal_executions for part in execution.display_parts] == [notice.item_id]
        if media_type == "image/png":
            without_vision = project_user_message_content(
                result["messages"][-1].content, target_format=target_format, image_input=False,
            )
            assert [diagnostic["status"] for diagnostic in without_vision["diagnostics"]] == ["not_sent"]
        point = await cold.forks.source.freeze_display_point(
            source_session_id=owner.session_id, source_owner=owner, mode="full_rollout_copy",
        )
        resources = await cold.forks.source.project_display_resources(
            source_session_id=owner.session_id, source_owner=owner, mode="full_rollout_copy", anchor_mode="inclusive",
            source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
            anchor_item_id=point.anchor_item_id,
            source_uri=f"boxteam://workspace/{read_workspace_id(Path(integration_workspace_root_path))}/resources/session/{owner.session_id}/threads/{owner.thread_id}",
        )
        assert [(resource.resource_uri, resource.availability) for resource in resources] == [
            (message.attachments[0].file_id, "available"),
        ]


@dataclass
class MessageLifetimeCase:
    store: AttachmentBlobStore
    saver: RolloutCheckpointSaver
    messages: MessageService
    orchestrator: SessionOrchestrator
    owner: MutationIntentOwner
    identity: UserExecutionIdentity
    started_jobs: list[str]

    async def delete_thread(self) -> None:
        with session_thread_owner_scope(self.owner, langgraph_thread_id=self.owner.thread_id):
            await self.saver.adelete_thread(self.owner.thread_id)


@pytest.fixture
def message_lifetime_case(integration_workspace_root_path, session_bundle_factory):
    root = Path(integration_workspace_root_path)
    sessions = root / ".boxteam" / "sessions"
    session_id = create_prefixed_id("ses")
    session_bundle_factory(sessions, session_id)
    store = AttachmentBlobStore(root)
    resolver = store._path_resolver
    thread_id = resolver.main_thread_id(session_id)
    identity = UserExecutionIdentity(
        job_id=create_prefixed_id("job"), turn_id=create_prefixed_id("turn"),
        admission_idempotency_key=create_prefixed_id("adm"),
    )
    started_jobs = []

    async def get_session(_session_id):
        return SimpleNamespace(current_agent_id="default", current_provider_id=None)

    async def resolve_main_thread(_session_id):
        return thread_id

    async def run_preparation(_session_id, operation):
        return await operation()

    async def start_job(_session_id, _content, **kwargs):
        queued_input = SimpleNamespace(
            session_id=_session_id, message=_content, job_id=identity.job_id,
            **kwargs,
        )
        messages.accept_job_input(queued_input, queue_input=ExecutionQueueInput(execution_binding=queued_input.execution_binding, message_id=queued_input.message_id, agent_id="default", provider_id=None, delivery_policy="after_turn", gateway_id=None, request_id=None, created_at=queued_input.message_created_at))
        started_jobs.append(kwargs["message_id"])
        return JobDispatchSnapshotDTO(
            session_id=session_id, thread_id=thread_id, job_id=identity.job_id,
            job_status="running", active_job_id=identity.job_id,
            queued_jobs_ahead=0, queued_job_count=0, pending_job_count=1,
        )

    with RolloutCheckpointSaver(sessions, attachment_store=store) as saver:
        messages = MessageService(
            main_thread_resolver=resolver.main_thread_id, checkpointer=saver,
            attachment_store=store, workspace_root=root,
        )
        orchestrator = SessionOrchestrator(
            message_service=messages,
            session_service=SimpleNamespace(
                get=get_session, resolve_main_thread=resolve_main_thread, path_resolver=resolver,
            ),
            config_service=SimpleNamespace(
                resolve_agent_id=lambda agent_id: agent_id,
                validate_agent_id=lambda agent_id: None,
            ),
            job_service=SimpleNamespace(
                run_session_preparation=run_preparation, start_job=start_job,
                reserve_user_execution_identity=lambda: identity,
            ),
            job_event_bus=JobEventBus(), attachment_holder=store.deletion_holder,
        )
        yield MessageLifetimeCase(
            store, saver, messages, orchestrator,
            MutationIntentOwner(session_id=session_id, thread_id=thread_id),
            identity, started_jobs,
        )
    store.close()


@pytest.fixture
def message_lifetime_app(message_lifetime_case):
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(messages_api.router, prefix="/api/v1")
    application.dependency_overrides[verify_local_token] = lambda: "test-token"
    application.dependency_overrides[get_session_orchestrator] = lambda: message_lifetime_case.orchestrator
    return application


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["directory_staged", "directory_renamed", "directory_durable"])
async def test_message_http_retry_accepts_same_attachment_after_failed_upload(
    message_lifetime_case, message_lifetime_app, integration_workspace_root_path, monkeypatch, boundary,
):
    """经真实路由重发原请求；仅 Job 执行由 probe 代替，不代替 canonical 准入。"""
    case = message_lifetime_case
    session_id, thread_id = case.owner.session_id, case.owner.thread_id
    body = BODY + b":" + session_id.encode() + b":" + boundary.encode()
    request = MessageRunRequest(run=RunOptions(), message=MessageCreateRequest(
        content="读取附件", attachments=[AttachmentRef(
            file_id="inline:retry", name="retry.txt", content_type="text/plain",
            data_url="data:text/plain;base64," + base64.b64encode(body).decode(),
        )],
    )).model_dump(mode="json", by_alias=True)
    def fail_sync(operation_id, phase, *, details=None):
        if phase == "attachment.ingest." + boundary:
            raise OSError("注入上传失败")
        observe_phase_sync(operation_id, phase, details=details)

    async def fail_async(operation_id, phase, *, details=None):
        fail_sync(operation_id, phase, details=details)

    transport = ASGITransport(app=message_lifetime_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://workspace") as client:
        path = f"/api/v1/sessions/{session_id}/threads/{thread_id}/messages"
        with monkeypatch.context() as patch:
            patch.setattr("app.services.infrastructure.attachment_blob_catalog.store.observe_phase", fail_async)
            patch.setattr("app.services.infrastructure.attachment_blob_catalog.store.observe_phase_sync", fail_sync)
            failed = await client.post(path, json=request)
        assert failed.status_code == 500, failed.text
        assert case.started_jobs == []
        assert case.saver.read_canonical_items(session_id, case.saver._storage.storage.main_thread_id(session_id)) == ()
        (old,) = case.store._catalog.connection.execute(
            "SELECT * FROM attachment_directory_ingests WHERE owner_session_id=?", (session_id,),
        ).fetchall()
        old = dict(old)
        assert old["state"] == "aborted"
        response = await client.post(path, json=request)
        assert response.status_code == 200, response.text
        envelope = response.json()
        assert envelope["request_id"] == response.headers["x-request-id"]
        accepted = envelope["data"]
        assert accepted["dispatch"]["session_id"] == session_id
        assert accepted["thread_id"] == thread_id
        assert case.started_jobs == [accepted["message_id"]]
        assert dict(case.store._catalog.connection.execute(
            "SELECT * FROM attachment_directory_ingests WHERE ingest_idempotency_key=?",
            (old["ingest_idempotency_key"],),
        ).fetchone()) == old
        records = case.store._catalog.connection.execute(
            "SELECT state FROM attachment_directory_ingests WHERE owner_session_id=?", (session_id,),
        ).fetchall()
        assert sorted(row["state"] for row in records) == ["aborted", "published"]
        frozen = case.saver.accepted_user_message(
            session_id, thread_id=thread_id,
            job_id=accepted["job_id"], message_id=accepted["message_id"],
        )
        (attachment,) = frozen.response_metadata["attachments"]
        assert "data_url" not in attachment
        assert case.store.read(session_id, attachment["file_id"], owner_thread_id=thread_id).data == body
        with RolloutCheckpointSaver(
            Path(integration_workspace_root_path) / ".boxteam" / "sessions", attachment_store=case.store,
        ) as cold:
            assert cold.accepted_user_message(
                session_id, thread_id=thread_id,
                job_id=accepted["job_id"], message_id=accepted["message_id"],
            ) == frozen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "pause_at", "cancel"),
    [
        ("create", "after_upload", False),
        ("create", "acceptance", False),
        ("create", "acceptance", True),
        ("prepared", "acceptance", False),
        ("prepared", "acceptance", True),
    ],
)
async def test_thread_delete_drains_message_before_releasing_real_attachment(
    message_lifetime_case, monkeypatch, entry, pause_at, cancel,
):
    case = message_lifetime_case
    session_id, thread_id = case.owner.session_id, case.owner.thread_id
    prepared = None
    attachment = AttachmentRef(
        file_id="inline:message-race", name="race.txt", content_type="text/plain",
        data_url="data:text/plain;base64," + base64.b64encode(BODY).decode(),
    )
    if entry == "prepared":
        prepared = await case.messages.create(
            session_id, MessageCreateRequest(role="user", content="读取附件", attachments=[attachment]),
            thread_id=thread_id,
        )

    paused = Event()
    release = Event()
    accepted = []
    create = case.messages.create
    accept = case.messages.accept_user_message

    async def paused_create(*args, **kwargs):
        message = await create(*args, **kwargs)
        paused.set()
        await asyncio.to_thread(release.wait, 5)
        return message

    def paused_accept(message, *, execution_identity, queue_input):
        if pause_at == "acceptance":
            paused.set()
            assert release.wait(5), "测试未释放真实 acceptance worker"
        accept(message, execution_identity=execution_identity, queue_input=queue_input)
        canonical = case.saver.accepted_user_message(
            session_id, thread_id=thread_id,
            job_id=execution_identity.job_id, message_id=message.message_id,
        )
        data = case.store.read(session_id, message.attachments[0].file_id).data
        accepted.append((message, canonical, data))

    if pause_at == "after_upload":
        monkeypatch.setattr(case.messages, "create", paused_create)
    monkeypatch.setattr(case.messages, "accept_user_message", paused_accept)
    if prepared is None:
        operation = case.orchestrator.create_message(
            session_id,
            MessageRunRequest(run=RunOptions(), message=MessageCreateRequest(
                role="user", content="读取附件", attachments=[attachment],
            )),
        )
    else:
        operation = case.orchestrator.dispatch_prepared_message(
            session_id, prepared, requested_agent_id="default", execution_binding=case.identity,
        )
    dispatch = asyncio.create_task(operation)
    deletion = None
    try:
        assert await asyncio.to_thread(paused.wait, 3)
        ((attachment_id,),) = case.store._catalog.connection.execute(
            "SELECT attachment_id FROM attachment_directory_associations WHERE session_id=? AND state='active'", (session_id,),
        ).fetchall()
        file_id = case.store.reference(attachment_id)
        assert case.store.read(session_id, file_id).data == BODY
        deletion = asyncio.create_task(case.delete_thread())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(deletion), timeout=0.15)
        if cancel:
            dispatch.cancel()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(dispatch), timeout=0.15)
            assert not deletion.done(), "取消不能放任尚未结束的 acceptance worker 与删除并发"
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await dispatch
        else:
            result = await dispatch
            assert result.thread_id == thread_id
        await deletion
        ((message, canonical, data),) = accepted
        assert canonical.response_metadata["attachments"][0]["file_id"] == file_id
        assert message.attachments[0].data_url is None
        assert file_id in str(canonical.content)
        assert data == BODY
        assert case.started_jobs == ([] if cancel else [message.message_id])
        assert case.store._catalog.connection.execute(
            "SELECT attachment_id FROM attachment_directory_associations WHERE session_id=? AND state='active'", (session_id,),
        ).fetchall() == []
        assert not case.saver._storage.storage.root(session_id, thread_id=thread_id).exists()
    finally:
        release.set()
        await asyncio.gather(dispatch, *([deletion] if deletion is not None else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["relative", "absolute", "uri"])
async def test_native_attachment_ingress_freezes_one_uri_for_dispatch_and_cold_history(
    message_lifetime_case, integration_workspace_root_path, entry,
):
    """真实创建和派发取得统一引用；Job 校验、canonical 和冷读消费同一事实。"""
    case = message_lifetime_case
    root = Path(integration_workspace_root_path)
    asset = root / "assets" / "报告 #1.pdf"
    asset.parent.mkdir(exist_ok=True)
    asset.write_bytes(BODY)
    gateway_id = "gateway_332f92ee6191880e797569a47e22922b"
    token = set_current_gateway_id(gateway_id)
    try:
        expected_uri = resource_display_uri(
            scope="workspace", scope_id=read_workspace_id(root), kind="files",
            tail_segments=("root", "assets", asset.name), gateway_authority=get_current_gateway_id(),
        )
        value = {"relative": f"assets/{asset.name}", "absolute": str(asset), "uri": expected_uri}[entry]
        # 用户正文保留原字节，只有系统生成的资源字段归一。
        text = f"请阅读 {asset}"
        prepared = await case.messages.create(
            case.owner.session_id,
            MessageCreateRequest(content=text, attachments=[AttachmentRef(file_id=value)]),
        )
        expected = AttachmentRef(file_id=expected_uri, name=asset.name, content_type="application/pdf")
        assert prepared.attachments == [expected]
        accepted = await case.orchestrator.dispatch_prepared_message(
            case.owner.session_id, prepared, requested_agent_id="default", execution_binding=case.identity,
        )
        assert case.started_jobs == [prepared.message_id]
        events = await case.orchestrator._job_event_bus.list_events(accepted.job_id)
        event = next(event for event in events if event.type == "message_created")
        event_attachments = event.model_dump(mode="json")["payload"]["attachments"]
        assert event_attachments == [expected.model_dump(exclude={"data_url"})]
        assert str(root) not in json.dumps(event_attachments, ensure_ascii=False)
        frozen = case.saver.accepted_user_message(
            case.owner.session_id, thread_id=case.owner.thread_id,
            job_id=accepted.job_id, message_id=prepared.message_id,
        )
        assert frozen.content[0] == {"type": "text", "text": text}
        generated = frozen.content[1:]
        assert generated[0]["metadata"]["file_id"] == expected_uri
        assert generated[0]["metadata"]["path"] == expected_uri
        assert str(root) not in json.dumps(generated, ensure_ascii=False)
        assert frozen.response_metadata["attachments"] == [expected.model_dump(exclude={"data_url", "directory_uri"})]
        assert str(root) not in json.dumps(frozen.response_metadata, ensure_ascii=False)
        # 新建 Saver，排除 MessageService DTO 或热消息缓存替持久化作证。
        with RolloutCheckpointSaver(root / ".boxteam" / "sessions", attachment_store=case.store) as cold:
            restored = cold.accepted_user_message(
                case.owner.session_id, thread_id=case.owner.thread_id,
                job_id=accepted.job_id, message_id=prepared.message_id,
            )
            assert restored == frozen
    finally:
        reset_current_gateway_id(token)
