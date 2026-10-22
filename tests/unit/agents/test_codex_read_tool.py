from __future__ import annotations

import base64
import json
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemState
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from pydantic import ValidationError

from app.agents.workspace_backend import build_workspace_backend
from app.core.identifier import create_uuid_hex
from app.core.resource_uri import parse_session_artifact_uri, resource_display_uri
from app.core.trace_middleware import get_current_gateway_id
from app.core.workspace_identity import bind_workspace_id
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.tool_output_store import (
    ToolOutputStore,
    extract_tool_output_reference,
)


@pytest.fixture
def configured_read_tool(configure_filesystem_tools, tmp_path):
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_filesystem_tools(middleware, workspace_root=tmp_path)
    tool = next(tool for tool in middleware.tools if tool.name == "read_file")
    return tmp_path, tool


def test_read_file_exposes_agent_visible_schema(configured_read_tool):
    _workspace_root, tool = configured_read_tool

    schema = tool.tool_call_schema.model_json_schema()

    assert set(schema["properties"]) == {"path", "line_offset", "max_lines", "max_bytes", "max_response_bytes", "timeout_ms"}
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

    # 外部路径明确拒绝，不能把它改投工作区内同名文件。
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
    assert json.loads(result.content)["code"] == "capability_denied"


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
async def test_read_file_reuses_path_returned_by_ls(configure_filesystem_tools, configured_read_tool):
    workspace_root, tool = configured_read_tool
    file_path = workspace_root / "listed-file.txt"
    file_path.write_text("listed path works", encoding="utf-8")
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(workspace_root),
        tool_token_limit_before_evict=None,
    )
    configure_filesystem_tools(middleware, workspace_root=workspace_root)
    ls_tool = next(item for item in middleware.tools if item.name == "ls")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_listed_path"),
    )
    listing = await ls_tool.coroutine(path=".", runtime=runtime)
    listed_paths = [entry["uri"] for entry in json.loads(listing.content)["entries"]]
    listed_path = next(
        path for path in listed_paths if path.endswith("listed-file.txt")
    )

    result = await tool.coroutine(path=listed_path, runtime=runtime)

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert "listed path works" in result.content


@pytest.mark.asyncio
async def test_deepagents_filesystem_tools_share_relative_path_contract(
    configure_filesystem_tools,
    tmp_path: Path,
) -> None:
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_filesystem_tools(middleware, workspace_root=tmp_path)
    tools = {item.name: item for item in middleware.tools}
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_shared_relative_path"),
    )
    relative_path = "src/shared-contract.txt"

    write_result = await tools["write_file"].coroutine(
        file_path=relative_path,
        content="before contract marker",
        expected_version="absent",
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
        expected_version="sha256:" + sha256(b"before contract marker").hexdigest(),
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
    assert json.loads(ls_result.content)["entries"][0]["uri"].endswith("/files/root/src/shared-contract.txt")
    assert json.loads(glob_result.content)["entries"][0]["uri"].endswith("/files/root/src/shared-contract.txt")
    assert grep_result.content.splitlines()[0].startswith("boxteam://workspace/")
    assert relative_path in write_result.content
    assert relative_path in edit_result.content
    assert "after contract marker" in read_result.content


@pytest.mark.asyncio
async def test_grep_rejects_boxteam_runtime_and_unscoped_root_search(
    configure_filesystem_tools,
    tmp_path: Path,
) -> None:
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_filesystem_tools(middleware, workspace_root=tmp_path)
    tool = next(item for item in middleware.tools if item.name == "grep")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_bounded_grep_scope"),
    )

    runtime_result = await tool.coroutine(
        pattern="screenshotPage",
        path=".boxteam",
        glob="**/*",
        output_mode="content",
        runtime=runtime,
    )
    root_result = await tool.coroutine(
        pattern="screenshotPage",
        path=".",
        glob="**/*",
        runtime=runtime,
    )

    for result in (runtime_result, root_result):
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert str(tmp_path) not in result.content
    assert json.loads(runtime_result.content) == {
        "owner": "resource_uri_resolver",
        "code": "capability_denied",
        "message": "原生文件目标越出固定目录能力",
    }
    assert ".boxteam" in root_result.content
    assert "限定到源码目录" in root_result.content


