"""Python 控制客户端与 Launcher 原控制 owner 的真实 IPC 合同。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app.core.logging_config import RuntimeLoggingConflictError
from app.gateway.runtime.logging_control import RuntimeLoggingProcessControl
from app.schemas.runtime_logging import RuntimeLoggingApply
from tests.harness.python.run_context import TestRunContext


@dataclass(frozen=True)
class LauncherControl:
    address: str
    pid: int
    artifacts: Path


@pytest.fixture
async def launcher_control(request: pytest.FixtureRequest) -> AsyncIterator[LauncherControl]:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    script = context.artifacts_dir / "launcher-control.mjs"
    script.write_text("""
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const [root, home] = process.argv.slice(2);
const { createGatewaySupervisorControl, gatewaySupervisorControlAddress } = await import(
  pathToFileURL(path.join(root, 'packages/launcher/src/gateway/control-channel.mjs')));
const { runtimeLoggingControl } = await import(
  pathToFileURL(path.join(root, 'src/shared/serverRuntimeLoggingControl.mjs')));
const { createRuntimeLogger, loadRuntimeLogContract } = await import(
  pathToFileURL(path.join(root, 'src/shared/serverRuntimeLogging.mjs')));
const contract = loadRuntimeLogContract(root);
const logger = createRuntimeLogger('launcher', 'control-fixture', process.stderr, contract);
const address = gatewaySupervisorControlAddress(home);
const control = createGatewaySupervisorControl({ address,
  onHandoff() { throw new Error('此装具只验证日志控制'); },
  onRuntimeLogging(payload) {
    return runtimeLoggingControl(payload, { service: 'launcher', token: 'fixture-private-token', contract, logger });
  },
});
await control.listen();
process.stdout.write(JSON.stringify({ address, pid: process.pid }) + '\\n');
process.stdin.resume();
process.stdin.once('end', async () => { await control.close(); });
""", encoding="utf-8")
    with (context.artifacts_dir / "launcher.stderr.raw").open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            "node", str(script), str(context.project_root), str(context.boxteam_home_for_node(request.node.nodeid)),
            cwd=context.workspace_root, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr,
        )
        try:
            assert process.stdout is not None
            line = await asyncio.wait_for(process.stdout.readline(), 5)
            (context.artifacts_dir / "launcher-ready.json").write_bytes(line)
            ready = json.loads(line)
            yield LauncherControl(ready["address"], ready["pid"], context.artifacts_dir)
        finally:
            assert process.stdin is not None
            process.stdin.close()
            try:
                code = await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise
            (context.artifacts_dir / "launcher-exit.json").write_text(json.dumps({"exit_code": code}) + "\n")
            assert code == 0


@pytest.mark.asyncio
async def test_real_launcher_control_confirms_identity_revision_and_settings(launcher_control: LauncherControl) -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        control = RuntimeLoggingProcessControl(client=client, token="fixture-private-token")
        target = {"address": launcher_control.address, "pid": launcher_control.pid}
        before = await control.launcher_status(**target)
        assert before.service == "launcher" and before.applied_revision is None
        request = RuntimeLoggingApply(
            runtime_instance_id=before.runtime_instance_id, revision="candidate-调试", expected_revision=None,
            level="DEBUG", pretty=False,
        )
        applied = await control.launcher_apply(**target, request=request)
        assert applied == before.model_copy(update={"applied_revision": request.revision, "level": "debug", "pretty": False})
        assert await control.launcher_apply(**target, request=request) == applied
        with pytest.raises(RuntimeLoggingConflictError, match="身份"):
            await control.launcher_status(address=launcher_control.address, pid=launcher_control.pid + 1)
        with pytest.raises(RuntimeError, match="revision"):
            await control.launcher_apply(**target, request=request.model_copy(update={"pretty": True}))
        restored = await control.launcher_apply(**target, request=request.model_copy(update={
            "revision": "restore", "expected_revision": request.revision, "level": before.level, "pretty": before.pretty,
        }))
        assert restored.runtime_instance_id == before.runtime_instance_id and restored.pid == before.pid
        assert restored.level == before.level and restored.pretty == before.pretty
        (launcher_control.artifacts / "observations.json").write_text(json.dumps({
            "before": before.model_dump(), "applied": applied.model_dump(), "restored": restored.model_dump(),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_real_launcher_control_rejects_invalid_private_token(launcher_control: LauncherControl) -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        control = RuntimeLoggingProcessControl(client=client, token="wrong-token")
        with pytest.raises(RuntimeError, match="invalid runtime control token"):
            await control.launcher_status(address=launcher_control.address, pid=launcher_control.pid)
