from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.mounted_workspaces import MountedWorkspaceRegistry
from app.core.workspace_identity import load_or_create_workspace_id


def _workspace(root: Path) -> tuple[str, Path]:
    root.mkdir(parents=True)
    return load_or_create_workspace_id(root), root


def test_registry_resolves_each_mounted_workspace_without_root_aliases(tmp_path):
    workspace_a = _workspace(tmp_path / "a")
    workspace_b = _workspace(tmp_path / "b")
    registry = MountedWorkspaceRegistry.from_payload(
        [
            {"workspace_id": workspace_a[0], "workspace_root": str(workspace_a[1]), "terminal_backend_url": None, "browser_backend_url": None, "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None},
            {"workspace_id": workspace_b[0], "workspace_root": str(workspace_b[1]), "terminal_backend_url": None, "browser_backend_url": None, "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None},
        ]
    )

    assert registry.resolve(workspace_a[0]).workspace_root == workspace_a[1].resolve()
    assert registry.resolve(workspace_b[0]).workspace_root == workspace_b[1].resolve()
    assert registry.workspace_ids == (workspace_a[0], workspace_b[0])
    with pytest.raises(KeyError, match="未挂载"):
        registry.resolve(str(uuid4()))


@pytest.mark.parametrize(
    "raw_value",
    [None, "", "[]", "{}", "not-json"],
)
def test_environment_requires_nonempty_json_mount_array(monkeypatch, raw_value):
    if raw_value is None:
        monkeypatch.delenv(MountedWorkspaceRegistry.ENVIRONMENT_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(MountedWorkspaceRegistry.ENVIRONMENT_VARIABLE, raw_value)

    with pytest.raises((RuntimeError, ValueError)):
        MountedWorkspaceRegistry.from_environment()


def test_registry_rejects_duplicate_ids_and_roots(tmp_path):
    workspace_id, workspace_root = _workspace(tmp_path / "workspace")

    with pytest.raises(ValueError, match="重复 workspace_id"):
        MountedWorkspaceRegistry.from_payload(
            [
                {"workspace_id": workspace_id, "workspace_root": str(workspace_root), "terminal_backend_url": None, "browser_backend_url": None, "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None},
                {"workspace_id": workspace_id, "workspace_root": str(tmp_path), "terminal_backend_url": None, "browser_backend_url": None, "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None},
            ]
        )

    with pytest.raises(ValueError, match="重复工作区根"):
        MountedWorkspaceRegistry.from_payload(
            [
                {"workspace_id": workspace_id, "workspace_root": str(workspace_root), "terminal_backend_url": None, "browser_backend_url": None, "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None},
                {"workspace_id": str(uuid4()), "workspace_root": str(workspace_root), "terminal_backend_url": None, "browser_backend_url": None, "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None},
            ]
        )


def test_registry_rejects_identity_mismatch(tmp_path):
    actual_id, workspace_root = _workspace(tmp_path / "workspace")
    with pytest.raises(RuntimeError, match="身份文件不一致"):
        MountedWorkspaceRegistry.from_payload(
            [
                {
                    "workspace_id": str(uuid4()),
                    "workspace_root": str(workspace_root), "terminal_backend_url": None, "browser_backend_url": None, "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None}
            ]
        )
    assert load_or_create_workspace_id(workspace_root) == actual_id


def test_environment_mount_payload_uses_explicit_complete_startup_contract(
    monkeypatch, tmp_path
):
    workspace_id, workspace_root = _workspace(tmp_path / "workspace")
    monkeypatch.setenv(
        MountedWorkspaceRegistry.ENVIRONMENT_VARIABLE,
        json.dumps(
            [{"workspace_id": workspace_id, "workspace_root": str(workspace_root), "terminal_backend_url": None, "browser_backend_url": None, "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None}]
        ),
    )

    registry = MountedWorkspaceRegistry.from_environment()

    assert registry.resolve(workspace_id).workspace_root == workspace_root.resolve()
