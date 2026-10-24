from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.gateway.auth import verify_gateway_token
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.main import app
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime import port_forwarding
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.gateway.server.port_forwarding import get_port_forward_manager
from app.schemas.gateway.port_forwarding import CreatePortForwardRequest


class _FakeManagedProcess:
    def __init__(self) -> None:
        self.process = SimpleNamespace(poll=lambda: None)
        self.closed = False

    def close(self, *, timeout_seconds: float = 8) -> None:
        del timeout_seconds
        self.closed = True


def _registry(tmp_path: Path) -> GatewayWorkspaceRegistry:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert_remote_gateway(
        RemoteGatewayConnection(
            connection_id="rgw_server",
            name="Development server",
            host="server.example.com",
            port=22,
            username="developer",
            private_key_path="/keys/never-persist-content",
            ssh_config_host="development-alias",
            remote_gateway_port=8014,
            remote_gateway_id="remote-gateway",
            protocol_version=1,
            source_owner="manual",
        )
    )
    for workspace_id, root_path in (
        ("workspace_a", "/srv/a"),
        ("workspace_b", "/srv/b"),
    ):
        registry.upsert(
            WorkspaceTarget(
                workspace_id=workspace_id,
                name=workspace_id,
                root_path=None,
                backend_url="http://127.0.0.1:41000",
                connection_kind="remote_gateway",
                backend_workspace_id="00000000-0000-4000-8000-000000000002",
                remote_gateway_connection_id="rgw_server",
                remote_workspace_id=f"remote_{workspace_id}",
            ),
            activate=False,
        )
    return registry


def _manager(
    tmp_path: Path,
    registry: GatewayWorkspaceRegistry,
    calls: list[dict[str, object]],
    processes: list[_FakeManagedProcess],
) -> SshPortForwardManager:
    def starter(**kwargs: object):
        calls.append(kwargs)
        process = _FakeManagedProcess()
        processes.append(process)
        return process, tmp_path / "logs" / f"{kwargs['forward_id']}.log"

    manager = SshPortForwardManager(
        registry=registry,
        storage_path=tmp_path / "port-forwards.json",
        log_dir=tmp_path / "logs",
        process_starter=starter,
        port_allocator=lambda: 41234,
    )

    async def ready(*_: object) -> None:
        return None

    manager._wait_until_listening = ready  # type: ignore[method-assign]
    return manager


