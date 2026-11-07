"""真实 Launcher 换代、早期候选失败和最终停机；不替换 Gateway lifespan。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import commentjson
import httpx
import psutil
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.workspace import terminate_process
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


@dataclass(frozen=True)
class LauncherCase:
    context: TestRunContext
    home: Path
    client: httpx.Client
    process: subprocess.Popen[str]
    environment: dict[str, str]

    def stop(self) -> dict[str, object]:
        # 真实产品入口；本测试是 Launcher 父进程，必须并行 reap 避免 POSIX 僵尸阻塞 stop。
        stopped = subprocess.Popen([self.environment["BOXTEAM_TEST_NODE_HOST_EXECUTABLE"],
            "packages/launcher/bin/boxteam.mjs", "stop"], env=self.environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            launcher_code = self.process.wait(timeout=160)
            stdout, stderr = stopped.communicate(timeout=10)
            receipt = {"exit_code": stopped.returncode, "stdout": stdout, "stderr": stderr,
                "launcher_exit_code": launcher_code}
            (self.context.artifacts_dir / "stop.json").write_text(json.dumps(receipt,
                ensure_ascii=False, indent=2), encoding="utf-8")
            assert stopped.returncode == 0, stderr
            assert "status=stopped" in stdout
            assert launcher_code == (0 if sys.platform == "win32" else 128)
            return receipt
        finally:
            terminate_process(stopped, timeout_seconds=10)


@pytest.fixture
def launcher_case(request: pytest.FixtureRequest) -> Iterator[LauncherCase]:
    executable = os.getenv("BOXTEAM_TEST_NODE_HOST_EXECUTABLE")
    modules = os.getenv("BOXTEAM_TEST_NODE_HOST_MODULES")
    if not executable or not modules:
        pytest.skip("UNMET_PREREQUISITE: 需显式提供固定 Node 及原扩展管理依赖")
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    component = context.runtime_root_for_node(request.node.nodeid) / "component"
    command = ["bun", "--eval", """