@pytest.mark.asyncio
async def test_listing_tools_hide_and_reject_boxteam_runtime(
    configure_filesystem_tools,
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("main", encoding="utf-8")
    (tmp_path / ".boxteam" / "sessions").mkdir(parents=True)
    (tmp_path / ".boxteam" / "sessions" / "trace.jsonl").write_text(
        "runtime", encoding="utf-8"
    )
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_filesystem_tools(middleware, workspace_root=tmp_path)
    tools = {item.name: item for item in middleware.tools}
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_runtime_boundary"),
    )

    listing = await tools["ls"].coroutine(path=".", runtime=runtime)
    glob_result = await tools["glob"].coroutine(
        pattern="**/*",
        path=".",
        runtime=runtime,
    )
    assert ".boxteam" not in listing.content
    assert ".boxteam" not in glob_result.content

    for tool_name, kwargs in (
        ("ls", {"path": ".boxteam"}),
        ("glob", {"pattern": "**/*", "path": ".boxteam"}),
        ("read_file", {"path": ".boxteam/sessions/trace.jsonl"}),
        (
            "write_file",
            {"file_path": ".boxteam/sessions/new.txt", "content": "blocked"},
        ),
        (
            "edit_file",
            {
                "file_path": ".boxteam/sessions/trace.jsonl",
                "old_string": "runtime",
                "new_string": "changed",
            },
        ),
    ):
        if tool_name in {"write_file", "edit_file"}:
            kwargs["expected_version"] = "absent" if tool_name == "write_file" else "sha256:" + "0" * 64
        result = await tools[tool_name].coroutine(runtime=runtime, **kwargs)
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert json.loads(result.content)["code"] == "capability_denied"
        assert str(tmp_path) not in result.content
    assert (tmp_path / ".boxteam" / "sessions" / "trace.jsonl").read_text(encoding="utf-8") == "runtime"
    assert not (tmp_path / ".boxteam" / "sessions" / "new.txt").exists()


@pytest.mark.asyncio
async def test_glob_rejects_runtime_pattern_before_backend_scan(configure_filesystem_tools, tmp_path: Path) -> None:
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_filesystem_tools(middleware, workspace_root=tmp_path)
    tool = next(item for item in middleware.tools if item.name == "glob")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_runtime_pattern"),
    )

    result = await tool.coroutine(
        pattern="**/.boxteam/**/*",
        path=".",
        runtime=runtime,
    )

    assert result.status == "error"
    assert "运行时目录" in result.content


@pytest.fixture
async def attachment_tools(configure_filesystem_tools, tmp_path: Path, session_bundle_factory):
    """附件通过真实 ingest 发布，工具只取得当前 Session 的受信读取端口。"""
    session_id = "ses_019b781159d47374805bc4427d081a14"
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", session_id)
    store = AttachmentBlobStore(tmp_path)
    data = b"\x89PNG\r\n\x1a\nattachment"
    ref = (await store.persist_inline(session_id, [AttachmentRef(file_id="inline:abc.png", name="abc.png", content_type="image/png", data_url="data:image/png;base64," + base64.b64encode(data).decode())], thread_id=store._path_resolver.main_thread_id(session_id), message_id="msg_" + create_uuid_hex(namespace="message")))[0]

    def read(file_id, max_bytes):
        if parse_session_artifact_uri(file_id).tail_segments[0] != session_id:
            raise PermissionError("附件 URI 不属于当前 Agent SessionThread")
        return store.read(session_id, file_id, owner_thread_id=store._path_resolver.main_thread_id(session_id))

    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_filesystem_tools(middleware, workspace_root=tmp_path, artifact_reader=read)
    tools = {item.name: item for item in middleware.tools}
    yield tmp_path, tools, ref, store
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("qualified", [False, True])
async def test_read_file_reads_logical_attachment_with_catalog_mime(attachment_tools, qualified: bool) -> None:
    _root, tools, ref, _store = attachment_tools
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_attachment_read"),
    )
    attachment_result = await tools["read_file"].coroutine(
        path=ref.file_id if qualified else parse_session_artifact_uri(ref.file_id, local_gateway_id=get_current_gateway_id()).display_uri,
        runtime=runtime,
    )

    assert isinstance(attachment_result, ToolMessage)
    assert attachment_result.status == "success"
    assert any(
        isinstance(block, dict) and block.get("type") == "image" and block.get("mime_type") == "image/png"
        for block in attachment_result.content
    )


