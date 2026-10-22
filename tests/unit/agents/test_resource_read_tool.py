"""真实 read_file 入口的统一文件 owner、媒体投影和读取预算。"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission

from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.workspace_backend import build_workspace_backend
from app.core.resource_uri import resource_display_uri
from app.core.workspace_identity import load_or_create_workspace_id


@pytest.fixture
def read_case(configure_filesystem_tools, tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    middleware = FilesystemMiddleware(backend=build_workspace_backend(workspace), tool_token_limit_before_evict=None)
    old_read = next(tool for tool in middleware.tools if tool.name == "read_file")

    def forbidden(*args, **kwargs):
        pytest.fail("普通 read_file 不得回到旧 DeepAgents 读取")

    async def forbidden_async(*args, **kwargs):
        pytest.fail("普通 read_file 不得回到旧 DeepAgents 读取")

    old_read.func, old_read.coroutine = forbidden, forbidden_async
    owner = configure_filesystem_tools(middleware, workspace_root=workspace)
    return workspace, owner, {tool.name: tool for tool in middleware.tools}


async def _read(tools, path: str, synchronous: bool, **limits):
    call = {"name": "read_file", "args": {
        "path": path, "runtime": SimpleNamespace(tool_call_id="call_resource_read"), **limits,
    }, "id": "call_resource_read", "type": "tool_call"}
    message = tools["read_file"].invoke(call) if synchronous else await tools["read_file"].ainvoke(call)
    assert message.tool_call_id == "call_resource_read"
    return message


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_three_read_inputs_share_fixed_owner_and_preserve_bytes(read_case, monkeypatch, tmp_path, synchronous):
    workspace, owner, tools = read_case
    target = workspace / "中文 %.txt"
    target.write_bytes(b"\xef\xbb\xbfline one\r\nline two\n")
    relay = tmp_path / "relay"
    relay.mkdir()
    (relay / target.name).write_text("RELAY DECOY")
    monkeypatch.chdir(relay)
    uri = owner.input_uri(target.name)
    results = [await _read(tools, value, synchronous) for value in (target.name, str(target), uri)]
    assert all(message.status == "success" for message in results)
    assert results[0] == results[1] == results[2]
    assert "\ufeffline one\r" in results[0].content and "line two" in results[0].content
    assert results[0].additional_kwargs["resource_navigation"]["node"]["uri"] == uri
    version = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    assert results[0].additional_kwargs["resource_navigation"]["content_hash"] == version
    assert version in results[0].content
    assert str(workspace) not in results[0].model_dump_json() and "RELAY DECOY" not in results[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_skill_ls_file_uris_read_without_source_or_activation_writes(read_case, synchronous):
    workspace, _, tools = read_case
    root = workspace / ".boxteam/skills/demo"
    (root / "references/scripts").mkdir(parents=True)
    names = ("中文 空格.txt", "%2e%2e.txt", ".hidden.txt")
    for name in names:
        (root / "references/scripts" / name).write_text("正文 " + name, encoding="utf-8")
    (root / "SKILL.md").write_text("Skill content only")
    uri = resource_display_uri(scope="workspace", scope_id=load_or_create_workspace_id(workspace),
                               kind="skills", tail_segments=("demo",))
    before = {str(path.relative_to(workspace)): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    result = await _read(tools, uri + "/SKILL.md", synchronous)
    assert result.status == "success" and "Skill content only" in result.content
    for directory in ("references", "scripts"):
        listing = await tools["ls"].coroutine(path=uri, runtime=SimpleNamespace(tool_call_id="call_list"))
        uri = next(node["uri"] for node in json.loads(listing.content)["entries"] if node["uri"].endswith("/" + directory))
    listing = await tools["ls"].coroutine(path=uri, runtime=SimpleNamespace(tool_call_id="call_list"))
    contents = []
    for entry in json.loads(listing.content)["entries"]:
        result = await _read(tools, entry["uri"], synchronous)
        assert result.status == "success"
        assert result.additional_kwargs["resource_navigation"]["node"]["uri"] == entry["uri"]
        contents.append(result.content)
    assert all(any("正文 " + name in content for content in contents) for name in names)
    assert {str(path.relative_to(workspace)): path.read_bytes() for path in workspace.rglob("*") if path.is_file()} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("extension,media_type", [
    *[(extension, "image") for extension in ("png", "jpeg", "jpg", "webp", "gif", "heic", "heif")],
    *[(extension, "audio") for extension in ("wav", "mp3", "aiff", "aac", "ogg", "flac")],
    *[(extension, "video") for extension in ("mp4", "mpeg", "mov", "avi", "flv", "mpg", "webm", "wmv", "3gpp")],
    *[(extension, "file") for extension in ("pdf", "ppt", "pptx")],
])
async def test_existing_media_set_is_complete_not_utf8_decoded(read_case, synchronous, extension, media_type):
    workspace, owner, tools = read_case
    name = f"中文 %.{extension.upper()}"
    raw = b"\x89\xff\x00\x80media original"
    (workspace / name).write_bytes(raw)
    result = await _read(tools, owner.input_uri(name), synchronous)
    assert result.status == "success", result.content
    block, = result.content_blocks
    assert block["type"] == media_type and base64.b64decode(block["base64"]) == raw
    assert block["mime_type"]
    assert result.additional_kwargs["read_file_path"] == owner.input_uri(name)
    assert result.additional_kwargs["resource_navigation"]["truncated"] is False
    assert result.additional_kwargs["resource_navigation"]["content_hash"] == "sha256:" + hashlib.sha256(raw).hexdigest()


@pytest.mark.asyncio
async def test_real_read_image_result_keeps_provider_projection(read_case):
    workspace, owner, tools = read_case
    (workspace / "image %.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    uri = owner.input_uri("image %.png")
    result = await _read(tools, uri, False)
    for vision in (False, True):
        model = BoxteamLiteLLMChatModel(model="openai/test-model", api_key="test-key",
            api_base="https://example.com/v1", provider_id="read-projection", image_input_replay=vision)
        wire, = model._convert_messages_to_dicts([result])
        assert wire["tool_call_id"] == result.tool_call_id
        block, = wire["content"]
        if vision:
            assert block == {"type": "image", "base64": base64.b64encode((workspace / "image %.png").read_bytes()).decode(), "mime_type": "image/png"}
        else:
            assert block["type"] == "text" and uri in block["text"]
    assert result.content_blocks[0]["type"] == "image"


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_read_ranges_encoding_and_media_budgets_fail_honestly(read_case, synchronous):
    workspace, _, tools = read_case
    (workspace / "text.txt").write_text("first\nsecond\nthird\n")
    result = await _read(tools, "text.txt", synchronous, line_offset=2, max_lines=1)
    assert "2\tsecond" in result.content and "first" not in result.content and "third" not in result.content
    assert result.additional_kwargs["resource_navigation"]["truncation_reason"] == "line_limit"
    version = "sha256:" + hashlib.sha256((workspace / "text.txt").read_bytes()).hexdigest()
    assert version in result.content
    result = await _read(tools, "text.txt", synchronous, max_bytes=3)
    assert result.status == "success" and result.additional_kwargs["resource_navigation"]["content_hash"] is None
    assert version not in result.content and "sha256:" not in result.content
    result = await _read(tools, "text.txt", synchronous, line_offset=2, max_bytes=3)
    assert result.status == "error" and json.loads(result.content)["reason"] == "file_byte_limit"
    result = await _read(tools, "text.txt", synchronous, line_offset=5)
    assert result.status == "error" and json.loads(result.content)["reason"] == "invalid_range"
    (workspace / "text.txt").write_bytes(b"\xff")
    result = await _read(tools, "text.txt", synchronous)
    assert result.status == "error" and json.loads(result.content)["code"] == "invalid_encoding"
    (workspace / "media.png").write_bytes(b"\xff" * 3000)
    for limits, reason in (({"max_bytes": 3}, "file_byte_limit"), ({"max_response_bytes": 1024}, "response_byte_limit")):
        result = await _read(tools, "media.png", synchronous, **limits)
        assert result.status == "error" and json.loads(result.content)["reason"] == reason
        assert not isinstance(result.content, list)


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_formatted_read_result_has_total_byte_and_time_budget(read_case, monkeypatch, synchronous):
    workspace, owner, tools = read_case
    (workspace / "text.txt").write_text('"\\中文\n' * 1000)
    result = await _read(tools, "text.txt", synchronous, max_response_bytes=2048)
    assert result.status == "success" and len(result.model_dump_json().encode()) <= 2048
    assert result.additional_kwargs["resource_navigation"]["truncation_reason"] == "response_byte_limit"
    assert "已截断" in result.content
    assert "sha256:" + hashlib.sha256((workspace / "text.txt").read_bytes()).hexdigest() in result.content
    original = owner.input_uri

    def slow(value):
        time.sleep(0.02)
        return original(value)

    monkeypatch.setattr(owner, "input_uri", slow)
    result = await _read(tools, "text.txt", synchronous, timeout_ms=1)
    assert result.status == "error" and json.loads(result.content)["reason"] == "deadline"


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_read_private_and_escaping_symlink_targets_are_rejected(read_case, tmp_path, synchronous):
    workspace, owner, tools = read_case
    private = workspace / ".boxteam/private.txt"
    private.write_text("PRIVATE CONTENT")
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE CONTENT")
    (workspace / "alias.txt").symlink_to(private)
    (workspace / "escape.txt").symlink_to(outside)
    uri = owner.input_uri(".")
    for path in (".boxteam/private.txt", "alias.txt", "escape.txt", uri + "/escape.txt"):
        result = await _read(tools, path, synchronous)
        assert result.status == "error" and json.loads(result.content)["code"] == "capability_denied"
        assert "PRIVATE CONTENT" not in result.content and "OUTSIDE CONTENT" not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_existing_workspace_read_permission_applies_to_all_three_inputs(configure_filesystem_tools, tmp_path, synchronous):
    (tmp_path / "secret.txt").write_text("DO NOT READ")
    middleware = FilesystemMiddleware(backend=build_workspace_backend(tmp_path),
        _permissions=[FilesystemPermission(operations=["read"], paths=["/secret.txt"], mode="deny")])
    owner = configure_filesystem_tools(middleware, workspace_root=tmp_path)
    tools = {tool.name: tool for tool in middleware.tools}
    for path in ("secret.txt", str(tmp_path / "secret.txt"), owner.input_uri("secret.txt")):
        result = await _read(tools, path, synchronous)
        assert result.status == "error" and json.loads(result.content)["code"] == "capability_denied"
        assert "DO NOT READ" not in result.content


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("name", ["ls", "read_file", "glob", "grep"])
@pytest.mark.parametrize("uri,code", [
    ("file:///private/native-uri-marker/secret", "unknown_scheme"),
    ("boxteam://workspace/root/resources/files/root/x?path=/native-uri-marker", "query_rejected"),
    ("boxteam://workspace/root/resources/files/root/native-uri-marker%2Fsecret", "invalid_character"),
    (r"boxteam://workspace/root/resources/files/root/native-uri-marker\secret", "backslash_rejected"),
])
async def test_file_tool_grammar_error_preserves_code_without_input(read_case, synchronous, name, uri, code):
    workspace, _, tools = read_case
    before = {path.relative_to(workspace): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    arguments = {"path": uri, "runtime": SimpleNamespace(tool_call_id="uri-grammar-error")}
    if name in {"glob", "grep"}:
        arguments["pattern"] = "needle"
    call = {"name": name, "args": arguments, "id": "uri-grammar-error", "type": "tool_call"}
    message = tools[name].invoke(call) if synchronous else await tools[name].ainvoke(call)
    assert message.status == "error" and message.tool_call_id == "uri-grammar-error"
    body = json.loads(message.content)
    assert body["owner"] == "resource_uri_grammar" and body["code"] == code
    assert body["message"] and "native-uri-marker" not in message.model_dump_json()
    assert {path.relative_to(workspace): path.read_bytes() for path in workspace.rglob("*") if path.is_file()} == before
