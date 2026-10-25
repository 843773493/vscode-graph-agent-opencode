from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.mark.asyncio
async def test_structured_user_attachment_message_boundaries_in_real_browser() -> None:
    project_root = Path.cwd().resolve()
    script_path = (
        project_root
        / "tests"
        / "integration"
        / "clients"
        / "web"
        / "components"
        / "structured_user_message_attachments.mjs"
    )
    run_id = f"structured-attachments-{uuid4().hex}"
    previous_run_id = os.environ.get("BOXTEAM_TEST_RUN_ID")
    environment = os.environ.copy()
    environment.update(
        {
            "BOXTEAM_PROJECT_ROOT": str(project_root),
            "BOXTEAM_TEST_RUN_ID": run_id,
        }
    )
    os.environ["BOXTEAM_TEST_RUN_ID"] = run_id
    try:
        # Vite 装具直接加载组件源码，不消费整站构建生成的 dist。
        run_context = TestRunContext.from_test_file(
            script_path,
            project_root=project_root,
        )
        result_path = (
            run_context.artifacts_dir
            / "structured-user-message-attachments-result.json"
        )
        screenshot_path = run_context.artifacts_dir / "structured-user-message-attachments-failure.png"
        # 浏览器路径由 Node 装具统一选择；显式环境覆盖原样透传。
        browser_result = await asyncio.to_thread(
            subprocess.run,
            ["node", str(script_path)],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        if previous_run_id is None:
            os.environ.pop("BOXTEAM_TEST_RUN_ID", None)
        else:
            os.environ["BOXTEAM_TEST_RUN_ID"] = previous_run_id
    assert browser_result.returncode == 0, (
        "结构化用户附件消息浏览器集成失败:\n"
        f"stdout:\n{browser_result.stdout}\n"
        f"stderr:\n{browser_result.stderr}\n"
        f"结果: {result_path}\n截图: {screenshot_path}"
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["thumbnailRequest"]["maxEdge"] == "512"
    assert result["retryRecovered"] is True
    assert result["noRawPayloadVisible"] is True
    assert result["imageOpenedInResourcePanel"] is True
    assert result["pdfOpenedInResourcePanel"] is True
    assert result["textOpenedInResourcePanel"] is True
    assert result["failedOriginalVisible"] is True
    assert result["noPageErrors"] is True
