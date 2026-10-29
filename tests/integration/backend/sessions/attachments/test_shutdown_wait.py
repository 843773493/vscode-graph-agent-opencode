"""附件连接关闭等待不得阻塞Backend观察、诊断或提前归还维护锁。"""

from __future__ import annotations

import asyncio
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event, Thread

import httpx
import pytest

from app import backend_lifecycle, main
from app.core.path_utils import build_session_catalog_components, workspace_context
from app.core.resource_uri import parse_attachment_uri
from app.schemas.internal_v2.common import JobStatus
from app.services.business.job.service import JobService
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentBlobCatalog,
)
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceBusyError,
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    MountedCase,
    _plan_model_call,
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    mounted_case as mounted_case,  # noqa: PLC0414 - 工作区沿本测试文件隔离。
)
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS


@pytest.mark.parametrize("wait_kind", ["connection", "queue"])
@pytest.mark.parametrize("observation", ["timeout", "cancel"])
async def test_attachment_close_retains_original_owner_until_sync_work_finishes(
    mounted_case: MountedCase, monkeypatch: pytest.MonkeyPatch,
    inprocess_model_replay, wait_kind: str, observation: str,
) -> None:
    token = secrets.token_hex(32)
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", token)
    deadlines: list[asyncio.Timeout] = []

    def capture_deadline(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        deadline = asyncio.timeout(seconds)
        deadlines.append(deadline)
        return deadline

    monkeypatch.setattr(backend_lifecycle, "timeout", capture_deadline)
    lifetime = main.lifespan(main.app)
    await lifetime.__aenter__()
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="attachment-close-queue")
    previous_executor = getattr(loop, "_default_executor", None)
    entered, release, settled = Event(), Event(), Event()
    begin = Event()
    failures: list[BaseException] = []
    attempts: list[AttachmentBlobCatalog] = []
    holder: Thread | None = None
    waiter: asyncio.Task[None] | None = None
    queued = None
    try:
        peer, selected = mounted_case.containers
        jobs = selected.job_service
        assert isinstance(jobs, JobService)
        backend = main.app.state.workspace_lifespan
        selected_lifetime = next(item for item in backend.workspaces if item.container is selected)
        peer_lifetime = next(item for item in backend.workspaces if item.container is peer)
        replay, controller = inprocess_model_replay
        body = b"attachment close must retain the original bytes\n"
        base = f"/api/v1/workspaces/{selected.workspace_id}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://workspace.test",
            headers=LOCAL_TOKEN_HEADERS,
        ) as client:
            created = await client.post(base + "/sessions", json={"title": "附件关闭中的真实输入"})
            assert created.status_code == 200, created.text
            session_id = created.json()["data"]["session_id"]
            thread = await client.get(base + f"/sessions/{session_id}/main-thread")
            assert thread.status_code == 200, thread.text
            thread_id = thread.json()["data"]["thread_id"]
            _plan_model_call(replay, selected, session_id, thread_id)
            sent = await client.post(base + f"/sessions/{session_id}/messages", json={
                "message": {"content": "保存附件", "attachments": [upload(body).model_dump(mode="json")]},
                "run": {},
            })
            assert sent.status_code == 200, sent.text
            job = jobs._jobs[sent.json()["data"]["job_id"]]
            assert job.task is not None
            await asyncio.wait_for(asyncio.shield(job.task), 30)
            await jobs.drain_finalizations()
            assert job.status == JobStatus.completed, job.error_message
            assert controller.transport.call_count == 1
            ref, = job.attachments
            attachment_id = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
            assert attachment_id is not None
            history = selected.checkpointer._storage.storage.jsonl_path(session_id, thread_id=thread_id)
            canonical = history.read_bytes()
            locator = selected.session_catalog.resolver.resolve_session_node(session_id)
            catalog = selected.attachment_blob_store._catalog
            connection = catalog.connection
            original_close = AttachmentBlobCatalog.close

            def hold_sync_work() -> None:
                entered.set()
                if not release.wait(12):
                    raise TimeoutError("测试附件关闭屏障未释放")

            def read_original_connection() -> None:
                try:
                    if not begin.wait(12):
                        raise TimeoutError("测试未进入附件连接关闭")
                    # 在原连接上执行真实SQLite读取；UDF只阻塞读回执，不代填业务状态。
                    connection.execute("SELECT attachment_close_wait() FROM attachment_directories").fetchall()
                except BaseException as error:  # noqa: BLE001 - 把线程原错误交回测试核对。
                    failures.append(error)
                finally:
                    settled.set()

            def observe_close(current: AttachmentBlobCatalog) -> None:
                if current is catalog:
                    attempts.append(current)
                    if wait_kind == "connection":
                        begin.set()
                        if not entered.wait(3):
                            raise TimeoutError("测试原SQLite读取未开始")
                original_close(current)

            monkeypatch.setattr(AttachmentBlobCatalog, "close", observe_close)
            if wait_kind == "connection":
                connection.create_function("attachment_close_wait", 0, hold_sync_work)
                holder = Thread(target=read_original_connection, name="attachment-shutdown-read")
                holder.start()
            else:
                close_source = selected.workspace_source_owner.close

                async def close_source_then_occupy_worker() -> None:
                    nonlocal queued
                    close_source()
                    await asyncio.wait_for(asyncio.shield(peer_lifetime._owner), 5)
                    loop.set_default_executor(executor)
                    queued = executor.submit(hold_sync_work)
                    if not entered.wait(3):
                        raise TimeoutError("测试线程池占用未开始")

                # 前序资源真实关闭后才制造排队，避免阻塞不属于本片的文件关闭步骤。
                monkeypatch.setattr(selected.workspace_source_owner, "close", close_source_then_occupy_worker)
                selected_lifetime._steps = type(selected_lifetime._steps)(
                    (stage, close_source_then_occupy_worker if stage == "workspace_source.close" else action)
                    for stage, action in selected_lifetime._steps
                )

            waiter = asyncio.create_task(backend.close())
            async with asyncio.timeout(5):
                while not entered.is_set() or selected_lifetime.pending_close_stage != "attachments.close":
                    await asyncio.sleep(0.001)
            assert not settled.is_set(), "同步关闭等待不能阻塞事件循环直到测试屏障自行超时"
            await asyncio.wait_for(asyncio.shield(peer_lifetime._owner), 5)
            owner = backend._close_owner
            assert owner is not None and not owner.done() and peer_lifetime.closed
            assert len(deadlines) == 1
            if observation == "timeout":
                deadlines[0].reschedule(loop.time())
                with pytest.raises(TimeoutError, match="stage=workspaces.*attachments.close"):
                    await asyncio.wait_for(waiter, 3)
            else:
                waiter.cancel("仅取消关闭观察")
                with pytest.raises(asyncio.CancelledError, match="仅取消关闭观察"):
                    await asyncio.wait_for(waiter, 3)
            assert backend._close_owner is owner and not owner.done()
            assert main.app.state.workspace_container_registry is backend.registry
            assert main.app.state.workspace_lifespan is backend
            assert locator.is_dir() and history.read_bytes() == canonical
            assert catalog._connections, "原关闭执行完成前保留同一连接"
            assert attempts == ([catalog] if wait_kind == "connection" else [])
            with pytest.raises(RuntimeError, match="已关闭"):
                catalog.acquire_access()
            with pytest.raises(RuntimeError, match="已关闭"):
                catalog.get_directory(attachment_id)
            for container in mounted_case.containers:
                with pytest.raises(AttachmentMaintenanceBusyError), AttachmentMaintenanceLock(
                    container.workspace_root / ".boxteam/attachments", exclusive=True,
                ):
                    pytest.fail("附件原关闭结束前不得归还Backend维护锁")
            response = await asyncio.wait_for(client.get(
                "/api/v1/internal/runtime-logging", headers={"X-Local-Token": token},
            ), 3)
            assert response.status_code == 200, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
            assert not release.is_set() and not settled.is_set() and not failures
            release.set()
            await asyncio.wait_for(backend.close(), 5)
            assert backend._close_owner is owner and backend.closed
            assert attempts == [catalog] and catalog._connections == {}
            assert history.read_bytes() == canonical and locator.is_dir()
            with (
                closing(build_session_catalog_components(
                    selected.workspace_root / ".boxteam/sessions", selected.workspace_id,
                )) as cold_catalog,
                workspace_context(selected.workspace_id, selected.workspace_root, session_catalog=cold_catalog),
                closing(AttachmentBlobStore(selected.workspace_root, path_resolver=cold_catalog.resolver)) as cold,
            ):
                assert cold_catalog.resolver.resolve_session_node(session_id) == locator
                assert cold.read(session_id, ref.file_id).data == body
                record = cold._catalog.get_directory(attachment_id)
                assert record is not None and record.relative_locator
            assert main.app.state.workspace_lifespan is None
            for container in mounted_case.containers:
                with AttachmentMaintenanceLock(container.workspace_root / ".boxteam/attachments", exclusive=True):
                    pass
    finally:
        release.set()
        begin.set()
        monkeypatch.setattr(loop, "_default_executor", previous_executor)
        if holder is not None:
            await asyncio.to_thread(holder.join, 3)
            assert not holder.is_alive()
        if queued is not None:
            await asyncio.wrap_future(queued)
        executor.shutdown(wait=True)
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)
        await lifetime.__aexit__(None, None, None)
    assert not failures
