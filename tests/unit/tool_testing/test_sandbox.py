from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.exceptions import ForbiddenError
from app.core.resource_uri import resource_display_uri
from app.core.workspace_identity import read_workspace_id
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from app.tool_testing.cases.apply_patch_case import create_apply_patch_cases
from app.tool_testing.sandbox import ToolTestFilePaths


@pytest.fixture
def attempt_root(tmp_path: Path) -> Path:
    return tmp_path / ".boxteam" / "tool_tests" / "apply_patch" / "provider" / "case" / "workspace"


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("input_kind", ["relative", "absolute", "uri"])
async def test_real_patch_in_private_attempt_uses_same_file_owner(
    tmp_path: Path, attempt_root: Path, asynchronous: bool, input_kind: str,
) -> None:
    case = create_apply_patch_cases()[0]
    prepared = case.prepare(workspace_root=tmp_path, attempt_root=attempt_root, asset_root=tmp_path)
    workspace_id = read_workspace_id(attempt_root)
    root_uri = resource_display_uri(scope="workspace", scope_id=workspace_id, kind="files", tail_segments=("root",))
    arguments = {
        "input": "*** Begin Patch\n*** Update File: target.txt\n@@\n alpha\n-beta\n+gamma\n+delta\n*** End Patch",
        "explanation": "隔离目录真实修改",
        "expected_versions": json.loads(prepared.prompt.split("expected_versions = ")[1]),
        "target": {"relative": ".", "absolute": str(attempt_root), "uri": root_uri}[input_kind],
    }
    result = await prepared.tool.ainvoke(arguments) if asynchronous else prepared.tool.invoke(arguments)
    receipt = json.loads(result)
    assert receipt["status"] == "success"
    assert receipt["files"][0]["path"] == root_uri + "/target.txt"
    assert receipt["files"][0]["state"] == "published"
    assert case.evaluate(attempt_root=attempt_root, tool_result=result).passed
    # 普通入口仍无法读取同一个私有 attempt，内部授权不扩散到文件树。
    ordinary = WorkspaceFilePaths(workspace=tmp_path, workspace_id=workspace_id, bindings=None)
    with pytest.raises(ForbiddenError):
        ordinary.resolve(str(attempt_root / "target.txt"))


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("boundary", ["parent", "sibling", "private", "symlink"])
async def test_real_patch_rejects_targets_outside_attempt_capability(
    tmp_path: Path, attempt_root: Path, asynchronous: bool, boundary: str,
) -> None:
    case = create_apply_patch_cases()[0]
    prepared = case.prepare(workspace_root=tmp_path, attempt_root=attempt_root, asset_root=tmp_path)
    outside = attempt_root.parent / "sibling"
    outside.mkdir()
    (outside / "target.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    private = attempt_root / ".boxteam" / "target.txt"
    private.write_text("alpha\nbeta\n", encoding="utf-8")
    target, name = {
        "parent": (str(attempt_root.parent), "sibling/target.txt"),
        "sibling": (str(outside), "target.txt"),
        "private": (".", ".boxteam/target.txt"),
        "symlink": (".", "alias/target.txt"),
    }[boundary]
    if boundary == "symlink":
        business = tmp_path / ".boxteam" / "sessions" / "private-data"
        business.mkdir(parents=True)
        (business / "target.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        (attempt_root / "alias").symlink_to(business, target_is_directory=True)
    before = {path: path.read_bytes() for path in tmp_path.rglob("target.txt")}
    arguments = {
        "input": f"*** Begin Patch\n*** Update File: {name}\n@@\n alpha\n-beta\n+changed\n*** End Patch",
        "explanation": "验证能力边界",
        "expected_versions": {name: json.loads(prepared.prompt.split("expected_versions = ")[1])["target.txt"]},
        "target": target,
    }
    result = await prepared.tool.ainvoke(arguments) if asynchronous else prepared.tool.invoke(arguments)
    receipt = json.loads(result)
    assert receipt["status"] == "error"
    assert receipt["effect"] == "not_executed"
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("boundary", ["store", "outside", "alias", "nested_private"])
def test_attempt_root_must_be_owned_without_alias(
    tmp_path: Path, boundary: str,
) -> None:
    store = tmp_path / ".boxteam" / "tool_tests"
    store.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = store / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    target = {"store": store, "outside": outside, "alias": alias / "attempt", "nested_private": store / ".boxteam" / "attempt"}[boundary]
    with pytest.raises(ForbiddenError):
        ToolTestFilePaths(workspace_root=tmp_path, attempt_root=target)
    assert not (outside / ".boxteam").exists()
