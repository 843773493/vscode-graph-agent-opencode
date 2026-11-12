"""真实语言选择与重载，复用 Gateway、Workbench 和原展示存储。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from functools import partial
from pathlib import Path

import httpx
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.clients.web import (
    test_workspace_metadata_bootstrap as metadata_support,
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_case as metadata_case,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file


@pytest.fixture(autouse=True)
def localization_gateway_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    # 本组现在验证实际执行，沿原测试 composition 绑定调用 ID 与响应屏障。
    monkeypatch.setattr(metadata_support, 'start_gateway_process', partial(
        metadata_support.start_gateway_process, asgi_app='tests.support.e2e_control.gateway_app:app',
    ))


@pytest.fixture
def localization_execution_interaction(request: pytest.FixtureRequest) -> str:
    model = 'e2e-stub-model'
    cassette = build_cassette(
        asset_id='localization-running-execution', model=model,
        url=f'http://127.0.0.1:{integration_port_block_for_file(Path(request.node.path)).port(10)}/v1/chat/completions',
        frames=chat_completion_frames(response_id='localization-running-execution', model=model,
            deltas=[{'role': 'assistant', 'content': '语言切换后执行完成 /home/alice/{0}'}], finish_reason='stop'),
    )
    return json.dumps(cassette.raw['interactions'], ensure_ascii=False)


@pytest.fixture(autouse=True)
def metadata_web_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_LOCALIZATION_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with (assets / "out/main.js").open("rb") as main:
        fingerprint = hashlib.file_digest(main, "sha256").hexdigest()
    (context.artifacts_dir / "localization-assets.json").write_text(json.dumps({
        "assets": str(assets), "prepared": bool(prepared), "main_sha256": fingerprint,
        "version": json.loads((assets / "version.json").read_text(encoding="utf-8")),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


async def test_builtin_languages_reload_preserves_thread_drafts_and_dirty_file(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
    localization_execution_interaction: str,
) -> None:
    client, context, workspace, provider = metadata_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "显示语言验收需要 Chromium"
    original = "原始文件正文 {0}\n" + "\n".join(f"locale line {index}" for index in range(1, 100))
    (workspace / "locale.txt").write_text(original, encoding="utf-8")
    (workspace / "locale-preview.md").write_text(
        "# Markdown locale preview\n\n"
        "![blocked remote image](http://127.0.0.1:9/blocked.png)\n",
        encoding="utf-8",
    )
    result_path = context.artifacts_dir / "localization.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
               "node", "tests/integration/clients/web/quick_access/localization.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(
            subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, **provider.environment,
                 "BOXTEAM_LOCALIZATION_MODEL_INTERACTIONS": localization_execution_interaction,
                 "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=195, check=False,
        )
    (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(result_path.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert all(result[key] for key in ("defaultChinese", "writeFailureVisible", "cancelPreserved", "englishReloaded", "clearedToChinese",
                                      "draftsRestored", "dirtyRestored", "splitRestored", "backendIdentitiesPreserved"))
    assert len(result['modelRequests']) == 1 and result["externalTranslationRequests"] == []
    assert result['backgroundExecution']['survivedEnglishReload'] and result['backgroundExecution']['completedOnce']
    assert len(result['commitFailure']['writes']) == 1 and result['dirtyCancellationPreserved']
    assert [item['locale'] for item in result['imagePreviews']] == ['en', 'zh-cn']
    assert (workspace / "locale.txt").read_text(encoding="utf-8") == original
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    execution = result['backgroundExecution']
    assert calls == {f"model_call/{execution['sessionId']}/{execution['threadId']}": 1}, '语言重载不得重新执行任务'
