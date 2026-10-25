"""模型文件工具读取 inline Skill 时，发行身份拒绝不暴露安装位置。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware

from app.agents.workspace_backend import build_workspace_backend
from app.agents.workspace_filesystem_tools import configure_workspace_filesystem_tools
from app.core.distribution_identity import MANIFEST_ENV
from app.core.resource_uri import resource_display_uri
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def manifest_tools(integration_workspace_root_path, text_mutations_factory, tmp_path, monkeypatch):
    workspace = Path(integration_workspace_root_path)
    project = tmp_path / "private-installation"
    skill = project / "resources/skills/demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(b"frozen inline skill\r\n")
    manifest = project / "runtime-manifest.json"
    original = json.dumps({"distribution": "standalone", "version": "1.2.3"}).encode()
    manifest.write_bytes(original)
    monkeypatch.setenv(MANIFEST_ENV, str(manifest))
    workspace_id = load_or_create_workspace_id(workspace)
    navigation = ResourceNavigation(workspace_root=workspace, workspace_id=workspace_id, project_root=project,
        file_paths=WorkspaceFilePaths(workspace=workspace, workspace_id=workspace_id, bindings=None))
    middleware = FilesystemMiddleware(backend=build_workspace_backend(workspace), tool_token_limit_before_evict=None)
    configure_workspace_filesystem_tools(middleware, workspace_root=workspace,
        resource_navigation=navigation, text_mutations=text_mutations_factory(workspace))
    gateway = set_current_gateway_id("gateway_0123456789abcdef0123456789abcdef")
    try:
        yield SimpleNamespace(manifest=manifest, original=original, project=project,
            workspace=workspace, tools={tool.name: tool for tool in middleware.tools},
            uri=resource_display_uri(scope="inline", scope_id="standalone-1_2_3", kind="skills", tail_segments=("demo",)))
    finally:
        reset_current_gateway_id(gateway)


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("tool_name", ["ls", "read_file"])
@pytest.mark.parametrize("failure", ["json", "object", "distribution", "version"])
async def test_inline_tool_rejection_and_same_owner_repair(manifest_tools, caplog, synchronous, tool_name, failure):
    case = manifest_tools
    private = "/manifest-private/secret-value"
    if failure == "json":
        damaged = b'{"manifest-private":'
    elif failure == "object":
        damaged = b'["manifest-private"]'
    else:
        payload = json.loads(case.original)
        payload[failure] = private
        damaged = json.dumps(payload).encode()
    uri = case.uri if tool_name == "ls" else case.uri + "/SKILL.md"
    call = {"name": tool_name, "args": {"path": uri, "runtime": SimpleNamespace(tool_call_id="manifest-tool")},
            "id": "manifest-tool", "type": "tool_call"}
    tool = case.tools[tool_name]

    async def invoke():
        return tool.invoke(call) if synchronous else await tool.ainvoke(call)

    def workspace_bytes():
        return {str(path.relative_to(case.workspace)): path.read_bytes()
                for path in case.workspace.rglob("*") if path.is_file()}

    baseline = workspace_bytes()
    initial = await invoke()
    assert initial.status == "success", initial.content
    case.manifest.write_bytes(damaged)
    rejected = await invoke()
    assert rejected.status == "error" and rejected.tool_call_id == "manifest-tool"
    for native in (str(case.project), str(case.workspace), "manifest-private"):
        assert native not in rejected.content
    assert "artifact=runtime_manifest" in rejected.content and "stage=" in rejected.content
    assert str(case.manifest) in caplog.text
    assert case.manifest.read_bytes() == damaged and workspace_bytes() == baseline
    case.manifest.write_bytes(case.original)
    recovered = await invoke()
    assert recovered == initial
    assert workspace_bytes() == baseline
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    artifact = f"{tool_name}-{failure}-{'sync' if synchronous else 'async'}.json"
    (context.artifacts_dir / artifact).write_text(json.dumps({
        "initial": initial.model_dump(mode="json"), "rejected": rejected.model_dump(mode="json"),
        "recovered": recovered.model_dump(mode="json"),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