@pytest.mark.asyncio
async def test_logical_attachment_rejects_cross_session_writes_and_old_paths(attachment_tools) -> None:
    _root, tools, ref, store = attachment_tools
    runtime = cast("ToolRuntime[None, FilesystemState]", SimpleNamespace(tool_call_id="call_attachment_denied"))
    peer_uri = ref.file_id.replace("ses_019b781159d47374805bc4427d081a14", "ses_019b9cdd1ad5758788a18bb227323523")
    assert (await tools["read_file"].coroutine(path=peer_uri, runtime=runtime)).status == "error"
    before = store.read("ses_019b781159d47374805bc4427d081a14", ref.file_id).data

    for tool_name, kwargs in (
        (
            "read_file",
            {
                "path": (
                    ".boxteam/sessions/ses_019b781159d47374805bc4427d081a14/"
                    "attachments/abc.png"
                )
            },
        ),
        (
            "ls",
            {"path": ref.file_id},
        ),
        ("write_file", {"file_path": ref.file_id, "content": "spoofed"}),
        ("read_file", {"path": "boxteam-session://ses_019b781159d47374805bc4427d081a14/attachments/" + parse_session_artifact_uri(ref.file_id).tail_segments[-1]}),
        ("edit_file", {"file_path": ref.file_id, "old_string": "attachment", "new_string": "spoofed"}),
    ):
        if tool_name in {"write_file", "edit_file"}:
            kwargs["expected_version"] = "absent" if tool_name == "write_file" else "sha256:" + "0" * 64
        result = await tools[tool_name].coroutine(runtime=runtime, **kwargs)
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        code = json.loads(result.content)["code"]
        if str(kwargs.get("path", "")).startswith("boxteam-session://"):
            assert code == "unknown_scheme"
        elif tool_name in {"read_file", "ls"}:
            assert code == "capability_denied"
        else:
            assert code == "capability_denied"
    assert store.read("ses_019b781159d47374805bc4427d081a14", ref.file_id).data == before
    assert not (store._path_resolver.resolve_session_node("ses_019b781159d47374805bc4427d081a14") / "attachments").exists()


