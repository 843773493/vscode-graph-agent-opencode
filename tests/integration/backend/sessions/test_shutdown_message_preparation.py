"""关闭等待真实发送入口的准备与持久提交，不提前释放其依赖。"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import threading
from contextlib import closing

import httpx
import pytest

from app import backend_lifecycle, main
from app.core.sqlite_state import sqlite_file_uri
from app.domain.itemized.execution_admission import UserExecutionIdentity
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.message import MessageDTO
from app.services.business.job.service import JobService
from app.testing.model_stream import transport as model_transport
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    MountedCase,
    _plan_model_call,
    assert_closed,
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    mounted_case as mounted_case,  # noqa: PLC0414 - 工作区按当前测试文件隔离。
)
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS


@pytest.mark.parametrize("stage", ["message", "acceptance", "job"])
@pytest.mark.parametrize("observation", ["timeout", "cancel"])
async def test_shutdown_preserves_preparation_until_original_message_commit_finishes(
    mounted_case: MountedCase, monkeypatch: pytest.MonkeyPatch, stage: str, observation: str,
    inprocess_model_replay,
) -> None:
    entered, release_async = asyncio.Event(), asyncio.Event()
    release_sync = threading.Event()
    loop = asyncio.get_running_loop()
    committed: list[tuple[MessageDTO, UserExecutionIdentity]] = []
    body = b"shutdown must retain the original attachment and accepted input\n"
    async with main.lifespan(main.app) as _main_lifespan_state_44:
        await _main_lifespan_state_44["after_http_startup"]()
        owner, peer = mounted_case.containers
        jobs = owner.job_service
        assert isinstance(jobs, JobService)
        backend = main.app.state.workspace_lifespan
        observation_timeout = backend_lifecycle.timeout
        base = f"/api/v1/workspaces/{owner.workspace_id}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://workspace.test",
            headers=LOCAL_TOKEN_HEADERS,
        ) as client:
            created = await client.post(base + "/sessions", json={"title": "关闭中的原准备窗口"})
            assert created.status_code == 200, created.text
            session_id = created.json()["data"]["session_id"]
            thread = await client.get(base + f"/sessions/{session_id}/main-thread")
            assert thread.status_code == 200, thread.text
            thread_id = thread.json()["data"]["thread_id"]
            storage = owner.checkpointer._storage.storage
            index_path = storage.index_path(session_id, thread_id=thread_id)
            jsonl_path = storage.jsonl_path(session_id, thread_id=thread_id)
            create_message = owner.message_service.create
            accept = owner.message_service.accept_user_message
            accept_job = owner.message_service.accept_job_input
            if stage == "job":
                replay, _ = inprocess_model_replay
                _plan_model_call(replay, owner, session_id, thread_id)

                async def hold_response(_operation_id, _phase, **_kwargs):
                    # 若迟到Job已进入模型，停在同一回放端口，仍由产品关闭取消。
                    await asyncio.Event().wait()

                monkeypatch.setattr(model_transport, "observe_phase", hold_response)

            async def prepare(*args, **kwargs):
                if stage == "message":
                    entered.set()
                    await release_async.wait()
                return await create_message(*args, **kwargs)

            def commit(message, *, execution_identity, queue_input):
                if stage == "acceptance":
                    loop.call_soon_threadsafe(entered.set)
                    if not release_sync.wait(10):
                        raise TimeoutError("原消息提交屏障未释放")
                accept(message, execution_identity=execution_identity, queue_input=queue_input)
                committed.append((message, execution_identity))

            def commit_job(job, *, queue_input):
                if stage == "job":
                    loop.call_soon_threadsafe(entered.set)
                    if not release_sync.wait(10):
                        raise TimeoutError("原Job登记屏障未释放")
                return accept_job(job, queue_input=queue_input)

            monkeypatch.setattr(owner.message_service, "create", prepare)
            monkeypatch.setattr(owner.message_service, "accept_user_message", commit)
            monkeypatch.setattr(owner.message_service, "accept_job_input", commit_job)
            request = asyncio.create_task(client.post(base + f"/sessions/{session_id}/messages", json={
                "message": {"content": "关闭不能抢先关闭本次提交的存储", "attachments": [{
                    "file_id": "inline:shutdown", "name": "shutdown.txt", "content_type": "text/plain",
                    "data_url": "data:text/plain;base64," + base64.b64encode(body).decode(),
                }]}, "run": {},
            }))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                assert jobs._session_preparations == {session_id: 2 if stage == "job" else 1}
                assert jobs._jobs == {}
                # 只缩短本次观察预算，不推进全局时钟或取消原关闭执行。
                monkeypatch.setattr(backend_lifecycle, "timeout", lambda seconds: asyncio.timeout(0.1))
                closing_waiter = asyncio.create_task(backend.close())
                if observation == "timeout":
                    with pytest.raises(TimeoutError, match="Backend关闭等待超过30秒"):
                        await asyncio.wait_for(asyncio.shield(closing_waiter), 5)
                else:
                    async with asyncio.timeout(5):
                        while not peer.resource_platform.process_root_scope.is_closed:
                            await asyncio.sleep(0)
                    closing_waiter.cancel("取消关闭观察")
                    with pytest.raises(asyncio.CancelledError, match="取消关闭观察"):
                        await closing_waiter
                close_owner = backend._close_owner
                assert close_owner is not None and not close_owner.done()
                assert main.app.state.workspace_lifespan is backend
                assert not jobs.accepting_jobs
                assert not owner.resource_platform.process_root_scope.is_closed
                assert owner.session_catalog.resolver.catalog_store.connection is not None
                assert owner.attachment_blob_store._catalog.connection is not None
                assert not request.done()
                assert jobs._session_preparations == {session_id: 2 if stage == "job" else 1}
                rejected = await client.post(base + f"/sessions/{session_id}/messages", json={
                    "message": {"content": "关闭后拒绝的新输入"}, "run": {},
                })
                assert rejected.status_code == 503, rejected.text
                assert rejected.headers["X-Request-ID"] == rejected.json()["request_id"]
            finally:
                monkeypatch.setattr(backend_lifecycle, "timeout", observation_timeout)
                release_async.set()
                release_sync.set()
                result, = await asyncio.wait_for(asyncio.gather(request, return_exceptions=True), 5)
                await asyncio.wait_for(backend.close(), 5)
            assert isinstance(result, httpx.Response)
            assert result.status_code == (200 if stage == "job" else 503), result.text
            assert result.headers["X-Request-ID"] == result.json()["request_id"]
            assert backend._close_owner is close_owner and close_owner.done()
            assert jobs._session_preparations == {}
            if stage == "job":
                job, = jobs._jobs.values()
                assert job.status == JobStatus.cancelled
                assert job.job_id == result.json()["data"]["job_id"]
                assert not jobs._finalization_tasks
            else:
                assert jobs._jobs == {}
    assert_closed(mounted_case)
    message, identity = committed.pop()
    assert committed == [] and message.thread_id == thread_id
    attachment, = message.attachments
    assert attachment.directory_uri is not None and attachment.file_id.startswith("boxteam://")
    with closing(sqlite3.connect(sqlite_file_uri(index_path) + "?mode=ro", uri=True)) as connection:
        job_id, phase, input_json = connection.execute("SELECT job_id,phase,input_json FROM execution_queue").fetchone()
    assert job_id == identity.job_id and phase == ("terminal" if stage == "job" else "queued")
    frozen = json.loads(input_json)
    assert frozen["message_id"] == message.message_id
    history = jsonl_path.read_text(encoding="utf-8")
    assert message.message_id in history and message.content in history and attachment.file_id in history
    assert [path.read_bytes() for path in owner.workspace_root.glob(".boxteam/attachments/*/*/*/*/original/*")] == [body]
