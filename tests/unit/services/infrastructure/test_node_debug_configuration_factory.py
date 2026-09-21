from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from app.core.exceptions import ForbiddenError
from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.node_debug import NodeDebugBreakpointRequest
from app.services.infrastructure.node_debug.configuration.configuration_factory import (
    NodeDebugConfigurationFactory,
)


def _factory(tmp_path: Path) -> NodeDebugConfigurationFactory:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    return NodeDebugConfigurationFactory(workspace_root=workspace_root)


def test_resolve_script_path_rejects_workspace_escape(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    outside = tmp_path / "outside.mjs"
    outside.write_text("console.log('outside');\n", encoding="utf-8")

    with pytest.raises(ForbiddenError, match="Path traversal"):
        factory.resolve_script_path("../outside.mjs")


def test_resolve_working_directory_requires_existing_directory_inside_workspace(
    tmp_path: Path,
) -> None:
    factory = _factory(tmp_path)
    workspace_root = tmp_path / "workspace"
    source_root = workspace_root / "src"
    source_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    assert factory.resolve_working_directory("") == workspace_root.resolve()
    assert factory.resolve_working_directory("src") == source_root.resolve()
    with pytest.raises(ValueError, match="必须位于当前 workspace 内"):
        factory.resolve_working_directory(str(outside))
    with pytest.raises(FileNotFoundError, match="调试工作目录不存在"):
        factory.resolve_working_directory("missing")


def test_normalize_args_enforces_string_and_count_contract(
    tmp_path: Path,
) -> None:
    factory = _factory(tmp_path)

    assert factory.normalize_args(["--inspect", "entry.mjs"]) == [
        "--inspect",
        "entry.mjs",
    ]
    with pytest.raises(ValueError, match="最多 20 个"):
        factory.normalize_args(["--arg"] * 21)
    with pytest.raises(TypeError, match="必须全部是字符串"):
        factory.normalize_args(cast(list[str], ["--arg", 1]))


def test_configuration_from_request_normalizes_paths_args_and_breakpoint(
    tmp_path: Path,
) -> None:
    factory = _factory(tmp_path)
    workspace_root = tmp_path / "workspace"
    source_root = workspace_root / "src"
    source_root.mkdir()
    source = source_root / "entry.mjs"
    source.write_text(
        "const answer = 41;\nconsole.log(answer);\nanswer += 1;\n",
        encoding="utf-8",
    )

    configuration = factory.configuration_from_request(
        configuration_id=create_prefixed_id("dbgcfg"),
        name="  入口调试  ",
        script_path="src\\entry.mjs",
        working_directory="src",
        launch_profile_name="node-default",
        args=["--inspect", "entry.mjs"],
        breakpoints=[
            NodeDebugBreakpointRequest(
                path="src\\entry.mjs",
                line=2,
                column=3,
                condition=" answer > 0 ",
                hit_condition=2,
                log_message="answer={answer}",
            )
        ],
    )

    assert configuration.name == "入口调试"
    assert configuration.script_path == "src/entry.mjs"
    assert configuration.working_directory == "src"
    assert configuration.args == ["--inspect", "entry.mjs"]
    assert len(configuration.breakpoints) == 1
    breakpoint = configuration.breakpoints[0]
    assert breakpoint.path == "src/entry.mjs"
    assert breakpoint.line == 2
    assert breakpoint.column == 3
    assert breakpoint.condition == "answer > 0"
    assert breakpoint.hit_condition == 2
    assert breakpoint.log_message == "answer={answer}"
    assert breakpoint.source_line == "console.log(answer);"
    assert breakpoint.source_digest is not None
