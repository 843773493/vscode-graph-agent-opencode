from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.mark.asyncio
async def test_structured_user_attachment_message_boundaries_in_real_browser(request: pytest.FixtureRequest) -> None:
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
    # Python 登记 JS 装具的产物及 runtime，使其跟随当前用例一起保留和清理。
    run_context = TestRunContext.from_test_file(script_path, project_root=project_root).prepare()
    run_context.runtime_root_for_node(request.node.nodeid)
    result_path = run_context.artifacts_dir / "structured-user-message-attachments-result.json"
    screenshot_path = run_context.artifacts_dir / "structured-user-message-attachments-failure.png"
    # Vite 装具直接加载组件源码，不消费整站构建生成的 dist。
    browser_result = await asyncio.to_thread(
        subprocess.run,
        ["node", str(script_path)],
        cwd=project_root,
        env=os.environ | {"BOXTEAM_PROJECT_ROOT": str(project_root)},
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
    owner = result["owner"]
    assert owner["gatewayId"] and owner["workspaceId"]
    assert result["attachmentRequests"]
    assert all(
        {key: item[key] for key in ("gatewayId", "workspaceId")} == owner
        for item in result["attachmentRequests"]
    )
    assert result["thumbnailRequest"]["maxEdge"] == "512"
    assert result["retryRecovered"] is True
    assert result["noRawPayloadVisible"] is True
    assert result["imageOpenedInResourcePanel"] is True
    assert result["pdfOpenedInResourcePanel"] is True
    assert result["textOpenedInResourcePanel"] is True
    assert result["failedOriginalVisible"] is True
    assert result["noPageErrors"] is True
