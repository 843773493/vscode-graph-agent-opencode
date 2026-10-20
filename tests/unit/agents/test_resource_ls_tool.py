"""真实 ls 工具入口的共享导航、路径语义和有界结果。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.tools import StructuredTool

from app.agents.workspace_backend import build_workspace_backend
from app.core.resource_uri import resource_display_uri
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.resource_platform.sources import resource_navigation


@pytest.fixture
def ls_case(configure_filesystem_tools, tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    middleware = FilesystemMiddleware(backend=build_workspace_backend(workspace), tool_token_limit_before_evict=None)
    old_ls = next(tool for tool in middleware.tools if tool.name == "ls")

    def forbidden_backend(*args, **kwargs):
        pytest.fail("ls 不应再调用 DeepAgents backend")

    async def forbidden_async_backend(*args, **kwargs):
        pytest.fail("ls 不应再调用 DeepAgents backend")

    old_ls.func, old_ls.coroutine = forbidden_backend, forbidden_async_backend
    owner = configure_filesystem_tools(middleware, workspace_root=workspace)
    tool = next(tool for tool in middleware.tools if tool.name == "ls")
    return workspace, owner, tool


async def _list(tool: StructuredTool, path: str, *, synchronous: bool, **limits):
    call = {"name": "ls", "args": {
        "path": path, "runtime": SimpleNamespace(tool_call_id="call_resource_ls"), **limits,
    }, "id": "call_resource_ls", "type": "tool_call"}
    # 同步入口也在已有事件循环中调用，防止实现重新套 asyncio.run。
    message = tool.invoke(call) if synchronous else await tool.ainvoke(call)
    assert message.tool_call_id == "call_resource_ls"
    return message, json.loads(message.content) if message.content.startswith("{") else message.content


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_ls_native_and_uri_targets_share_owner_without_relay_cwd(ls_case, monkeypatch, tmp_path, synchronous):
    workspace, owner, tool = ls_case
    target = workspace / "src"
    target.mkdir()
    (target / "中文 %.txt").write_text("actual", encoding="utf-8")
    relay = tmp_path / "relay"
    (relay / "src").mkdir(parents=True)
    (relay / "src" / "decoy.txt").write_text("relay decoy")
    monkeypatch.chdir(relay)
    uri = owner.input_uri("src")
    results = []
    for value in ("src", str(target), uri):
        message, result = await _list(tool, value, synchronous=synchronous)
        assert message.status == "success"
        assert str(workspace) not in message.content and str(relay) not in message.content
        assert result["node"]["uri"] == uri and result["view"] == "live"
        results.append(result)
    assert results[0] == results[1] == results[2]
    assert results[0]["entries"][0]["uri"] == uri + "/%E4%B8%AD%E6%96%87%20%25.txt"


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_ls_skill_directories_reopen_encoded_children_without_tracking(ls_case, synchronous):
    workspace, _owner, tool = ls_case
    root = workspace / ".boxteam" / "skills" / "demo"
    (root / "references" / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_text("skill")
    names = ("中文 %.txt", "%2e%2e.txt", ".hidden.txt")
    for name in names:
        (root / "references" / "scripts" / name).write_text(name, encoding="utf-8")
    before = sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*"))
    uri = resource_display_uri(scope="workspace", scope_id=load_or_create_workspace_id(workspace),
                               kind="skills", tail_segments=("demo",))
    for directory in ("references", "scripts"):
        message, result = await _list(tool, uri, synchronous=synchronous)
        assert message.status == "success"
        uri = next(entry["uri"] for entry in result["entries"] if entry["uri"].endswith("/" + directory))
    message, result = await _list(tool, uri, synchronous=synchronous)
    assert message.status == "success"
    assert {entry["uri"].rsplit("/", 1)[1] for entry in result["entries"]} == {
        "%E4%B8%AD%E6%96%87%20%25.txt", "%252e%252e.txt", ".hidden.txt",
    }
    assert all(entry["resource_shape"] == "file" and "read" in entry["operations"] for entry in result["entries"])
    assert sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*")) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_ls_pages_and_cursor_bindings_are_enforced_at_tool_entry(ls_case, synchronous):
    workspace, owner, tool = ls_case
    for name in ("a.txt", "b.txt", "c.txt"):
        (workspace / name).write_text(name)
    (workspace / "sub").mkdir()
    message, page = await _list(tool, ".", synchronous=synchronous, limit=1)
    assert message.status == "success" and page["truncated"]
    assert page["truncation_reason"] == "entry_limit" and page["next_cursor"]
    for target, limit in (("sub", 1), (".", 2)):
        message, error = await _list(tool, target, synchronous=synchronous, limit=limit, cursor=page["next_cursor"])
        assert message.status == "error"
        assert error["owner"] == "resource_file_operations" and error["reason"] == "invalid_cursor"
    seen = list(page["entries"])
    while page["next_cursor"]:
        message, page = await _list(tool, owner.input_uri("."), synchronous=synchronous, limit=1, cursor=page["next_cursor"])
        assert message.status == "success"
        seen.extend(page["entries"])
    assert len(seen) == len({entry["uri"] for entry in seen}) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_ls_limits_and_external_paths_fail_without_redirecting(ls_case, tmp_path, synchronous):
    workspace, _owner, tool = ls_case
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside")
    (workspace / "a.txt").write_text("a")
    (workspace / "b.txt").write_text("b")
    message, error = await _list(tool, str(outside), synchronous=synchronous)
    assert message.status == "error" and "尚未装配原生 Gateway 目录根绑定" in error
    assert "secret.txt" not in message.content
    message, error = await _list(tool, ".", synchronous=synchronous, scan_limit=1)
    assert message.status == "error"
    assert (error["owner"], error["reason"]) == ("resource_file_operations", "scan_limit")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    message, error = await _list(tool, "escape", synchronous=synchronous)
    assert message.status == "error" and error["code"] == "capability_denied"
    message, error = await _list(tool, "boxteam-session://private", synchronous=synchronous)
    assert message.status == "error" and error["owner"] == "resource_uri_grammar"


def test_ls_schema_exposes_navigation_limits_and_injects_runtime(ls_case):
    _workspace, _owner, tool = ls_case
    schema = tool.tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"path", "limit", "cursor", "timeout_ms", "scan_limit", "max_response_bytes"}
    assert schema["required"] == ["path"]
    assert tool._injected_args_keys == {"runtime"}


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_ls_deadline_includes_input_resolution(ls_case, monkeypatch, synchronous):
    _workspace, owner, tool = ls_case
    resolve = owner.input_uri

    def slow_input(value):
        time.sleep(0.02)
        return resolve(value)

    monkeypatch.setattr(owner, "input_uri", slow_input)
    message, error = await _list(tool, ".", synchronous=synchronous, timeout_ms=1)
    assert message.status == "error"
    assert (error["owner"], error["code"], error["reason"]) == ("resource_file_operations", "output_limit_exceeded", "deadline")


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_ls_response_budget_is_enforced_before_tool_delivery(ls_case, synchronous):
    workspace, _owner, tool = ls_case
    for index in range(20):
        (workspace / (f"{index:02}-" + "x" * 90 + ".txt")).write_text("bounded")
    message, result = await _list(tool, ".", synchronous=synchronous, max_response_bytes=2048)
    assert message.status == "success" and len(message.content.encode()) <= 2048
    assert result["truncated"] and result["truncation_reason"] == "response_byte_limit"
    assert result["entries"] and result["next_cursor"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [False, True])
@pytest.mark.parametrize("synchronous", [False, True])
async def test_ls_io_errors_keep_domain_without_exposing_native_locator(ls_case, monkeypatch, transport, synchronous):
    workspace, owner, tool = ls_case
    error = httpx.ConnectError("private endpoint http://secret-owner/") if transport else PermissionError(13, "Permission denied", str(workspace))

    def fail(*args, **kwargs):
        raise error

    if transport:
        async def fail_async(*args, **kwargs):
            raise error
        monkeypatch.setattr(owner, "navigate_sync", fail)
        monkeypatch.setattr(owner, "navigate", fail_async)
    else:
        monkeypatch.setattr(resource_navigation.os, "scandir", fail)
    try:
        message, result = await _list(tool, ".", synchronous=synchronous)
    finally:
        monkeypatch.undo()
    assert message.status == "error"
    assert (result["owner"], result["code"], result["reason"]) == (
        "resource_file_operations", "execution_failed", "transport" if transport else "filesystem",
    )
    assert str(workspace) not in message.content and "secret-owner" not in message.content
