from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemState
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from pydantic import ValidationError

from app.agents.workspace_backend import build_workspace_backend
from app.agents.workspace_filesystem_tools import configure_workspace_filesystem_tools
from app.services.infrastructure.tool_output_store import (
    ToolOutputStore,
    extract_tool_output_reference,
)


@pytest.fixture
def configured_read_tool(tmp_path):
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_workspace_filesystem_tools(middleware, workspace_root=tmp_path)
    tool = next(tool for tool in middleware.tools if tool.name == "read_file")
    return tmp_path, tool


def test_read_file_exposes_agent_visible_schema(configured_read_tool):
    _workspace_root, tool = configured_read_tool

    schema = tool.tool_call_schema.model_json_schema()

    assert set(schema["properties"]) == {"path", "line_offset", "max_lines"}
    assert schema["required"] == ["path"]
    assert "additionalProperties" not in schema
    assert schema["properties"]["line_offset"]["default"] == 1
    assert "file_path" not in schema["properties"]
    assert "offset" not in schema["properties"]
    assert "limit" not in schema["properties"]


def test_read_file_marks_runtime_as_injected_for_langchain_callbacks(
    configured_read_tool,
):
    _workspace_root, tool = configured_read_tool

    assert tool._injected_args_keys == {"runtime"}


@pytest.mark.asyncio
async def test_read_file_accepts_langgraph_injected_runtime(configured_read_tool):
    workspace_root, tool = configured_read_tool
    file_path = workspace_root / "injected-runtime.txt"
    file_path.write_text("runtime works\n", encoding="utf-8")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_injected_runtime"),
    )

    result = await tool.ainvoke(
        {
            "name": "read_file",
            "args": {
                "path": "injected-runtime.txt",
                "runtime": runtime,
            },
            "id": "call_injected_runtime",
            "type": "tool_call",
        }
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert "runtime works" in result.content


@pytest.mark.asyncio
async def test_read_file_rejects_unknown_arguments(configured_read_tool):
    workspace_root, tool = configured_read_tool
    file_path = workspace_root / "strict-schema.txt"
    file_path.write_text("strict schema works\n", encoding="utf-8")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_strict_schema"),
    )

    with pytest.raises(ValidationError):
        await tool.ainvoke(
            {
                "name": "read_file",
                "args": {
                    "path": "strict-schema.txt",
                    "runtime": runtime,
                    "unexpected": True,
                },
                "id": "call_strict_schema",
                "type": "tool_call",
            }
        )


@pytest.mark.asyncio
async def test_read_file_accepts_relative_path_and_one_indexed_lines(
    configured_read_tool,
):
    workspace_root, tool = configured_read_tool
    file_path = workspace_root / "nested" / "README.md"
    file_path.parent.mkdir()
    file_path.write_text("first\nsecond\nthird\n", encoding="utf-8")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_read"),
    )

    result = await tool.coroutine(
        path="nested/README.md",
        line_offset=2,
        max_lines=1,
        runtime=runtime,
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert "2\tsecond" in result.content
    assert "first" not in result.content
    assert "third" not in result.content


@pytest.mark.asyncio
async def test_read_file_never_reads_host_absolute_path(
    configured_read_tool,
    tmp_path,
):
    _workspace_root, tool = configured_read_tool
    outside_file = tmp_path.parent / "outside-host-file.txt"
    outside_file.write_text("HOST_ONLY_CONTENT", encoding="utf-8")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_read"),
    )

    result = await tool.coroutine(path=str(outside_file), runtime=runtime)

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "HOST_ONLY_CONTENT" not in result.content


@pytest.mark.asyncio
async def test_read_file_returns_explicit_error_for_traversal(configured_read_tool):
    _workspace_root, tool = configured_read_tool
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_traversal"),
    )

    result = await tool.coroutine(path="../secret.txt", runtime=runtime)

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "不是有效的工作区相对路径" in result.content


@pytest.mark.asyncio
async def test_read_file_resolves_relative_path_from_workspace(configured_read_tool):
    workspace_root, tool = configured_read_tool
    file_path = workspace_root / "relative.txt"
    file_path.write_text("workspace-relative", encoding="utf-8")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_read"),
    )

    result = await tool.coroutine(path="relative.txt", runtime=runtime)

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert "workspace-relative" in result.content


@pytest.mark.asyncio
async def test_read_file_reuses_path_returned_by_ls(configured_read_tool):
    workspace_root, tool = configured_read_tool
    file_path = workspace_root / "listed-file.txt"
    file_path.write_text("listed path works", encoding="utf-8")
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(workspace_root),
        tool_token_limit_before_evict=None,
    )
    configure_workspace_filesystem_tools(middleware, workspace_root=workspace_root)
    ls_tool = next(item for item in middleware.tools if item.name == "ls")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_listed_path"),
    )
    listing = await ls_tool.coroutine(path=".", runtime=runtime)
    listed_paths = ast.literal_eval(listing.content)
    listed_path = next(
        path for path in listed_paths if path.endswith("listed-file.txt")
    )

    result = await tool.coroutine(path=listed_path, runtime=runtime)

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert "listed path works" in result.content


