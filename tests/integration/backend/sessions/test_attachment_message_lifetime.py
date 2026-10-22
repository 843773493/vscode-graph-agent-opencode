"""消息准备窗口内，真实附件发布和 canonical acceptance 必须先于线程删除。"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import messages as messages_api
from app.api.deps import get_session_orchestrator, verify_local_token
from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import JobEventBus
from app.core.resource_uri import resource_display_uri
from app.core.session_thread_context import session_thread_owner_scope
from app.core.trace_middleware import (
    TraceMiddleware,
    get_current_gateway_id,
    reset_current_gateway_id,
    set_current_gateway_id,
)
from app.core.workspace_identity import read_workspace_id
from app.domain.itemized.execution_admission import UserExecutionIdentity
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.runtime.session_orchestrator import SessionOrchestrator
from app.schemas.internal_v2.job import JobDispatchSnapshotDTO
from app.schemas.internal_v2.message import (
    AttachmentRef,
    MessageCreateRequest,
    MessageRunRequest,
    RunOptions,
)
from app.services.business.message_service import MessageService
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)

BODY = b"attachment survives until canonical acceptance"


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
        messages.accept_job_input(SimpleNamespace(
            session_id=_session_id, message=_content, job_id=identity.job_id,
            **kwargs,
        ))
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
@pytest.mark.parametrize("boundary", ["staging", "hashed", "renamed"])
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
    method = "_write_staging" if boundary == "staging" else "_commit_staged_blob"
    original = getattr(case.store, method)

    def fail_once(*args, **kwargs):
        if boundary != "hashed":
            original(*args, **kwargs)
        raise OSError("注入上传失败")

    transport = ASGITransport(app=message_lifetime_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://workspace") as client:
        path = f"/api/v1/sessions/{session_id}/threads/{thread_id}/messages"
        with monkeypatch.context() as patch:
            patch.setattr(case.store, method, fail_once)
            failed = await client.post(path, json=request)
        assert failed.status_code == 500, failed.text
        assert case.started_jobs == []
        assert case.saver.read_canonical_items(session_id) == ()
        (old,) = case.store._catalog.connection.execute(
            "SELECT * FROM attachment_ingest_records WHERE owner_session_id=?", (session_id,),
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
            "SELECT * FROM attachment_ingest_records WHERE ingest_idempotency_key=?",
            (old["ingest_idempotency_key"],),
        ).fetchone()) == old
        records = case.store._catalog.connection.execute(
            "SELECT state FROM attachment_ingest_records WHERE owner_session_id=?", (session_id,),
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

    def paused_accept(message, *, execution_identity):
        if pause_at == "acceptance":
            paused.set()
            assert release.wait(5), "测试未释放真实 acceptance worker"
        accept(message, execution_identity=execution_identity)
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
        (ref,) = case.store._catalog.list_owner_refs_for_session(session_id, active_only=True)
        file_id = case.store.reference(session_id, ref.attachment_id)
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
        assert case.store._catalog.list_owner_refs_for_session(session_id, active_only=True) == ()
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
        assert frozen.response_metadata["attachments"] == [expected.model_dump(exclude={"data_url"})]
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
