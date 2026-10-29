"""后台失败只发布事实，原异常不进入控制库或会话资源响应。"""

from __future__ import annotations

import asyncio
import errno
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

import httpx
import pytest

from app import container as container_module
from app import main
from app.core import failures
from app.core.background_task_contracts import (
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
    BackgroundTaskStep,
)
from app.core.background_task_registry import BackgroundTaskRegistry
from app.services.infrastructure.background_work_store import BackgroundWorkStore
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    MountedCase,
    assert_closed,
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    mounted_case as mounted_case,  # noqa: PLC0414 - 工作区按当前测试文件隔离。
)
from tests.support.background_work import ControlledBackgroundHandler
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS


@dataclass
class FailingBackgroundHandler(ControlledBackgroundHandler):
    error: Exception = field(default_factory=RuntimeError)

    async def step(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep:
        await super().step(handle)
        raise self.error


@pytest.fixture
async def completion_errors():
    loop = asyncio.get_running_loop()
    original = loop.get_exception_handler()
    observed: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: observed.append(context))
    yield observed
    loop.set_exception_handler(original)


@pytest.mark.parametrize("effect", ["atomic_mailbox", "external"])
@pytest.mark.parametrize("sink_failed", [False, True])
@pytest.mark.parametrize("error_kind,path", [
    ("io", "/private/background/checkpoint.json"),
    ("runtime", r"C:\private\background\checkpoint.json"),
    ("sqlite", r"\\target\private\background\checkpoint.sqlite"),
])
async def test_failure_publishes_facts_and_preserves_recovery(
    mounted_case: MountedCase, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, completion_errors: list[dict[str, object]],
    effect: Literal["atomic_mailbox", "external"], sink_failed: bool,
    error_kind: str, path: str,
) -> None:
    error = (
        PermissionError(errno.EACCES, "后台原生访问失败", path) if error_kind == "io"
        else sqlite3.OperationalError(f"后台 SQLite 失败: {path}") if error_kind == "sqlite"
        else RuntimeError(f"后台执行失败: {path}")
    )
    handler = FailingBackgroundHandler(effect=effect, error=error)
    # 只注入 handler 外部执行边界；registry、领取、SQL、资源投影和 API 均用产品实现。
    monkeypatch.setattr(container_module, "SystemTimeEmitterHandler", lambda: handler)

    async def no_native_resources(_client: object) -> list[dict[str, object]]:
        return []

    # 此片不创建终端或浏览器；只替代两个辅助服务的清单响应，保留真实 owner/聚合/投影。
    monkeypatch.setattr(container_module.TerminalManagerClient, "list_resources", no_native_resources)
    monkeypatch.setattr(container_module.BrowserManagerClient, "list_resources", no_native_resources)
    async with main.lifespan(main.app):
        owner, peer = mounted_case.containers
        base = f"/api/v1/workspaces/{owner.workspace_id}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://workspace.test",
            headers=LOCAL_TOKEN_HEADERS,
        ) as client:
            created = await client.post(base + "/sessions", json={"title": "后台失败事实与恢复"})
            assert created.status_code == 200, created.text
            assert created.headers["X-Request-ID"] == created.json()["request_id"]
            session_id = created.json()["data"]["session_id"]
            thread = await client.get(base + f"/sessions/{session_id}/main-thread")
            assert thread.status_code == 200, thread.text
            thread_id = thread.json()["data"]["thread_id"]
            resources_url = base + f"/sessions/{session_id}/resources"
            registry = owner.background_task_registry
            handle = await registry.spawn(
                session_id=session_id, thread_id=thread_id, agent_id="default",
                task_id="bgt_failure-publication",
                descriptor=BackgroundTaskDescriptor(kind=handler.kind, version=1, payload={}),
            )
            await asyncio.wait_for(handler.started.wait(), 5)
            task = registry.get_task(session_id, handle.task_id)
            assert task is not None

            def reject_diagnostic(*_args, **_kwargs) -> None:
                raise OSError(errno.EIO, "诊断 sink 失败", "/private/diagnostics/service.log")

            with monkeypatch.context() as patch:
                if sink_failed:
                    patch.setattr(failures.logger, "error", reject_diagnostic)
                handler.finish.set()
                with pytest.raises(type(error)) as raised:
                    await asyncio.wait_for(task, 5)
                assert raised.value is error
            await asyncio.sleep(0)
            assert len(completion_errors) == 1
            assert completion_errors[0]["exception"] is error
            assert handler.calls == 1
            status = "reconcile_required" if effect == "external" else "failed"
            expected_note = (
                f"后台任务失败 session_id={session_id}, thread_id={thread_id}, task_id={handle.task_id}: "
                f"stage=background-task-execution, error={type(error).__name__}"
            )
            if error_kind == "io":
                expected_note += f", errno={errno.EACCES}"
            elif error_kind == "sqlite":
                expected_note += ", sqlite_errorcode=None"
            if sink_failed:
                expected_note += ", diagnostic_sink_error=OSError"
            store = BackgroundWorkStore(sessions_dir=owner.workspace_root / ".boxteam/sessions")
            with store.control(session_id, read_only=True) as control:
                persisted = control.get_background_task(handle.task_id)
                assert persisted is not None
                assert persisted.status == status
                assert persisted.checkpoint == {"done": False}
                assert persisted.external_effect_pending == (effect == "external")
                assert persisted.fencing_token > handle.fencing_token
                row = control.connection.execute(
                    "SELECT metadata_json FROM background_tasks WHERE task_id=?", (handle.task_id,),
                ).fetchone()
                assert json.loads(row["metadata_json"]) == {"status_note": expected_note}
                lease = control.get_lease(persisted.lease_id)
                assert lease.state == ("active" if effect == "external" else "failed")

            response = await client.get(resources_url)
            assert response.status_code == 200, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
            resource, = response.json()["data"]["items"]
            assert resource["status"] == status
            assert resource["metadata"] == {
                "status_note": expected_note, "thread_id": thread_id, "agent_id": "default",
                "descriptor_kind": handler.kind, "descriptor_version": 1,
            }
            if sink_failed:
                assert any("诊断日志输出失败" in note and "service.log" in note for note in error.__notes__)
            else:
                records = [record for record in caplog.records if record.name == failures.__name__]
                assert len(records) == 1
                assert records[0].exc_info is not None and records[0].exc_info[1] is error
                assert str(error) in caplog.text
            active = await client.get(resources_url, params={"include_history": "false"})
            assert active.status_code == 200, active.text
            assert active.json()["data"]["items"] == ([resource] if effect == "external" else [])
            wrong_workspace = await client.get(
                f"/api/v1/workspaces/{peer.workspace_id}/sessions/{session_id}/resources",
            )
            assert wrong_workspace.status_code == 404, wrong_workspace.text

            # 同进程重建 store/registry，核真实落盘；不冒称独立后端进程重启。
            await registry.stop()
            replacement = ControlledBackgroundHandler(effect=effect)
            cold = BackgroundTaskRegistry(
                store=store, residency_tracker=ThreadResidencyTracker(), handlers=(replacement,),
            )
            await cold.start()
            try:
                assert cold.get_handle(session_id, handle.task_id) == persisted
                assert cold.get_task(session_id, handle.task_id) is None
                assert replacement.calls == 0
                if effect == "external":
                    with pytest.raises(ValueError, match="尚未确认"):
                        await cold.reconcile(session_id, handle.task_id)
                    unknown = cold.get_handle(session_id, handle.task_id)
                    assert unknown is not None and unknown.external_effect_pending
                    assert unknown.metadata == {"status_note": expected_note}
                    replacement.reconciliation = BackgroundTaskStep(
                        checkpoint={"done": True}, completed=True, result={"confirmed": True},
                    )
                    confirmed = await cold.reconcile(session_id, handle.task_id)
                    assert confirmed.status == "completed"
                    assert not confirmed.external_effect_pending
                    assert replacement.calls == 0
                    # 原 API 从唯一控制库看到显式对账结果，无第二份内存业务状态。
                    completed = await client.get(resources_url)
                    assert completed.status_code == 200, completed.text
                    current, = completed.json()["data"]["items"]
                    assert current["status"] == "completed"
                    assert current["metadata"]["result"] == {"confirmed": True}
                with store.control(session_id, read_only=True) as control:
                    assert control.list_non_terminal_leases() == ()
            finally:
                await cold.stop()
    assert_closed(mounted_case)