def _write_saved_forwards(
    storage_path: Path,
    forwards: list[tuple[str, str, int, int]],
) -> None:
    storage_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "items": [
                    {
                        "forward_id": forward_id,
                        "workspace_id": workspace_id,
                        "connection_id": "rgw_server",
                        "remote_host": "127.0.0.1",
                        "remote_port": remote_port,
                        "local_host": "127.0.0.1",
                        "local_port": local_port,
                        "protocol": "http",
                        "label": None,
                        "desired_running": True,
                    }
                    for forward_id, workspace_id, remote_port, local_port in forwards
                ],
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_create_binds_forward_to_workspace_and_reuses_ssh_alias(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    processes: list[_FakeManagedProcess] = []
    manager = _manager(tmp_path, _registry(tmp_path), calls, processes)

    created = await manager.create(
        "workspace_a",
        CreatePortForwardRequest(
            remote_port=5173,
            protocol="http",
            label="Vite",
        ),
    )

    assert created.workspace_id == "workspace_a"
    assert created.connection_id == "rgw_server"
    assert created.local_port == 41234
    assert created.local_url == "http://127.0.0.1:41234"
    assert created.status == "active"
    assert calls[0]["ssh_config_host"] == "development-alias"
    assert calls[0]["host"] == "server.example.com"
    assert calls[0]["username"] == "developer"
    forward = calls[0]["forward"]
    assert forward.remote_host == "127.0.0.1"
    assert forward.remote_port == 5173

    persisted = json.loads(
        (tmp_path / "port-forwards.json").read_text(encoding="utf-8")
    )
    assert persisted["items"][0]["workspace_id"] == "workspace_a"
    assert persisted["items"][0]["desired_running"] is True
    assert "private_key" not in json.dumps(persisted)
    if os.name != "nt":
        assert (tmp_path / "port-forwards.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_server_remote_port_has_single_workspace_owner(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, _registry(tmp_path), [], [])
    await manager.create(
        "workspace_a",
        CreatePortForwardRequest(remote_port=5173, local_port=41235),
    )

    with pytest.raises(ValueError, match="已归属于工作区 workspace_a"):
        await manager.create(
            "workspace_b",
            CreatePortForwardRequest(remote_port=5173, local_port=41236),
        )


@pytest.mark.asyncio
async def test_change_local_port_restarts_same_forward_and_persists_new_port(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    processes: list[_FakeManagedProcess] = []
    manager = _manager(tmp_path, _registry(tmp_path), calls, processes)
    created = await manager.create(
        "workspace_a",
        CreatePortForwardRequest(remote_port=5173, local_port=41235),
    )

    changed = await manager.change_local_port(
        "workspace_a",
        created.forward_id,
        41236,
    )

    assert changed.forward_id == created.forward_id
    assert changed.local_port == 41236
    assert changed.local_url == "http://127.0.0.1:41236"
    assert processes[0].closed is True
    assert calls[1]["forward"].local_port == 41236
    persisted = json.loads(
        (tmp_path / "port-forwards.json").read_text(encoding="utf-8")
    )
    assert persisted["items"][0]["local_port"] == 41236


@pytest.mark.asyncio
async def test_change_label_replaces_definition_and_persists_display_name(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, _registry(tmp_path), [], [])
    created = await manager.create(
        "workspace_a",
        CreatePortForwardRequest(remote_port=5173, local_port=41236, label="旧名称"),
    )

    changed = await manager.change_label("workspace_a", created.forward_id, "新名称")

    assert changed.label == "新名称"
    assert (await manager.list("workspace_a"))[0].label == "新名称"
    persisted = json.loads(
        (tmp_path / "port-forwards.json").read_text(encoding="utf-8")
    )
    assert persisted["items"][0]["label"] == "新名称"


@pytest.mark.asyncio
async def test_local_port_conflict_is_rejected_across_workspaces(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, _registry(tmp_path), [], [])
    await manager.create(
        "workspace_a",
        CreatePortForwardRequest(remote_port=5173, local_port=41237),
    )

    with pytest.raises(ValueError, match="本地端口 .* 已被转发"):
        await manager.create(
            "workspace_b",
            CreatePortForwardRequest(remote_port=8080, local_port=41237),
        )


@pytest.mark.asyncio
async def test_delete_closes_process_before_removing_persisted_definition(
    tmp_path: Path,
) -> None:
    processes: list[_FakeManagedProcess] = []
    manager = _manager(tmp_path, _registry(tmp_path), [], processes)
    created = await manager.create(
        "workspace_a",
        CreatePortForwardRequest(remote_port=5173, local_port=41238),
    )

    await manager.delete("workspace_a", created.forward_id)

    assert processes[0].closed is True
    assert await manager.list("workspace_a") == []
    persisted = json.loads(
        (tmp_path / "port-forwards.json").read_text(encoding="utf-8")
    )
    assert persisted["items"] == []


@pytest.mark.asyncio
async def test_reconcile_removes_forward_owned_by_stale_projection(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    processes: list[_FakeManagedProcess] = []
    manager = _manager(tmp_path, registry, [], processes)
    await manager.create(
        "workspace_a",
        CreatePortForwardRequest(remote_port=5173, local_port=41241),
    )

    registry.remove("workspace_a")
    await manager.reconcile_workspaces()

    assert processes[0].closed is True
    persisted = json.loads(
        (tmp_path / "port-forwards.json").read_text(encoding="utf-8")
    )
    assert persisted["items"] == []


@pytest.mark.asyncio
async def test_restore_failure_remains_visible_on_workspace_list(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    storage_path = tmp_path / "port-forwards.json"
    storage_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "items": [
                    {
                        "forward_id": "pf_stale",
                        "workspace_id": "workspace_a",
                        "connection_id": "rgw_replaced",
                        "remote_host": "127.0.0.1",
                        "remote_port": 5173,
                        "local_host": "127.0.0.1",
                        "local_port": 41239,
                        "protocol": "http",
                        "label": "Vite",
                        "desired_running": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = SshPortForwardManager(
        registry=_registry(tmp_path),
        storage_path=storage_path,
        log_dir=tmp_path / "logs",
    )

    await manager.restore()
    restored = await manager.list("workspace_a")

    assert restored[0].status == "error"
    assert "error_type=RuntimeError" in (restored[0].error or "")
    assert "绑定的远程 Gateway 已变化" in caplog.text
    with pytest.raises(RuntimeError, match="SSH 端口转发健康检查失败"):
        manager.assert_healthy()


@pytest.mark.asyncio
async def test_restore_starts_saved_forwards_concurrently(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    storage_path = tmp_path / "port-forwards.json"
    _write_saved_forwards(
        storage_path,
        [
            ("pf_1", "workspace_a", 5173, 41250),
            ("pf_2", "workspace_b", 3000, 41251),
        ],
    )
    calls: list[dict[str, object]] = []
    processes: list[_FakeManagedProcess] = []
    manager = _manager(tmp_path, registry, calls, processes)
    entered_ports: list[int] = []
    both_forwards_started = asyncio.Event()
    release_readiness = asyncio.Event()

    async def wait_for_listening(
        _process: _FakeManagedProcess,
        local_port: int,
    ) -> None:
        entered_ports.append(local_port)
        if len(entered_ports) == 2:
            both_forwards_started.set()
        await release_readiness.wait()

    manager._wait_until_listening = wait_for_listening  # type: ignore[method-assign]
    restore = asyncio.create_task(manager.restore())
    try:
        await asyncio.wait_for(both_forwards_started.wait(), timeout=0.5)
    except TimeoutError:
        release_readiness.set()
        await restore
        raise
    release_readiness.set()
    await restore

    assert sorted(entered_ports) == [41250, 41251]
    assert len(calls) == 2
    assert [item.status for item in await manager.list("workspace_a")] == ["active"]
    assert [item.status for item in await manager.list("workspace_b")] == ["active"]


@pytest.mark.asyncio
async def test_restore_cancellation_closes_started_forward(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    storage_path = tmp_path / "port-forwards.json"
    _write_saved_forwards(storage_path, [("pf_waiting", "workspace_a", 5173, 41252)])
    processes: list[_FakeManagedProcess] = []
    manager = _manager(tmp_path, registry, [], processes)
    readiness_started = asyncio.Event()

    async def wait_for_listening(
        _process: _FakeManagedProcess,
        _local_port: int,
    ) -> None:
        readiness_started.set()
        await asyncio.Future()

    manager._wait_until_listening = wait_for_listening  # type: ignore[method-assign]
    restore = asyncio.create_task(manager.restore())
    await asyncio.wait_for(readiness_started.wait(), timeout=0.5)

    restore.cancel()
    with pytest.raises(asyncio.CancelledError):
        await restore

    assert len(processes) == 1
    assert processes[0].closed is True
    restored = await manager.list("workspace_a")
    assert restored[0].status == "error"
    assert restored[0].error == "SSH 端口转发启动被取消"


@pytest.mark.asyncio
async def test_restore_limits_parallel_process_starts(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    storage_path = tmp_path / "port-forwards.json"
    forwards = [
        (
            f"pf_{index}",
            "workspace_a" if index % 2 == 0 else "workspace_b",
            1000 + index,
            41300 + index,
        )
        for index in range(port_forwarding._RESTORE_CONCURRENCY + 2)
    ]
    _write_saved_forwards(storage_path, forwards)
    calls: list[dict[str, object]] = []
    processes: list[_FakeManagedProcess] = []
    manager = _manager(tmp_path, registry, calls, processes)
    entered_count = 0
    peak_active = 0
    active = 0
    concurrency_reached = asyncio.Event()
    release_readiness = asyncio.Event()

    async def wait_for_listening(
        _process: _FakeManagedProcess,
        _local_port: int,
    ) -> None:
        nonlocal active, entered_count, peak_active
        active += 1
        entered_count += 1
        peak_active = max(peak_active, active)
        if entered_count == port_forwarding._RESTORE_CONCURRENCY:
            concurrency_reached.set()
        await release_readiness.wait()
        active -= 1

    manager._wait_until_listening = wait_for_listening  # type: ignore[method-assign]
    restore = asyncio.create_task(manager.restore())
    await asyncio.wait_for(concurrency_reached.wait(), timeout=0.5)
    await asyncio.sleep(0)

    assert entered_count == port_forwarding._RESTORE_CONCURRENCY
    assert peak_active == port_forwarding._RESTORE_CONCURRENCY

    release_readiness.set()
    await restore

    assert entered_count == len(forwards)
    assert peak_active == port_forwarding._RESTORE_CONCURRENCY
    assert len(calls) == len(forwards)


@pytest.mark.asyncio
async def test_failed_start_retains_process_when_close_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    processes: list[_FakeManagedProcess] = []
    manager = _manager(tmp_path, _registry(tmp_path), [], processes)
    close_attempts = 0

    def start_process(**_: object) -> tuple[_FakeManagedProcess, Path]:
        process = _FakeManagedProcess()
        processes.append(process)

        def close(*, timeout_seconds: float = 8) -> None:
            nonlocal close_attempts
            del timeout_seconds
            close_attempts += 1
            if close_attempts == 1:
                raise RuntimeError("close failed")
            process.closed = True

        process.close = close
        return process, tmp_path / "logs" / "failed-start.log"

    manager._process_starter = start_process

    async def readiness_failure(*_: object) -> None:
        raise TimeoutError("readiness failed")

    manager._wait_until_listening = readiness_failure  # type: ignore[method-assign]
    created = await manager.create(
        "workspace_a",
        CreatePortForwardRequest(remote_port=5173, local_port=41253),
    )

    assert created.status == "error"
    assert "error_type=RuntimeError" in (created.error or "")
    assert "close failed" in caplog.text
    assert processes[0].closed is False
    assert manager._runtimes[created.forward_id].process is processes[0]

    await manager.delete("workspace_a", created.forward_id)

    assert close_attempts == 2
    assert processes[0].closed is True


def test_log_tail_reads_bounded_utf8_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "large-ssh.log"
    content = "旧日志🚀\n" * 20_000 + "末尾错误🧭\n" * 1_000
    log_path.write_text(content, encoding="utf-8")
    max_tail_bytes = port_forwarding._LOG_TAIL_LIMIT * 4 + 3
    read_sizes: list[int] = []
    real_open = Path.open

    class ReadProbe:
        def __init__(self, stream) -> None:
            self._stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self._stream.close()

        def seek(self, *args):
            return self._stream.seek(*args)

        def tell(self) -> int:
            return self._stream.tell()

        def read(self, size: int) -> bytes:
            read_sizes.append(size)
            return self._stream.read(size)

    def open_probe(path: Path, *args, **kwargs):
        return ReadProbe(real_open(path, *args, **kwargs))

    def reject_full_read(*_args, **_kwargs):
        pytest.fail("日志尾部读取不得调用完整 read_text")

    monkeypatch.setattr(Path, "open", open_probe)
    monkeypatch.setattr(Path, "read_text", reject_full_read)

    actual = SshPortForwardManager._read_log_tail(log_path)

    assert actual == content[-port_forwarding._LOG_TAIL_LIMIT :].strip()
    assert read_sizes == [max_tail_bytes]


def test_port_forward_runtime_generation_protocol_is_reversible(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, _registry(tmp_path), [], [])

    assert manager.prepare_runtime_generation("gateway-generation") is None
    assert manager.apply_runtime_generation("gateway-generation") is None
    manager.promote_runtime_generation("gateway-generation")
    proof = manager.runtime_health_proof(generation="gateway-generation")
    assert proof.details["runtime_generation"] == "gateway-generation"
    manager.rollback_runtime_generation("gateway-generation", None)


def test_load_rejects_duplicate_persisted_local_port(tmp_path: Path) -> None:
    items = [
        {
            "forward_id": f"pf_{index}",
            "workspace_id": f"workspace_{index}",
            "connection_id": "rgw_server",
            "remote_host": "127.0.0.1",
            "remote_port": 5000 + index,
            "local_host": "127.0.0.1",
            "local_port": 41240,
            "protocol": "tcp",
            "label": None,
            "desired_running": True,
        }
        for index in range(2)
    ]
    storage_path = tmp_path / "port-forwards.json"
    storage_path.write_text(
        json.dumps({"schema_version": 1, "items": items}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="本地端口重复"):
        SshPortForwardManager(
            registry=_registry(tmp_path),
            storage_path=storage_path,
            log_dir=tmp_path / "logs",
        )


@pytest.mark.asyncio
async def test_local_workspace_is_rejected(tmp_path: Path) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="local",
            name="Local",
            root_path=str(tmp_path),
            backend_url="http://127.0.0.1:8010",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
        )
    )
    manager = SshPortForwardManager(
        registry=registry,
        storage_path=tmp_path / "port-forwards.json",
        log_dir=tmp_path / "logs",
    )

    with pytest.raises(ValueError, match="只支持远程 Gateway 投影工作区"):
        await manager.create(
            "local",
            CreatePortForwardRequest(remote_port=5173),
        )


def test_create_api_returns_complete_workspace_forward_list(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _registry(tmp_path), [], [])
    app.dependency_overrides[verify_gateway_token] = lambda: "test-token"
    app.dependency_overrides[get_port_forward_manager] = lambda: manager
    try:
        response = TestClient(app).post(
            "/api/gateway/workspaces/workspace_a/port-forwards",
            headers={"X-Request-ID": "req-port-forward"},
            json={
                "remote_port": 5173,
                "local_port": 41241,
                "protocol": "https",
                "label": "Preview",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["request_id"] == "req-port-forward"
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["workspace_id"] == "workspace_a"
    assert items[0]["connection_id"] == "rgw_server"
    assert items[0]["status"] == "active"
    assert items[0]["local_url"] == "https://127.0.0.1:41241"


@pytest.fixture
def forward_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, SshPortForwardManager, list[_FakeManagedProcess]]]:
    processes: list[_FakeManagedProcess] = []
    manager = _manager(tmp_path, _registry(tmp_path), [], processes)
    monkeypatch.setitem(app.dependency_overrides, verify_gateway_token, lambda: "test-token")
    monkeypatch.setitem(app.dependency_overrides, get_port_forward_manager, lambda: manager)
    client = TestClient(app)
    try:
        yield client, manager, processes
    finally:
        client.close()


@pytest.mark.parametrize("native_path", [
    "/srv/private/key.pem", r"C:\private\key.pem",
    r"\\server\private\key.pem", r"\\?\UNC\server\private\key.pem",
], ids=["posix", "drive", "unc", "extended-unc"])
@pytest.mark.parametrize("stage", ["spawn", "readiness", "exit", "close", "restore", "reconnect"])
def test_error_response_excludes_native_location(
    forward_http: tuple[TestClient, SshPortForwardManager, list[_FakeManagedProcess]],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    native_path: str, stage: str,
) -> None:
    client, manager, processes = forward_http
    log_path = tmp_path / "ssh.log"
    original_log = f"Identity file {native_path} is not accessible\n".encode()
    log_path.write_bytes(original_log)
    native_error = OSError(2, "SSH 文件访问失败", native_path)
    native_error.winerror = 206

    def start(**_: object) -> tuple[_FakeManagedProcess, Path]:
        if stage == "spawn":
            raise native_error
        process = _FakeManagedProcess()
        processes.append(process)
        return process, log_path

    async def failed_readiness(*_: object) -> None:
        raise native_error

    monkeypatch.setattr(manager, "_process_starter", start)
    if stage == "readiness":
        monkeypatch.setattr(manager, "_wait_until_listening", failed_readiness)
    endpoint = "/api/gateway/workspaces/workspace_a/port-forwards"
    headers = {"X-Request-ID": "req-forward-location"}
    response = client.post(endpoint, headers=headers, json={"remote_port": 5173, "local_port": 41241})
    assert response.status_code == 200
    item = response.json()["data"]["items"][0]
    forward_id = item["forward_id"]
    stored = (tmp_path / "port-forwards.json").read_bytes()
    original_close = processes[0].close if processes else None
    if stage == "exit":
        processes[0].process.poll = lambda: 27
    elif stage == "close":
        def failed_close(*, timeout_seconds: float = 8) -> None:
            del timeout_seconds
            raise native_error
        monkeypatch.setattr(processes[0], "close", failed_close)
        response = client.delete(f"{endpoint}/{forward_id}", headers=headers)
        assert response.status_code == 502
        assert manager._runtimes[forward_id].process is processes[0]
        assert processes[0].closed is False
    elif stage == "restore":
        asyncio.run(manager.close())
        monkeypatch.setattr(manager, "_wait_until_listening", failed_readiness)
        asyncio.run(manager.restore())
    elif stage == "reconnect":
        monkeypatch.setattr(manager, "_wait_until_listening", failed_readiness)
        response = client.post(f"{endpoint}/{forward_id}/reconnect", headers=headers)
        assert response.status_code == 200
        assert len(processes) == 2 and all(process.closed for process in processes)
    if stage != "close":
        response = client.get(endpoint, headers=headers)
        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["status"] == "error"
        assert item["forward_id"] == forward_id
        if stage == "exit":
            assert "returncode=27" in item["error"]
        else:
            assert "errno=2" in item["error"] and "winerror=206" in item["error"]
    else:
        assert "errno=2" in response.json()["detail"]
        assert "winerror=206" in response.json()["detail"]
    assert response.headers["X-Request-ID"] == headers["X-Request-ID"]
    assert response.json()["request_id"] == headers["X-Request-ID"]
    error_text = response.json()["detail"] if stage == "close" else item["error"]
    assert native_path not in error_text
    assert json.dumps(native_path)[1:-1] not in response.text
    assert "请查看 Gateway 原始诊断日志" in response.text
    assert log_path.read_bytes() == original_log
    assert (tmp_path / "port-forwards.json").read_bytes() == stored
    assert any(
        native_path in str(record.args)
        or isinstance(record.args, tuple) and any(native_path in str(arg) for arg in record.args)
        or record.exc_info and getattr(record.exc_info[1], "filename", None) == native_path
        for record in caplog.records
    )
    if stage == "close":
        monkeypatch.setattr(processes[0], "close", original_close)
        deleted = client.delete(f"{endpoint}/{forward_id}", headers=headers)
        assert deleted.status_code == 200 and deleted.json()["data"]["items"] == []
        assert len(processes) == 1 and processes[0].closed is True
    elif stage == "exit":
        recovered = client.post(f"{endpoint}/{forward_id}/reconnect", headers=headers)
        assert recovered.status_code == 200
        assert recovered.json()["data"]["items"][0]["forward_id"] == forward_id
        assert recovered.json()["data"]["items"][0]["status"] == "active"
        assert len(processes) == 2 and processes[0].closed is True


@pytest.mark.parametrize(("error_type", "status"), [(LookupError, 404), (ValueError, 409), (OSError, 502)])
def test_request_failure_preserves_status_without_native_detail(
    forward_http: tuple[TestClient, SshPortForwardManager, list[_FakeManagedProcess]],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    error_type: type[Exception], status: int,
) -> None:
    client, manager, _ = forward_http
    native_path = "/srv/private/port-forwards.json"
    original_error = error_type(native_path)

    async def fail(_: str) -> None:
        raise original_error

    monkeypatch.setattr(manager, "list", fail)
    response = client.get(
        "/api/gateway/workspaces/workspace_a/port-forwards",
        headers={"X-Request-ID": "req-forward-rejected"},
    )
    assert response.status_code == status
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == "req-forward-rejected"
    assert native_path not in response.text
    assert f"error_type={error_type.__name__}" in response.json()["detail"]
    assert any(record.exc_info and record.exc_info[1] is original_error for record in caplog.records)
    assert manager._definitions == {} and not manager._storage_path.exists()
