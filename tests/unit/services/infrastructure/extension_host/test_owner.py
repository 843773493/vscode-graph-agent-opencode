"""原宿主进程边界替身，验证准入、并发启动与关闭责任。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.services.infrastructure.extension_host.owner import (
    NodeExtensionHostOwner,
    NodeHostConnection,
    _HostSession,
)
from app.services.infrastructure.extension_host.process import NodeExtensionHost
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime


@dataclass
class Case:
    owner: NodeExtensionHostOwner
    workspace_ids: tuple[str, ...]
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    hosts: list[NodeExtensionHost] = field(default_factory=list)
    close_failures: list[OSError] = field(default_factory=list)


@pytest.fixture
async def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Case]:
    roots = [tmp_path / "A", tmp_path / "B"]
    for root in roots:
        root.mkdir()
    mounts = MountedWorkspaceRegistry([MountedWorkspace(str(uuid4()), root) for root in roots])
    runtime = NodeHostRuntime(Path(sys.executable), tmp_path / "host.mjs", "a" * 40, "fixture", "24.22.1")
    owner = NodeExtensionHostOwner(backend_id=str(uuid4()), mounted_workspaces=mounts,
        runtime=runtime, home=tmp_path / "home", environment={}, startup_timeout=2)
    result = Case(owner, mounts.workspace_ids)
    result.gate.set()

    @asynccontextmanager
    async def connect(host: NodeExtensionHost) -> AsyncIterator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
        result.hosts.append(host)
        result.started.set()
        original_close = host.child.close

        async def close() -> None:
            if result.close_failures:
                raise result.close_failures.pop(0)
            await original_close()

        monkeypatch.setattr(host.child, "close", close)
        try:
            await result.gate.wait()
            yield asyncio.StreamReader(), Mock(spec=asyncio.StreamWriter)
        finally:
            await host.child.close()

    monkeypatch.setattr(NodeExtensionHost, "connect", connect)
    # 测试的是owner的身份及生命周期；平台IPC由真实进程集成验证。
    monkeypatch.setattr(sys, "platform", "linux")
    try:
        yield result
    finally:
        result.gate.set()
        result.close_failures.clear()
        await owner.close()


@pytest.mark.parametrize("invalid", ["backend", "empty", "duplicate", "foreign", "client", "unavailable"])
async def test_invalid_target_or_view_never_opens_a_host(case: Case, invalid: str) -> None:
    ids = case.workspace_ids
    if invalid == "empty":
        ids = ()
    elif invalid == "duplicate":
        ids = (ids[0], ids[0])
    elif invalid == "foreign":
        ids = (str(uuid4()),)
    elif invalid == "unavailable":
        case.owner.runtime = None
    with pytest.raises((ValueError, KeyError, NotImplementedError)):
        async with case.owner.connect(gateway_id="gateway", client_id=" " if invalid == "client" else "client",
                view_id="view", expected_backend_id="wrong" if invalid == "backend" else case.owner.backend_id,
                workspace_ids=ids):
            pytest.fail("非法目标不能发布连接")
    assert not case.hosts and not case.owner.active


async def test_views_preserve_client_identity_and_independent_generations(case: Case) -> None:
    async with case.owner.connect(gateway_id="gateway", expected_backend_id=case.owner.backend_id,
            client_id="one", view_id="same", workspace_ids=case.workspace_ids) as first:
        async with case.owner.connect(gateway_id="gateway", expected_backend_id=case.owner.backend_id,
                client_id="two", view_id="same", workspace_ids=case.workspace_ids[::-1]) as second:
            assert first.identity.workspace_ids == case.workspace_ids
            assert second.identity.workspace_ids == case.workspace_ids[::-1]
            assert first.identity.generation != second.identity.generation
            with pytest.raises(ValueError, match="尚未关闭"):
                async with case.owner.connect(gateway_id="gateway", expected_backend_id=case.owner.backend_id,
                        client_id="one", view_id="same", workspace_ids=case.workspace_ids[:1]):
                    pytest.fail("不能覆盖已有视图")
        assert second.host.child.state.exit_verified
        assert not first.host.child.state.exit_verified
        assert case.owner.active == (first.identity,)
    assert not case.owner.active
    async with case.owner.connect(gateway_id="gateway", expected_backend_id=case.owner.backend_id,
            client_id="one", view_id="same", workspace_ids=case.workspace_ids) as restarted:
        assert restarted.identity.generation != first.identity.generation
    assert len(case.hosts) == 3


async def test_shutdown_during_start_cannot_publish_or_abandon_the_reserved_host(case: Case) -> None:
    case.gate.clear()

    async def connecting() -> None:
        async with case.owner.connect(gateway_id="gateway", expected_backend_id=case.owner.backend_id,
                client_id="client", view_id="view", workspace_ids=case.workspace_ids):
            pytest.fail("关闭期间不能发布连接")

    opening = asyncio.create_task(connecting())
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(case.started.wait(), 2)
        closing = asyncio.create_task(case.owner.close())
        await asyncio.sleep(0)
        assert not closing.done() and len(case.owner.active) == 1
        case.gate.set()
        with pytest.raises(RuntimeError, match="撤销准入"):
            await opening
        await closing
        assert not case.owner.active and case.hosts[0].child.state.exit_verified
    finally:
        case.gate.set()
        await asyncio.gather(*(task for task in (opening, closing) if task is not None), return_exceptions=True)


async def test_cancelled_start_releases_the_original_generation_and_log(case: Case) -> None:
    case.gate.clear()

    async def connecting() -> None:
        async with case.owner.connect(gateway_id="gateway", expected_backend_id=case.owner.backend_id,
                client_id="client", view_id="view", workspace_ids=case.workspace_ids):
            pytest.fail("取消启动不能发布连接")

    opening = asyncio.create_task(connecting())
    try:
        await asyncio.wait_for(case.started.wait(), 2)
        session = next(iter(case.owner._sessions.values()))
        opening.cancel("客户端断开")
        with pytest.raises(asyncio.CancelledError, match="客户端断开"):
            await opening
        assert not case.owner.active and case.hosts[0].child.state.exit_verified
        assert session._output is not None and session._output.closed
    finally:
        case.gate.set()
        await asyncio.gather(opening, return_exceptions=True)


async def test_cleanup_failure_preserves_original_error_generation_and_log_until_retry(case: Case) -> None:
    failure = OSError("原子进程退出未核实")
    original = ValueError("原RPC失败")
    connection: NodeHostConnection | None = None
    session: _HostSession | None = None
    with pytest.raises(BaseExceptionGroup) as caught:
        async with case.owner.connect(gateway_id="gateway", expected_backend_id=case.owner.backend_id,
                client_id="client", view_id="view", workspace_ids=case.workspace_ids) as connection:
            session = case.owner._sessions[connection.identity.generation]
            case.close_failures.append(failure)
            raise original
    assert caught.value.exceptions == (original, failure)
    assert connection is not None and session is not None
    assert case.owner.active == (connection.identity,)
    assert session._output is not None and not session._output.closed
    assert not connection.host.child.state.exit_verified
    await case.owner.close()
    assert not case.owner.active and connection.host.child.state.exit_verified
    assert session._output.closed and len(case.hosts) == 1
    with pytest.raises(RuntimeError, match="正在关闭"):
        async with case.owner.connect(gateway_id="gateway", expected_backend_id=case.owner.backend_id,
                client_id="new", view_id="view", workspace_ids=case.workspace_ids):
            pytest.fail("关闭后不能重建宿主")
