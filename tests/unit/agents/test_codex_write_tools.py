"""正式模型写入入口的版本、原字节和共享 owner 行为。"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.permissions import FilesystemPermission
from pydantic import ValidationError

from app.agents.tools.apply_patch import create_apply_patch_tool
from app.agents.workspace_backend import build_workspace_backend
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_service import (
    WorkspaceFileConflictError,
    WorkspaceService,
)


@pytest.fixture
def write_tools(workspace_scope, configure_filesystem_tools):
    root = workspace_scope[1]
    middleware = FilesystemMiddleware(backend=build_workspace_backend(root), tool_token_limit_before_evict=None)
    configure_filesystem_tools(middleware, workspace_root=root)
    return root, {tool.name: tool for tool in middleware.tools}, SimpleNamespace(tool_call_id="call_text")


async def _invoke(tools, name, runtime, **arguments):
    return await tools[name].ainvoke({"type": "tool_call", "name": name, "id": runtime.tool_call_id,
                                    "args": {**arguments, "runtime": runtime}})


async def _read_version(tools, path, runtime):
    result = await _invoke(tools, "read_file", runtime, path=path)
    assert result.status == "success", result.content
    return re.search(r"sha256:[0-9a-f]{64}", result.content).group()


@pytest.mark.parametrize("input_kind", ["relative", "absolute", "uri"])
async def test_write_and_edit_share_three_inputs_and_preserve_original_bytes(write_tools, input_kind):
    root, tools, runtime = write_tools
    from app.core.resource_uri import resource_display_uri
    from app.core.workspace_identity import read_workspace_id
    uri = resource_display_uri(scope="workspace", scope_id=read_workspace_id(root), kind="files", tail_segments=("root", "原 文%25.txt"))
    target = {"relative": "原 文%25.txt", "absolute": str(root / "原 文%25.txt"), "uri": uri}[input_kind]
    created = await _invoke(tools, "write_file", runtime, file_path=target, content="\ufeffold\r\nkeep\r\n", expected_version="absent")
    assert created.status == "success", created.content
    version = await _read_version(tools, target, runtime)
    created_receipt = json.loads(created.content)
    assert created_receipt["files"][0]["content_hash"] == version
    edited = await _invoke(tools, "edit_file", runtime, file_path=target, old_string="old", new_string="new", expected_version=version)
    receipt = json.loads(edited.content)
    assert edited.status == "success" and receipt["effect"] == "completed", receipt
    assert receipt["target"] == uri and receipt["files"][0]["path"] == uri
    assert receipt["files"][0]["state"] == "published"
    assert (root / "原 文%25.txt").read_bytes() == b"\xef\xbb\xbfnew\r\nkeep\r\n"
    assert await _read_version(tools, target, runtime) == receipt["files"][0]["content_hash"]


async def test_old_version_and_expected_absent_never_overwrite_new_content(write_tools):
    root, tools, runtime = write_tools
    path = root / "conflict.txt"
    path.write_bytes(b"old\n")
    version = await _read_version(tools, "conflict.txt", runtime)
    path.write_bytes(b"external\r\n")
    edit = await _invoke(tools, "edit_file", runtime, file_path="conflict.txt", expected_version=version, old_string="old", new_string="lost")
    create = await _invoke(tools, "write_file", runtime, file_path="conflict.txt", expected_version="absent", content="lost")
    for result in (edit, create):
        receipt = json.loads(result.content)
        assert result.status == "error" and receipt["code"] == "revision_conflict"
        assert receipt["effect"] == "not_executed" and receipt["retryable"] is False
    assert path.read_bytes() == b"external\r\n"


@pytest.mark.parametrize("competitor", ["patch", "web"])
async def test_edit_competes_with_other_owner_writers_using_same_version(write_tools, competitor):
    root, tools, runtime = write_tools
    path = root / "concurrent.txt"
    path.write_bytes(b"old\n")
    version = await _read_version(tools, "concurrent.txt", runtime)
    edit = _invoke(tools, "edit_file", runtime, file_path="concurrent.txt", expected_version=version, old_string="old", new_string="edit")
    if competitor == "patch":
        patch = create_apply_patch_tool(workspace_root=root)
        other = patch.ainvoke({"input": "*** Begin Patch\n*** Update File: concurrent.txt\n@@\n-old\n+patch\n*** End Patch",
                              "expected_versions": {"concurrent.txt": version}, "explanation": "同版本竞争"})
    else:
        config = Mock(spec=ConfigService)
        config.get_workspace_preview_max_bytes.return_value = 1048576
        config.get_workspace_preview_binary_sample_bytes.return_value = 8192
        service = WorkspaceService(config_service=config, workspace_root=root)
        other = service.update_file_content(path="concurrent.txt", content="web\n", expected_revision=version.removeprefix("sha256:"))
    first, second = await asyncio.gather(edit, other, return_exceptions=True)
    first = json.loads(first.content)
    if competitor == "patch":
        second = json.loads(second)
        assert sorted((first["status"], second["status"])) == ["error", "success"]
        assert next(item for item in (first, second) if item["status"] == "error")["code"] == "revision_conflict"
    elif first["status"] == "success":
        assert isinstance(second, WorkspaceFileConflictError)
    else:
        assert first["code"] == "revision_conflict" and not isinstance(second, Exception)
    assert path.read_bytes() in {b"edit\n", competitor.encode() + b"\n"}


async def test_exact_edit_ambiguity_and_replace_all_remain_explicit(write_tools):
    root, tools, runtime = write_tools
    path = root / "repeated.txt"
    path.write_bytes(b"same\r\nsame\r\n")
    version = await _read_version(tools, "repeated.txt", runtime)
    ambiguous = await _invoke(tools, "edit_file", runtime, file_path="repeated.txt", expected_version=version, old_string="same", new_string="new")
    assert ambiguous.status == "error" and path.read_bytes() == b"same\r\nsame\r\n"
    replaced = await _invoke(tools, "edit_file", runtime, file_path="repeated.txt", expected_version=version, old_string="same", new_string="new", replace_all=True)
    assert replaced.status == "success" and path.read_bytes() == b"new\r\nnew\r\n"


@pytest.mark.parametrize("name,args", [("write_file", {"content": "new"}), ("edit_file", {"old_string": "old", "new_string": "new"})])
async def test_public_write_schema_requires_caller_version(write_tools, name, args):
    root, tools, runtime = write_tools
    assert "expected_version" in tools[name].tool_call_schema.model_json_schema()["required"]
    with pytest.raises(ValidationError):
        await _invoke(tools, name, runtime, file_path="missing-version.txt", **args)
    assert not (root / "missing-version.txt").exists()


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file", "apply_patch"])
async def test_all_text_tools_reject_same_write_permission_before_any_publication(
    workspace_scope, configure_filesystem_tools, tool_name,
):
    root = workspace_scope[1]
    rules = [FilesystemPermission(operations=["write"], paths=["/blocked.txt"], mode="deny")]
    middleware = FilesystemMiddleware(backend=build_workspace_backend(root), _permissions=rules)
    configure_filesystem_tools(middleware, workspace_root=root)
    tools = {tool.name: tool for tool in middleware.tools}
    runtime = SimpleNamespace(tool_call_id="call_denied_text")
    blocked = root / "blocked.txt"
    if tool_name != "write_file":
        blocked.write_bytes(b"old\r\n")
        version = await _read_version(tools, "blocked.txt", runtime)
    if tool_name == "apply_patch":
        result = json.loads(await create_apply_patch_tool(workspace_root=root, permissions=rules).ainvoke({
            "input": "*** Begin Patch\n*** Add File: allowed.txt\n+new\n*** Update File: blocked.txt\n@@\n-old\n+new\n*** End Patch",
            "expected_versions": {"allowed.txt": "absent", "blocked.txt": version},
            "explanation": "整批必须受同一权限规则约束",
        }))
    else:
        arguments = {"content": "new", "expected_version": "absent"} if tool_name == "write_file" else {
            "old_string": "old", "new_string": "new", "expected_version": version,
        }
        result = json.loads((await _invoke(tools, tool_name, runtime, file_path="blocked.txt", **arguments)).content)
    assert result["status"] == "error" and result["code"] == "capability_denied", result
    assert result["effect"] == "not_executed" and result["retryable"] is False
    assert not (root / "allowed.txt").exists()
    if tool_name == "write_file":
        assert not blocked.exists()
    else:
        assert blocked.read_bytes() == b"old\r\n"
    assert not (root / ".boxteam/cache/text_mutations").exists()
