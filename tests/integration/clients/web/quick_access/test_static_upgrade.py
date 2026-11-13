"""正式 Workbench 产物的隔离副本，在真实浏览器缓存保留时升级。"""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.clients.web.quick_access.source_snapshot import (
    snapshot_workbench_sources,
)
from tests.support.model_stream.config import model_stream_replay
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture
def localization_builds(request: pytest.FixtureRequest) -> list[dict[str, object]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    prepared = os.getenv("BOXTEAM_LOCALIZATION_UPGRADE_BUILDS")
    if prepared:
        # 显式信任本测试已有收据；核入口指纹，NLS 配对和文案仍由真实页面验证。
        receipt_path = Path(prepared).resolve(strict=True)
        prepared_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        for build in prepared_receipt["builds"]:
            assets = Path(build["assets"])
            assert hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest() == build["main_sha256"]
        (context.artifacts_dir / "localization-build-reuse.json").write_text(json.dumps({
            "receipt_path": str(receipt_path), "receipt": prepared_receipt,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return prepared_receipt["builds"]
    snapshot, source_receipt = snapshot_workbench_sources(context.project_root, runtime)
    (context.artifacts_dir / "source-snapshot.json").write_text(
        json.dumps(source_receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    product_path = snapshot / "src/clients/shared/core/i18n/messages/zh-cn.json"
    nls_path = snapshot / "src/clients/workbench/resources/localization/zh-cn-project.json"
    product = json.loads(product_path.read_text(encoding="utf-8"))
    nls = json.loads(nls_path.read_text(encoding="utf-8"))
    module = "vs/workbench/contrib/localization/common/localizationsActions"
    upstream_nls = json.loads((snapshot / "src/clients/workbench/resources/localization/zh-cn-upstream-main.json")
                              .read_text(encoding="utf-8"))
    placeholder = nls["contents"][module].get("chooseLocale", upstream_nls["contents"][module]["chooseLocale"])
    builds: list[dict[str, object]] = []
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    for label in ("before", "after"):
        if label == "after":
            # 只改独占完整快照的两份翻译；A/B 都由真实正式构建生成，绝不改产物或伪造 hash。
            product["navigation.catalog.refresh"] += " · 升级验收"
            placeholder += " · 升级验收"
            nls["contents"][module]["chooseLocale"] = placeholder
            product_path.write_text(json.dumps(product, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            nls_path.write_text(json.dumps(nls, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assets = runtime / label / "dist"
        command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180",
                   "--", "bun", "run", "build", "--outdir", str(assets)]
        receipt: dict[str, object] = {
            "command": command, "cwd": str(snapshot / "src/clients/workbench"), "assets": str(assets),
            "product_source_sha256": hashlib.sha256(product_path.read_bytes()).hexdigest(),
            "nls_source_sha256": hashlib.sha256(nls_path.read_bytes()).hexdigest(), "exit_code": None,
        }
        started = time.monotonic()
        try:
            with (context.artifacts_dir / f"localization-build-{label}.log").open("x", encoding="utf-8") as log:
                completed = subprocess.run(command, cwd=snapshot / "src/clients/workbench", stdout=log,
                                           stderr=subprocess.STDOUT, text=True, timeout=195, check=False)
            receipt["exit_code"] = completed.returncode
            assert completed.returncode == 0, f"正式本地化构建失败：{label}"
            version = json.loads((assets / "version.json").read_text(encoding="utf-8"))
            manifest_path = version["localization"]["nlsManifest"]
            manifest = json.loads((assets / manifest_path.lstrip("/")).read_text(encoding="utf-8"))
            main_sha256 = hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest()
            receipt.update(version=version, main_sha256=main_sha256)
            builds.append({"assets": str(assets), "manifest": manifest_path, "buildId": manifest["buildId"],
                           "main_sha256": main_sha256,
                           "language": manifest["assets"]["zh-cn"], "product": product["navigation.catalog.refresh"],
                           "placeholder": placeholder})
        finally:
            receipt["elapsed_seconds"] = time.monotonic() - started
            (context.artifacts_dir / f"localization-build-{label}.json").write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
    assert builds[0]["buildId"] != builds[1]["buildId"]
    assert builds[0]["language"] != builds[1]["language"]
    (context.artifacts_dir / "localization-build-pair.json").write_text(json.dumps({
        "source": source_receipt, "builds": builds,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return builds


@pytest.fixture(scope="module")
def integration_workspace_root_path(request: pytest.FixtureRequest) -> str:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    return str(prepare_test_workspace(
        workspace_root=context.workspace_root,
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(".boxteam/workspace-identity.json",),
    ))


async def test_cached_workbench_executes_upgraded_bundle_and_worker(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    web_workbench_assets: Path, search_gateway_port: int,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "静态升级验收需要 Chromium"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    assets = runtime / "mutable-build"
    shutil.copytree(web_workbench_assets, assets)
    # 仅副本加入观测标记；原构建、产品源码和其它测试不受影响。
    markers = {
        "out/main.js": ';globalThis.__boxteamStaticUpgrade="version-1";',
        "out/vs/editor/common/services/editorWebWorkerMain.js":
            ';if(self.name==="boxteam-static-upgrade")self.postMessage({boxteamStaticUpgrade:"version-1"});',
    }
    for path, marker in markers.items():
        with (assets / path).open("a", encoding="utf-8") as file:
            file.write("\n" + marker + "\n")
    gateway = await asyncio.to_thread(start_gateway_process,
        workspace_root=Path(integration_workspace_root_path), default_backend_url=None, port=search_gateway_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true"})
    command = ["node", "tests/integration/clients/web/quick_access/static_upgrade.mjs"]
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    supervised = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                  "--timeout-seconds", "120", "--", *command]
    result_path = context.artifacts_dir / "static-upgrade.json"
    try:
        with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
            process = await asyncio.to_thread(subprocess.run, supervised, cwd=context.project_root,
                env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_STATIC_UPGRADE_ASSETS": str(assets),
                     "BOXTEAM_BROWSER_RUNTIME_PATH": str(runtime),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=150, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(process.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(process.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
            "command": command, "supervised_command": supervised, "exit_code": process.returncode}) + "\n")
        assert process.returncode == 0, f"{process.stdout}\n{process.stderr}\n{result_path}"
        result = json.loads(result_path.read_text())
        assert result["warmRevalidation"] and result["upgradeExecuted"] and result["missingWorkerRejected"]
        assert result["pageErrors"] == []
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)


async def test_cached_localization_pairs_real_builds_after_upgrade(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    localization_builds: list[dict[str, object]], search_gateway_port: int,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "本地化升级验收需要 Chromium"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "90", "--",
               "node", "tests/integration/clients/web/quick_access/localization_upgrade.mjs"]
    results = []
    with model_stream_replay(context.artifacts_dir, runtime_root=runtime,
                             business_assertion="static-localization-upgrade-without-model-call") as provider:
        for index, build in enumerate(localization_builds):
            gateway = await asyncio.to_thread(start_gateway_process,
                workspace_root=Path(integration_workspace_root_path), default_backend_url=None, port=search_gateway_port,
                boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
                artifacts_dir=context.artifacts_dir / f"gateway-{index}",
                extra_env={**provider.environment, "BOXTEAM_WEB_ASSETS": str(build["assets"]),
                           "LITELLM_LOCAL_MODEL_COST_MAP": "true"})
            result_path = context.artifacts_dir / f"localization-upgrade-{index}.json"
            try:
                with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
                    process = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
                        env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                             "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_BROWSER_RUNTIME_PATH": str(runtime),
                             "BOXTEAM_LOCALIZATION_EXPECTED": json.dumps(build, ensure_ascii=False),
                             "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                        capture_output=True, text=True, timeout=105, check=False)
                (context.artifacts_dir / f"browser-{index}.stdout.raw").write_text(process.stdout, encoding="utf-8")
                (context.artifacts_dir / f"browser-{index}.stderr.raw").write_text(process.stderr, encoding="utf-8")
                (context.artifacts_dir / f"browser-{index}.json").write_text(json.dumps({
                    "command": command, "exit_code": process.returncode,
                }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                assert process.returncode == 0, process.stdout + process.stderr
                result = json.loads(result_path.read_text(encoding="utf-8"))
                assert result["phase"] == "passed" and result["pageErrors"] == []
                assert result["externalTranslationRequests"] == []
                results.append(result)
            finally:
                await asyncio.to_thread(close_gateway_process, gateway)
        with provider.control._condition:
            calls = {key: value for key, value in provider.control._counters.items() if key.startswith("model_call/")}
        assert calls == {}, "升级静态语言资源不能触发模型请求"
    before, after = results
    assert before["owner"] == after["owner"]
    assert after["coldMain"]["request"]["if-none-match"] == before["coldMain"]["response"]["etag"]
    assert after["coldMain"]["response"]["etag"] != before["coldMain"]["response"]["etag"]
    assert before["sourceLanguage"] != after["sourceLanguage"]
