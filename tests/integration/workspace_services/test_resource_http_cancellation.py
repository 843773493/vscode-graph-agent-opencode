"""真实资源 owner 经 HTTP 协议桩验证传输取消；不声称外部进程已终止。"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.abstractions.browser_resources import BrowserRequestError
from app.services.business.workspace_resources.owner import ResourceConflict
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.sessions import WORKSPACE_ID
from tests.support.resource_owners.terminal import (
    TerminalOwnerFixture,
    build_terminal_owner_fixture,
)


@dataclass
class PendingHTTP:
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    respond: asyncio.Event = field(default_factory=asyncio.Event)
    requests: list[bytes] = field(default_factory=list)
    response: bytes = b""
    slow_response: bool = False
    sent: int = 0

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[str]:
        tasks: set[asyncio.Task[None]] = set()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            header = await reader.readuntil(b"\r\n\r\n")
            lengths = [line.split(b":", 1)[1] for line in header.split(b"\r\n")
                       if line.lower().startswith(b"content-length:")]
            body = await reader.readexactly(int(lengths[0])) if lengths else b""
            self.requests.append(header + body)
            self.arrived.set()
            eof = asyncio.create_task(reader.read(1))
            release = asyncio.create_task(self.respond.wait())
            try:
                done, _ = await asyncio.wait({eof, release}, return_when=asyncio.FIRST_COMPLETED)
                if eof in done:
                    assert eof.result() == b""
                    self.disconnected.set()
                else:
                    writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                                 + str(len(self.response)).encode() + b"\r\n\r\n")
                    await writer.drain()
                    if self.slow_response:
                        for byte in self.response:
                            if eof.done():
                                assert eof.result() == b""
                                self.disconnected.set()
                                break
                            writer.write(bytes([byte]))
                            await writer.drain()
                            self.sent += 1
                            await asyncio.sleep(0.01)
                    else:
                        writer.write(self.response)
                        await writer.drain()
            finally:
                eof.cancel()
                release.cancel()
                await asyncio.gather(eof, release, return_exceptions=True)
                writer.close()
                await writer.wait_closed()

        def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            tasks.add(asyncio.create_task(handle(reader, writer)))

        async with await asyncio.start_server(accept, "127.0.0.1", 0) as server:
            try:
                yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            finally:
                self.respond.set()
                if tasks:
                    await asyncio.wait_for(asyncio.gather(*tasks), 5)


@pytest.fixture(params=["terminal", "browser"])
def bundle(request: pytest.FixtureRequest, tmp_path: Path) -> TerminalOwnerFixture | BrowserOwnerFixture:
    return (build_terminal_owner_fixture if request.param == "terminal" else build_browser_owner_fixture)(tmp_path)


@pytest.fixture
def pending_http() -> PendingHTTP:
    return PendingHTTP()


def transport(bundle: TerminalOwnerFixture | BrowserOwnerFixture, url: str) -> TerminalManagerClient | BrowserManagerClient:
    return (TerminalManagerClient if isinstance(bundle, TerminalOwnerFixture) else BrowserManagerClient)(
        backend_url=url, workspace_id=WORKSPACE_ID,
    )


async def test_cancel_closes_the_socket_and_keeps_unknown_owner_across_restart(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, pending_http: PendingHTTP, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    assert bundle.owner is not None
    pending_http.response = json.dumps({"data": {bundle.owner.kind: bundle.transport.records[rid]}}).encode()
    async with pending_http.serve() as url:
        monkeypatch.setattr(bundle.transport, "stop_resource", transport(bundle, url).stop_resource)
        stopping = asyncio.create_task(bundle.command(rid, action="stop", expected_revision=1))
        try:
            await asyncio.wait_for(pending_http.arrived.wait(), 5)
            stopping.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stopping
            # 取消 HTTP 等待必须真正关闭连接；服务端状态仍然未知。
            await asyncio.wait_for(pending_http.disconnected.wait(), 1)
            record = bundle.owner._store.records[rid]
            assert record.status == "stopping" and record.reconciliation_status == "unknown"
            bundle.restart()
            assert bundle.owner is not None
            observed = await bundle.owner.get_resource(rid)
            assert observed["reconciliation_status"] == "unknown" and observed["available_actions"] == []
            with pytest.raises(ResourceConflict, match="尚待 owner 核实"):
                await bundle.command(rid, action="stop", expected_revision=observed["owner_revision"])
            assert len(pending_http.requests) == 1
        finally:
            pending_http.respond.set()
            if not stopping.done():
                stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)


async def test_resource_http_does_not_wait_for_the_default_thread_pool(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, pending_http: PendingHTTP,
) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    assert bundle.owner is not None
    kind = bundle.owner.kind
    runtime = {**bundle.transport.records[rid], **({"page_id": "page_internal"} if kind == "browser" else {}), "status": "terminated" if kind == "terminal" else "closed"}
    pending_http.response = json.dumps({"data": {kind: runtime}}).encode()
    pending_http.respond.set()
    loop = asyncio.get_running_loop()
    previous = loop._default_executor
    unblocked = threading.Event()
    entered = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)

    def occupy() -> None:
        entered.set()
        unblocked.wait()

    blocked = loop.run_in_executor(pool, occupy)
    loop.set_default_executor(pool)
    try:
        while not entered.is_set():
            await asyncio.sleep(0)
        async with pending_http.serve() as url:
            result = await asyncio.wait_for(transport(bundle, url).stop_resource(rid), 1)
            assert result[kind]["status"] == runtime["status"]
            assert len(pending_http.requests) == 1 and not unblocked.is_set()
    finally:
        unblocked.set()
        await blocked
        # 恢复同一测试 loop 的执行器；不让后续用例继承已关闭的线程池。
        loop.set_default_executor(previous if previous is not None else ThreadPoolExecutor())
        pool.shutdown(wait=True)


async def test_slow_response_cannot_extend_the_total_http_deadline(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, pending_http: PendingHTTP, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    assert bundle.owner is not None
    module = f"app.services.infrastructure.{bundle.owner.kind}_manager_client"
    monkeypatch.setattr(f"{module}.DEFAULT_{bundle.owner.kind.upper()}_REQUEST_TIMEOUT_SECONDS", 0.2)
    pending_http.response = b'{"data":' + b" " * 500 + b"{}}"
    pending_http.slow_response = True
    pending_http.respond.set()
    async with pending_http.serve() as url:
        monkeypatch.setattr(bundle.transport, "stop_resource", transport(bundle, url).stop_resource)
        started = asyncio.get_running_loop().time()
        # Browser 保留其结构化 408；Terminal 保留原生 TimeoutError 类别。
        error_type = TimeoutError if isinstance(bundle, TerminalOwnerFixture) else BrowserRequestError
        with pytest.raises(error_type) as failed:
            await asyncio.wait_for(bundle.command(rid, action="stop", expected_revision=1), 2)
        assert asyncio.get_running_loop().time() - started < 1
        if isinstance(failed.value, BrowserRequestError):
            assert failed.value.status == 408 and failed.value.code == "browser_tool_timeout"
        await asyncio.wait_for(pending_http.disconnected.wait(), 1)
        assert 1 < pending_http.sent < len(pending_http.response)
        assert len(pending_http.requests) == 1
        assert bundle.owner._store.records[rid].reconciliation_status == "unknown"
