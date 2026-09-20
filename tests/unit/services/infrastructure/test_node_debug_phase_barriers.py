"""Node 调试固定端口与启动 phase barrier 的 5.5 验收。

本文件只验证两条删除/恢复边界：同一固定 Inspector 端口仍按精确 owner
隔离；spawn 后、PID 身份登记前 backend 崩溃时，重启恢复不能凭端口或复用
PID 认领/误杀其它进程。其余 launch claim 决策矩阵由
``test_node_debug_launch_claim.py`` 覆盖。
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationCreateRequest,
    NodeDebugLaunchClaimDTO,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.launch_claim import (
    claim_with_spawn_identity,
    new_launch_claim,
)
from app.services.infrastructure.node_debug.process_identity import (
    IDENTITY_SOURCE_LINUX_PROC,
    NodeDebugProcessIdentity,
)
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.node_debug.session_store import NodeDebugSessionStore
from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.support.node_debug_dependencies import (
    permissive_node_debug_session_admission,
)

_PARENT_SESSION_ID = "ses_00000000400040008000000000000001"
_OTHER_SESSION_ID = "ses_00000000400040008000000000000002"
_CONFIGURATION_ID = "dbgcfg_44444444444444444444444444444444"


class _FixedPortConfig:
    def __init__(self, port: int, *, timeout: float = 0.5) -> None:
        self._port = port
        self._timeout = timeout

    def get_debug_runtime_config(self) -> dict[str, object]:
        return {
            "enabled": True,
            "default_adapter": "node_inspector",
            "command_timeout_seconds": self._timeout,
            "node": {
                "inspector_host": "127.0.0.1",
                "inspector_port": self._port,
                "executable": "",
            },
            "python": {
                "adapter": "debugpy",
                "debugpy_host": "127.0.0.1",
                "debugpy_port": 0,
            },
            "launch_profiles": {
                "node-default": {
                    "adapter": "node_inspector",
                    "runtime": "node",
                    "program": "",
                    "working_directory": "",
                    "args": [],
                }
            },
        }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _create_session(sessions_root: Path, session_id: str) -> Path:
    return seed_catalog_session_bundle(sessions_root, session_id, title=session_id).directory


def _service(
    tmp_path: Path,
    resolver: object,
    *,
    port: int,
    timeout: float = 0.5,
) -> NodeDebugService:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(exist_ok=True)
    (workspace_root / "entry.mjs").write_text(
        "setInterval(() => {}, 1000);\n",
        encoding="utf-8",
    )
    return NodeDebugService(
        workspace_root=workspace_root,
        config_service=_FixedPortConfig(port, timeout=timeout),
        session_store=NodeDebugSessionStore(resolver),
        session_admission=permissive_node_debug_session_admission(),
        external_resource_leases=ExternalResourceLeaseLedger(),
    )


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js Inspector")
async def test_fixed_inspector_port_conflict_is_owner_isolated(tmp_path: Path) -> None:
    """固定端口冲突只失败当前 owner，先启动的 owner 保持运行。"""
    sessions_root = tmp_path / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_root)
    resolver.initialize()
    _create_session(sessions_root, _PARENT_SESSION_ID)
    _create_session(sessions_root, _OTHER_SESSION_ID)
    service = _service(tmp_path, resolver, port=_free_port())

    try:
        for session_id, name in (
            (_PARENT_SESSION_ID, "主方案"),
            (_OTHER_SESSION_ID, "另一个方案"),
        ):
            created = await service.create_configuration(
                NodeDebugConfigurationCreateRequest(
                    session_id=session_id,
                    thread_id="main",
                    name=name,
                    script_path="entry.mjs",
                )
            )
            assert created.active_configuration_name == name

        first = await service.start(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            path="entry.mjs",
            args=[],
            breakpoints=[],
        )
        assert first.status == "running"
        assert first.pid is not None

        with pytest.raises(RuntimeError, match="启动 Node Inspector 失败"):
            await service.start(
                session_id=_OTHER_SESSION_ID,
                thread_id="main",
                path="entry.mjs",
                args=[],
                breakpoints=[],
            )

        first_after_conflict = await service.get_state(_PARENT_SESSION_ID, "main")
        assert first_after_conflict.status == "running"
        assert first_after_conflict.pid == first.pid
        assert first_after_conflict.session_id == _PARENT_SESSION_ID
        assert first_after_conflict.thread_id == "main"
        second_claim = service._session_store.read_launch_claim(  # type: ignore[union-attr]
            _OTHER_SESSION_ID, "main"
        )
        assert second_claim is not None
        assert second_claim.phase == "settled"
    finally:
        await service.close()


class _SpawnPidBarrier(BaseException):
    """模拟 backend 在 spawn 后、PID 身份登记前退出。"""


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js Inspector")
async def test_spawn_pid_barrier_recovery_does_not_kill_reused_port_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """launch_pending 无 PID 时重启保持阻断，端口复用进程不得被误杀。"""
    sessions_root = tmp_path / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_root)
    resolver.initialize()
    _create_session(sessions_root, _PARENT_SESSION_ID)
    port = _free_port()
    service = _service(tmp_path, resolver, port=port, timeout=0.5)
    created = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            name="phase barrier",
            script_path="entry.mjs",
        )
    )
    assert created.active_configuration_id is not None

    def crash_after_spawn(pid: int):
        del pid
        raise _SpawnPidBarrier("backend 在 PID 登记前退出")

    monkeypatch.setattr(
        "app.services.infrastructure.node_debug.service.probe_process_identity",
        crash_after_spawn,
    )

    start_task = asyncio.create_task(
        service.start(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            path="entry.mjs",
            args=[],
            breakpoints=[],
        )
    )
    with pytest.raises(_SpawnPidBarrier):
        await start_task

    claim = service._session_store.read_launch_claim(  # type: ignore[union-attr]
        _PARENT_SESSION_ID, "main"
    )
    assert claim is not None
    assert claim.phase == "launch_pending"
    assert claim.pid is None
    runtime = service._runtimes[(_PARENT_SESSION_ID, "main")]
    process = runtime.process
    assert process is not None
    assert process.returncode is None

    # 新 backend 只能把“无 PID 的 launch_pending”提升为
    # reconcile_required，不能把同端口上碰巧存在的实例当成旧 owner。
    restored = _service(tmp_path, resolver, port=port, timeout=0.5)
    blocked = await restored.get_state(_PARENT_SESSION_ID, "main")
    assert blocked.status == "reconcile_required"
    blocked_claim = restored._session_store.read_launch_claim(  # type: ignore[union-attr]
        _PARENT_SESSION_ID, "main"
    )
    assert blocked_claim is not None
    assert blocked_claim.phase == "reconcile_required"

    # 停掉崩溃前留下的进程，但取消旧 monitor，保持 durable claim 的未知态；
    # 再以同端口启动一个无关实例，重复恢复不得触碰它。
    monitor = runtime.process_task
    if monitor is not None:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
    process.terminate()
    await process.wait()
    foreign = await asyncio.create_subprocess_exec(
        shutil.which("node") or "node",
        "--inspect-brk=127.0.0.1:" + str(port),
        str(service._workspace_root / "entry.mjs"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await asyncio.sleep(0.1)
        assert foreign.returncode is None
        still_blocked = await restored.get_state(_PARENT_SESSION_ID, "main")
        assert still_blocked.status == "reconcile_required"
        assert foreign.returncode is None
    finally:
        if foreign.returncode is None:
            foreign.terminate()
            await foreign.wait()
        await restored.close()
        await service.close()


def _spawned_claim(*, pid: int, marker: str) -> NodeDebugLaunchClaimDTO:
    identity = NodeDebugProcessIdentity(
        pid=pid,
        source=IDENTITY_SOURCE_LINUX_PROC,
        start_marker=marker,
    )
    return claim_with_spawn_identity(
        new_launch_claim(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            configuration_id=_CONFIGURATION_ID,
            inspector_host="127.0.0.1",
            inspector_port=9229,
        ),
        pid=pid,
        identity=identity,
    )
