"""同一 pytest 会话复用正式 Workbench 构建，模块分别保留消费收据。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.processes.workspace import terminate_process

WORKBENCH_BUILD_TIMEOUT_SECONDS = 180
WORKBENCH_BUILD_MEMORY_LIMIT_KB = 4194304


@dataclass(frozen=True)
class WorkbenchBuild:
    assets: Path
    receipt_path: Path
    log_path: Path


@pytest.fixture(scope="session")
def shared_web_workbench_build(integration_test_gate: None) -> WorkbenchBuild:
    context = TestRunContext.from_test_file(Path("tests/harness/python/web_workbench.py")).prepare()
    assets = context.runtime_root_for_node("shared-workbench-build") / "dist"
    command = ["bun", "run", "build", "--outdir", str(assets)]
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    supervised_command = [
        sys.executable, str(supervisor),
        "--memory-limit-kb", str(WORKBENCH_BUILD_MEMORY_LIMIT_KB),
        "--timeout-seconds", str(WORKBENCH_BUILD_TIMEOUT_SECONDS), "--", *command,
    ]
    directory = context.project_root / "src/clients/workbench"
    log_path = context.artifacts_dir / "workbench-build.log"
    receipt: dict[str, object] = {
        "command": command, "cwd": str(directory), "exit_code": None,
        "log": str(log_path), "assets": str(assets), "timeout_seconds": WORKBENCH_BUILD_TIMEOUT_SECONDS,
        "supervised_command": supervised_command, "memory_limit_kb": WORKBENCH_BUILD_MEMORY_LIMIT_KB,
    }
    receipt_path = context.artifacts_dir / "workbench-build.json"
    started = time.monotonic()
    try:
        with log_path.open("x", encoding="utf-8") as log:
            process = subprocess.Popen(supervised_command, cwd=directory, stdout=log, stderr=subprocess.STDOUT, text=True)
            try:
                receipt["exit_code"] = process.wait()
            except BaseException as error:
                # 原监督器回收整个构建树；Python中断也先通知它完成清理，再关闭日志。
                try:
                    terminate_process(process)
                except BaseException as cleanup_error:
                    raise error from cleanup_error
                raise
            finally:
                receipt["supervisor_exit_code"] = process.returncode
        if process.returncode == 124:
            raise subprocess.TimeoutExpired(command, WORKBENCH_BUILD_TIMEOUT_SECONDS)
        if process.returncode == 0:
            receipt["version"] = json.loads((assets / "version.json").read_text(encoding="utf-8"))
            with (assets / "out/main.js").open("rb") as main:
                receipt["main_sha256"] = hashlib.file_digest(main, "sha256").hexdigest()
    except subprocess.TimeoutExpired as error:
        receipt["error"] = str(error)
        raise
    finally:
        receipt["elapsed_seconds"] = time.monotonic() - started
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    assert process.returncode == 0, f"Workbench 构建失败：{log_path}\n{log_path.read_text(encoding='utf-8')}"
    return WorkbenchBuild(assets, receipt_path, log_path)


@pytest.fixture(scope="module")
def web_workbench_assets(
    request: pytest.FixtureRequest, shared_web_workbench_build: WorkbenchBuild,
) -> Path:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    build = shared_web_workbench_build
    log_path = context.artifacts_dir / "workbench-build.log"
    shutil.copyfile(build.log_path, log_path)
    receipt = json.loads(build.receipt_path.read_text(encoding="utf-8"))
    receipt.update(log=str(log_path), shared_build_receipt=str(build.receipt_path))
    (context.artifacts_dir / "workbench-build.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return build.assets
