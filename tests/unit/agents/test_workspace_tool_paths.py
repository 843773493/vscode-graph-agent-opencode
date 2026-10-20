from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.agents.workspace_tool_paths import WorkspaceToolPathResolver
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
def test_workspace_resolver_uses_standard_paths(
    tmp_path: Path,
    raw_path: str,
    expected: str,
) -> None:
    assert WorkspaceToolPathResolver(tmp_path).workspace_relative_path(raw_path) == expected


@pytest.mark.parametrize(
    "raw_path",
    ["", "../secret", "src/../secret", "bad\x00path"],
)
def test_workspace_resolver_rejects_empty_or_escaping_paths(
    tmp_path: Path,
    raw_path: str,
) -> None:
    with pytest.raises(ValueError):
        WorkspaceToolPathResolver(tmp_path).workspace_relative_path(raw_path)


def test_workspace_resolver_maps_host_absolute_inside_workspace(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    inside = (workspace_root / "src" / "main.mjs").resolve()

    assert (
        WorkspaceToolPathResolver(workspace_root).workspace_relative_path(str(inside))
        == "src/main.mjs"
    )


def test_workspace_resolver_binds_root_without_relay_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    resolver = WorkspaceToolPathResolver(root)
    monkeypatch.chdir(tmp_path)
    assert resolver.workspace_relative_path("src/main.mjs") == "src/main.mjs"
    assert resolver.workspace_relative_path(".") == "."


def test_workspace_resolver_never_escapes_on_host_absolute_path(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    resolver = WorkspaceToolPathResolver(workspace_root)
    host_path = (tmp_path / "outside.mjs").resolve()

    with pytest.raises(ValueError, match="访问能力"):
        resolver.workspace_relative_path(str(host_path))


@pytest.mark.parametrize("raw_path", ["boxteam-session://current/attachments/att_owned", "boxteam-session://current/attachments/../secret"])
def test_ordinary_workspace_resolver_rejects_old_artifact_scheme(tmp_path: Path, raw_path: str) -> None:
    with pytest.raises(ValueError):
        WorkspaceToolPathResolver(tmp_path).workspace_relative_path(raw_path)


def test_three_inputs_share_workspace_target_without_identity_writes(tmp_path: Path) -> None:
    workspace_id = "019d1b4a-1111-7000-8000-000000000001"
    bind_workspace_id(tmp_path, workspace_id)
    identity = tmp_path / ".boxteam/workspace-identity.json"
    before = identity.stat()
    resolver = WorkspaceToolPathResolver(tmp_path)
    relative = "目录/文件 %.txt"
    uri = resource_display_uri(scope="workspace", scope_id=workspace_id, kind="files", tail_segments=("root", "目录", "文件 %.txt"))
    assert [resolver.workspace_relative_path(value) for value in [relative, str(tmp_path / relative), uri]] == [relative] * 3
    assert identity.stat().st_mtime_ns == before.st_mtime_ns


def test_uri_does_not_create_missing_workspace_identity(tmp_path: Path) -> None:
    uri = resource_display_uri(scope="workspace", scope_id="019d1b4a-1111-7000-8000-000000000001", kind="files", tail_segments=("root", "file.txt"))
    with pytest.raises(FileNotFoundError):
        WorkspaceToolPathResolver(tmp_path).workspace_relative_path(uri)
    assert not (tmp_path / ".boxteam").exists()


@pytest.mark.parametrize("value", [
    "Boxteam://workspace/target/resources/files/root/file.txt",
    "file://target/file.txt",
    "boxteam://workspace/target/resources/skills/example/SKILL.md",
])
def test_unsupported_uri_never_becomes_native_filename(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError):
        WorkspaceToolPathResolver(tmp_path).workspace_relative_path(value)
    assert not (tmp_path / ".boxteam").exists()


@pytest.mark.skipif(os.name == "nt", reason="非 Windows 目标的明确拒绝")
@pytest.mark.parametrize("value", ["C:/other.txt", r"C:\other.txt", r"\\server\share\other.txt", r"src\main.mjs"])
def test_foreign_paths_never_become_posix_workspace_targets(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError):
        WorkspaceToolPathResolver(tmp_path).workspace_relative_path(value)


@pytest.mark.parametrize("input_kind", ["relative", "absolute", "uri"])
def test_symlink_outside_workspace_is_rejected(tmp_path: Path, input_kind: str) -> None:
    root = tmp_path / "workspace"
    workspace_id = bind_workspace_id(root, "019d1b4a-1111-7000-8000-000000000001")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    value = {
        "relative": "link.txt",
        "absolute": str(root / "link.txt"),
        "uri": resource_display_uri(scope="workspace", scope_id=workspace_id, kind="files", tail_segments=("root", "link.txt")),
    }[input_kind]
    with pytest.raises(ValueError, match="访问能力"):
        WorkspaceToolPathResolver(root).workspace_relative_path(value)
    assert outside.read_text(encoding="utf-8") == "outside"
