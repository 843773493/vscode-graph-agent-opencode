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
    ["", "../secret", "~/secret", "C:/secret", "/src/main.mjs"],
)
def test_normalize_workspace_relative_path_rejects_absolute_or_escaping_paths(
    raw_path: str,
) -> None:
    with pytest.raises(ValueError):
        normalize_workspace_relative_path(raw_path)


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


def test_workspace_resolver_rejects_host_absolute_path(
    tmp_path: Path,
) -> None:
    resolver = WorkspaceToolPathResolver(tmp_path / "workspace")
    host_path = (tmp_path / "outside.mjs").resolve()

    with pytest.raises(ValueError, match="必须是工作区相对路径"):
        resolver.resolve_workspace_path(str(host_path))


def test_backend_virtual_path_is_hidden_from_model_output() -> None:
    assert backend_virtual_to_workspace_relative("/src/main.mjs") == "src/main.mjs"
    assert backend_virtual_to_workspace_relative("/") == "."
