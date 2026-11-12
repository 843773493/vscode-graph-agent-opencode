"""源码外 ASAR 的显示语言重载与正常关窗恢复。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.clients.electron.test_packaged_workbench import _run_process
from tests.integration.clients.electron.test_packaged_workbench import (
    electron_service_ports as electron_service_ports,  # noqa: PLC0414
)
from tests.integration.clients.electron.test_packaged_workbench import (
    packaged_electron as packaged_electron,  # noqa: PLC0414
)
from tests.integration.clients.web.quick_access.test_localization import (
    localization_execution_interaction as localization_execution_interaction,  # noqa: PLC0414
)
from tests.integration.clients.web.quick_access.test_localization import (
    localization_gateway_composition as localization_gateway_composition,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_case as metadata_case,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment


async def test_packaged_electron_builtin_languages_and_renderer_reload(
    request: pytest.FixtureRequest,
    packaged_electron: Path,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
    localization_execution_interaction: str,
) -> None:
    client, context, workspace, provider = metadata_case
    original = "原始文件正文 {0}\n" + "\n".join(f"locale line {index}" for index in range(1, 100))
    (workspace / "locale.txt").write_text(original, encoding="utf-8")
    (workspace / "locale-preview.md").write_text(
        "# Markdown locale preview\n\n"
        "![blocked remote image](http://127.0.0.1:9/blocked.png)\n",
        encoding="utf-8",
    )
    runtime = context.runtime_root_for_node(request.node.nodeid)
    artifacts = context.artifacts_dir
    result_path = artifacts / "localization.json"
    node = shutil.which("node")
    assert node is not None, "Playwright 原生 Electron inspector 驱动需要 Node"
    command = [sys.executable, str(context.project_root / "app/runtime/posix_job.py"),
        "--memory-limit-kb", "4194304", "--timeout-seconds", "240", "--", "xvfb-run", "-a",
        node, "tests/integration/clients/electron/localization.mjs"]
    with chromium_temporary_environment(runtime / "native-tmp") as temporary:
        exit_code = await asyncio.to_thread(_run_process, command, cwd=context.project_root,
            log=artifacts / "driver.log", timeout=255, environment={**os.environ, **temporary, **provider.environment,
                "BOXTEAM_LOCALIZATION_MODEL_INTERACTIONS": localization_execution_interaction,
                "BOXTEAM_ELECTRON_EXECUTABLE": str(packaged_electron),
                "BOXTEAM_ELECTRON_GATEWAY": str(client.base_url).rstrip("/"),
                "BOXTEAM_ELECTRON_RUNTIME": str(runtime), "BOXTEAM_ELECTRON_ARTIFACTS": str(artifacts),
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path)})
    assert exit_code == 0, f"Electron 显示语言验收失败：{artifacts}"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert all(result[key] for key in ("defaultChinese", "writeFailureVisible", "cancelPreserved", "englishReloaded",
        "clearedToChinese", "draftsRestored", "dirtyRestored", "splitRestored", "backendIdentitiesPreserved",
        "nativeRestartRestored"))
    assert len(result['modelRequests']) == 1 and result["externalTranslationRequests"] == []
    assert result['backgroundExecution']['survivedEnglishReload'] and result['backgroundExecution']['completedOnce']
    assert len(result['commitFailure']['writes']) == 1 and result['dirtyCancellationPreserved']
    assert [item['locale'] for item in result['imagePreviews']] == ['en', 'zh-cn']
    native = result["native"]
    assert len(native["launches"]) == len(native["exits"]) == 2
    assert len({entry["url"] for entry in native["launches"]}) == 1
    assert all(entry["code"] == 0 and entry["signal"] is None for entry in native["exits"])
    assert not [entry for entry in native["consoleErrors"] if "IndexedDB" in entry["text"]
        or "database" in entry["text"].lower() and "closed" in entry["text"].lower()]
    assert (workspace / "locale.txt").read_text(encoding="utf-8") == original
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    execution = result['backgroundExecution']
    assert calls == {f"model_call/{execution['sessionId']}/{execution['threadId']}": 1}, '语言重载不得重新执行任务'
