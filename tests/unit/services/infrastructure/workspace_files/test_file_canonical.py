"""真实文件 owner 发布规范拼写，保留目录级大小写和逻辑链接入口。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from app.api import workspace as workspace_api
from app.api.deps import get_workspace_service, verify_local_token
from app.core.resource_uri import file_uri_child
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.resource_navigation import ResourceNavigationRequest
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.resource_platform.sources.gateway_resource_navigation import (
    qualify_navigation_result,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.workspace_service import WorkspaceService

GATEWAY = "gateway_01900000000070008000000000000001"


@pytest.fixture
def file_case(empty_workspace_scope: tuple[str, Path]):
    workspace_id, workspace = empty_workspace_scope
    config = Mock(spec=ConfigService)
    config.get_workspace_file_default_limit.return_value = 500
    config.get_workspace_preview_max_bytes.return_value = 1024 * 1024
    config.get_workspace_preview_binary_sample_bytes.return_value = 8192
    service = WorkspaceService(config_service=config, workspace_root=workspace)
    directory = workspace / "Mixed"
    directory.mkdir()
    (directory / "File.TXT").write_text("canonical needle", encoding="utf-8")
    root = service.file_paths.workspace_uri
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(workspace_api.router, prefix="/api/v1")
    app.dependency_overrides[get_workspace_service] = lambda: service
    app.dependency_overrides[verify_local_token] = lambda: "test-token"
    navigation = ResourceNavigation(workspace_root=workspace, workspace_id=workspace_id,
                                    project_root=Path.cwd(), file_paths=service.file_paths)
    try:
        yield SimpleNamespace(service=service, workspace=workspace, directory=directory,
                              root=root, app=app, navigation=navigation)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_file_owner_http_and_navigation_publish_one_canonical_reference(file_case):
    case = file_case
    requested = file_uri_child(case.root, *(["mIXED", "fILE.txt"] if os.name == "nt" else ["Mixed", "File.TXT"]))
    canonical = file_uri_child(case.root, "Mixed", "File.TXT")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=case.app), base_url="http://test") as client:
        reply = await client.post("/api/v1/workspace/files/references", json={"paths": [requested, canonical]})
        assert reply.status_code == 200, reply.text
        assert reply.json()["data"]["uris"] == [canonical, canonical]
        assert reply.json()["request_id"] == reply.headers["X-Request-ID"]
        content = await client.get("/api/v1/workspace/files/content", params={"path": requested})
        assert content.status_code == 200, content.text
        assert content.json()["data"]["path"] == canonical
        saved = await client.put("/api/v1/workspace/files/content", params={"path": requested},
                                 json={"content": "saved needle", "expected_revision": content.json()["data"]["revision"]})
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["path"] == canonical
    described = await case.navigation.navigate(ResourceNavigationRequest(uri=requested, operation="describe"), local_gateway_id=GATEWAY)
    assert described.model_dump(mode="json")["node"]["uri"] == requested
    assert described.model_dump(mode="json")["node"]["canonical_uri"] == canonical
    query_root = requested.rsplit("/", 1)[0]
    result = await case.navigation.navigate(ResourceNavigationRequest(uri=query_root, operation="search", query="needle", search_mode="content"), local_gateway_id=GATEWAY)
    assert [entry.canonical_uri for entry in result.entries] == [canonical]
    assert [match.uri for match in result.matches] == [requested.rsplit("/", 1)[0] + "/File.TXT"]
    routed = qualify_navigation_result(result, uri=query_root, gateway_id=GATEWAY)
    assert routed.entries[0].canonical_uri == canonical
    assert case.directory.joinpath("File.TXT").read_text(encoding="utf-8") == "saved needle"
    listed = await case.service.list_files(path=query_root)
    assert listed.path == file_uri_child(case.root, "Mixed")
    assert [item.path for item in listed.items] == [canonical]


@pytest.mark.asyncio
async def test_missing_file_keeps_uncreated_suffix_and_cannot_save_without_preimage(file_case):
    case = file_case
    parent = "mIXED" if os.name == "nt" else "Mixed"
    requested = file_uri_child(case.root, parent, "NewFolder", "NEW.txt")
    expected = file_uri_child(case.root, "Mixed", "NewFolder", "NEW.txt")
    assert (await case.service.file_references([requested])).uris == [expected]
    with pytest.raises(FileNotFoundError):
        await case.service.update_file_content(path=requested, content="cannot create", expected_revision="0" * 64)
    assert not case.directory.joinpath("NewFolder").exists()


@pytest.mark.asyncio
async def test_resource_uri_canonical_keeps_symbolic_link_entry(file_case):
    case = file_case
    link = case.workspace / "Alias"
    link.symlink_to(case.directory, target_is_directory=True)
    alias = file_uri_child(case.root, "aLIAS" if os.name == "nt" else "Alias", "fILE.txt" if os.name == "nt" else "File.TXT")
    direct = file_uri_child(case.root, "Mixed", "File.TXT")
    expected = file_uri_child(case.root, "Alias", "File.TXT")
    assert (await case.service.file_references([alias, direct])).uris == [expected, direct]
    described = await case.navigation.navigate(ResourceNavigationRequest(uri=alias, operation="describe"), local_gateway_id=GATEWAY)
    assert described.node.canonical_uri == expected and described.node.canonical_uri != direct


@pytest.fixture
def sensitive_directory(file_case):
    directory = file_case.directory / "Sensitive"
    directory.mkdir()
    if os.name == "nt":
        command = ["fsutil.exe", "file", "setCaseSensitiveInfo", str(directory), "enable"]
        result = subprocess.run(command, capture_output=True, timeout=15, check=False)
        assert result.returncode == 0, (command, result.returncode, result.stdout, result.stderr)
    directory.joinpath("Case.txt").write_text("upper needle", encoding="utf-8")
    directory.joinpath("case.txt").write_text("lower needle", encoding="utf-8")
    assert sorted(item.name for item in directory.iterdir()) == ["Case.txt", "case.txt"]
    return directory


@pytest.mark.asyncio
async def test_mixed_directory_sensitivity_keeps_distinct_files(file_case, sensitive_directory):
    case = file_case
    parent = "mIXED" if os.name == "nt" else "Mixed"
    requested = [file_uri_child(case.root, parent, "Sensitive", name) for name in ("Case.txt", "case.txt")]
    canonical = [file_uri_child(case.root, "Mixed", "Sensitive", name) for name in ("Case.txt", "case.txt")]
    assert (await case.service.file_references(requested)).uris == canonical
    contents = [await case.service.get_file_content(path=reference) for reference in requested]
    assert [content.path for content in contents] == canonical
    assert [content.content for content in contents] == ["upper needle", "lower needle"]
    wrong = file_uri_child(case.root, parent, "Sensitive", "CASE.txt")
    with pytest.raises(FileNotFoundError):
        await case.service.get_file_content(path=wrong)
