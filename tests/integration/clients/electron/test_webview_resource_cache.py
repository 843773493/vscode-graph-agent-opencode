"""真实 Linux ASAR 正常关窗重启，分别核原图片与 Webview 的持久字节复用。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sys
from itertools import groupby
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
from tests.integration.clients.web.quick_access.test_webview_resource_cache import (
    assert_webview_result,
    prepare_webview_files,
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_case as metadata_case,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment


@pytest.mark.asyncio
async def test_packaged_electron_image_and_webview_restart_cache(
    request: pytest.FixtureRequest,
    packaged_electron: Path,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, provider = metadata_case
    fixture = await prepare_webview_files(client, context, workspace)
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC",
    )
    (workspace / "native-cache.png").write_bytes(image)
    runtime = context.runtime_root_for_node(request.node.nodeid)
    artifacts = context.artifacts_dir
    result_path = artifacts / "webview-cache.json"
    node = shutil.which("node")
    assert node is not None, "Playwright 原生 Electron inspector 驱动需要 Node"
    command = [sys.executable, str(context.project_root / "app/runtime/posix_job.py"),
        "--memory-limit-kb", "4194304", "--timeout-seconds", "240", "--", "xvfb-run", "-a",
        node, "tests/integration/clients/electron/webview_resource_cache.mjs"]
    with chromium_temporary_environment(runtime / "native-tmp") as temporary:
        exit_code = await asyncio.to_thread(_run_process, command, cwd=context.project_root,
            log=artifacts / "driver.log", timeout=255, environment={**os.environ, **temporary,
                "BOXTEAM_ELECTRON_EXECUTABLE": str(packaged_electron),
                "BOXTEAM_ELECTRON_GATEWAY": str(client.base_url).rstrip("/"),
                "BOXTEAM_ELECTRON_RUNTIME": str(runtime), "BOXTEAM_ELECTRON_ARTIFACTS": str(artifacts),
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_WEBVIEW_FIXTURE": json.dumps(fixture)})
    assert exit_code == 0, f"Electron 图片／Webview 重启缓存验收失败：{artifacts}"
    result = assert_webview_result(result_path)
    native = result["native"]
    assert len(native["launches"]) == len(native["exits"]) == 2
    assert len({entry["url"] for entry in native["launches"]}) == 1
    assert all(entry["code"] == 0 and entry["signal"] is None for entry in native["exits"])
    assert not [entry for entry in native["consoleErrors"] if "IndexedDB" in entry["text"]
                or "database" in entry["text"].lower() and "closed" in entry["text"].lower()]
    reads = result["imageReads"]
    assert [phase for phase, _ in groupby(entry["phase"] for entry in reads)] == [
        "cold-image", "warm-image", "restart-image",
    ]
    cold = reads[0]
    assert cold["status"] == 200 and cold["encodedBodyBytes"] == len(image)
    assert all(entry["phase"] != "cold-image" and entry["status"] == 304
               and entry["encodedBodyBytes"] == 0 and entry["finished"]
               and entry["url"] == cold["url"]
               and entry["wireRequest"]["if-none-match"] == cold["response"]["etag"]
               and entry["response"]["etag"] == cold["response"]["etag"] for entry in reads[1:])
    assert len({entry["response"]["x-request-id"] for entry in reads}) == len(reads)
    assert (workspace / "native-cache.png").read_bytes() == image
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "原文件预览不应触发模型请求"
