"""真实Node/Chromium/HTTP核对旧授权撤销的有界等待与迟到回执。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from app.abstractions.browser_resources import BrowserRequestError
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.harness.python.browser_manager import (
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def browser_client(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    integration_backend_port: int, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[BrowserManagerClient, Path, TestRunContext]]:
    with TestRunContext.from_test_file(Path(__file__)) as run:
        runtime = run.runtime_root_for_node(request.node.nodeid)
        runtime.mkdir(parents=True, exist_ok=True)
        for name in ("TMPDIR", "TMP", "TEMP"):
            monkeypatch.setenv(name, str(runtime))
        root = Path(integration_workspace_root_path)
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
