"""为正式页面测试构建唯一的 Workbench 静态产物，不启动替代页面服务。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.fixture(scope="module")
def web_workbench_assets(request: pytest.FixtureRequest) -> Path:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    assets = context.runtime_root_for_node(request.node.nodeid) / "dist"
    command = ["bun", "run", "build", "--outdir", str(assets)]
    directory = context.project_root / "src/clients/workbench"
    log_path = context.artifacts_dir / "workbench-build.log"
    with log_path.open("x", encoding="utf-8") as log:
        result = subprocess.run(
            command, cwd=directory, stdout=log, stderr=subprocess.STDOUT,
            timeout=180, check=False,
        )
    receipt: dict[str, object] = {
        "command": command, "cwd": str(directory), "exit_code": result.returncode,
        "log": str(log_path), "assets": str(assets),
    }
    if result.returncode == 0:
        receipt["version"] = json.loads((assets / "version.json").read_text(encoding="utf-8"))
        with (assets / "out/main.js").open("rb") as main:
            receipt["main_sha256"] = hashlib.file_digest(main, "sha256").hexdigest()
    (context.artifacts_dir / "workbench-build.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    assert result.returncode == 0, f"Workbench 构建失败：{log_path}\n{log_path.read_text(encoding='utf-8')}"
    return assets
