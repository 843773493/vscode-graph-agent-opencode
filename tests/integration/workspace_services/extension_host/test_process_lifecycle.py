"""真实原生进程的标准流与宿主 IPC 启动失败；不替代完整 Backend 验收。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path

import pytest

from app.core.child_process import OwnedChildProcess
from app.core.shielded_cleanup import run_cleanup_shielded
from app.services.infrastructure.extension_host.process import NodeExtensionHost
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.extension_host.test_node_host import (
    node_host_runtime as node_host_runtime,  # noqa: PLC0414 - 复用显式匹配上游的运行时准入
)


@pytest.fixture
def process_context(request: pytest.FixtureRequest) -> TestRunContext:
    return TestRunContext.from_test_file(Path(request.node.path)).prepare()


async def test_native_standard_streams_write_the_complete_tail_before_verified_exit(
    process_context: TestRunContext,
) -> None:
    context = process_context
    log = context.artifacts_dir / "native-output.raw"
    owner = OwnedChildProcess("extension-host:output")
    script = ("import os,sys; os.write(1,b'A'*262144); os.write(2,b'B'*262144); "
              "sys.stdin.buffer.read(); os.write(1,b'last-stdout'); os.write(2,b'last-stderr')")
    with log.open("wb") as output:
        try:
            await owner.start([sys.executable, "-c", script], env=os.environ,
                              cwd=context.workspace_root, stdout=output, stderr=-2)
            assert owner.process is not None and owner.process.stdout is None
        finally:
            await owner.close()
        assert owner.state.exit_verified and owner.state.returncode == 0
        # 父进程的日志句柄不归子进程；close不能顺带释放调用者的文件。
        output.write(b'parent-tail')
    assert log.read_bytes() == b'A' * 262144 + b'B' * 262144 + b'last-stdoutlast-stderrparent-tail'
    (context.artifacts_dir / "native-output-process.json").write_text(
        json.dumps(asdict(owner.state), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
async def unconnected_host(node_host_runtime: tuple[str, str], process_context: TestRunContext,
                           request: pytest.FixtureRequest) -> AsyncIterator[tuple[NodeExtensionHost, Path, Path]]:
    if sys.platform != "linux":
        pytest.skip("UNMET_PREREQUISITE: 私有 IPC 当前仅验证 Linux")
    runtime = process_context.runtime_root_for_node(request.node.nodeid)
    runtime.mkdir(parents=True, exist_ok=True)
    source, log = runtime / "no-ipc.mjs", process_context.artifacts_dir / "startup.raw"
    source.write_text("import { writeFileSync } from 'node:fs';\n"
                      "writeFileSync('started', 'ready');\nconsole.log('waiting-for-ipc');\n"
                      "setInterval(() => {}, 1000);\n", encoding="utf-8")
    with log.open("wb") as output:
        host = NodeExtensionHost(request.node.nodeid,
            runtime=NodeHostRuntime(Path(node_host_runtime[0]), source, "0" * 40, "fixture", "24.22.1"),
            directory=runtime / "host", environment=os.environ, output=output, startup_timeout=2)
        try:
            yield host, source, log
        finally:
            await run_cleanup_shielded(host.child.close)


@pytest.mark.parametrize("mode", ["missing", "timeout", "cancel"])
async def test_ipc_start_failure_and_cancellation_settle_the_original_process(
    unconnected_host: tuple[NodeExtensionHost, Path, Path], mode: str,
) -> None:
    host, source, log = unconnected_host
    if mode == "missing":
        source.unlink()

    async def connect() -> None:
        async with host.connect():
            pytest.fail("无 IPC 的进程不应发布宿主连接")

    if mode == "cancel":
        connecting = asyncio.create_task(connect())
        try:
            async with asyncio.timeout(2):
                while not (source.with_name("host") / "started").is_file():
                    await asyncio.sleep(0.01)
        finally:
            connecting.cancel("用户取消宿主启动")
            with pytest.raises(asyncio.CancelledError, match="用户取消宿主启动"):
                await connecting
    else:
        with pytest.raises(RuntimeError if mode == "missing" else TimeoutError):
            await connect()
    assert host.child.state.exit_verified
    assert host.child.state.returncode == (1 if mode == "missing" else -15)
    assert not list(source.with_name("host").glob("ipc-*"))
    assert (b'Cannot find module' if mode == "missing" else b'waiting-for-ipc') in log.read_bytes()
    with pytest.raises(RuntimeError, match="不能重复启动"):
        await connect()