@pytest.mark.asyncio
async def test_deepagents_filesystem_tools_share_relative_path_contract(
    tmp_path: Path,
) -> None:
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_workspace_filesystem_tools(middleware, workspace_root=tmp_path)
    tools = {item.name: item for item in middleware.tools}
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_shared_relative_path"),
    )
    relative_path = "src/shared-contract.txt"

    write_result = await tools["write_file"].coroutine(
        file_path=relative_path,
        content="before contract marker",
        runtime=runtime,
    )
    ls_result = await tools["ls"].coroutine(path="src", runtime=runtime)
    glob_result = await tools["glob"].coroutine(
        pattern="**/*.txt",
        path="src",
        runtime=runtime,
    )
    grep_result = await tools["grep"].coroutine(
        pattern="contract marker",
        path="src",
        runtime=runtime,
    )
    edit_result = await tools["edit_file"].coroutine(
        file_path=relative_path,
        old_string="before",
        new_string="after",
        runtime=runtime,
    )
    read_result = await tools["read_file"].coroutine(
        path=relative_path,
        runtime=runtime,
    )

    for result in (
        write_result,
        ls_result,
        glob_result,
        grep_result,
        edit_result,
        read_result,
    ):
        assert isinstance(result, ToolMessage)
        assert result.status == "success", result.content
    assert relative_path in ls_result.content
    assert relative_path in glob_result.content
    assert relative_path in grep_result.content
    assert "/src/shared-contract.txt" not in ls_result.content
    assert "/src/shared-contract.txt" not in glob_result.content
    assert "/src/shared-contract.txt" not in grep_result.content
    assert relative_path in write_result.content
    assert relative_path in edit_result.content
    assert "after contract marker" in read_result.content


@pytest.mark.asyncio
async def test_all_filesystem_tools_reject_backend_virtual_paths(
    tmp_path: Path,
) -> None:
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_workspace_filesystem_tools(middleware, workspace_root=tmp_path)
    tools = {item.name: item for item in middleware.tools}
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_reject_virtual_path"),
    )
    calls = (
        tools["ls"].coroutine(path="/src", runtime=runtime),
        tools["read_file"].coroutine(path="/src/main.mjs", runtime=runtime),
        tools["write_file"].coroutine(
            file_path="/src/main.mjs",
            content="",
            runtime=runtime,
        ),
        tools["edit_file"].coroutine(
            file_path="/src/main.mjs",
            old_string="before",
            new_string="after",
            runtime=runtime,
        ),
        tools["glob"].coroutine(pattern="**/*", path="/src", runtime=runtime),
        tools["grep"].coroutine(pattern="needle", path="/src", runtime=runtime),
    )

    for call in calls:
        result = await call
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "不能以 / 开头" in result.content


@pytest.mark.asyncio
async def test_read_file_resolves_agent_visible_bundled_skill_path(
    tmp_path: Path,
) -> None:
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(
            tmp_path,
            bundled_skill_groups=("debugging",),
            project_root=Path.cwd(),
        ),
        tool_token_limit_before_evict=None,
    )
    configure_workspace_filesystem_tools(middleware, workspace_root=tmp_path)
    tool = next(item for item in middleware.tools if item.name == "read_file")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_bundled_skill"),
    )

    result = await tool.coroutine(
        path=".boxteam/bundled-skills/debugging/SKILL.md",
        runtime=runtime,
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert "# 源码调试工具" in result.content


@pytest.mark.asyncio
async def test_read_file_resolves_model_visible_session_artifact_path(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_relative_artifact"
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", session_id)
    store = ToolOutputStore(
        workspace_root=tmp_path,
        max_lines=8,
        max_bytes=1_024,
    )
    stored = store.bound(
        session_id=session_id,
        tool_name="large_tool",
        tool_call_id="call_relative_artifact",
        message=ToolMessage(
            content="MODEL_VISIBLE_ARTIFACT\n" + "x" * 2_000,
            tool_call_id="call_relative_artifact",
        ),
    )
    reference = extract_tool_output_reference(stored)
    assert reference is not None
    read_path = reference["read_path"]
    assert isinstance(read_path, str)
    assert not read_path.startswith("/")

    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_workspace_filesystem_tools(middleware, workspace_root=tmp_path)
    read_tool = next(item for item in middleware.tools if item.name == "read_file")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_read_relative_artifact"),
    )

    result = await read_tool.coroutine(path=read_path, runtime=runtime)

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert "MODEL_VISIBLE_ARTIFACT" in result.content


def test_all_filesystem_tool_schemas_require_relative_paths(tmp_path: Path) -> None:
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_workspace_filesystem_tools(middleware, workspace_root=tmp_path)

    for tool in middleware.tools:
        if tool.name == "execute":
            continue
        schema_text = str(tool.tool_call_schema.model_json_schema())
        assert "workspace-relative" in schema_text.lower()
        assert "Absolute path" not in schema_text
