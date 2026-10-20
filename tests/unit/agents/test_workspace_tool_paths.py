from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.agents.workspace_tool_paths import (
    WorkspaceToolPathResolver,
    backend_virtual_to_workspace_relative,
    normalize_workspace_relative_path,
)
from app.core.resource_uri import resource_display_uri
from app.core.workspace_identity import bind_workspace_id


@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        ("src/main.mjs", "src/main.mjs"),
        ("./src/main.mjs", "src/main.mjs"),
        (".", "."),
    ],
)
def test_normalize_workspace_relative_path_uses_standard_paths(
    raw_path: str,
    expected: str,
) -> None:
    assert normalize_workspace_relative_path(raw_path) == expected


@pytest.mark.parametrize(
    "raw_path",
    ["", "../secret", "~/secret", "C:/secret"],
)
def test_normalize_workspace_relative_path_rejects_empty_or_escaping_paths(
    raw_path: str,
) -> None:
    with pytest.raises(ValueError):
        normalize_workspace_relative_path(raw_path)


def test_user_absolute_path_requires_explicit_target() -> None:
    with pytest.raises(ValueError, match="明确目标工作区"):
        normalize_workspace_relative_path("/src/main.mjs")


def test_normalize_workspace_relative_path_maps_host_absolute_inside_workspace(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    inside = (workspace_root / "src" / "main.mjs").resolve()

    assert (
        normalize_workspace_relative_path(
            str(inside),
            workspace_root=workspace_root,
        )
        == "src/main.mjs"
    )


def test_workspace_resolver_maps_relative_path_to_internal_virtual_path(
    tmp_path: Path,
) -> None:
    resolver = WorkspaceToolPathResolver(tmp_path)

    assert resolver.resolve_workspace_path("src/main.mjs") == (
        tmp_path / "src" / "main.mjs"
    )
    assert resolver.backend_virtual_path("src/main.mjs") == "/src/main.mjs"
    assert resolver.backend_virtual_path(".") == "/"
    assert resolver.workspace_relative_path(".") == "."


def test_workspace_resolver_never_escapes_on_host_absolute_path(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    resolver = WorkspaceToolPathResolver(workspace_root)
    host_path = (tmp_path / "outside.mjs").resolve()

    with pytest.raises(ValueError, match="访问能力"):
        resolver.resolve_workspace_path(str(host_path))


def test_backend_virtual_path_is_hidden_from_model_output() -> None:
    assert backend_virtual_to_workspace_relative("/src/main.mjs") == "src/main.mjs"
    assert backend_virtual_to_workspace_relative("/") == "."


@pytest.mark.parametrize("raw_path", ["boxteam-session://current/attachments/att_owned", "session-artifacts/current/attachments/att_owned"])
def test_attachment_uri_and_alias_share_one_internal_route(raw_path: str) -> None:
    assert normalize_workspace_relative_path(raw_path) == "session-artifacts/current/attachments/att_owned"


@pytest.mark.parametrize("raw_path", ["boxteam-session://current/attachments/../secret", "boxteam-session://current/attachments/att_owned?owner=other", "boxteam-session://current/attachments/att_owned#other"])
def test_attachment_uri_rejects_hidden_locator_overrides(raw_path: str) -> None:
    with pytest.raises(ValueError):
        normalize_workspace_relative_path(raw_path)


def test_three_inputs_share_workspace_target_without_identity_writes(tmp_path: Path) -> None:
    workspace_id = "019d1b4a-1111-7000-8000-000000000001"
    bind_workspace_id(tmp_path, workspace_id)
    identity = tmp_path / ".boxteam/workspace-identity.json"
    before = identity.stat()
    resolver = WorkspaceToolPathResolver(tmp_path)
    relative = "目录/文件 %.txt"
    uri = resource_display_uri(scope="workspace", scope_id=workspace_id, kind="files", tail_segments=("root", "目录", "文件 %.txt"))
    assert [resolver.resolve_workspace_path(value) for value in [relative, str(tmp_path / relative), uri]] == [tmp_path / relative] * 3
    assert identity.stat().st_mtime_ns == before.st_mtime_ns


def test_uri_does_not_create_missing_workspace_identity(tmp_path: Path) -> None:
    uri = resource_display_uri(scope="workspace", scope_id="019d1b4a-1111-7000-8000-000000000001", kind="files", tail_segments=("root", "file.txt"))
    with pytest.raises(FileNotFoundError):
        WorkspaceToolPathResolver(tmp_path).resolve_workspace_path(uri)
    assert not (tmp_path / ".boxteam").exists()


@pytest.mark.parametrize("value", [
    "Boxteam://workspace/target/resources/files/root/file.txt",
    "file://target/file.txt",
    "boxteam://workspace/target/resources/skills/example/SKILL.md",
])
def test_unsupported_uri_never_becomes_native_filename(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError):
        WorkspaceToolPathResolver(tmp_path).resolve_workspace_path(value)
    assert not (tmp_path / ".boxteam").exists()


@pytest.mark.skipif(os.name == "nt", reason="非 Windows 目标的明确拒绝")
@pytest.mark.parametrize("value", ["C:/other.txt", r"C:\other.txt", r"\\server\share\other.txt", r"src\main.mjs"])
def test_foreign_paths_never_become_posix_workspace_targets(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError):
        WorkspaceToolPathResolver(tmp_path).resolve_workspace_path(value)
