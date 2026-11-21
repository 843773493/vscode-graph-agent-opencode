"""测试入口必须编译本次 SDK 源码，不能以旧 dist 的通过替代验证。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def sdk_test_project(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, Path]:
    source = Path.cwd()
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(source / "sdk/node", root / "sdk/node",
                    ignore=shutil.ignore_patterns("node_modules", "dist", "build", "__pycache__"))
    runner = root / "tests/runner"
    runner.mkdir(parents=True)
    for name in ("run-tests.mjs", "performance.mjs", "run-workspace-services.mjs"):
        shutil.copyfile(source / "tests/runner" / name, runner / name)
    for name in ("resources.py", "report.py"):
        destination = root / "tests/harness/performance" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / "tests/harness/performance" / name, destination)
    package = root / "node_modules/@boxteam/plugin-sdk"
    package.parent.mkdir(parents=True)
    package.symlink_to(root / "sdk/node", target_is_directory=True)
    (root / "package.json").write_text('{"type":"module"}')
    # 只记录这个独立编译器成功完成的次数，不替代真实构建。
    builder = root / "sdk/node/build.mjs"
    builder.write_text(builder.read_text() + '\nimport { appendFileSync } from "node:fs";\n'
                       'appendFileSync(process.env.SDK_BUILD_MARKER, "built\\n");\n')
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir / request.node.name.replace("[", "-").replace("]", "")
    artifacts.mkdir(parents=True, exist_ok=True)
    return root, artifacts


def run(project: tuple[Path, Path], label: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    root, artifacts = project
    result = subprocess.run(command, cwd=root, env={**os.environ,
        "BOXTEAM_PROJECT_ROOT": str(root), "SDK_BUILD_MARKER": str(root / "builds.txt"),
        "BOXTEAM_TEST_PREPARED_NODE_SDK_ROOT": "", "BOXTEAM_TEST_RUN_ID": label,
    }, capture_output=True, text=True, timeout=30, check=False)
    (artifacts / f"{label}.stdout.log").write_text(result.stdout)
    (artifacts / f"{label}.stderr.log").write_text(result.stderr)
    (artifacts / f"{label}.json").write_text(json.dumps({
        "command": command, "cwd": str(root), "exit_code": result.returncode,
    }, indent=2) + "\n")
    return result


def prepare_stale_sdk(project: tuple[Path, Path]) -> Path:
    root, _ = project
    source = root / "sdk/node/src/maintenance.ts"
    original = source.read_text()
    source.write_text(original + '\nexport const preparationProbe = "old";\n')
    prepared = run(project, "initial-build", ["node", "sdk/node/build.mjs"])
    assert prepared.returncode == 0, prepared.stderr
    source.write_text(original + '\nexport const preparationProbe = "current";\n')
    (root / "builds.txt").unlink()
    return source


def write_matrix(root: Path, command: list[str]) -> None:
    (root / "tests/runner/matrix.jsonc").write_text(json.dumps({
        "schemaVersion": 1, "suites": [{
            "id": "sdk-consumer", "layer": "integration", "client": None, "enabled": True,
            "timeoutMs": 10000, "dataLimitKb": 4194304, "command": command,
        }],
    }))


def write_consumer(root: Path, file: Path, engine: str) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(f'import {{ test }} from "{engine}:test";\n'
        'import assert from "node:assert/strict";\n'
        'import { writeFileSync } from "node:fs";\n'
        'import { preparationProbe } from "@boxteam/plugin-sdk/maintenance";\n'
        'test("读取本次 SDK", () => {\n'
        '  assert.equal(preparationProbe, "current");\n'
        f'  writeFileSync({json.dumps(str(root / f"{engine}.marker"))}, "executed");\n'
        '});\n')


@pytest.mark.parametrize("engine", ["bun", "node"])
def test_matrix_rebuilds_changed_sdk_and_refuses_stale_success(sdk_test_project, engine: str) -> None:
    root, _ = sdk_test_project
    source = prepare_stale_sdk(sdk_test_project)
    write_consumer(root, root / "consumer.test.mjs", engine)
    write_matrix(root, [engine, "test" if engine == "bun" else "--test", "consumer.test.mjs"])
    command = ["bun", "tests/runner/run-tests.mjs", "--suite=sdk-consumer", "--perf=off"]
    listed = run(sdk_test_project, "list", ["bun", "tests/runner/run-tests.mjs", "--list"])
    assert listed.returncode == 0, listed.stderr
    assert not (root / "builds.txt").exists()
    current = run(sdk_test_project, "current-source", command)
    assert current.returncode == 0, current.stdout + current.stderr
    assert (root / "builds.txt").read_text().splitlines() == ["built"]
    marker = root / f"{engine}.marker"
    assert marker.read_text() == "executed"
    marker.unlink()
    valid = source.read_text()
    source.write_text("export const invalid = ;\n")
    invalid = run(sdk_test_project, "syntax-error", command)
    assert invalid.returncode != 0
    assert "测试 SDK 构建失败" in invalid.stderr
    assert not marker.exists()
    source.write_text(valid + '\nthrow new Error("current_sdk_runtime_failure");\n')
    failed = run(sdk_test_project, "runtime-error", command)
    assert failed.returncode != 0
    assert "current_sdk_runtime_failure" in failed.stdout + failed.stderr
    assert not marker.exists()


@pytest.mark.parametrize("through_matrix", [False, True])
def test_workspace_services_prepare_once_for_both_engines(sdk_test_project, through_matrix: bool) -> None:
    root, _ = sdk_test_project
    source = prepare_stale_sdk(sdk_test_project)
    for engine in ("bun", "node"):
        write_consumer(root, root / f"src/workspace-services/{engine}.test.js", engine)
    command = ["bun", "tests/runner/run-workspace-services.mjs"]
    if through_matrix:
        write_matrix(root, command)
        command = ["bun", "tests/runner/run-tests.mjs", "--suite=sdk-consumer", "--perf=off"]
    result = run(sdk_test_project, "workspace-current", command)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "builds.txt").read_text().splitlines() == ["built"]
    for engine in ("bun", "node"):
        assert (root / f"{engine}.marker").read_text() == "executed"
        (root / f"{engine}.marker").unlink()
    source.write_text("export const invalid = ;\n")
    failed = run(sdk_test_project, "workspace-syntax-error", command)
    assert failed.returncode != 0
    assert "测试 SDK 构建失败" in failed.stderr
    assert not list(root.glob("*.marker"))
