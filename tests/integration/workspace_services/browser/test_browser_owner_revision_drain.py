"""真实Node/Chromium/HTTP核对旧授权撤销的有界等待与迟到回执。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import pytest

from app.abstractions.browser_resources import BrowserRequestError
from app.container import WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import catalog_provider_scope
from app.core.workspace_identity import load_or_create_workspace_id
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
            await asyncio.wait_for(client.sync_owner_revision(first, 2), 8)
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
        await client.sync_owner_revision(first, 2)
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
