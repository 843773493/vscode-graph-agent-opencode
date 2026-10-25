"""真实Node/Chromium/HTTP核对旧授权撤销的有界等待与迟到回执。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.abstractions.browser_resources import BrowserRequestError
from app.container import WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import catalog_provider_scope
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.business.workspace_resources import recovery
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.harness.python.browser_manager import (
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.support.processes.workspace import terminate_process
from tests.support.workspace_tool_runtime import workspace_tool_runtime
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture
def browser_client(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    integration_backend_port: int, integration_config_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[BrowserManagerClient, Path, TestRunContext]]:
    with TestRunContext.from_test_file(Path(__file__)) as run:
        runtime = run.runtime_root_for_node(request.node.nodeid)
        runtime.mkdir(parents=True, exist_ok=True)
        for name in ("TMPDIR", "TMP", "TEMP"):
            monkeypatch.setenv(name, str(runtime))
        root = prepare_default_test_workspace(
            workspace_root=Path(integration_workspace_root_path) / uuid4().hex,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        )
        install_test_workspace_config(
            workspace_root=root, config_path=Path(integration_config_path),
            schema_path=Path.cwd() / "configs/workspace_schema.jsonc",
        )
        handle = start_browser_manager_processes(
            workspace_root=root, artifacts_dir=run.artifacts_dir,
            backend_port=integration_backend_port + 60,
            frontend_port=integration_backend_port + 61, backend_host="127.0.0.1",
        )
        run.add_cleanup("Browser进程", lambda: close_browser_manager_processes(handle))
        yield BrowserManagerClient(
            workspace_id=load_or_create_workspace_id(root),
            backend_url=f"http://127.0.0.1:{handle.backend_port}",
        ), root, run


@pytest.fixture
def owner_mount(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    integration_backend_port: int,
) -> MountedWorkspace:
    client, root, run = browser_client
    terminal_port = integration_backend_port + 62
    terminal = start_terminal_backend(
        backend_port=terminal_port, workspace_root=str(root), artifacts_dir=run.artifacts_dir,
    )
    run.add_cleanup("Terminal进程", lambda: terminate_process(terminal))
    return MountedWorkspace(
        load_or_create_workspace_id(root), root, browser_backend_url=client.backend_url,
        terminal_backend_url=f"http://127.0.0.1:{terminal_port}",
    )


@pytest.fixture
async def blocked_action() -> AsyncIterator[tuple[str, asyncio.Event, asyncio.Event]]:
    arrived = asyncio.Event()
    release = asyncio.Event()
    handlers: list[asyncio.Task[None]] = []

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            arrived.set()
            await release.wait()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.append(asyncio.create_task(respond(reader, writer)))

    async with await asyncio.start_server(accept, "127.0.0.1", 0) as server:
        try:
            yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/", arrived, release
        finally:
            release.set()
            if handlers:
                await asyncio.wait_for(asyncio.gather(*handlers), 5)


async def test_owner_revision_timeout_keeps_the_original_execution_and_rejects_its_late_result(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    blocked_action: tuple[str, asyncio.Event, asyncio.Event],
) -> None:
    client, root, run = browser_client
    url, arrived, release = blocked_action
    first = f"browser_{uuid4().hex}"
    second = f"browser_{uuid4().hex}"
    page = "data:text/html,%3Cinput%20id=field%20value=initial%3E"
    for browser_id in (first, second):
        await client.create_browser(browser_id=browser_id, owner_revision=1, title="撤销等待", url=page)
    running = asyncio.create_task(client.perform(first, "run", {
        "code": f"await fetch({json.dumps(url)}); await page.locator('#field').fill('late'); return 'old';",
        "timeoutMs": 20000,
    }, actor="agent"))
    try:
        await asyncio.wait_for(arrived.wait(), 5)
        before = await client.get_resource(first)
        assert before["active_operation_count"] == 1
        started = time.monotonic()
        with pytest.raises(BrowserRequestError) as caught:
            await asyncio.wait_for(client.sync_owner_revision(first, 2, for_stop=False), 8)
        sync_elapsed = time.monotonic() - started
        assert 4.5 <= sync_elapsed < 8
        error = caught.value
        assert error.method == "POST"
        assert error.path == f"/api/browsers/{first}/owner-revision"
        assert error.status == 408
        assert error.code == "browser_owner_revision_drain_timeout"
        assert "recovery" not in error.payload
        assert "retryable" not in error.payload
        assert not running.done()
        current = await client.get_resource(first)
        assert current["status"] == "running"
        assert current["transport_owner_revision"] == 2
        assert current["active_operation_count"] == 1
        assert current["active_page_id"] == before["active_page_id"]
        assert current["active_operation"] == before["active_operation"]
        stored = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
        record, = [record for record in stored["browsers"] if record["browser_id"] == first]
        assert record["transport_owner_revision"] == 2
        assert record["status"] == "running"
        independent = await asyncio.wait_for(client.perform(second, "read", {}, actor="agent"), 2)
        assert independent["operation"]["status"] == "completed"
        release.set()
        with pytest.raises(BrowserRequestError) as late:
            await asyncio.wait_for(running, 5)
        assert late.value.code == "browser_transport_revoked"
        await client.sync_owner_revision(first, 2, for_stop=False)
        observed = await client.perform(first, "run", {
            "code": "return await page.locator('#field').inputValue();",
        }, actor="agent")
        assert observed["result"] == "late"
        assert observed["operation"]["status"] == "completed"
        stopped = await client.stop_resource(first)
        assert stopped["browser"]["status"] == "closed"
        assert stopped["browser"]["transport_owner_revision"] == 2
        assert stopped["browser"]["reconciliation_status"] == "verified"
        run.write_result("passed", {
            "browser_id": first, "revision": 2, "late_result_code": late.value.code,
            "timeout_code": error.code, "independent_browser_id": second,
            "sync_elapsed_seconds": sync_elapsed,
            "scope": "真实Node/Chromium/HTTP与Python客户端；受控HTTP动作屏障，不含Python业务owner恢复gate或Gateway/主Web",
        })
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        for browser_id in (first, second):
            await client.delete_resource(browser_id)


async def test_cold_python_owner_keeps_healthy_browser_usable_after_peer_revision_timeout(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    blocked_action: tuple[str, asyncio.Event, asyncio.Event],
    owner_mount: MountedWorkspace,
) -> None:
    client, root, run = browser_client
    url, arrived, release = blocked_action
    ids: list[str] = []
    # 两次正式服务图生命周期之间保留workspace资源；创建和关联均由产品生成。
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, session_id, _context):
        for _ in range(2):
            resource = await container.browser_resource_owner.create_browser(
                session_id=session_id, retention_scope="workspace",
                holder_id="request:recovery-create", operation_id="recovery-create",
                url="data:text/html,%3Cinput%20id=field%20value=initial%3E",
            )
            ids.append(str(resource["resource_id"]))
    first, second = ids
    before = {rid: await client.get_resource(rid) for rid in ids}
    running = asyncio.create_task(client.perform(first, "run", {
        "code": f"await fetch({json.dumps(url)}); await page.locator('#field').fill('late'); return 'old';",
        "timeoutMs": 20000,
    }, actor="agent"))
    # 冷建正式服务图并直接调用恢复入口，错误由本调用接收；不启动另一个周期worker抢先恢复。
    with (
        WorkspaceContainerRegistry(
            mounted_workspaces=MountedWorkspaceRegistry([owner_mount]), project_root=Path.cwd(),
        ) as registry,
        catalog_provider_scope(registry),
        ExitStack() as cleanup,
    ):
        container = registry.get(owner_mount.workspace_id)
        cleanup.callback(container.workspace_activity_service.store.close)
        owner = container.browser_resource_owner
        try:
            await asyncio.wait_for(arrived.wait(), 5)
            with pytest.raises(BrowserRequestError) as caught:
                await asyncio.wait_for(owner.get_resource(first), 8)
            assert caught.value.code == "browser_owner_revision_drain_timeout"
            assert not running.done()
            healthy = await asyncio.wait_for(owner.get_resource(second), 2)
            expected = int(before[second]["transport_owner_revision"]) + 1
            assert healthy["owner_revision"] == expected
            assert healthy["reconciliation_status"] == "verified"
            result = await owner.perform(
                second, action="run", payload={"code": "return await page.locator('#field').inputValue();"},
                expected_revision=expected, holder_id="request:recovered-read", operation_id="recovered-read",
            )
            assert result["result"] == "initial"
            unknown = await owner.get_resource(first)
            assert unknown["reconciliation_status"] == "unknown"
            assert unknown["owner_revision"] == int(before[first]["transport_owner_revision"]) + 1
            assert (await client.get_resource(first))["active_operation_count"] == 1
            release.set()
            with pytest.raises(BrowserRequestError) as late:
                await asyncio.wait_for(running, 5)
            assert late.value.code == "browser_transport_revoked"
            assert (await owner.get_resource(second))["owner_revision"] == expected
            run.write_result("passed", {
                "unknown_browser_id": first, "healthy_browser_id": second,
                "healthy_revision": expected, "late_result_code": late.value.code,
                "scope": "正式Session/资源创建、生命周期关闭后冷建Python服务图及真实Node/HTTP；不含冷图后台worker、进程硬重启、挂起期间全局等待隔离或Gateway/主Web",
            })
        finally:
            release.set()
            await asyncio.gather(running, return_exceptions=True)
            for rid in ids:
                record = owner._store.get(rid)
                await owner.command(
                    rid, action="delete", expected_revision=record.owner_revision,
                    holder_id=f"request:recovery-cleanup:{rid}", operation_id="recovery-cleanup",
                )


@pytest.mark.parametrize("action", ["stop", "delete"])
async def test_close_keeps_timed_out_script_unknown_until_its_original_execution_ends(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    blocked_action: tuple[str, asyncio.Event, asyncio.Event], action: str,
) -> None:
    client, root, run = browser_client
    url, arrived, release = blocked_action
    first, second = (f"browser_{uuid4().hex}" for _ in range(2))
    for browser_id in (first, second):
        await client.create_browser(browser_id=browser_id, owner_revision=1, title="原执行核实", url="about:blank")
    running = asyncio.create_task(client.perform(first, "run", {
        # 不消费取消signal，原生fetch在工具超时后仍由本次脚本持有。
        "code": f"await fetch({json.dumps(url)}); return await page.title();",
        "timeoutMs": 1000,
    }, actor="agent"))
    close = client.stop_resource if action == "stop" else client.delete_resource
    marker = f"verified-close-after-script-{first}"
    try:
        await asyncio.wait_for(arrived.wait(), 5)
        with pytest.raises(BrowserRequestError) as timed_out:
            await asyncio.wait_for(running, 5)
        assert timed_out.value.code == "browser_tool_timeout"
        assert timed_out.value.payload["recovery"] == "page_reset"
        # 真实页面reset仍允许后续短操作；原脚本等待不能被这一成功掩盖。
        await client.perform(first, "run", {
            "code": f"page.once('close', () => console.error({json.dumps(marker)})); return 'reset-ready';",
        }, actor="agent")
        started = time.monotonic()
        with pytest.raises(BrowserRequestError) as blocked:
            await asyncio.wait_for(close(first), 8)
        elapsed = time.monotonic() - started
        assert 4.5 <= elapsed < 8
        assert blocked.value.code == "browser_close_operation_drain_timeout"
        assert blocked.value.status == 408
        assert not release.is_set()
        unknown = await client.get_resource(first)
        assert unknown["status"] == "running" and unknown["reconciliation_status"] == "unknown"
        stored = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
        record, = [item for item in stored["browsers"] if item["browser_id"] == first]
        assert record["status"] == "running" and record["reconciliation_status"] == "unknown"
        assert record["transport_owner_revision"] == 1
        async with httpx.AsyncClient() as http:
            health = await http.get(f"{client.backend_url}/health")
        assert health.status_code == 200
        assert health.json()["browser_runtime"]["context_count"] == 1
        independent = await asyncio.wait_for(client.perform(second, "read", {}, actor="agent"), 2)
        assert independent["operation"]["status"] == "completed"
        diagnostics = "\n".join(p.read_text() for p in run.artifacts_dir.glob("browser-backend-*.stderr.log"))
        assert diagnostics.count(marker) == 1
        release.set()
        verified = (await asyncio.wait_for(close(first), 5))["browser"]
        expected = "closed" if action == "stop" else "deleted"
        assert verified["status"] == expected and verified["reconciliation_status"] == "verified"
        assert verified["browser_id"] == first and verified["transport_owner_revision"] == 1
        diagnostics = "\n".join(p.read_text() for p in run.artifacts_dir.glob("browser-backend-*.stderr.log"))
        assert diagnostics.count(marker) == 1
        run.write_result("passed", {
            "action": action, "browser_id": first, "close_wait_seconds": elapsed,
            "scope": "Python传输客户端→真实Node/Chromium/HTTP脚本与原停止/删除入口；不代表Python业务owner挂起接管、强制终止或Gateway/主Web",
        })
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        await close(first)
        await client.delete_resource(second)


@pytest.mark.parametrize("action", ["stop", "delete"])
async def test_python_management_closes_context_before_waiting_for_its_script(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, action: str,
) -> None:
    client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, session_id, _context):
        owner = container.browser_resource_owner
        browser = await owner.create_browser(
            session_id=session_id, retention_scope="workspace", url="about:blank",
            holder_id="request:context-wait-create", operation_id="context-wait-create",
        )
        peer = await owner.create_browser(
            session_id=session_id, retention_scope="workspace", url="about:blank",
            holder_id="request:context-wait-peer", operation_id="context-wait-peer",
        )
        rid = str(browser["resource_id"])
        detached = await owner.command(
            rid, action="detach", expected_revision=int(browser["owner_revision"]),
            thread=owner._store.get(rid).source_thread,
            holder_id="request:context-wait-detach", operation_id="context-wait-detach",
        )
        try:
            with pytest.raises(BrowserRequestError) as timed_out:
                await owner.perform(
                    rid, action="run", payload={
                        # 页面reset只关闭page；原脚本须由真正关闭context才能结束。
                        "code": "await new Promise(resolve => context.once('close', resolve)); return 'context已关闭';",
                        "timeoutMs": 1000,
                    }, expected_revision=int(detached["owner_revision"]),
                    holder_id="request:context-wait-run", operation_id="context-wait-run",
                )
            assert timed_out.value.code == "browser_tool_timeout"
            current = await owner.get_resource(rid)
            assert current["reconciliation_status"] == "verified"
            assert owner._leases.leases_for_resource(rid) == []
            started = time.monotonic()
            stopped = await asyncio.wait_for(owner.command(
                rid, action=action, expected_revision=int(current["owner_revision"]),
                holder_id="request:context-wait-close", operation_id="context-wait-close",
            ), 8)
            assert stopped["status"] == ("closed" if action == "stop" else "deleted")
            assert stopped["reconciliation_status"] == "verified"
            assert stopped["owner_revision"] == int(current["owner_revision"]) + 1
            assert owner._leases.leases_for_resource(rid) == []
            healthy = await owner.perform(
                str(peer["resource_id"]), action="read", payload={},
                expected_revision=int(peer["owner_revision"]),
                holder_id="request:context-wait-read", operation_id="context-wait-read",
            )
            assert healthy["operation"]["status"] == "completed"
            async with httpx.AsyncClient() as http:
                health = await http.get(f"{client.backend_url}/health")
            assert health.json()["browser_runtime"]["context_count"] == 1
            run.write_result("passed", {
                "action": action, "browser_id": rid, "close_seconds": time.monotonic() - started,
                "scope": "正式Session/Python业务owner→真实Node/Chromium；原脚本仅等待context关闭，不替代强制终止与Gateway/主Web",
            })
        finally:
            # 原实现失败时仍由真实关闭入口放行原脚本，避免污染后续生命周期清理。
            await client.stop_resource(rid)


@pytest.mark.parametrize("phase", ["listing", "revision"])
async def test_cold_owner_budget_keeps_real_browser_and_frozen_revision_after_lost_receipt(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, session_id, _context):
        created = await container.browser_resource_owner.create_browser(
            session_id=session_id, retention_scope="workspace",
            holder_id="request:budget-create", operation_id="budget-create",
            url="data:text/html,%3Cinput%20id=field%20value=retained%3E",
        )
    rid = str(created["resource_id"])
    before = await client.get_resource(rid)
    with (
        WorkspaceContainerRegistry(
            mounted_workspaces=MountedWorkspaceRegistry([owner_mount]), project_root=Path.cwd(),
        ) as registry,
        catalog_provider_scope(registry),
        ExitStack() as cleanup,
    ):
        container = registry.get(owner_mount.workspace_id)
        cleanup.callback(container.workspace_activity_service.store.close)
        owner = container.browser_resource_owner
        entered, release = asyncio.Event(), asyncio.Event()
        contexts: list[asyncio.Timeout] = []
        observed: list[object] = []
        listing, syncing = owner._transport.list_resources, owner._transport.sync_owner_revision

        def deadline(seconds: float) -> asyncio.Timeout:
            assert seconds == 30
            context = asyncio.timeout(seconds)
            contexts.append(context)
            return context

        async def list_receipt() -> list[dict[str, object]]:
            receipt = await listing()
            observed.append(receipt)
            if phase == "listing":
                entered.set()
                await release.wait()
            return receipt

        async def revision_receipt(resource_id: str, revision: int, *, for_stop: bool) -> None:
            await syncing(resource_id, revision, for_stop=for_stop)
            observed.append((resource_id, revision))
            if phase == "revision":
                entered.set()
                await release.wait()

        with monkeypatch.context() as patch:
            patch.setattr(recovery, "timeout", deadline, raising=False)
            patch.setattr(owner._transport, "list_resources", list_receipt)
            patch.setattr(owner._transport, "sync_owner_revision", revision_receipt)
            waiting = asyncio.create_task(owner.get_resource(rid))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                assert len(contexts) == 1
                contexts[0].reschedule(asyncio.get_running_loop().time())
                with pytest.raises(TimeoutError, match="未确认外部操作停止"):
                    await asyncio.wait_for(waiting, 2)
                current = await client.get_resource(rid)
                assert current["status"] == "running"
                assert current["active_page_id"] == before["active_page_id"]
                expected = int(before["transport_owner_revision"]) + (phase == "revision")
                assert current["transport_owner_revision"] == expected
                record = owner._store.get(rid)
                assert record.reconciliation_status == ("unknown" if phase == "revision" else "verified")
                assert record.owner_revision == expected
                assert owner._recovery._pending == ({rid: expected} if phase == "revision" else None)
                assert owner._leases.leases_for_resource(rid) == []
            finally:
                release.set()
                if not waiting.done():
                    waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
        # 同版本同步只核实授权；原页面和历史版本不重建或重放。
        recovered = await owner.get_resource(rid)
        assert recovered["owner_revision"] == int(before["transport_owner_revision"]) + 1
        value = await client.perform(rid, "run", {
            "code": "return await page.locator('#field').inputValue();",
        }, actor="agent")
        assert value["result"] == "retained"
        assert recovered["reconciliation_status"] == ("unknown" if phase == "revision" else "verified")
        run.write_result("passed", {
            "phase": phase, "browser_id": rid, "owner_revision": recovered["owner_revision"],
            "receipt_count": len(observed), "page_id": before["active_page_id"],
            "scope": "正式创建与冷服务图、真实Node/Chromium/HTTP；仅暂停已返回回执并推进原30秒截止，不含Gateway、硬重启或不合作进程终止",
        })
        await owner.command(
            rid, action="delete", expected_revision=int(recovered["owner_revision"]),
            holder_id="request:budget-cleanup", operation_id="budget-cleanup",
        )
