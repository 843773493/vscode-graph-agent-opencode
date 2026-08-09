from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def test_anchored_overlay_is_not_visible_at_viewport_origin() -> None:
    project_root = Path.cwd().resolve()
    output_root = (
        project_root
        / "out"
        / "tests"
        / "e2e"
        / "clients"
        / "web"
        / "test_anchored_overlay_positioning"
    )
    workspace_root = output_root / "workspace"
    artifacts = output_root / "artifacts"
    workspace_root.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    result_path = artifacts / "anchored-overlay-positioning-result.json"
    screenshot_path = artifacts / "anchored-overlay-positioning-failure.png"
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)

    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        raise AssertionError("AnchoredOverlay Web E2E 需要 Chromium")

    environment = os.environ.copy()
    environment.update(
        {
            "BOXTEAM_PROJECT_ROOT": str(project_root),
            "BOXTEAM_E2E_RESULT_PATH": str(result_path),
            "BOXTEAM_E2E_SCREENSHOT_PATH": str(screenshot_path),
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
        }
    )
    result = subprocess.run(
        ["node", "tests/e2e/clients/web/anchored_overlay_positioning.mjs"],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, (
        "AnchoredOverlay 浏览器回归测试失败:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}\n"
        f"截图: {screenshot_path}"
    )

    browser_result = json.loads(result_path.read_text(encoding="utf-8"))
    assert browser_result["overlay"]["left"] > 0
    assert browser_result["overlay"]["top"] > 0
    assert browser_result["overlay"]["right"] <= browser_result["viewport"]["width"]
    assert browser_result["overlay"]["bottom"] <= browser_result["viewport"]["height"]
