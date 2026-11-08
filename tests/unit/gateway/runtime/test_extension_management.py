"""HTTP 管理 owner 的取消、接管和排空；原生安装行为由真实集成覆盖。"""

from __future__ import annotations

import asyncio
import json
import threading
from base64 import b64decode
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from pydantic import JsonValue

from app.core.trace_middleware import reset_current_request_id, set_current_request_id
from app.gateway.runtime import extension_management
from app.gateway.runtime.extension_management import (
    ExtensionManagementInstance,
    GatewayExtensionManagement,
)
from app.gateway.runtime.process_logs import ProcessLogFile, ProcessLogOwnership
from app.gateway.server.extension_management import management_owner
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementError,
    NativeExtensionPublication,
    NativeFileRead,
    NativeFileStat,
)
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime

EXTENSION_KEY = "publisher.probe-1.0.0"


@dataclass
class ManagementProcessFake:
    """只记录 OS 边界调用，不模拟管理服务的准入或生命周期。"""

    events: list[str]
    pid: int = 12345
    alive: bool = True
    close_timeouts: list[float] = field(default_factory=list)
    detach_calls: int = 0

    def is_alive(self) -> bool:
        return self.alive

    def close(self, *, timeout_seconds: float = 8) -> None:
        self.events.append("process-close")
        self.close_timeouts.append(timeout_seconds)
        self.alive = False

    def detach(self) -> None:
        self.events.append("process-detach")
        self.detach_calls += 1


class PublicationStream(httpx.AsyncByteStream):
    def __init__(self, publication: NativeExtensionPublication, events: list[str]) -> None:
        self.content = (json.dumps({"result": publication.model_dump(mode="json")}) + "\n").encode()
        self.closed = asyncio.Event()
        self.events = events

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.content

    async def aclose(self) -> None:
        self.events.append("publication-close")
        self.closed.set()


@dataclass
class HttpControl:
    owner: GatewayExtensionManagement
    process: ManagementProcessFake
    client: httpx.AsyncClient
    instance: ExtensionManagementInstance
    replies: asyncio.Queue[httpx.Response]
    startup_replies: dict[str, httpx.Response]
    requests: list[httpx.Request]
    command_sent: asyncio.Event
    events: list[str]
    spawn: Mock
    adopt_log: Mock
    open_log: Mock
    client_factory: Mock

    @property
    def commands(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path in {"/management", "/publication"}]

    def reply(self, payload: dict[str, JsonValue]) -> None:
        self.replies.put_nowait(httpx.Response(200, json=payload))

    def publish(self, publication: NativeExtensionPublication) -> PublicationStream:
        stream = PublicationStream(publication, self.events)
        self.replies.put_nowait(httpx.Response(200, stream=stream))
        return stream


@pytest.fixture
def process_origin() -> Literal["spawn", "adopt"]:
    return "adopt"


