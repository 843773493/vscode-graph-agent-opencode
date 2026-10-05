"""启动控制器消费真实 identity CLI，并在 transient unit 消失时快速失败。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.workspaces import prepare_default_test_workspace


@pytest.fixture
def launcher_workspace(request: pytest.FixtureRequest) -> Path:
    return prepare_default_test_workspace(
        workspace_root=Path.cwd() / "out/tests/integration/test_development_launcher/workspace" / request.node.name,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )


@pytest.fixture
def bun_executable() -> str:
    executable = shutil.which("bun")
    if executable is None:
        raise RuntimeError("启动器验证必须提供 Bun")
    return executable


def _run_bun(bun: str, source: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bun, "-e", source, *arguments],
        cwd=Path.cwd(), env=os.environ.copy(), text=True, capture_output=True,
        timeout=10, check=False,
    )


def test_development_launcher_consumes_real_identity_cli_twice(
    launcher_workspace: Path, bun_executable: str,
) -> None:
    result = _run_bun(bun_executable, r"""
import { resolveBackendWorkspaceId } from "./scripts/launch/dev-environment.mjs";
const options = { projectRoot: process.cwd(), workspaceRoot: process.argv[1], pythonExecutable: process.argv[2] };
console.log(JSON.stringify([resolveBackendWorkspaceId(options), resolveBackendWorkspaceId(options)]));
""", str(launcher_workspace), sys.executable)
    assert result.returncode == 0, result.stderr
    first, second = json.loads(result.stdout)
    identity = json.loads((launcher_workspace / ".boxteam/workspace-identity.json").read_text())
    assert first == second == identity["workspace_id"]


@pytest.mark.parametrize("stdout,stderr,exit_code", [
    ('{"workspace_id":"00000000-0000-4000-8000-000000000001"}\n', "", 0),
    ("00000000-0000-4000-8000-000000000001", "", 0),
    ("00000000-0000-4000-8000-000000000001\nextra\n", "", 0),
    ("00000000-0000-4000-8000-00000000000A\n", "", 0),
    ("00000000-0000-4000-8000-000000000001\n", "warning", 0),
    ("", "failed", 2),
])
def test_development_launcher_rejects_noncanonical_cli_response(
    bun_executable: str, stdout: str, stderr: str, exit_code: int,
) -> None:
    result = _run_bun(bun_executable, r"""
import { resolveBackendWorkspaceId } from "./scripts/launch/dev-environment.mjs";
const [stdout, stderr, exitCode] = JSON.parse(process.argv[1]);
Bun.spawnSync = () => ({ stdout: Buffer.from(stdout), stderr: Buffer.from(stderr), exitCode });
try {
  resolveBackendWorkspaceId({ projectRoot: process.cwd(), workspaceRoot: "/unused", pythonExecutable: "/unused" });
  throw new Error("非法 CLI 响应被接受");
} catch (error) {
  if (!error.message.startsWith("Workspace backend UUID CLI")) throw error;
  console.log(error.message);
}
""", json.dumps([stdout, stderr, exit_code]))
    assert result.returncode == 0, result.stderr
    assert "Workspace backend UUID CLI" in result.stdout


@pytest.mark.parametrize("load,active,sub", [
    ("not-found", "inactive", "dead"),
    ("loaded", "failed", "failed"),
    ("loaded", "inactive", "dead"),
    ("loaded", "deactivating", "stop-sigterm"),
])
def test_ready_wait_rejects_exited_or_collected_unit_without_health_poll(
    bun_executable: str, load: str, active: str, sub: str,
) -> None:
    result = _run_bun(bun_executable, r"""
import { waitForGateway } from "./scripts/launch/dev-systemd.mjs";
const [load, active, sub] = JSON.parse(process.argv[1]);
let reads = 0;
Bun.spawnSync = () => {
  reads += 1;
  return { exitCode: 0, stdout: Buffer.from(`LoadState=${load}\nActiveState=${active}\nSubState=${sub}\nMainPID=0\n`), stderr: Buffer.alloc(0) };
};
globalThis.fetch = () => { throw new Error("退出的unit不能进入健康请求"); };
const started = performance.now();
try { await waitForGateway({ serviceName: "test.service", readyFile: "/unused" }); throw new Error("未拒绝"); }
catch (error) {
  if (!error.message.includes("就绪前已退出或被回收")) throw error;
  console.log(JSON.stringify({ reads, elapsedMs: performance.now() - started, error: error.message }));
}
""", json.dumps([load, active, sub]))
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["reads"] == 1
    assert receipt["elapsedMs"] < 1000


def test_ready_wait_does_not_accept_health_after_unit_disappears(
    launcher_workspace: Path, bun_executable: str,
) -> None:
    ready = launcher_workspace / "development-ready.json"
    ready.write_text('{"pid":1234}')
    result = _run_bun(bun_executable, r"""
import { waitForGateway } from "./scripts/launch/dev-systemd.mjs";
let reads = 0;
Bun.spawnSync = () => {
  reads += 1;
  const state = reads === 1
    ? "LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=1234\n"
    : "LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\n";
  return { exitCode: 0, stdout: Buffer.from(state), stderr: Buffer.alloc(0) };
};
globalThis.fetch = async () => ({ ok: true });
try { await waitForGateway({ serviceName: "test.service", readyFile: process.argv[1], gatewayHealthUrl: "http://unused" }); throw new Error("接受了退出unit的健康回执"); }
catch (error) {
  if (!error.message.includes("就绪前已退出或被回收")) throw error;
  console.log(JSON.stringify({ reads, error: error.message }));
}
""", str(ready))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["reads"] == 3

def test_development_launcher_accepts_crlf_but_rejects_extra_windows_line(
    bun_executable: str,
) -> None:
    result = _run_bun(bun_executable, r"""
import { resolveBackendWorkspaceId } from "./scripts/launch/dev-environment.mjs";
const id = "00000000-0000-4000-8000-000000000001";
let stdout = id + "\r\n";
Bun.spawnSync = () => ({ stdout: Buffer.from(stdout), stderr: Buffer.alloc(0), exitCode: 0 });
const options = { projectRoot: process.cwd(), workspaceRoot: "/unused", pythonExecutable: "/unused" };
const actual = resolveBackendWorkspaceId(options);
if (actual !== id) throw new Error("CRLF没有精确收窄为UUID");
stdout += "extra\r\n";
try { resolveBackendWorkspaceId(options); throw new Error("接受额外Windows行"); }
catch (error) { if (!error.message.includes("只输出一行标准 UUID")) throw error; }
console.log(actual);
""")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "00000000-0000-4000-8000-000000000001\n"