import {buildNodeExtensionHost} from './src/clients/workbench/scripts/nodeHost.mjs';
import {symlink} from 'node:fs/promises';
import {resolve,join} from 'node:path';
const [output,modules]=process.argv.slice(-2);
await buildNodeExtensionHost(resolve('src/clients/workbench'),output);
await symlink(resolve(modules),join(output,'node_modules'),'junction');
""", str(component), modules]
    build = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=60, check=False)
    (context.artifacts_dir / "build.json").write_text(json.dumps({"command": command,
        "exit_code": build.returncode, "stdout": build.stdout, "stderr": build.stderr}, indent=2), encoding="utf-8")
    assert build.returncode == 0, build.stderr
    root = context.project_root
    manifest = component.with_name("runtime-manifest.json")
    manifest.write_text(json.dumps({
        "schema_version": 1, "distribution": "source-development", "version": "0.0.2",
        "python_executable": sys.executable, "application_root": str(root),
        "config_resources": {key: str(root / "configs" / f"{key}.jsonc")
            for key in ("gateway_inline", "gateway_schema", "workspace_inline", "workspace_schema")},
        "skill_resources": str(root / "resources/skills"), "web_assets": None, "chromium_executable": None,
        "node": {"source": "launcher", "executable": None},
        "extension_host": {"node_executable": executable, "component_root": str(component)},
    }), encoding="utf-8")
    workspace = prepare_test_workspace(workspace_root=context.workspace_root / context.run_root.name,
        template_root=root / "tests/fixtures/workspaces/default_test_workspace", template_items=())
    install_test_workspace_config(workspace_root=workspace, config_path=root / "configs/tests/workspace/default.jsonc",
        schema_path=root / "configs/workspace_schema.jsonc")
    home = context.boxteam_home_for_node(request.node.nodeid)
    credential = home / "state/gateway/credentials/local-token"
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text("management-lifecycle-token\n", encoding="utf-8")
    credential.chmod(0o600)
    port = integration_port_block_for_file(Path(__file__)).port(0)
    environment = {**os.environ, "BOXTEAM_HOME": str(home), "BOXTEAM_RUNTIME_MANIFEST": str(manifest),
        "BOXTEAM_PROJECT_ROOT": str(root), "BOXTEAM_GATEWAY_PORT": str(port),
        "BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT": str(workspace), "WORKSPACE_ROOT": str(workspace)}
    for key in ("BOXTEAM_GATEWAY_ROOT", "BOXTEAM_DEFAULT_BACKEND_URL", "BOXTEAM_CONFIG_CANDIDATE_REF",
                "BOXTEAM_CONFIG_GENERATION", "BOXTEAM_CONFIG_FENCING_TOKEN"):
        environment.pop(key, None)
    with (context.artifacts_dir / "launcher.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([executable, "packages/launcher/bin/boxteam.mjs", "start", "--no-open"],
            cwd=root, env=environment, stdout=log, stderr=log, text=True)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False,
                              headers={"X-Local-Token": "management-lifecycle-token"}) as client:
                case = LauncherCase(context, home, client, process, environment)
                deadline = time.monotonic() + 60
                while True:
                    assert process.poll() is None, "Launcher 在 Gateway 就绪前退出"
                    try:
                        response = client.get("/api/gateway/health")
                    except httpx.TransportError:
                        response = None
                    if response is not None and response.status_code == 200:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("真实 Launcher Gateway 未按时就绪")
                    time.sleep(0.05)
                client.post("/api/gateway/users/guest", json={"tracking": {"test": True}}).raise_for_status()
                yield case
        finally:
            terminate_process(process, timeout_seconds=160)


def test_launcher_reuses_service_after_failed_candidate_and_successful_handoff_then_stops_it(
    launcher_case: LauncherCase,
) -> None:
    case = launcher_case
    evidence: dict[str, object] = {}
    manager: psutil.Process | None = None
    try:
        health = case.client.get("/api/gateway/health").raise_for_status().json()["data"]
        listing = case.client.get("/api/gateway/workspaces").raise_for_status().json()["data"]
        workspace = listing["items"][0]
        # 真正初始化 Backend，换代后同时核其服务身份未重建。
        backend = case.client.get(f"/api/v1/workspaces/{workspace['workspace_id']}/health",
            timeout=60).raise_for_status().json()
        [service_file] = (case.home / "state/backend/extension-management").glob("*/service.json")
        service = json.loads(service_file.read_text(encoding="utf-8"))
        manager = psutil.Process(service["process_id"])
        created_at = manager.create_time()
        evidence["original"] = {"gateway": health, "backend": backend, "service": service,
            "manager_created_at": created_at}
        target = {"protocol_version": 1, "gateway_id": health["gateway_id"], "backend_id": service["backend_id"]}

        def inventory() -> dict[str, object]:
            response = case.client.post("/api/gateway/extension-installations/inventory", json=target)
            response.raise_for_status()
            payload = response.json()
            assert response.headers["X-Request-ID"] == payload["request_id"]
            assert json.loads(service_file.read_text(encoding="utf-8")) == service
            assert manager is not None and manager.is_running() and manager.create_time() == created_at
            return payload["data"]

        original_inventory = inventory()
        rejected_stop = subprocess.run(["bun", "--eval", """
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {gatewaySupervisorControlAddress,requestGatewaySupervisorControl} from './packages/launcher/src/gateway/control-channel.mjs';
const home=process.env.BOXTEAM_HOME;
const lock=JSON.parse(readFileSync(join(home,'state/launcher.lock'),'utf8'));
for(const [token,launcher_token] of [['invalid-token',lock.token],['management-lifecycle-token','invalid-instance']]) {
  await assert.rejects(requestGatewaySupervisorControl({address:gatewaySupervisorControlAddress(home),timeoutMs:5000,
    payload:{operation:'stop',token,launcher_token,pid:lock.pid}}),/invalid runtime control token|实例已变化/);
}
console.log(JSON.stringify({rejected:2}));
"""], env=case.environment, capture_output=True, text=True, encoding="utf-8", timeout=15, check=False)
        evidence["rejected_stop"] = {"exit_code": rejected_stop.returncode,
            "stdout": rejected_stop.stdout, "stderr": rejected_stop.stderr}
        assert rejected_stop.returncode == 0, rejected_stop.stderr
        assert json.loads(rejected_stop.stdout) == {"rejected": 2}
        assert case.process.poll() is None and inventory() == original_inventory
        config_path = case.home / "config/gateway.jsonc"
        config = commentjson.loads(config_path.read_text(encoding="utf-8"))
        config["runtime"]["gateway"]["process"]["health"]["poll_interval_seconds"] += 0.25
        config_path.write_text(json.dumps(config), encoding="utf-8")
        deadline = time.monotonic() + 20
        while True:
            status = case.client.get("/api/gateway/config/reload-status").raise_for_status().json()["data"]
            if status["state"] == "pending_restart":
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"来源修改未生成重启候选: {status}")
            time.sleep(0.05)
        # 仅观察产品入口生成的 intent，不替产品准备候选、检查点或执行状态。
        with sqlite3.connect(f"file:{case.home / 'state/gateway/gateway.sqlite'}?mode=ro", uri=True) as database:
            row = database.execute("SELECT target_generation, fencing_token FROM gateway_restart_intent "
                "WHERE candidate_ref = ?", (status["candidate_ref"],)).fetchone()
        assert row is not None
        pending_environment = {**case.environment, "BOXTEAM_CONFIG_CANDIDATE_REF": status["candidate_ref"],
            "BOXTEAM_CONFIG_GENERATION": row[0], "BOXTEAM_CONFIG_FENCING_TOKEN": row[1]}

        def handoff(label: str) -> dict[str, object]:
            command = ["bun", "--eval", """