@pytest.fixture
async def control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    process_origin: Literal["spawn", "adopt"],
) -> AsyncIterator[HttpControl]:
    runtime = NodeHostRuntime(tmp_path / "node", tmp_path / "extensionHostProcess.mjs", "a" * 40, "1.0.0", "24.0.0")
    owner = GatewayExtensionManagement(backend_id=str(uuid4()), runtime=runtime,
        directory=tmp_path / "management", environment={}, log_dir=tmp_path / "logs",
        logging_startup={"level": "info", "pretty": False, "revision": "initial"}, operation_timeout=0.1)
    events: list[str] = []
    process = ManagementProcessFake(events)
    instance = ExtensionManagementInstance(backend_id=owner.backend_id, process_id=process.pid,
        runtime_instance_id=str(uuid4()), base_url="http://127.0.0.1:45001", commit=runtime.commit,
        vscode_version=runtime.vscode_version, node_version=runtime.node_version)
    owner.directory.mkdir()
    if process_origin == "adopt":
        (owner.directory / "service.json").write_text(instance.model_dump_json(), encoding="utf-8")
    sink = {"path": str(tmp_path / "logs/extension-management.log"), "owner_id": uuid4().hex,
        "role": "extension-management", "source": "gateway", "device": "1", "inode": "2"}
    open_log = Mock(return_value=Mock(spec=ProcessLogFile, sink=sink))
    startup_replies = {
        "/health": httpx.Response(200, json={**instance.model_dump(mode="json"), "status": "ok"}),
        "/internal/runtime-logging": httpx.Response(200, json={"data": {
            "service": "extension-management", "pid": process.pid, "runtime_instance_id": instance.runtime_instance_id,
            "applied_revision": "initial", "level": "info", "pretty": False, "log_sink": sink}}),
    }
    replies: asyncio.Queue[httpx.Response] = asyncio.Queue()
    requests: list[httpx.Request] = []
    command_sent = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-Local-Token"] == "original-runtime-token"
        if request.url.path in startup_replies:
            assert request.method == "GET"
            return startup_replies[request.url.path]
        assert request.method == "POST"
        if request.url.path == "/shutdown":
            events.append("http-shutdown")
            process.alive = False
            return httpx.Response(200, json={"drained": True})
        assert request.url.path in {"/management", "/publication"}
        command_sent.set()
        return await replies.get()

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)
    close_client = client.aclose

    async def observe_client_close() -> None:
        events.append("client-close")
        await close_client()

    class ProcessHandleFake:
        def __init__(self, pid: int, log_ownership: ProcessLogOwnership | None = None) -> None:
            assert pid == process.pid
            self.pid, self.log_ownership = pid, log_ownership

        def is_alive(self) -> bool:
            return process.is_alive()

        def close(self, *, timeout_seconds: float = 8) -> None:
            process.close(timeout_seconds=timeout_seconds)

        def detach(self) -> None:
            process.detach()

    class ExistingProcessFake(ProcessHandleFake):
        pass

    class SpawnedProcessFake(ProcessHandleFake):
        def __init__(self, *, process: ManagementProcessFake, log_file: ProcessLogFile) -> None:
            super().__init__(process.pid)

        def confirm_service_identity(self, pid: int) -> None:
            assert pid == self.pid and owner.instance is None
            events.append("identity-confirmed")

    def spawn_service(*_args: object, **_kwargs: object) -> ManagementProcessFake:
        (owner.directory / "service.json").write_text(instance.model_dump_json(), encoding="utf-8")
        return process

    monkeypatch.setattr(client, "aclose", observe_client_close)
    client_factory = Mock(return_value=client)
    monkeypatch.setattr(extension_management.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(extension_management, "get_gateway_local_token", lambda: "original-runtime-token")
    monkeypatch.setattr(extension_management, "AdoptedManagedProcess", ExistingProcessFake)
    monkeypatch.setattr(extension_management, "ManagedProcess", SpawnedProcessFake)
    spawn = Mock(side_effect=spawn_service if process_origin == "spawn" else AssertionError("已有服务不能另启管理进程"))
    monkeypatch.setattr(extension_management, "_spawn_logged_process", spawn)
    adopt_log = Mock(return_value=Mock(spec=ProcessLogOwnership))
    monkeypatch.setattr(extension_management.ProcessLogStore, "adopt", adopt_log)
    monkeypatch.setattr(extension_management.ProcessLogStore, "open", open_log)
    try:
        yield HttpControl(owner, process, client, instance, replies, startup_replies, requests,
            command_sent, events, spawn, adopt_log, open_log, client_factory)
    finally:
        # 没有真实子进程；停止由各用例显式驱动，teardown 不另开一次停机预算。
        await client.aclose()


@pytest.fixture
def publication(tmp_path: Path) -> NativeExtensionPublication:
    root = tmp_path / "extensions"
    package = root / EXTENSION_KEY
    package.mkdir(parents=True)
    (package / "extension.js").write_bytes(b"target-python-content")
    return NativeExtensionPublication(extension_id="publisher.probe", extension_key=package.name,
        native_uri=package.as_uri(), extensions_root_uri=root.as_uri())


@pytest.fixture
def file_command() -> dict[str, JsonValue]:
    return {"method": "extension-file-read", "profile_id": "default", "extension_id": "publisher.probe",
        "extension_key": EXTENSION_KEY, "current_profile_member": False, "relative_segments": ["extension.js"]}


@dataclass
class BlockedRead:
    stream: PublicationStream
    entered: asyncio.Event
    release: threading.Event
    results: list[NativeFileStat | NativeFileRead]


@pytest.fixture
async def blocked_read(control: HttpControl, publication: NativeExtensionPublication,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[BlockedRead]:
    stream = control.publish(publication)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    native_read = extension_management.publication_file
    results: list[NativeFileStat | NativeFileRead] = []

    def read(publication: NativeExtensionPublication, *, extension_key: str, relative_segments: JsonValue,
        file_operation: Literal["stat", "read"],
    ) -> NativeFileStat | NativeFileRead:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "测试未释放原 Python 读取"
        result = native_read(publication, extension_key=extension_key,
            relative_segments=relative_segments, file_operation=file_operation)
        results.append(result)
        control.events.append("worker-finished")
        return result

    monkeypatch.setattr(extension_management, "publication_file", read)
    try:
        yield BlockedRead(stream, entered, release, results)
    finally:
        release.set()


async def test_existing_http_service_is_adopted_without_starting_another_process(control: HttpControl) -> None:
    await control.owner.start()
    assert control.owner.instance == control.instance
    assert control.owner.process is not None and control.owner.process.pid == control.process.pid
    assert [(request.method, request.url.path) for request in control.requests] == [
        ("GET", "/health"), ("GET", "/internal/runtime-logging")]
    control.adopt_log.assert_called_once()
    control.spawn.assert_not_called()
    control.owner.assert_healthy()


async def test_cancelled_management_request_settles_original_http_receipt_before_next_request(control: HttpControl) -> None:
    c = control
    await c.owner.start()
    first = asyncio.create_task(c.owner.request({"method": "install", "vsix_path": "/target/a.vsix"}))
    following: asyncio.Task[dict[str, JsonValue]] | None = None
    inventory: dict[str, JsonValue] = {"profile_id": "default", "target_platform": "linux-x64", "extensions": [{"source": "profile",
        "extension_key": EXTENSION_KEY, "description": {"id": "publisher.probe"}, "is_valid": True, "validations": [],
        "metadata": {}, "readme_path": None, "changelog_path": None}]}
    try:
        await asyncio.wait_for(c.command_sent.wait(), 5)
        first.cancel()
        following = asyncio.create_task(c.owner.request({"method": "inventory"}))
        done, _ = await asyncio.wait((first, following), timeout=0.05)
        assert not done and len(c.commands) == 1
    finally:
        c.reply({"result": {"installed": {"id": "publisher.probe", "version": "1.0.0",
            "profile_id": "default", "reused": False}, "inventory": inventory}})
        c.reply({"result": inventory})
        await asyncio.gather(first, *(() if following is None else (following,)), return_exceptions=True)
    with pytest.raises(asyncio.CancelledError):
        first.result()
    assert following is not None and following.result() == {"result": inventory}
    assert [json.loads(request.content)["method"] for request in c.commands] == ["install", "inventory"]
    c.owner.assert_healthy()


@pytest.mark.parametrize("process_origin", ["spawn", "adopt"])
async def test_unknown_http_receipt_is_not_replayed_or_restarted(
    control: HttpControl, process_origin: Literal["spawn", "adopt"],
) -> None:
    c = control
    await c.owner.start()
    c.events.clear()
    c.reply({"unknown": True})
    with pytest.raises(ValueError, match="回执不完整") as original:
        await c.owner.request({"method": "install", "vsix_path": "/target/a.vsix"})
    with pytest.raises(RuntimeError, match="不自动重启或重放") as failed:
        c.owner.assert_healthy()
    assert failed.value.__cause__ is original.value
    with pytest.raises(RuntimeError, match="不自动重启或重放"):
        await c.owner.request({"method": "inventory"})
    with pytest.raises(RuntimeError, match="拒绝重试"):
        await c.owner.start()
    assert [json.loads(request.content)["method"] for request in c.commands] == ["install"]
    assert c.spawn.call_count == (1 if process_origin == "spawn" else 0)
    await c.owner.close()
    assert c.events == ["http-shutdown", "process-close", "client-close"]
    assert c.process.close_timeouts == [0] and c.owner.drained


@pytest.mark.parametrize("process_origin", ["spawn", "adopt"])
async def test_unreachable_shutdown_reports_failure_without_claiming_process_drain(
    control: HttpControl, monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = control
    await c.owner.start()
    c.reply({"unknown": True})
    with pytest.raises(ValueError, match="回执不完整"):
        await c.owner.request({"method": "install", "vsix_path": "/target/a.vsix"})
    c.events.clear()
    original = httpx.ConnectError("扩展管理 shutdown 无法连接")

    async def unavailable(*_args: object, **_kwargs: object) -> httpx.Response:
        raise original

    monkeypatch.setattr(c.client, "post", unavailable)
    with pytest.raises(httpx.ConnectError) as closing:
        await asyncio.wait_for(c.owner.close(), timeout=1)
    assert closing.value is original and not c.owner.drained
    assert c.owner.process is not None and c.process.alive
    assert c.process.close_timeouts == [] and c.process.detach_calls == 0
    assert not c.client.is_closed and c.events == []


async def test_cancelled_python_read_holds_publication_stream_and_next_http_request(
    control: HttpControl, blocked_read: BlockedRead, file_command: dict[str, JsonValue],
) -> None:
    c, read = control, blocked_read
    await c.owner.start()
    reading = asyncio.create_task(c.owner.request(file_command))
    following: asyncio.Task[dict[str, JsonValue]] | None = None
    try:
        await asyncio.wait_for(read.entered.wait(), 5)
        reading.cancel()
        following = asyncio.create_task(c.owner.request({"method": "inventory"}))
        done, _ = await asyncio.wait((reading, following), timeout=0.05)
        assert not done and len(c.commands) == 1
        assert not read.stream.closed.is_set() and not c.client.is_closed
        assert c.commands[0].url.path == "/publication"
        assert json.loads(c.commands[0].content) == {key: value for key, value in file_command.items()
            if key not in {"method", "relative_segments"}} | {"method": "extension-publication"}
    finally:
        read.release.set()
        c.reply({"result": {"profile_id": "after-read", "target_platform": "linux-x64", "extensions": []}})
        await asyncio.gather(reading, *(() if following is None else (following,)), return_exceptions=True)
    with pytest.raises(asyncio.CancelledError):
        reading.result()
    assert following is not None and following.result() == {"result": {"profile_id": "after-read", "target_platform": "linux-x64", "extensions": []}}
    assert len(read.results) == 1 and isinstance(read.results[0], NativeFileRead)
    assert b64decode(read.results[0].content_base64) == b"target-python-content"
    assert c.events.index("worker-finished") < c.events.index("publication-close")
    assert len(c.commands) == 2
    c.owner.assert_healthy()


@pytest.mark.parametrize("typed_call", [False, True], ids=["http-receipt", "typed-call"])
async def test_missing_package_file_preserves_native_error_and_owner_health(
    control: HttpControl, publication: NativeExtensionPublication, file_command: dict[str, JsonValue], typed_call: bool,
) -> None:
    c = control
    await c.owner.start()
    stream = c.publish(publication)
    command = {**file_command, "relative_segments": ["missing.js"]}
    if typed_call:
        with pytest.raises(ExtensionManagementError) as missing:
            await c.owner.call(command, response_type=NativeFileRead)
        assert missing.value.code == "EntryNotFound" and "missing.js" in str(missing.value)
    else:
        result = await c.owner.request(command)
        assert set(result) == {"error", "code"}
        assert result["code"] == "EntryNotFound" and "missing.js" in str(result["error"])
    assert stream.closed.is_set()
    c.owner.assert_healthy()
    c.reply({"result": {"profile_id": "default", "target_platform": "linux-x64", "extensions": []}})
    assert await c.owner.request({"method": "inventory"}) == {"result": {"profile_id": "default", "target_platform": "linux-x64", "extensions": []}}


async def test_preserve_close_waits_for_worker_before_closing_client_or_detaching(
    control: HttpControl, blocked_read: BlockedRead, file_command: dict[str, JsonValue],
) -> None:
    c, read = control, blocked_read
    await c.owner.start()
    reading = asyncio.create_task(c.owner.request(file_command))
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(read.entered.wait(), 5)
        closing = asyncio.create_task(c.owner.close(preserve=True))
        done, _ = await asyncio.wait((closing,), timeout=0.05)
        assert not done and not c.client.is_closed and c.process.detach_calls == 0
        assert not read.stream.closed.is_set() and c.process.alive
        with pytest.raises(RuntimeError, match="未运行"):
            c.owner.assert_healthy()
    finally:
        read.release.set()
        await asyncio.gather(reading, *(() if closing is None else (closing,)), return_exceptions=True)
    assert NativeFileRead.model_validate(reading.result()["result"]) == read.results[0]
    assert closing is not None and closing.result() is None
    assert c.events == ["worker-finished", "publication-close", "client-close", "process-detach"]
    assert c.owner.drained and c.client.is_closed and c.process.alive
    assert c.process.close_timeouts == [] and c.process.detach_calls == 1
    assert not any(request.url.path == "/shutdown" for request in c.requests)


@pytest.fixture
def install_request_id() -> Iterator[str]:
    token = set_current_request_id("original-install-request")
    try:
        yield "original-install-request"
    finally:
        reset_current_request_id(token)


@pytest.fixture
def environment_reply(control: HttpControl) -> dict[str, JsonValue]:
    return {"result": {"app_root_uri": "file:///target/app", "global_storage_home_uri": "file:///target/global",
        "workspace_storage_home_uri": "file:///target/workspaces", "untitled_workspaces_home_uri": "file:///target/untitled",
        "profile_id": "default", "settings_native_uri": "file:///target/settings.json",
        "reference_service": {"url": control.instance.base_url + "/references", "backend_id": control.owner.backend_id,
            "runtime_instance_id": control.instance.runtime_instance_id}}}


@pytest.mark.parametrize("known_error", [False, True])
@pytest.mark.parametrize("method", ["install", "install-gallery"])
async def test_download_capability_is_bound_to_original_request_and_revoked_at_terminal_receipt(
    control: HttpControl, install_request_id: str, known_error: bool, method: str,
) -> None:
    c = control
    await c.owner.start()
    command: dict[str, JsonValue] = ({"method": "install", "vsix_path": "/target/a.vsix"} if method == "install" else
        {"method": "install-gallery", "extension_id": "publisher.probe", "version": "1.0.0", "options": {}})
    installing = asyncio.create_task(c.owner.request(command,
        download_base_url="http://127.0.0.1:40500"))
    try:
        await asyncio.wait_for(c.command_sent.wait(), 1)
        transport = json.loads(c.commands[-1].content)["request_transport"]
        capability = transport["url"].rsplit("/", 1)[1]
        assert transport == {"url": f"http://127.0.0.1:40500/api/gateway/internal/extension-management/{c.owner.backend_id}/downloads/{capability}",
            "request_id": install_request_id}
        c.owner.authorize_download(capability, install_request_id)
        for rejected in ((capability, "another-request"), ("another-capability", install_request_id)):
            with pytest.raises(PermissionError):
                c.owner.authorize_download(*rejected)
    finally:
        c.reply({"error": "依赖不可用", "code": "DownloadFailed"} if known_error else {
            "result": {"installed": {"id": "publisher.probe", "version": "1.0.0", "profile_id": "default", "reused": False},
                "inventory": {"profile_id": "default", "target_platform": "linux-x64", "extensions": []}}})
        await installing
    with pytest.raises(PermissionError):
        c.owner.authorize_download(capability, install_request_id)
    c.owner.assert_healthy()


@pytest.mark.parametrize("barrier_fails", [False, True])
@pytest.mark.parametrize("quiesce_first", [False, True], ids=["preserve-close", "pre-listener-quiesce"])
async def test_unknown_install_keeps_download_until_original_node_queue_settles_before_handoff(
    control: HttpControl, install_request_id: str, environment_reply: dict[str, JsonValue],
    barrier_fails: bool, quiesce_first: bool,
) -> None:
    c = control
    await c.owner.start()
    c.reply({"unknown": True})
    with pytest.raises(ValueError, match="回执不完整"):
        await c.owner.request({"method": "install", "vsix_path": "/target/a.vsix"},
            download_base_url="http://127.0.0.1:40500")
    capability = json.loads(c.commands[-1].content)["request_transport"]["url"].rsplit("/", 1)[1]
    c.owner.authorize_download(capability, install_request_id)
    c.command_sent.clear()
    closing = asyncio.create_task(c.owner.quiesce() if quiesce_first else c.owner.close(preserve=True))
    try:
        await asyncio.wait_for(c.command_sent.wait(), 1)
        assert not closing.done() and not c.client.is_closed and c.process.detach_calls == 0
        c.owner.authorize_download(capability, install_request_id)
        assert [json.loads(request.content)["method"] for request in c.commands] == ["install", "environment"]
    finally:
        c.reply({"unknown": True} if barrier_fails else environment_reply)
        await asyncio.gather(closing, return_exceptions=True)
    if barrier_fails:
        with pytest.raises(ValueError, match="交接没有确认"):
            closing.result()
        assert not c.owner.drained and not c.client.is_closed and c.process.detach_calls == 0
        c.owner.authorize_download(capability, install_request_id)
        # 原查询失败不能声称结清；完整停机仍通过原 shutdown 核实际 drain。
        await c.owner.close(preserve=False)
    else:
        closing.result()
        if quiesce_first:
            assert not c.owner.drained and not c.client.is_closed and c.process.alive and c.process.detach_calls == 0
            with pytest.raises(PermissionError):
                c.owner.authorize_download(capability, install_request_id)
            await c.owner.close(preserve=True)
        assert c.owner.drained and c.client.is_closed and c.process.alive and c.process.detach_calls == 1
    with pytest.raises(PermissionError):
        c.owner.authorize_download(capability, install_request_id)


async def test_pre_listener_quiesce_timeout_keeps_unsettled_download_and_service_identity(
    control: HttpControl, install_request_id: str,
) -> None:
    c = control
    await c.owner.start()
    c.reply({"unknown": True})
    with pytest.raises(ValueError, match="回执不完整"):
        await c.owner.request({"method": "install", "vsix_path": "/target/a.vsix"},
            download_base_url="http://127.0.0.1:40500")
    capability = json.loads(c.commands[-1].content)["request_transport"]["url"].rsplit("/", 1)[1]
    with pytest.raises(TimeoutError):
        await c.owner.quiesce()
    assert not c.owner.drained and not c.client.is_closed and c.process.alive and c.process.detach_calls == 0
    c.owner.authorize_download(capability, install_request_id)
    requests = len(c.requests)
    with pytest.raises(TimeoutError, match="停机总期限已耗尽"):
        await c.owner.close(preserve=True)
    assert len(c.requests) == requests and not c.owner.drained
    c.owner.authorize_download(capability, install_request_id)


@pytest.mark.parametrize("quiesce_first", [False, True])
async def test_full_shutdown_cannot_restart_budget_after_quiesce_or_http_drain(
    control: HttpControl, monkeypatch: pytest.MonkeyPatch, quiesce_first: bool,
) -> None:
    c = control
    await c.owner.start()
    c.owner._operation_timeout = 0.3
    stages: list[str] = []
    exit_process: asyncio.Task[None] | None = None

    async def delayed_exit() -> None:
        await asyncio.sleep(0.2)
        c.process.alive = False

    async def shutdown(*_args: object, **_kwargs: object) -> httpx.Response:
        nonlocal exit_process
        stages.append("shutdown-started")
        await asyncio.sleep(0.2)
        stages.append("shutdown-drained")
        exit_process = asyncio.create_task(delayed_exit())
        return httpx.Response(200, json={"drained": True}, request=httpx.Request("POST", c.instance.base_url + "/shutdown"))

    monkeypatch.setattr(c.client, "post", shutdown)
    if quiesce_first:
        await c.owner.quiesce()
        await asyncio.sleep(0.2)
    try:
        with pytest.raises(TimeoutError):
            await c.owner.close()
        assert stages == (["shutdown-started"] if quiesce_first else ["shutdown-started", "shutdown-drained"])
        assert not c.owner.drained and c.process.alive and not c.client.is_closed
        assert not c.process.close_timeouts and c.process.detach_calls == 0
    finally:
        if exit_process is not None:
            exit_process.cancel()
            await asyncio.gather(exit_process, return_exceptions=True)


@pytest.mark.parametrize("failure", ["health-http", "health-identity", "logging-identity", "log-ownership"])
async def test_failed_existing_service_candidate_is_never_stopped_or_detached(control: HttpControl, failure: str) -> None:
    c = control
    record = (c.owner.directory / "service.json").read_bytes()
    if failure == "health-http":
        c.startup_replies["/health"] = httpx.Response(503, json={"error": "候选服务健康失败"})
        error_type, message = RuntimeError, "健康证明失败"
    elif failure == "health-identity":
        c.startup_replies["/health"] = httpx.Response(200, json={**c.instance.model_dump(mode="json"),
            "runtime_instance_id": "another-instance", "status": "ok"})
        error_type, message = RuntimeError, "健康身份"
    elif failure == "logging-identity":
        payload = c.startup_replies["/internal/runtime-logging"].json()
        payload["data"]["runtime_instance_id"] = "another-instance"
        c.startup_replies["/internal/runtime-logging"] = httpx.Response(200, json=payload)
        error_type, message = RuntimeError, "日志状态"
    else:
        c.adopt_log.side_effect = OSError("候选日志归属未核实")
        error_type, message = OSError, "日志归属未核实"
    with pytest.raises(error_type, match=message):
        await c.owner.start()
    assert c.owner.process is None and c.owner.drained and c.client.is_closed
    assert c.process.alive and c.process.close_timeouts == [] and c.process.detach_calls == 0
    assert (c.owner.directory / "service.json").read_bytes() == record
    assert all(request.method == "GET" for request in c.requests)
    c.spawn.assert_not_called()
    requests_before_retry = len(c.requests)
    with pytest.raises(RuntimeError, match="拒绝重试"):
        await c.owner.start()
    assert len(c.requests) == requests_before_retry


@pytest.mark.parametrize("process_origin", ["spawn", "adopt"])
async def test_full_close_requires_http_drain_before_reaping_process(control: HttpControl) -> None:
    c = control
    await c.owner.start()
    c.events.clear()
    await c.owner.close()
    assert c.owner.drained and c.client.is_closed and not c.process.alive
    assert c.process.close_timeouts == [0] and c.process.detach_calls == 0
    assert c.events == ["http-shutdown", "process-close", "client-close"]
    assert [request.url.path for request in c.requests].count("/shutdown") == 1
    await c.owner.close()
    assert [request.url.path for request in c.requests].count("/shutdown") == 1


@pytest.mark.parametrize("process_origin", ["spawn"])
async def test_startup_credential_failure_does_not_open_logs_or_spawn(
    control: HttpControl, monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = control
    original = OSError("本机凭据读取失败")
    monkeypatch.setattr(extension_management, "get_gateway_local_token", Mock(side_effect=original))
    with pytest.raises(OSError) as failed:
        await c.owner.start()
    assert failed.value is original and c.owner.drained
    c.open_log.assert_not_called()
    c.spawn.assert_not_called()
    c.client_factory.assert_not_called()
    assert not c.owner._log_store.root.exists()
    assert not (c.owner.directory / "service.json").exists()


@pytest.mark.parametrize("process_origin", ["spawn"])
async def test_startup_health_retries_reuse_one_http_client(
    control: HttpControl, monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = control
    original_get = c.client.get
    failures = 0

    async def transient_get(url: str, *, headers: dict[str, str], follow_redirects: bool) -> httpx.Response:
        nonlocal failures
        assert not c.client.is_closed
        if url.endswith("/health") and failures < 2:
            failures += 1
            raise httpx.ConnectError("本机服务尚未开始监听")
        return await original_get(url, headers=headers, follow_redirects=follow_redirects)

    monkeypatch.setattr(c.client, "get", transient_get)
    await c.owner.start()
    assert failures == 2 and c.owner.instance == c.instance
    assert c.spawn.call_args.kwargs["cwd"] == c.owner.runtime.entrypoint.parent
    assert c.owner.directory.is_dir()
    c.client_factory.assert_called_once_with(trust_env=False, timeout=0.1)
    assert not c.client.is_closed
    c.owner.assert_healthy()


@pytest.mark.parametrize("process_origin", ["spawn"])
@pytest.mark.parametrize("failure", ["health-identity", "log-sink", "confirm-identity"])
async def test_unverified_spawn_is_reaped_without_shutting_down_an_unproven_service(
    control: HttpControl, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    c = control
    if failure == "health-identity":
        c.startup_replies["/health"] = httpx.Response(200, json={**c.instance.model_dump(mode="json"),
            "runtime_instance_id": "another-instance", "status": "ok"})
        error_type, message = RuntimeError, "健康身份"
    elif failure == "log-sink":
        payload = c.startup_replies["/internal/runtime-logging"].json()
        payload["data"]["log_sink"]["inode"] = "other-inode"
        c.startup_replies["/internal/runtime-logging"] = httpx.Response(200, json=payload)
        error_type, message = RuntimeError, "日志描述符"
    else:
        def fail_confirmation(pid: int) -> None:
            assert pid == c.process.pid and c.owner.instance is None
            raise OSError("原 child 身份尚未核实")

        monkeypatch.setattr(extension_management.ManagedProcess, "confirm_service_identity", staticmethod(fail_confirmation))
        error_type, message = OSError, "身份尚未核实"
    with pytest.raises(error_type, match=message):
        await c.owner.start()
    assert c.owner.instance is None and c.owner.drained and c.client.is_closed
    assert not c.process.alive and len(c.process.close_timeouts) == 1
    assert 0 < c.process.close_timeouts[0] <= 0.05
    assert c.process.detach_calls == 0
    assert not any(request.url.path == "/shutdown" for request in c.requests)
    assert c.events == ["process-close", "client-close"]
    c.spawn.assert_called_once()
    c.open_log.assert_called_once()


async def test_dead_service_rejects_requests_without_restarting(control: HttpControl) -> None:
    c = control
    await c.owner.start()
    c.process.alive = False
    with pytest.raises(RuntimeError, match="未运行"):
        c.owner.assert_healthy()
    with pytest.raises(RuntimeError, match="未运行"):
        await c.owner.request({"method": "inventory"})
    assert c.commands == []
    c.spawn.assert_not_called()


async def test_application_restart_replaces_dead_service_record_without_recovery_proofs(control: HttpControl) -> None:
    c = control
    record = c.owner.directory / "service.json"
    original = record.read_bytes()
    package = c.owner.directory / "extensions" / "retained-version" / "extension.js"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"original-extension")
    c.process.alive = False
    def spawn_service(*_args: object, **_kwargs: object) -> ManagementProcessFake:
        c.process.alive = True
        record.write_text(c.instance.model_dump_json(), encoding="utf-8")
        return c.process

    c.spawn.side_effect = spawn_service
    await c.owner.start()
    c.owner.assert_healthy()
    assert record.read_bytes() == original and package.read_bytes() == b"original-extension"
    assert c.owner.instance == c.instance
    c.spawn.assert_called_once()
    c.open_log.assert_called_once()
    c.adopt_log.assert_not_called()
    assert c.commands == []
    await c.owner.close()
    assert c.owner.drained and c.process.close_timeouts == [0]


@pytest.mark.parametrize("preserve", [False, True])
async def test_dead_service_shutdown_reaps_handle_without_waiting_for_http_drain(
    control: HttpControl, preserve: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    c = control
    await c.owner.start()
    c.owner._download_admission = ("unknown-install", "original-request")
    c.process.alive = False
    with pytest.raises(RuntimeError, match="请重启服务组或应用.*结果可能未知"):
        c.owner.assert_healthy()
    await c.owner.quiesce()
    await c.owner.close(preserve=preserve)
    assert c.owner.drained and c.owner.instance is None and c.client.is_closed
    assert c.process.close_timeouts == [0] and c.process.detach_calls == 0
    assert c.events == ["process-close", "client-close"]
    assert c.commands == [] and not any(request.method == "POST" for request in c.requests)
    assert "安装结果未知" in caplog.text and "不自动重放" in caplog.text
    c.spawn.assert_not_called()


async def test_management_consumer_receives_restart_guidance_after_service_crash(control: HttpControl) -> None:
    c = control
    await c.owner.start()
    c.process.alive = False
    application = FastAPI()
    application.state.extension_management = c.owner
    request = Request({"type": "http", "app": application})
    with pytest.raises(HTTPException) as failed:
        management_owner(request, c.owner.backend_id)
    assert failed.value.status_code == 503
    assert "请重启服务组或应用" in failed.value.detail and "不自动重放" in failed.value.detail
    assert isinstance(failed.value.__cause__, RuntimeError)
    assert c.commands == []
    c.spawn.assert_not_called()
    await c.owner.close()
