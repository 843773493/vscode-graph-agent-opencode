"""通过真实 Node IPC 验证 Launcher 交接；不启动产品服务。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def control_probe(request: pytest.FixtureRequest) -> tuple[list[str], Path]:
    node = shutil.which("node")
    assert node is not None, "Launcher IPC 验收需要 Node"
    root = Path.cwd()
    context = TestRunContext.from_test_file(Path(request.node.path))
    run_id = uuid4().hex
    workspace = context.workspace_root / run_id
    workspace.mkdir(parents=True, exist_ok=False)
    output = context.artifacts_dir / run_id
    output.mkdir(parents=True, exist_ok=False)
    probe = workspace / "control-probe.mjs"
    probe.write_text(r'''
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
const { createGatewaySupervisorControl, requestGatewayHandoff, resolveGatewaySupervisorSocketPath } =
  await import(process.argv[2]);
const home = path.join(process.argv[3], "控制目录 " + "长目录".repeat(40));
const endpoint = resolveGatewaySupervisorSocketPath(home);
assert.equal(resolveGatewaySupervisorSocketPath(""), null);
assert.equal(resolveGatewaySupervisorSocketPath(home), endpoint);
assert.notEqual(resolveGatewaySupervisorSocketPath(home + "other"), endpoint);
if (process.platform === "win32") assert.ok(endpoint.startsWith("\\\\.\\pipe\\"));
else assert.ok(Buffer.byteLength(endpoint) <= 100);
const environment = {
  BOXTEAM_CONFIG_CANDIDATE_REF: "candidate-native",
  BOXTEAM_CONFIG_GENERATION: "generation-native",
  BOXTEAM_CONFIG_FENCING_TOKEN: "fence-native",
};
const calls = [];
const control = createGatewaySupervisorControl({ socketPath: endpoint, onHandoff: async payload => {
  calls.push(payload);
  return { accepted: true, target_generation: payload.target_generation };
}});
await control.listen();
let observed;
try {
  observed = await requestGatewayHandoff({ boxteamHome: home, environment, timeoutMs: 5000 });
  assert.deepEqual(observed, { handled: true, data: { accepted: true, target_generation: "generation-native" } });
  assert.deepEqual(calls, [{ type: "gateway_pending_handoff", candidate_ref: "candidate-native",
    target_generation: "generation-native", fencing_token: "fence-native" }]);
  if (process.platform === "win32") assert.equal(existsSync(path.join(home, "state")), false);
  else assert.equal(existsSync(endpoint), true);
} finally { await control.close(); }
if (process.platform !== "win32") assert.equal(existsSync(endpoint), false);
assert.deepEqual(await requestGatewayHandoff({ boxteamHome: home, environment, timeoutMs: 5000 }), { handled: false });
console.log(JSON.stringify({ platform: process.platform, endpoint, observed, calls, closed: true }));
''', encoding="utf-8")
    module = (root / "packages/launcher/src/process/gateway/handoff-control.mjs").as_uri()
    return [node, str(probe), module, str(workspace)], output


def test_gateway_launcher_native_control_endpoint(control_probe: tuple[list[str], Path]) -> None:
    command, output = control_probe
    result = subprocess.run(command, capture_output=True, timeout=20, check=False)
    (output / "stdout.raw").write_bytes(result.stdout)
    (output / "stderr.raw").write_bytes(result.stderr)
    (output / "result.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}, indent=2) + "\n")
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert json.loads(result.stdout)["closed"] is True
