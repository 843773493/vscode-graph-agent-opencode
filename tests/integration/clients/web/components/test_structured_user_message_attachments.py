from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_structured_user_attachment_message_boundaries_in_real_browser() -> None:
    project_root = Path.cwd().resolve()
    # Vite 装具直接加载组件源码，不消费整站构建生成的 dist。
    result_path = (
        project_root
        / "out"
        / "tests"
        / "integration"
        / "clients"
        / "web"
        / "components"
        / "structured_user_message_attachments"
        / "artifacts"
        / "structured-user-message-attachments-result.json"
    )
    screenshot_path = result_path.with_name(
        "structured-user-message-attachments-failure.png"
    )
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)
    # 浏览器路径由 Node 装具统一选择；显式环境覆盖原样透传。
    environment = os.environ.copy()
    environment.update(
        {
            "BOXTEAM_PROJECT_ROOT": str(project_root),
        }
    )
    browser_result = await asyncio.to_thread(
        subprocess.run,
        [
            "node",
            "tests/integration/clients/web/components/structured_user_message_attachments.mjs",
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
