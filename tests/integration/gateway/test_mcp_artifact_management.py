"""通过原命令面板管理真实发布制品；配置引用由原来源 CAS 生成。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    ArtifactIndex,
    InstallationCase,
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    artifact_download_gate as artifact_download_gate,  # noqa: PLC0414
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    artifact_index as artifact_index,  # noqa: PLC0414
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    installation_case as installation_case,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment


def test_workbench_artifact_references_confirmation_race_and_physical_uninstall(
    installation_case: InstallationCase, artifact_index: ArtifactIndex,
) -> None:
    case = installation_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "MCP 制品管理验收需要真实 Chromium"
    client = case.client
    listing = client.get("/api/gateway/workspaces").json()["data"]
    workspace = listing["active_workspace"]
    headers = {"X-BoxTeam-Target-Gateway-Id": workspace["gateway_id"], "X-BoxTeam-Workspace-Id": workspace["workspace_id"]}

    def api(method: str, path: str, body: dict | None = None) -> dict:
        response = client.request(method, "/api/v1/mcp/" + path, json=body, headers=headers)
        assert response.status_code == 200, response.text
        envelope = response.json()
        assert envelope["request_id"] == response.headers["X-Request-ID"]
        return envelope

    target = api("GET", "installation-capabilities")["target"]
    inspected = api("POST", "candidate-manifests", {"target": target, "source": artifact_index.source()})["data"]
    candidate = {"target": target, "source": artifact_index.source(), "manifest_digest": inspected["manifest_digest"],
                 "variant": inspected["variants"][0]["ref"]}
    plan = api("POST", "candidate-plans", candidate)["data"]
    operation = api("POST", "installations", {"candidate": candidate, "plan_digest": plan["plan_digest"],
                    "operation_id": "management-install", "entrypoint": "artifact-server"})["data"]
    deadline = time.monotonic() + 40
    while operation["status"] == "running":
        assert time.monotonic() < deadline, operation
        time.sleep(.1)
        operation = api("GET", "installation-operations/management-install")["data"]
    assert operation["status"] == "succeeded", operation
    artifact = operation["artifact"]
    supervisor = Path.cwd() / "app/runtime" / ("windows_job.py" if os.name == "nt" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
               "bun", "tests/integration/clients/web/mcp_artifact_management.mjs"]
    with chromium_temporary_environment(case.context.artifacts_dir / "chromium-tmp") as temporary:
        result = subprocess.run(command, timeout=195, capture_output=True, text=True, check=False,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                 "BOXTEAM_ARTIFACT_MANAGEMENT": json.dumps({"baseUrl": str(client.base_url).rstrip("/"),
                    "artifacts": str(case.context.artifacts_dir), "artifact": artifact, "target": target})})
    (case.context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
    (case.context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
    (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command,
        "exit_code": result.returncode}, indent=2), encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((case.context.artifacts_dir / "artifact-management.json").read_text(encoding="utf-8"))
    assert observed["phase"] == "complete" and observed["pageErrors"] == []
    assert observed["cancelledWithoutDelete"] and observed["referencesRetained"] and observed["raceRejected"]
    assert len(observed["deletes"]) == 2 and [item["status"] for item in observed["deletes"]] == [409, 200]
    assert not Path(artifact["command"]).exists()
    assert api("GET", "installations")["data"] == []
    operation_id = observed["deletes"][1]["requestBody"]["operation_id"]
    completed = api("GET", f"installation-operations/{operation_id}")["data"]
    assert completed == observed["queriedOperation"]["data"] and completed["status"] == "succeeded"
    final = completed["artifact"]
    assert final["state"] == "removed" and final["binding"] == artifact["binding"]
    assert api("GET", "servers")["data"] == []
