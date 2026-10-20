from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest

from app.core.exceptions import ForbiddenError
from app.core.identifier import create_prefixed_id
from app.core.resource_uri import resource_display_uri
from app.core.workspace_identity import bind_workspace_id
from app.schemas.internal_v2.node_debug import NodeDebugBreakpointRequest
from app.services.infrastructure.node_debug.configuration.configuration_factory import (
    NodeDebugConfigurationFactory,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from tests.support.node_debug_dependencies import node_debug_file_paths


def _factory(tmp_path: Path) -> NodeDebugConfigurationFactory:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    return NodeDebugConfigurationFactory(file_paths=node_debug_file_paths(workspace_root))


def test_resolve_script_path_rejects_workspace_escape(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    outside = tmp_path / "outside.mjs"
    outside.write_text("console.log('outside');\n", encoding="utf-8")

    with pytest.raises(ForbiddenError, match="超出当前目标目录"):
        factory.resolve_script_path("../outside.mjs")


def test_script_and_working_directory_share_native_and_uri_targets(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    source_root = tmp_path / "workspace" / "src"
    source_root.mkdir()
    source = source_root / "中文 entry.mjs"
    source.write_text("export {};\n", encoding="utf-8")
    source_uri = factory.resolve_workspace_path(str(source))[1]
    directory_uri = factory.resolve_workspace_path(str(source_root))[1]

    for value in ("src/中文 entry.mjs", str(source), source_uri):
        assert factory.resolve_script_path(value) == (source, "src/中文 entry.mjs")
    for value in ("src", str(source_root), directory_uri):
        assert factory.resolve_working_directory(value) == source_root


def test_absolute_script_outside_workspace_is_not_redirected(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    source = tmp_path / "outside.mjs"
    source.write_text("export {};\n", encoding="utf-8")
    # 即使存在原绝对路径对应的工作区内假目标，也必须拒绝。
    decoy = tmp_path / "workspace" / str(source).lstrip("/")
    decoy.parent.mkdir(parents=True)
    decoy.write_text("throw new Error('wrong target');\n", encoding="utf-8")
    with pytest.raises(ForbiddenError):
        factory.resolve_script_path(str(source))


@pytest.mark.skipif(os.name == "nt", reason="Windows 路径应由 Windows 目标解释")
def test_windows_drive_is_not_reinterpreted_on_linux(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with pytest.raises(ValueError, match="Windows.*目标解释"):
        factory.resolve_workspace_path(r"C:\work\entry.mjs", script=True)


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


@pytest.fixture
def path_factory():
    def create(root: Path) -> NodeDebugConfigurationFactory:
        return NodeDebugConfigurationFactory(file_paths=WorkspaceFilePaths(
            workspace=root.resolve(), workspace_id="019d1b4a-1111-7000-8000-000000000001",
            bindings=None,
        ))
    return create


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
    path_factory,
) -> None:
    assert path_factory(tmp_path).workspace_relative_path(raw_path) == expected


@pytest.mark.parametrize(
    "raw_path",
    ["", "../secret", "src/../secret", "bad\x00path"],
)
def test_workspace_resolver_rejects_empty_or_escaping_paths(
    tmp_path: Path,
    raw_path: str,
    path_factory,
) -> None:
    with pytest.raises(ValueError):
        path_factory(tmp_path).workspace_relative_path(raw_path)


def test_workspace_resolver_maps_host_absolute_inside_workspace(
    tmp_path: Path,
    path_factory,
) -> None:
    workspace_root = tmp_path / "workspace"
    inside = (workspace_root / "src" / "main.mjs").resolve()

    assert (
        path_factory(workspace_root).workspace_relative_path(str(inside))
        == "src/main.mjs"
    )


def test_workspace_resolver_binds_root_without_relay_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_factory,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    resolver = path_factory(root)
    monkeypatch.chdir(tmp_path)
    assert resolver.workspace_relative_path("src/main.mjs") == "src/main.mjs"
    assert resolver.workspace_relative_path(".") == "."


def test_workspace_resolver_never_escapes_on_host_absolute_path(
    tmp_path: Path,
    path_factory,
) -> None:
    workspace_root = tmp_path / "workspace"
    resolver = path_factory(workspace_root)
    host_path = (tmp_path / "outside.mjs").resolve()

    with pytest.raises(ValueError, match="访问能力"):
        resolver.workspace_relative_path(str(host_path))


@pytest.mark.parametrize("raw_path", ["boxteam-session://current/attachments/att_owned", "boxteam-session://current/attachments/../secret"])
def test_ordinary_workspace_resolver_rejects_old_artifact_scheme(tmp_path: Path, raw_path: str, path_factory) -> None:
    with pytest.raises(ValueError):
        path_factory(tmp_path).workspace_relative_path(raw_path)


def test_three_inputs_share_workspace_target_without_identity_writes(tmp_path: Path, path_factory) -> None:
    workspace_id = "019d1b4a-1111-7000-8000-000000000001"
    bind_workspace_id(tmp_path, workspace_id)
    identity = tmp_path / ".boxteam/workspace-identity.json"
    before = identity.stat()
    resolver = path_factory(tmp_path)
    relative = "目录/文件 %.txt"
    uri = resource_display_uri(scope="workspace", scope_id=workspace_id, kind="files", tail_segments=("root", "目录", "文件 %.txt"))
    assert [resolver.workspace_relative_path(value) for value in [relative, str(tmp_path / relative), uri]] == [relative] * 3
    assert identity.stat().st_mtime_ns == before.st_mtime_ns


def test_explicit_file_owner_does_not_read_or_create_identity(tmp_path: Path, path_factory) -> None:
    uri = resource_display_uri(scope="workspace", scope_id="019d1b4a-1111-7000-8000-000000000001", kind="files", tail_segments=("root", "file.txt"))
    assert path_factory(tmp_path).workspace_relative_path(uri) == "file.txt"
    assert not (tmp_path / ".boxteam").exists()


@pytest.mark.parametrize("value", [
    "Boxteam://workspace/target/resources/files/root/file.txt",
    "file://target/file.txt",
    "boxteam://workspace/target/resources/skills/example/SKILL.md",
])
def test_unsupported_uri_never_becomes_native_filename(tmp_path: Path, value: str, path_factory) -> None:
    with pytest.raises(ValueError):
        path_factory(tmp_path).workspace_relative_path(value)
    assert not (tmp_path / ".boxteam").exists()


@pytest.mark.skipif(os.name == "nt", reason="非 Windows 目标的明确拒绝")
@pytest.mark.parametrize("value", ["C:/other.txt", r"C:\other.txt", r"\\server\share\other.txt", r"src\main.mjs"])
def test_foreign_paths_never_become_posix_workspace_targets(tmp_path: Path, value: str, path_factory) -> None:
    with pytest.raises(ValueError):
        path_factory(tmp_path).workspace_relative_path(value)


@pytest.mark.parametrize("input_kind", ["relative", "absolute", "uri"])
def test_symlink_outside_workspace_is_rejected(tmp_path: Path, input_kind: str, path_factory) -> None:
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
        path_factory(root).workspace_relative_path(value)
    assert outside.read_text(encoding="utf-8") == "outside"
