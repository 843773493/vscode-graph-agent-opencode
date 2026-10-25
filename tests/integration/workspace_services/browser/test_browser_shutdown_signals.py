"""真实信号关闭失败保留原进程，重试保存原操作最终检查点并冷恢复。"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.abstractions.browser_resources import BrowserRequestError
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.harness.python.browser_manager import (
    BrowserManagerProcesses,
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace


@pytest.fixture
def browser_service(
    request: pytest.FixtureRequest, integration_backend_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[BrowserManagerProcesses, BrowserManagerClient, Path, TestRunContext, Path]]:
    with TestRunContext.from_test_file(Path(__file__)) as run:
        runtime = run.runtime_root_for_node(request.node.nodeid)
        runtime.mkdir(parents=True, exist_ok=True)
        for name in ("TMPDIR", "TMP", "TEMP"):
            monkeypatch.setenv(name, str(runtime))
        root = prepare_default_test_workspace(
            workspace_root=run.workspace_root / uuid4().hex,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        )
        artifacts = run.artifacts_dir / uuid4().hex
        handle = start_browser_manager_processes(
            workspace_root=root, artifacts_dir=artifacts,
            backend_port=integration_backend_port + 60,
            frontend_port=integration_backend_port + 61, backend_host="127.0.0.1",
        )
        run.add_cleanup("Browser 进程", lambda: close_browser_manager_processes(handle))
        yield handle, BrowserManagerClient(
            workspace_id=load_or_create_workspace_id(root),
            backend_url=f"http://127.0.0.1:{handle.backend_port}",
        ), root, run, artifacts


@pytest.fixture
async def blocked_action() -> AsyncIterator[tuple[str, asyncio.Event, asyncio.Event]]:
    arrived, release = asyncio.Event(), asyncio.Event()
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
            await asyncio.wait_for(asyncio.gather(*handlers), 5)


@pytest.mark.skipif(sys.platform == "win32", reason="本组核对 POSIX 信号；Windows SIGBREAK 留待实机验证")
@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGINT", "SIGHUP"])
async def test_signal_shutdown_failure_keeps_the_original_owner_for_retry_and_cold_recovery(
    browser_service: tuple[BrowserManagerProcesses, BrowserManagerClient, Path, TestRunContext, Path],
    blocked_action: tuple[str, asyncio.Event, asyncio.Event], signal_name: str,
) -> None:
    handle, client, root, run, artifacts = browser_service
    url, arrived, release = blocked_action
    browser_id = f"browser_{uuid4().hex}"
    await client.create_browser(
        browser_id=browser_id, owner_revision=1, title="信号关闭重试",
        url="data:text/html,%3Cinput%20id=field%20value=initial%3E",
    )
    running = asyncio.create_task(client.perform(browser_id, "run", {
        "code": f"await fetch({json.dumps(url)}); await page.locator('#field').fill('after-signal'); return 'completed';",
        "timeoutMs": 20000,
    }, actor="agent"))
    requested_signal = getattr(signal, signal_name)
    process = handle.backend_process
    denied_operation = None
    failure_message = "关闭失败，保留原运行时与 HTTP 管理入口"
    try:
        await asyncio.wait_for(arrived.wait(), 5)
        before = await client.get_resource(browser_id)
        state_file = root / ".boxteam/browser-manager/browsers.json"
        state_before = state_file.read_bytes()
        started = time.monotonic()
        process.send_signal(requested_signal)
        if signal_name == "SIGINT":
            # 原关闭仍在执行时的重复信号只等待同次关闭，不重复触发 Manager。
            await asyncio.sleep(0.1)
            process.send_signal(requested_signal)
        async with asyncio.timeout(8):
            while True:
                assert process.poll() is None, "关闭未核实不能退出原 Browser owner"
                diagnostics = "\n".join(
                    path.read_text() for path in artifacts.glob("browser-backend-*.stderr.log")
                )
                if "[browser-backend] 关闭" in diagnostics:
                    break
                await asyncio.sleep(0.05)
        elapsed = time.monotonic() - started
        assert 4.5 <= elapsed < 8
        assert not running.done()
        current = await client.get_resource(browser_id)
        assert current["status"] == "running"
        assert current["active_page_id"] == before["active_page_id"]
        assert current["active_operation_count"] == 1
        assert current["transport_owner_revision"] == 1
        assert state_file.read_bytes() == state_before
        async with httpx.AsyncClient() as http:
            health = await http.get(f"{client.backend_url}/health")
        assert health.status_code == 200
        assert health.json()["browser_runtime"]["connected"] is True
        assert health.json()["browser_runtime"]["context_count"] == 1
        assert diagnostics.count(failure_message) == 1
        assert "browser_shutdown_operation_drain_timeout" in diagnostics
        assert browser_id in diagnostics
        # 串行请求可等待原队尾；放行原操作后再核对它被拒绝，不能由测试阻止原操作完成。
        denied_operation = asyncio.create_task(client.perform(
            browser_id, "run", {"code": "throw new Error('不应执行');"}, actor="agent",
        ))
        rejected_id = f"browser_{uuid4().hex}"
        with pytest.raises(BrowserRequestError) as rejected:
            await client.create_browser(browser_id=rejected_id, owner_revision=1, title="关闭后创建", url="about:blank")
        assert rejected.value.code == "browser_manager_closing"
        assert rejected.value.payload["creation_receipt"] == {
            "workspace_id": load_or_create_workspace_id(root), "resource_id": rejected_id,
            "owner_revision": 1, "outcome": "not_created",
        }
        release.set()
        completed = await asyncio.wait_for(running, 5)
        assert completed["result"] == "completed"
        with pytest.raises(BrowserRequestError) as denied:
            await asyncio.wait_for(denied_operation, 5)
        assert denied.value.code == "browser_closing"
        process.send_signal(requested_signal)
        exit_code = await asyncio.to_thread(process.wait, timeout=8)
        assert exit_code == 128 + requested_signal
        stored = json.loads(state_file.read_text())
        record, = stored["browsers"]
        assert record["browser_id"] == browser_id
        assert record["resource_state"] == "discarded"
        assert record["reconciliation_status"] == "verified"

        close_browser_manager_processes(handle)
        restarted = start_browser_manager_processes(
            workspace_root=root, artifacts_dir=artifacts,
            backend_port=handle.backend_port, frontend_port=handle.frontend_port,
            backend_host="127.0.0.1",
        )
        run.add_cleanup("冷重启 Browser 进程", lambda: close_browser_manager_processes(restarted))
        cold = await client.get_resource(browser_id)
        assert cold["browser_id"] == browser_id and cold["resource_state"] == "discarded"
        restored = await client.perform(browser_id, "run", {
            "code": "return await page.locator('#field').inputValue();",
        }, actor="agent")
        assert restored["result"] == "after-signal"
        restarted.backend_process.send_signal(requested_signal)
        assert await asyncio.to_thread(restarted.backend_process.wait, timeout=8) == exit_code
        run.write_result("passed", {
            "signal": signal_name, "exit_code": exit_code, "browser_id": browser_id,
            "drain_wait_seconds": elapsed, "restored_value": restored["result"],
            "scope": "POSIX 真实信号/Node/Chromium/HTTP与冷进程重启；不含Windows、Gateway/主Web、强制终止或shutdown总期限",
        })
        (run.artifacts_dir / "result.json").replace(artifacts / "result.json")
    finally:
        release.set()
        await asyncio.gather(*[task for task in (running, denied_operation) if task is not None], return_exceptions=True)


@pytest.mark.parametrize(("fatal_kind", "signal_first"), [
    ("exception", False), ("rejection", False),
    pytest.param("exception", True, marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX 信号中途到达致命错误")),
])
async def test_fatal_error_still_exits_when_manager_drain_fails(
    browser_service: tuple[BrowserManagerProcesses, BrowserManagerClient, Path, TestRunContext, Path],
    blocked_action: tuple[str, asyncio.Event, asyncio.Event], fatal_kind: str, signal_first: bool,
) -> None:
    handle, client, _root, run, artifacts = browser_service
    url, arrived, release = blocked_action
    browser_id = f"browser_{uuid4().hex}"
    marker = f"fatal-{fatal_kind}-{browser_id}"
    await client.create_browser(browser_id=browser_id, owner_revision=1, title="致命错误退出", url="about:blank")
    failure = f"new Error({json.dumps(marker)})"
    trigger = f"throw {failure};" if fatal_kind == "exception" else f"void Promise.reject({failure});"
    signal_code = "process.kill(process.pid, 'SIGTERM');" if signal_first else ""
    running = asyncio.create_task(client.perform(browser_id, "run", {
        # 由真实脚本触发 Node 未捕获错误；HTTP 屏障让同一次关闭遇到未排空执行。
        "code": f"{signal_code} setTimeout(() => {{ {trigger} }}, 100); await fetch({json.dumps(url)}); return 'late';",
        "timeoutMs": 20000,
    }, actor="agent"))
    try:
        await asyncio.wait_for(arrived.wait(), 5)
        async with asyncio.timeout(8):
            while True:
                diagnostics = "\n".join(path.read_text() for path in artifacts.glob("browser-backend-*.stderr.log"))
                if "关闭检查点失败" in diagnostics:
                    break
                assert handle.backend_process.poll() is None
                await asyncio.sleep(0.05)
        # 原致命退出仍先等已接纳的 HTTP 请求结束；不把它误写成整个 shutdown 有界。
        assert not running.done()
        release.set()
        exit_code = await asyncio.to_thread(handle.backend_process.wait, timeout=8)
        assert exit_code == 1
        diagnostics = "\n".join(path.read_text() for path in artifacts.glob("browser-backend-*.stderr.log"))
        assert marker in diagnostics
        assert "关闭检查点失败" in diagnostics
        assert "保留原运行时与 HTTP 管理入口" not in diagnostics
        run.write_result("passed", {
            "fatal_kind": fatal_kind, "signal_first": signal_first, "exit_code": exit_code, "browser_id": browser_id,
            "scope": "真实 Node 未捕获错误与原致命退出合同；不将进程退出声明为外部资源停止证明",
        })
        (run.artifacts_dir / "result.json").replace(artifacts / "result.json")
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
