"""以固定旧产品进程生成 schema 3；当前产品只负责离线迁移。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_test_workspace

LEGACY_COMMIT = "cb90835523b9ff6d91e3f8dfaf3491855d55191d"
CASES_MODULE = "tests/integration/backend/sessions/attachment_migration/legacy_cases.py"
BODY = b"original attachment for offline directory migration\n"


@dataclass(frozen=True)
class BackupCase:
    workspace: Path
    output: Path
    index_path: Path
    original: Path
    session_id: str
    thread_id: str
    message_id: str
    canonical_items: tuple[tuple[object, ...], ...]
    evidence: dict[str, object]


@pytest.fixture(scope="session")
def legacy_schema3_inputs() -> Callable[[str], tuple[Path, dict[str, object]]]:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    runtime = context.runtime_root_for_node("legacy-schema3-source") / uuid4().hex
    runtime.mkdir(parents=True)
    source = runtime / "source"
    source.mkdir()
    archive = runtime / "source.tar"
    # 必须完整导出；缺失固定提交时明确失败，不能回退到工作树或手工生成旧表。
    git_root = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
    subprocess.run(["git", "-C", git_root, "archive", "--format=tar", "--output", str(archive), LEGACY_COMMIT],
                   timeout=30, check=True)
    with tarfile.open(archive) as stream:
        stream.extractall(source, filter="data")
    archive.unlink()
    driver = source / CASES_MODULE
    driver.parent.mkdir()
    shutil.copy2(Path.cwd() / CASES_MODULE, driver)
    tree = subprocess.check_output(["git", "rev-parse", LEGACY_COMMIT + "^{tree}"], text=True).strip()
    generated: dict[str, tuple[Path, dict[str, object]]] = {}

    def generate(scenario: str) -> tuple[Path, dict[str, object]]:
        if scenario in generated:
            return generated[scenario]
        target = runtime / scenario
        target.mkdir()
        receipt = target / "receipt.json"
        command = [sys.executable, "-m", "pytest", "-q", CASES_MODULE + "::test_generate_" + scenario]
        output = Path.cwd() / "out/tests/integration/backend/sessions/attachment_migration/legacy_cases"
        environment = {**os.environ, "BOXTEAM_LEGACY_INPUT_RECEIPT": str(receipt),
                       "BOXTEAM_LEGACY_INPUT_OUTPUT": str(output)}
        # 父套件的筛选条件不能筛掉独立旧产品进程中的输入生成节点。
        environment.pop("PYTEST_ADDOPTS", None)
        # 外层正式 runner 提供进程组硬超时与内存保护；此处限制单次旧产品生成。
        result = subprocess.run(command, cwd=source, env=environment, capture_output=True, text=True,
                                timeout=100, check=False)
        (target / "stdout.log").write_text(result.stdout, encoding="utf-8")
        (target / "stderr.log").write_text(result.stderr, encoding="utf-8")
        (target / "provenance.json").write_text(json.dumps({
            "commit": LEGACY_COMMIT, "tree": tree, "argv": command, "exit_code": result.returncode,
        }, indent=2) + "\n", encoding="utf-8")
        assert result.returncode == 0, f"固定旧产品输入生成失败：{target}\n{result.stdout}\n{result.stderr}"
        metadata = json.loads(receipt.read_bytes())
        workspace = output / "workspace"
        saved = target / "workspace"
        shutil.move(workspace, saved)
        generated[scenario] = saved, metadata
        return saved, metadata

    return generate


@pytest.fixture
def legacy_case_factory(
    request: pytest.FixtureRequest, tmp_path: Path,
    legacy_schema3_inputs: Callable[[str], tuple[Path, dict[str, object]]],
) -> Callable[[str], BackupCase]:
    def load(scenario: str) -> BackupCase:
        source, metadata = legacy_schema3_inputs(scenario)
        context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
        workspace = prepare_test_workspace(
            workspace_root=context.workspace_root, template_root=source,
            template_items=tuple(path.name for path in source.iterdir()),
        )
        return BackupCase(
            workspace=workspace, output=tmp_path / "snapshot", index_path=workspace / metadata["index"],
            original=workspace / metadata["original"], session_id=metadata["session_id"],
            thread_id=metadata["thread_id"], message_id=metadata["message_id"],
            canonical_items=tuple(tuple(row) for row in metadata["canonical_items"]), evidence=metadata,
        )

    return load


@pytest.fixture
def backup_case(legacy_case_factory: Callable[[str], BackupCase]) -> BackupCase:
    return legacy_case_factory("queued")


@pytest.fixture
def shared_blob_case(legacy_case_factory: Callable[[str], BackupCase]) -> BackupCase:
    return legacy_case_factory("shared")
