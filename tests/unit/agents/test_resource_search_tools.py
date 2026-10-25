"""模型搜索入口共用文件 owner，续页保持目标、查询和匹配位置。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from langgraph.prebuilt import ToolNode

from app.agents.workspace_backend import build_workspace_backend
from app.core.resource_uri import resource_display_uri
from app.core.workspace_identity import load_or_create_workspace_id


@pytest.fixture
def search_case(
    configure_filesystem_tools,
    empty_workspace_scope: tuple[str, Path],
):
    workspace = empty_workspace_scope[1]
    (workspace / "src/deep").mkdir(parents=True)
    middleware = FilesystemMiddleware(backend=build_workspace_backend(workspace), tool_token_limit_before_evict=None)
    old_glob = next(tool for tool in middleware.tools if tool.name == "glob")

    def forbidden(*args, **kwargs):
        pytest.fail("普通 glob 不得调用旧 backend")

    async def forbidden_async(*args, **kwargs):
        pytest.fail("普通 glob 不得调用旧 backend")

    old_glob.func, old_glob.coroutine = forbidden, forbidden_async
    owner = configure_filesystem_tools(middleware, workspace_root=workspace)
    return workspace, owner, {tool.name: tool for tool in middleware.tools}


async def _search(tools, tool_name: str, path: str, pattern: str, synchronous: bool, **options):
    call = {"name": tool_name, "args": {"path": path, "pattern": pattern,
        "runtime": SimpleNamespace(tool_call_id="call_search"), **options}, "id": "call_search", "type": "tool_call"}
    result = tools[tool_name].invoke(call) if synchronous else await tools[tool_name].ainvoke(call)
    assert result.tool_call_id == "call_search"
    return result


def test_search_tools_keep_runtime_annotations_for_graph_assembly(search_case):
    _, _, tools = search_case
    node = ToolNode([tools["glob"], tools["grep"]])
    assert set(node.tools_by_name) == {"glob", "grep"}


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_glob_and_grep_three_inputs_keep_fixed_owner_and_reopen_uris(search_case, tmp_path, monkeypatch, synchronous):
    workspace, owner, tools = search_case
    file = workspace / "src/deep/中文 %.txt"
    file.write_text("needle boxteam://literal\n", encoding="utf-8")
    relay = tmp_path / "relay"
    (relay / "src/deep").mkdir(parents=True)
    (relay / "src/deep" / file.name).write_text("RELAY DECOY")
    monkeypatch.chdir(relay)
    results = []
    for target in ("src", str(workspace / "src"), owner.input_uri("src")):
        glob = await _search(tools, "glob", target, "*.txt", synchronous)
        assert glob.status == "success", glob.content
        payload = json.loads(glob.content)
        entry, = payload["entries"]
        assert entry["uri"] == owner.input_uri("src/deep/中文 %.txt")
        read = await tools["read_file"].coroutine(path=entry["uri"], runtime=SimpleNamespace(tool_call_id="call_read_search"))
        assert read.status == "success" and "needle boxteam://literal" in read.content
        grep = await _search(tools, "grep", target, "boxteam://literal", synchronous, output_mode="content")
        assert grep.status == "success" and "needle boxteam://literal" in grep.content and entry["uri"] in grep.content
        assert str(workspace) not in grep.model_dump_json() and "RELAY DECOY" not in grep.content
        results.append((glob.content, grep.content))
    assert results[0] == results[1] == results[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_grep_pages_inside_one_file_bind_query_and_detect_changed_prefix(search_case, synchronous):
    workspace, owner, tools = search_case
    file = workspace / "src/matches.txt"
    file.write_text("".join(f"needle-{index}\n" for index in range(5)))
    path = owner.input_uri("src/matches.txt")
    cursor, pages, first_cursor = None, [], None
    for _ in range(4):
        result = await _search(tools, "grep", path, "needle", synchronous, limit=2, cursor=cursor, output_mode="content")
        assert result.status == "success", result.content
        metadata = result.additional_kwargs["resource_navigation"]
        pages.append(result.content)
        if not metadata["truncated"]:
            break
        assert metadata["truncation_reason"] == "entry_limit"
        cursor = metadata["next_cursor"]
        first_cursor = first_cursor or cursor
    else:
        pytest.fail("同文件匹配分页必须收敛")
    assert len(pages) == 3
    for index in range(5):
        assert sum(f"needle-{index}" in page for page in pages) == 1
    wrong = await _search(tools, "grep", path, "changed query", synchronous, limit=2, cursor=first_cursor)
    assert wrong.status == "error" and json.loads(wrong.content)["reason"] == "invalid_cursor"
    file.write_text("changed\nneedle-1\nneedle-2\n")
    stale = await _search(tools, "grep", path, "needle", synchronous, limit=2, cursor=first_cursor)
    assert stale.status == "error" and json.loads(stale.content)["reason"] == "stale_cursor"


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_skill_search_is_encoded_bounded_and_does_not_write_sources(search_case, synchronous):
    workspace, _, tools = search_case
    directory = workspace / ".boxteam/skills/demo/references"
    directory.mkdir(parents=True)
    names = ("中文 %.txt", "%2e%2e.txt", ".hidden.txt")
    for name in names:
        (directory / name).write_text("needle " + "长正文" * 100)
    uri = resource_display_uri(scope="workspace", scope_id=load_or_create_workspace_id(workspace),
        kind="skills", tail_segments=("demo",))
    before = {str(path.relative_to(workspace)): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    glob = await _search(tools, "glob", uri, "*.txt", synchronous)
    assert glob.status == "success"
    found = json.loads(glob.content)["entries"]
    assert len(found) == 3 and any(node["uri"].endswith("/%252e%252e.txt") for node in found)
    grep = await _search(tools, "grep", uri, "needle", synchronous, glob="*.txt", output_mode="content", max_snippet_bytes=32)
    assert grep.status == "success" and "片段已截断" in grep.content
    assert "长正文" * 100 not in grep.content
    assert all(node["uri"] in grep.content for node in found)
    assert {str(path.relative_to(workspace)): path.read_bytes() for path in workspace.rglob("*") if path.is_file()} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_search_limits_and_runtime_media_exclusions_are_explicit(search_case, synchronous):
    workspace, _, tools = search_case
    for directory in (".git", "node_modules", ".boxteam"):
        (workspace / "src" / directory).mkdir()
        (workspace / "src" / directory / "hidden.txt").write_text("needle HIDDEN")
    (workspace / "src/media.png").write_bytes(b"\xffneedle")
    (workspace / "src/binary.bin").write_bytes(b"\0needle")
    (workspace / "src/file.txt").write_text("needle\n")
    result = await _search(tools, "grep", "src", "needle", synchronous, output_mode="content")
    assert result.status == "success" and "file.txt" in result.content and "HIDDEN" not in result.content
    assert "media.png" not in result.content and "binary.bin" not in result.content
    limited = await _search(tools, "grep", "src", "needle", synchronous, max_bytes=2)
    assert limited.status == "error" and json.loads(limited.content)["reason"] == "file_byte_limit"
    scanned = await _search(tools, "glob", "src", "*.txt", synchronous, scan_limit=1)
    assert scanned.status == "error" and json.loads(scanned.content)["reason"] == "scan_limit"
    (workspace / "src/file.txt").write_bytes(b"\xff")
    invalid = await _search(tools, "grep", "src", "needle", synchronous, glob="*.txt")
    assert invalid.status == "error" and json.loads(invalid.content)["code"] == "invalid_encoding"


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_search_deadline_includes_input_resolution(search_case, monkeypatch, synchronous):
    _, owner, tools = search_case
    original = owner.input_uri

    def slow(value):
        time.sleep(0.02)
        return original(value)

    monkeypatch.setattr(owner, "input_uri", slow)
    for name, pattern in (("glob", "*.txt"), ("grep", "needle")):
        result = await _search(tools, name, "src", pattern, synchronous, timeout_ms=1)
        assert result.status == "error" and json.loads(result.content)["reason"] == "deadline"


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_search_does_not_publish_denied_file_matches(
    configure_filesystem_tools,
    empty_workspace_scope: tuple[str, Path],
    synchronous,
):
    workspace = empty_workspace_scope[1]
    (workspace / "src").mkdir()
    (workspace / "src/secret.txt").write_text("needle SECRET")
    (workspace / "src/public.txt").write_text("needle PUBLIC")
    middleware = FilesystemMiddleware(backend=build_workspace_backend(workspace),
        _permissions=[FilesystemPermission(operations=["read"], paths=["/src/secret.txt"], mode="deny")])
    configure_filesystem_tools(middleware, workspace_root=workspace)
    tools = {tool.name: tool for tool in middleware.tools}
    for tool_name, pattern in (("glob", "*.txt"), ("grep", "needle")):
        options = {"output_mode": "content"} if tool_name == "grep" else {}
        result = await _search(tools, tool_name, "src", pattern, synchronous, **options)
        assert result.status == "success", result.content
        assert "public.txt" in result.content and "secret.txt" not in result.content and "SECRET" not in result.content
