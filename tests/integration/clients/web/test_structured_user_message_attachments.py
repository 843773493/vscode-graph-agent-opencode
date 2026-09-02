from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_structured_user_attachment_message_boundaries_in_real_browser(
    request: pytest.FixtureRequest,
) -> None:
    project_root = Path.cwd().resolve()
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("结构化用户附件消息浏览器集成需要 Chromium")

    build = await asyncio.to_thread(
        subprocess.run,
        ["bun", "run", "build"],
        cwd=project_root / "src" / "clients" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    result_path = (
        project_root
        / "out"
        / "tests"
        / "integration"
        / "clients"
        / "web"
        / "structured_user_message_attachments"
        / "artifacts"
        / "structured-user-message-attachments-result.json"
    )
    screenshot_path = result_path.with_name(
        "structured-user-message-attachments-failure.png"
    )
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "BOXTEAM_PROJECT_ROOT": str(project_root),
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
        }
    )
    browser_result = await asyncio.to_thread(
        subprocess.run,
        [
            "node",
            "tests/integration/clients/web/structured_user_message_attachments.mjs",
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
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
