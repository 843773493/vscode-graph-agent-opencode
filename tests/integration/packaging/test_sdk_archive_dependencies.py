"""用包管理器真实 SDK 归档锁验证安装边界，不伪造锁或跳过依赖校验。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import commentjson
import pytest

from app.gateway.plugins.dependencies import _validate_node_lock, _validate_python_lock
from tests.harness.python.run_context import TestRunContext


@pytest.fixture(scope="module", params=["node", "python"])
def sdk_archive(request: pytest.FixtureRequest) -> tuple[str, Path, str]:
    """正式构建一次不可变归档，各场景只破坏自己的副本。"""
    language = request.param
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.run_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    output = context.run_root / "runtime" / "sdk"
    command = ["bun", "--eval", (
        'import { buildPluginSdk } from "./scripts/plugins/build-sdk.mjs"; '
        'console.log(JSON.stringify(buildPluginSdk({projectRoot: process.cwd(), '
        'language: process.argv[1], outputRoot: process.argv[2]})));'
    ), language, str(output)]
    result = subprocess.run(command, cwd=Path.cwd(), capture_output=True, text=True, timeout=60, check=False)
    (artifacts / f"{language}-build.stdout.log").write_text(result.stdout)
    (artifacts / f"{language}-build.stderr.log").write_text(result.stderr)
    assert result.returncode == 0, result.stderr
    built = json.loads(result.stdout)
    return language, Path(built["archive"]), built["version"]


@pytest.fixture
def sdk_project(sdk_archive: tuple[str, Path, str], tmp_path: Path) -> tuple[str, Path]:
    language, archive, sdk_version = sdk_archive
    project = tmp_path / language
    project.mkdir()
    relative = f"vendor/sdk/{archive.parent.name}/{archive.name}"
    destination = project / relative
    destination.parent.mkdir(parents=True)
    shutil.copyfile(archive, destination)
    if language == "node":
        (project / "package.json").write_text(json.dumps({"name": "sdk-test", "dependencies": {"@boxteam/plugin-sdk": f"file:{relative}"}}))
        result = subprocess.run(["bun", "--no-env-file", "install", "--lockfile-only", "--ignore-scripts"], cwd=project,
                                capture_output=True, text=True, timeout=30, check=False)
    else:
        (project / "pyproject.toml").write_text(f'''[project]
name = "sdk-test"
version = "1.0.0"
requires-python = ">=3.12"
dependencies = ["boxteam-plugin-sdk=={sdk_version}"]
[tool.uv.sources]
boxteam-plugin-sdk = {{ path = "{relative}" }}
''')
        result = subprocess.run(["uv", "--no-config", "lock", "--project", str(project)], cwd=project,
                                capture_output=True, text=True, timeout=30, check=False)
    (project / "lock.stdout").write_text(result.stdout)
    (project / "lock.stderr").write_text(result.stderr)
    assert result.returncode == 0, result.stderr
    return language, project


def validate(language: str, project: Path) -> None:
    if language == "node":
        _validate_node_lock(project / "package.json", project / "bun.lock")
    else:
        _validate_python_lock(project / "pyproject.toml", project / "uv.lock")


def test_native_sdk_lock_accepts_archive_and_rejects_tampering(sdk_project: tuple[str, Path]) -> None:
    language, project = sdk_project
    validate(language, project)
    archive = next((project / "vendor/sdk").glob("*/*"))
    original = archive.read_bytes()
    archive.write_bytes(original + b"tamper")
    with pytest.raises(ValueError, match="摘要"):
        validate(language, project)
    archive.write_bytes(original)
    archive.unlink()
    with pytest.raises(ValueError, match="不存在"):
        validate(language, project)


def test_native_sdk_lock_rejects_source_escape(sdk_project: tuple[str, Path]) -> None:
    language, project = sdk_project
    validate(language, project)
    if language == "node":
        file = project / "bun.lock"
        lock = commentjson.loads(file.read_text())
        lock["packages"]["@boxteam/plugin-sdk"][0] = "@boxteam/plugin-sdk@../outside.tgz"
        file.write_text(json.dumps(lock))
    else:
        file = project / "uv.lock"
        file.write_text(file.read_text().replace('source = { path = "vendor/', 'source = { path = "../vendor/'))
    with pytest.raises(ValueError):
        validate(language, project)
