from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.workspace_tool_paths import (
    WorkspaceToolPathResolver,
    backend_virtual_to_workspace_relative,
    normalize_workspace_relative_path,
)


@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        ("src/main.mjs", "src/main.mjs"),
        ("./src/main.mjs", "src/main.mjs"),
        (".", "."),
        (r"src\main.mjs", "src/main.mjs"),
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


def test_normalize_workspace_relative_path_strips_virtual_absolute_prefix() -> None:
    assert normalize_workspace_relative_path("/src/main.mjs") == "src/main.mjs"


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

    resolved = resolver.resolve_workspace_path(str(host_path))

    # 工作区外的宿主机绝对路径被收敛回工作区内，绝不逃出工作区。
    assert resolved.resolve().is_relative_to(workspace_root.resolve())
    assert resolved.resolve() != host_path


def test_backend_virtual_path_is_hidden_from_model_output() -> None:
    assert backend_virtual_to_workspace_relative("/src/main.mjs") == "src/main.mjs"
    assert backend_virtual_to_workspace_relative("/") == "."