import {requestGatewayHandoff} from './packages/launcher/src/gateway/supervisor.mjs';
console.log(JSON.stringify(await requestGatewayHandoff({boxteamHome:process.env.BOXTEAM_HOME})));
"""]
            result = subprocess.run(command, env=pending_environment, capture_output=True, text=True, encoding="utf-8",
                timeout=120, check=False)
            evidence[label] = {"command": command, "exit_code": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr}
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        # 在管理服务接管之后、候选 generation 登记之前触发真实初始化错误。
        # 故障原件归测试产物；只改本测试控制面文件，不替换产品处理器。
        invalid = case.home / "state/gateway/port-forwards.json"
        assert not invalid.exists()
        invalid.write_text("{broken", encoding="utf-8")
        try:
            rejected = handoff("failed_candidate")
            rejected_data = rejected["data"]
            assert isinstance(rejected_data, dict)
            assert rejected["handled"] is True and rejected_data["accepted"] is False
        finally:
            invalid.unlink()
        assert case.client.get("/api/gateway/health").raise_for_status().json()["data"] == health
        assert inventory() == original_inventory

        accepted = handoff("successful_handoff")
        accepted_data = accepted["data"]
        assert isinstance(accepted_data, dict)
        assert accepted["handled"] is True and accepted_data["accepted"] is True
        changed = case.client.get("/api/gateway/health").raise_for_status().json()["data"]
        assert changed["process_id"] != health["process_id"] and changed["gateway_id"] == health["gateway_id"]
        old_gateway = psutil.Process(health["process_id"]) if psutil.pid_exists(health["process_id"]) else None
        if old_gateway is not None:
            assert psutil.wait_procs([old_gateway], timeout=10)[1] == []
        assert inventory() == original_inventory
        recovered = case.client.get(f"/api/v1/workspaces/{workspace['workspace_id']}/health").raise_for_status().json()
        assert recovered["process_id"] == backend["process_id"]
        evidence["retained"] = {"gateway": changed, "backend": recovered, "service": service}
        evidence["stop_command"] = case.stop()
        assert psutil.wait_procs([manager], timeout=10)[1] == []
        assert not service_file.exists()
        evidence["shutdown"] = {"launcher_exit_code": case.process.returncode, "manager_exited": True}
        log = (case.context.artifacts_dir / "launcher.log").read_text(encoding="utf-8")
        assert "Application shutdown failed" not in log
    finally:
        (case.context.artifacts_dir / "lifecycle.json").write_text(json.dumps(evidence,
            ensure_ascii=False, indent=2), encoding="utf-8")
        # 断言失败仍回收本次已验证身份的独立服务，避免将故障进程遗留到后续测试。
        terminate_process(case.process, timeout_seconds=160)
        if manager is not None and manager.is_running() and manager.status() != psutil.STATUS_ZOMBIE:
            manager.terminate()
            assert psutil.wait_procs([manager], timeout=10)[1] == []