@pytest.mark.asyncio
async def test_grep_returns_bounded_timeout_without_python_fallback(
    configure_filesystem_tools,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    owner = configure_filesystem_tools(middleware, workspace_root=tmp_path)
    tool = next(item for item in middleware.tools if item.name == "grep")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_bounded_grep_timeout"),
    )

    original = owner.input_uri

    def slow_input(value):
        time.sleep(0.02)
        return original(value)

    monkeypatch.setattr(owner, "input_uri", slow_input)
    result = await tool.coroutine(
        pattern="needle",
        path="src",
        glob="**/*.py",
        timeout_ms=1,
        runtime=runtime,
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert json.loads(result.content)["reason"] == "deadline"


@pytest.mark.asyncio
async def test_filesystem_tools_accept_target_native_absolute_paths(
    configure_filesystem_tools,
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.mjs").write_text("before", encoding="utf-8")
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_filesystem_tools(middleware, workspace_root=tmp_path)
    tools = {item.name: item for item in middleware.tools}
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_normalize_virtual_path"),
    )

    ls_result = await tools["ls"].coroutine(path=str(tmp_path / "src"), runtime=runtime)
    read_result = await tools["read_file"].coroutine(
        path=str(tmp_path / "src/main.mjs"),
        runtime=runtime,
    )

    assert isinstance(ls_result, ToolMessage)
    assert ls_result.status == "success"
    assert isinstance(read_result, ToolMessage)
    assert read_result.status == "success"
    assert "before" in read_result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("input_kind", ["relative", "absolute", "uri"])
async def test_filesystem_tools_share_target_resolution(
    configure_filesystem_tools,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, input_kind: str,
) -> None:
    workspace, relay = tmp_path / "workspace", tmp_path / "relay"
    (workspace / "src").mkdir(parents=True)
    relay.mkdir()
    workspace_id = bind_workspace_id(workspace, "019d1b4a-1111-7000-8000-000000000001")
    source = workspace / "src/main.txt"
    source.write_text("selected target\n", encoding="utf-8")
    monkeypatch.chdir(relay)
    middleware = FilesystemMiddleware(backend=build_workspace_backend(workspace), tool_token_limit_before_evict=None)
    configure_filesystem_tools(middleware, workspace_root=workspace)
    tools = {item.name: item for item in middleware.tools}
    runtime = cast("ToolRuntime[None, FilesystemState]", SimpleNamespace(tool_call_id="call_fixed_target"))

    def target(relative: str) -> str:
        if input_kind == "absolute":
            return str(workspace / relative)
        if input_kind == "uri":
            return resource_display_uri(scope="workspace", scope_id=workspace_id, kind="files", tail_segments=("root", *Path(relative).parts))
        return relative

    operations = [
        ("ls", {"path": target("src")}),
        ("read_file", {"path": target("src/main.txt")}),
        ("glob", {"path": target("src"), "pattern": "*.txt"}),
        ("grep", {"path": target("src"), "pattern": "selected target"}),
        ("write_file", {"file_path": target("src/new.txt"), "content": "new target\n", "expected_version": "absent"}),
        ("edit_file", {"file_path": target("src/main.txt"), "old_string": "selected target", "new_string": "edited target", "expected_version": "sha256:" + sha256(b"selected target\n").hexdigest()}),
    ]
    for name, arguments in operations:
        if name in {"write_file", "edit_file"} and "expected_version" not in arguments:
            arguments["expected_version"] = "absent" if name == "write_file" else "sha256:" + "0" * 64
        result = await tools[name].coroutine(**arguments, runtime=runtime)
        assert result.status == "success", (name, result.content)
    assert source.read_text(encoding="utf-8") == "edited target\n"
    assert (workspace / "src/new.txt").read_text(encoding="utf-8") == "new target\n"
    assert list(relay.iterdir()) == []


@pytest.mark.asyncio
async def test_external_absolute_inputs_never_read_or_write_workspace_decoys(configure_filesystem_tools, tmp_path: Path) -> None:
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    decoy = workspace.joinpath(*outside.parts[1:])
    decoy.mkdir(parents=True)
    (outside / "owned.txt").write_text("REAL OUTSIDE", encoding="utf-8")
    (decoy / "owned.txt").write_text("DECOY", encoding="utf-8")
    middleware = FilesystemMiddleware(backend=build_workspace_backend(workspace), tool_token_limit_before_evict=None)
    configure_filesystem_tools(middleware, workspace_root=workspace)
    tools = {item.name: item for item in middleware.tools}
    runtime = cast("ToolRuntime[None, FilesystemState]", SimpleNamespace(tool_call_id="call_no_redirect"))
    operations = [
        ("ls", {"path": str(outside)}),
        ("read_file", {"path": str(outside / "owned.txt")}),
        ("glob", {"path": str(outside), "pattern": "*.txt"}),
        ("grep", {"path": str(outside), "pattern": "DECOY"}),
        ("write_file", {"file_path": str(outside / "new.txt"), "content": "WRONG"}),
        ("edit_file", {"file_path": str(outside / "owned.txt"), "old_string": "DECOY", "new_string": "WRONG"}),
    ]
    for name, arguments in operations:
        if name in {"write_file", "edit_file"} and "expected_version" not in arguments:
            arguments["expected_version"] = "absent" if name == "write_file" else "sha256:" + "0" * 64
        result = await tools[name].coroutine(**arguments, runtime=runtime)
        assert result.status == "error", (name, result.content)
        assert "DECOY" not in result.content
    assert (outside / "owned.txt").read_text(encoding="utf-8") == "REAL OUTSIDE"
    assert (decoy / "owned.txt").read_text(encoding="utf-8") == "DECOY"
    assert not (outside / "new.txt").exists()
    assert not (decoy / "new.txt").exists()


@pytest.mark.asyncio
async def test_filesystem_tools_reject_escaping_paths(
    configure_filesystem_tools,
    tmp_path: Path,
) -> None:
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_filesystem_tools(middleware, workspace_root=tmp_path)
    tools = {item.name: item for item in middleware.tools}
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_reject_escaping_path"),
    )
    calls = (
        tools["ls"].coroutine(path="../secret", runtime=runtime),
        tools["read_file"].coroutine(path="../secret", runtime=runtime),
        tools["glob"].coroutine(pattern="**/*", path="../secret", runtime=runtime),
    )

    for call in calls:
        result = await call
        assert isinstance(result, ToolMessage)
        assert result.status == "error"


