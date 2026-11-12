"""真实 Linux ASAR 清理展示镜像后正常关窗，验证受保护恢复数据。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from tests.integration.clients.electron.test_packaged_workbench import _run_process
from tests.integration.clients.electron.test_packaged_workbench import (
    electron_service_ports as electron_service_ports,  # noqa: PLC0414
)
from tests.integration.clients.electron.test_packaged_workbench import (
    packaged_electron as packaged_electron,  # noqa: PLC0414
)
from tests.integration.clients.web.quick_access.test_recent_history import (
    ThreadCacheCleanupCase,
    assert_cache_cleanup_result,
)
from tests.integration.clients.web.quick_access.test_recent_history import (
    browser_backend as browser_backend,  # noqa: PLC0414
)
from tests.integration.clients.web.quick_access.test_recent_history import (
    cache_cleanup_case as cache_cleanup_case,  # noqa: PLC0414
)
from tests.integration.clients.web.quick_access.test_recent_history import (
    history_cache_case as history_cache_case,  # noqa: PLC0414
)
from tests.integration.clients.web.quick_access.test_recent_history import (
    metadata_web_assets as metadata_web_assets,  # noqa: PLC0414
)
from tests.integration.clients.web.quick_access.test_recent_history import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment


async def test_packaged_electron_cleanup_preserves_history_backups_and_split_views(
    request: pytest.FixtureRequest,
    packaged_electron: Path,
    cache_cleanup_case: ThreadCacheCleanupCase,
) -> None:
    case = cache_cleanup_case
    context = case.context
    runtime = context.runtime_root_for_node(request.node.nodeid)
    artifacts = context.artifacts_dir
    result_path = artifacts / "thread-cache-cleanup.json"
    node = shutil.which("node")
    assert node is not None, "Playwright 原生 Electron inspector 驱动需要 Node"
    command = [sys.executable, str(context.project_root / "app/runtime/posix_job.py"),
        "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--", "xvfb-run", "-a",
        node, "tests/integration/clients/electron/thread_cache_cleanup.mjs"]
    with chromium_temporary_environment(runtime / "native-tmp") as temporary:
        exit_code = await asyncio.to_thread(_run_process, command, cwd=context.project_root,
            log=artifacts / "driver.log", timeout=195, environment={**os.environ, **temporary,
                "BOXTEAM_ELECTRON_EXECUTABLE": str(packaged_electron),
                "BOXTEAM_ELECTRON_GATEWAY": case.gateway_url,
                "BOXTEAM_ELECTRON_RUNTIME": str(runtime), "BOXTEAM_ELECTRON_ARTIFACTS": str(artifacts),
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_CACHE_THREADS": json.dumps(case.threads),
                "BOXTEAM_CACHE_FILE_ROOT_URI": case.root_uri})
    assert exit_code == 0, f"Electron 清理及受保护恢复验收失败：{artifacts}"
    result = assert_cache_cleanup_result(result_path, case)
    native = result["native"]
    assert len(native["launches"]) == len(native["exits"]) == 2
    assert len({entry["url"] for entry in native["launches"]}) == 1
    assert all(entry["code"] == 0 and entry["signal"] is None for entry in native["exits"])
    assert not [entry for entry in native["consoleErrors"] if "IndexedDB" in entry["text"]
        or "database" in entry["text"].lower() and "closed" in entry["text"].lower()]
