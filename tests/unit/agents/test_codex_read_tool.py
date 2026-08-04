from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemState
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage

from app.agents.codex_read_tool import configure_codex_read_file_tool
from app.agents.workspace_backend import build_workspace_backend


@pytest.fixture
def configured_read_tool(tmp_path):
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_codex_read_file_tool(middleware, workspace_root=tmp_path)
    tool = next(tool for tool in middleware.tools if tool.name == "read_file")
    return tmp_path, tool


def test_read_file_exposes_codex_style_schema(configured_read_tool):
    _workspace_root, tool = configured_read_tool

    schema = tool.args_schema.model_json_schema()

    assert set(schema["properties"]) == {"path", "line_offset", "max_lines"}
    assert schema["required"] == ["path"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["line_offset"]["default"] == 1
    assert "file_path" not in schema["properties"]
    assert "offset" not in schema["properties"]
    assert "limit" not in schema["properties"]


@pytest.mark.asyncio
async def test_read_file_accepts_absolute_host_path_and_one_indexed_lines(
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
        path=str(file_path),
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
async def test_read_file_accepts_absolute_path_outside_workspace(
    configured_read_tool,
    tmp_path_factory,
):
    _workspace_root, tool = configured_read_tool
    outside_root = tmp_path_factory.mktemp("outside-read")
    outside_file = outside_root / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_read"),
    )

    result = await tool.coroutine(path=str(outside_file), runtime=runtime)

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert "secret" in result.content


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
