"""真实 Linux ASAR 正常关窗重启后，从最近历史拒绝已删 Thread 的旧镜像。"""

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
    RecentHistoryCase,
    ThreadCacheCleanupCase,
    assert_deleted_history_result,
    assert_recent_history_result,
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
    metadata_case as metadata_case,  # noqa: PLC0414
)
from tests.integration.clients.web.quick_access.test_recent_history import (
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.integration.clients.web.quick_access.test_recent_history import (
    metadata_web_assets as metadata_web_assets,  # noqa: PLC0414
)
from tests.integration.clients.web.quick_access.test_recent_history import (
    recent_history_case as recent_history_case,  # noqa: PLC0414
)
from tests.integration.clients.web.quick_access.test_recent_history import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import TestFilePortBlock


@pytest.fixture(scope="module", autouse=True)
def recent_history_service_ports(electron_service_ports: TestFilePortBlock) -> None:
    """复用包时也在模型回放和 Backend 前保留实际使用的服务端口。"""


async def test_packaged_electron_corrupt_history_and_relocated_owner(
    request: pytest.FixtureRequest,
    packaged_electron: Path,
    recent_history_case: RecentHistoryCase,
) -> None:
    case = recent_history_case
    context = case.context
    runtime = context.runtime_root_for_node(request.node.nodeid)
    result_path = context.artifacts_dir / "recent-history.json"
    node = shutil.which("node")
    assert node is not None, "Playwright 原生 Electron inspector 驱动需要 Node"
    command = [sys.executable, str(context.project_root / "app/runtime/posix_job.py"),
        "--memory-limit-kb", "4194304", "--timeout-seconds", "300", "--", "xvfb-run", "-a",
        node, "tests/integration/clients/electron/recent_history.mjs", "recovery"]
    with chromium_temporary_environment(runtime / "native-tmp") as temporary:
        exit_code = await asyncio.to_thread(_run_process, command, cwd=context.project_root,
            log=context.artifacts_dir / "driver.log", timeout=315, environment={**os.environ, **temporary,
                "BOXTEAM_ELECTRON_EXECUTABLE": str(packaged_electron), "BOXTEAM_ELECTRON_GATEWAY": case.gateway_url,
                "BOXTEAM_ELECTRON_RUNTIME": str(runtime), "BOXTEAM_ELECTRON_ARTIFACTS": str(context.artifacts_dir),
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_HISTORY_OTHER_WORKSPACE": str(case.other),
                "BOXTEAM_HISTORY_REPLACEMENT_WORKSPACE": str(case.replacement)})
    assert exit_code == 0, f"Electron 损坏历史／搬迁归属验收失败：{context.artifacts_dir}"
    native = assert_recent_history_result(result_path, case)["native"]
    assert len(native["launches"]) == len(native["exits"]) == 16
    assert len({entry["url"] for entry in native["launches"]}) == 1
    assert len({entry["location"]["userData"] for entry in native["launches"]}) == 1
    assert all(entry["code"] == 0 and entry["signal"] is None for entry in native["exits"])
    assert not [entry for entry in native["consoleErrors"] if "IndexedDB" in entry["text"]
        or "database" in entry["text"].lower() and "closed" in entry["text"].lower()]


async def test_packaged_electron_deleted_thread_history_preserves_surviving_draft(
    request: pytest.FixtureRequest,
    packaged_electron: Path,
    cache_cleanup_case: ThreadCacheCleanupCase,
) -> None:
    case = cache_cleanup_case
    context = case.context
    runtime = context.runtime_root_for_node(request.node.nodeid)
    artifacts = context.artifacts_dir
    result_path = artifacts / "deleted-thread-history.json"
    node = shutil.which("node")
    assert node is not None, "Playwright 原生 Electron inspector 驱动需要 Node"
    command = [sys.executable, str(context.project_root / "app/runtime/posix_job.py"),
        "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--", "xvfb-run", "-a",
        node, "tests/integration/clients/electron/recent_history.mjs", "deleted"]
    with chromium_temporary_environment(runtime / "native-tmp") as temporary:
        exit_code = await asyncio.to_thread(_run_process, command, cwd=context.project_root,
            log=artifacts / "driver.log", timeout=195, environment={**os.environ, **temporary,
                "BOXTEAM_ELECTRON_EXECUTABLE": str(packaged_electron),
                "BOXTEAM_ELECTRON_GATEWAY": case.gateway_url,
                "BOXTEAM_ELECTRON_RUNTIME": str(runtime), "BOXTEAM_ELECTRON_ARTIFACTS": str(artifacts),
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_CACHE_THREADS": json.dumps(case.threads)})
    assert exit_code == 0, f"Electron 已删最近历史验收失败：{artifacts}"
    result = assert_deleted_history_result(result_path, case)
    native = result["native"]
    assert len(native["launches"]) == len(native["exits"]) == 3
    assert len({entry["url"] for entry in native["launches"]}) == 1
    assert len({entry["location"]["userData"] for entry in native["launches"]}) == 1
    assert all(entry["code"] == 0 and entry["signal"] is None for entry in native["exits"])
    assert not [entry for entry in native["consoleErrors"] if "IndexedDB" in entry["text"]
        or "database" in entry["text"].lower() and "closed" in entry["text"].lower()]
