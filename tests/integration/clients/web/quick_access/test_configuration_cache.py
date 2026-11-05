"""真实 Profile／非本地配置与原 IDB 写入屏障的 Integration。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_case as metadata_case,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_data,
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.workspaces import prepare_default_test_workspace


def _asset_fingerprints(assets: Path) -> dict[str, dict[str, str | int]]:
    fingerprints = {}
    for path in sorted(assets.rglob("*")):
        if path.is_file():
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            fingerprints[path.relative_to(assets).as_posix()] = {
                "sha256": digest, "bytes": path.stat().st_size,
            }
    return fingerprints


def _protected_command(context: TestRunContext, command: list[str], seconds: int) -> list[str]:
    supervisor = context.project_root / "app/runtime" / (
        "windows_job.py" if sys.platform == "win32" else "posix_job.py"
    )
    return [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
            "--timeout-seconds", str(seconds), "--", *command]


@pytest.fixture(params=["original", "delayed-cache-write"])
def configuration_assets(
    request: pytest.FixtureRequest, web_workbench_assets: Path,
) -> tuple[str, Path]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    variant = str(request.param)
    original = _asset_fingerprints(web_workbench_assets)
    assets = web_workbench_assets
    receipt: dict[str, object] = {
        "variant": variant, "original_assets": str(web_workbench_assets), "original_files": original,
        "boundary": "正式未插桩资产" if variant == "original" else
                    "独占资产副本，仅在原 IndexedDBFileSystemProvider.writeFile 插入精确目标屏障",
    }
    if variant == "delayed-cache-write":
        assets = context.runtime_root_for_node(request.node.nodeid) / "instrumented-dist"
        command = ["bun", "tests/integration/clients/web/quick_access/configuration_cache_browser.mjs",
                   "--prepare-assets", str(web_workbench_assets), str(assets), str(context.artifacts_dir)]
        supervised = _protected_command(context, command, 60)
        process = subprocess.run(supervised, cwd=context.project_root, capture_output=True,
                                 text=True, timeout=75, check=False)
        (context.artifacts_dir / "asset-preparation.stdout.raw").write_text(process.stdout, encoding="utf-8")
        (context.artifacts_dir / "asset-preparation.stderr.raw").write_text(process.stderr, encoding="utf-8")
        receipt.update(preparation_command=command, supervised_command=supervised,
                       preparation_exit_code=process.returncode)
        (context.artifacts_dir / "asset-preparation.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        assert process.returncode == 0, process.stdout + process.stderr
    current = _asset_fingerprints(assets)
    changed = sorted(path for path in original.keys() | current.keys() if original.get(path) != current.get(path))
    receipt.update(served_assets=str(assets), served_files=current, changed_files=changed)
    (context.artifacts_dir / "asset-fingerprints.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    if variant == "original":
        assert changed == []
    else:
        insertion = json.loads((context.artifacts_dir / "instrumentation.diff.json").read_text(encoding="utf-8"))
        assert changed == [insertion["file"]]
    return variant, assets


@pytest.fixture(autouse=True)
def metadata_web_assets(
    monkeypatch: pytest.MonkeyPatch, configuration_assets: tuple[str, Path],
) -> None:
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(configuration_assets[1]))


async def _owned_data(client: httpx.AsyncClient, path: str, owner: dict[str, str]) -> dict:
    response = await client.get(path, headers={
        "X-BoxTeam-Target-Gateway-Id": owner["gatewayId"], "X-BoxTeam-Workspace-Id": owner["workspaceId"],
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert response.headers["X-Request-ID"] == payload["request_id"]
    return payload


@pytest.mark.asyncio
async def test_real_profile_folder_cache_and_normal_close(
    request: pytest.FixtureRequest,
    configuration_assets: tuple[str, Path],
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, provider = metadata_case
    variant, _assets = configuration_assets
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "配置缓存页面验收需要 Chromium"
    runtime = context.runtime_root_for_node(request.node.nodeid)
    other = prepare_default_test_workspace(
        workspace_root=context.workspace_root / context.run_root.name / runtime.name / "configuration-B",
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
    )
    (other / ".boxteam/workspace-identity.json").unlink(missing_ok=True)
    shutil.copyfile(workspace / ".boxteam/workspace.jsonc", other / ".boxteam/workspace.jsonc")
    # 仅准备原来源输入；用户保存、Profile 和派生缓存均由真实产品入口创建。
    for target, font_size in ((workspace, 17), (other, 23)):
        (target / ".vscode").mkdir(exist_ok=True)
        (target / ".vscode/settings.json").write_text(
            json.dumps({"editor.fontSize": font_size, "files.autoSave": "off"}, indent=2) + "\n",
            encoding="utf-8",
        )
    registered = await client.post("/api/gateway/managed-workspaces", json={
        "root_path": str(other), "name": "ConfigurationCache B",
    })
    assert registered.status_code == 200, registered.text
    assert registered.headers["X-Request-ID"] == registered.json()["request_id"]
    workspaces = await metadata_data(client, "/api/gateway/workspaces")
    active = workspaces["active_workspace"]
    second = next(item for item in workspaces["items"] if item["name"] == "ConfigurationCache B")
    owners = {}
    initialization = {}
    for label, item in (("A", active), ("B", second)):
        owner = {"gatewayId": item["gateway_id"], "workspaceId": item["workspace_id"]}
        metadata = await _owned_data(client, "/api/v1/workspace", owner)
        roots = await _owned_data(client, "/api/v1/workspace/files/roots", owner)
        assert metadata["data"]["workspace_id"] == owner["workspaceId"]
        owner["root"] = roots["data"]["workspace_uri"]
        owners[label] = owner
        initialization[label] = {"metadata": metadata, "roots": roots}
    assert owners["A"]["workspaceId"] != owners["B"]["workspaceId"]
    (context.artifacts_dir / "configuration-input.json").write_text(
        json.dumps(initialization, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    result_path = context.artifacts_dir / "configuration-cache-browser.json"
    command = ["bun", "tests/integration/clients/web/quick_access/configuration_cache_browser.mjs"]
    supervised = _protected_command(context, command, 180)
    with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
        process = await asyncio.to_thread(
            subprocess.run, supervised, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_FIXTURE": json.dumps({"variant": variant, "owners": owners}),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                 "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "configuration-cache-browser.png"),
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=195, check=False,
        )
    (context.artifacts_dir / "browser.stdout.raw").write_text(process.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(process.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "supervised_command": supervised, "exit_code": process.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert process.returncode == 0, f"{process.stdout}\n{process.stderr}\n{result_path}"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["pageErrors"] == []
    assert result["routeStubs"] == []
    assert result["localSettingsNotDuplicated"] is True
    assert result["profileSettingsIsolated"] is True
    assert result["folderRoundTrip"] is True
    assert result["normalCloseReopened"] is True
    assert result["protectedSettingsRetained"] is True
    expected_a = 20
    if variant == "delayed-cache-write":
        assert result["lateWriteKeepsResource"] is True
        assert result["pendingClose"]["entered"]["path"] == result["folderCaches"]["A"]
        assert result["pendingClose"]["closedWithWritePending"] is True
        assert result["pendingClose"]["rebuiltFromOwner"] is True
        expected_a = 23
    assert json.loads((workspace / ".vscode/settings.json").read_text())["editor.fontSize"] == expected_a
    assert json.loads((other / ".vscode/settings.json").read_text())["editor.fontSize"] == 24
    with provider.control._condition:
        calls = {namespace: count for namespace, count in provider.control._counters.items()
                 if namespace.startswith("model_call/")}
    (context.artifacts_dir / "provider-call-counts.json").write_text(json.dumps(calls) + "\n", encoding="utf-8")
    assert calls == {}, "配置与 Profile 操作不应发送模型请求"
