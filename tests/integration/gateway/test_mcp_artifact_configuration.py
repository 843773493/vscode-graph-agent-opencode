"""原安装通知生成、确认并保存真实候选配置；响应丢失后只查询原 source。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import commentjson
import pytest

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


@pytest.mark.parametrize("enabled,response_lost", [(False, False), (True, True)], ids=["disabled", "enabled-response-lost"])
def test_original_install_notification_draft_confirmation_and_source_commit(
    installation_case: InstallationCase, artifact_index: ArtifactIndex, enabled: bool, response_lost: bool,
) -> None:
    case = installation_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "MCP 候选配置验收需要真实 Chromium"
    supervisor = Path.cwd() / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
               "node", "tests/integration/clients/web/mcp_artifact_configuration.mjs"]
    with chromium_temporary_environment(case.context.runtime_root_for_node(os.environ["PYTEST_CURRENT_TEST"]) / "chromium-tmp") as temporary:
        result = subprocess.run(command, timeout=195, capture_output=True, check=False,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                "BOXTEAM_ARTIFACT_CONFIGURATION": json.dumps({"baseUrl": str(case.client.base_url).rstrip("/"),
                    "registryUrl": artifact_index.url, "artifacts": str(case.context.artifacts_dir),
                    "enabled": enabled, "responseLost": response_lost})})
    (case.context.artifacts_dir / "browser.stdout.raw").write_bytes(result.stdout)
    (case.context.artifacts_dir / "browser.stderr.raw").write_bytes(result.stderr)
    (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command,
        "exit_code": result.returncode, "memory_limit_kb": 4194304, "timeout_seconds": 180}, indent=2), encoding="utf-8")
    assert result.returncode == 0, (result.stdout + result.stderr).decode("utf-8", errors="backslashreplace")
    observed = json.loads((case.context.artifacts_dir / "artifact-configuration.json").read_text(encoding="utf-8"))
    assert observed["phase"] == "complete" and observed["pageErrors"] == []
    assert observed["cancelledWithoutSave"] and observed["publishedWithoutConfiguration"]
    assert observed["sourceState"] in {"active", "pending", "waiting_settlement", "failed", "blocked"}
    assert len(observed["saves"]) == 1 and len(observed["drafts"]) == 2
    assert observed["queriedOriginalCommit"] is response_lost
    saved = commentjson.loads((case.workspace / ".boxteam/workspace.jsonc").read_text(encoding="utf-8"))["mcp"]["servers"]["configured-server"]
    assert saved["enabled"] is enabled and saved["managed_launch"] == observed["binding"]
    assert Path(observed["artifactCommand"]).is_file()
    assert all("/v0.1/servers" in request or "/simple/" in request for request in artifact_index.requests)