@pytest.mark.asyncio
async def test_read_file_rejects_agent_visible_bundled_skill_path(
    configure_filesystem_tools,
    tmp_path: Path,
) -> None:
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_filesystem_tools(middleware, workspace_root=tmp_path)
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
    assert result.status == "error"
    assert json.loads(result.content)["code"] == "capability_denied"


@pytest.mark.asyncio
async def test_read_file_rejects_non_definition_system_skill_paths(
    configure_filesystem_tools,
    tmp_path: Path,
) -> None:
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_filesystem_tools(middleware, workspace_root=tmp_path)
    tool = next(item for item in middleware.tools if item.name == "read_file")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_system_skill_boundary"),
    )

    result = await tool.coroutine(
        path=".boxteam/bundled-skills/debugging/README.md",
        runtime=runtime,
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert json.loads(result.content)["code"] == "capability_denied"


@pytest.mark.asyncio
async def test_read_file_resolves_model_visible_session_artifact_path(
    configure_filesystem_tools,
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_019bc4518d0079d3a53e974eea6f21a9"
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
    configure_filesystem_tools(middleware, workspace_root=tmp_path, artifact_reader=lambda uri, maximum: store.read(session_id, uri, maximum))
    read_tool = next(item for item in middleware.tools if item.name == "read_file")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_read_relative_artifact"),
    )

    result = await read_tool.coroutine(path=read_path, runtime=runtime)

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert "MODEL_VISIBLE_ARTIFACT" in result.content


@pytest.mark.asyncio
async def test_grep_resolves_model_visible_session_artifact_path(
    configure_filesystem_tools,
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_019bf1dcdb127cc0871f36d4c43359b5"
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", session_id)
    store = ToolOutputStore(
        workspace_root=tmp_path,
        max_lines=8,
        max_bytes=1_024,
    )
    stored = store.bound(
        session_id=session_id,
        tool_name="large_tool",
        tool_call_id="call_grep_artifact",
        message=ToolMessage(
            content="before\nretrieval-target=BOXTEAM_TEST\nafter\n" + "x" * 2_000,
            tool_call_id="call_grep_artifact",
        ),
    )
    reference = extract_tool_output_reference(stored)
    assert reference is not None
    read_path = reference["read_path"]
    assert isinstance(read_path, str)

    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_filesystem_tools(middleware, workspace_root=tmp_path, artifact_reader=lambda uri, maximum: store.read(session_id, uri, maximum))
    grep_tool = next(item for item in middleware.tools if item.name == "grep")
    runtime = cast(
        "ToolRuntime[None, FilesystemState]",
        SimpleNamespace(tool_call_id="call_grep_relative_artifact"),
    )

    result = await grep_tool.coroutine(
        pattern="retrieval-target",
        path=read_path,
        output_mode="content",
        runtime=runtime,
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert "retrieval-target=BOXTEAM_TEST" in result.content
    assert parse_session_artifact_uri(read_path, local_gateway_id=get_current_gateway_id()).display_uri in result.content


def test_all_filesystem_tool_schemas_require_relative_paths(configure_filesystem_tools, tmp_path: Path) -> None:
    middleware = FilesystemMiddleware(
        backend=build_workspace_backend(tmp_path),
        tool_token_limit_before_evict=None,
    )
    configure_filesystem_tools(middleware, workspace_root=tmp_path)

    for tool in middleware.tools:
        if tool.name == "execute":
            continue
        schema_text = str(tool.tool_call_schema.model_json_schema())
        assert "workspace-relative" in schema_text.lower()
        assert "Absolute path" not in schema_text
