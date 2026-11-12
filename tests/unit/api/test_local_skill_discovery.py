"""本地 Skill 发现沿真实固定根导航；浏览不登记来源或读取正文。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import resources
from app.api.deps import get_resource_navigation
from app.core.resource_uri import resource_display_uri, skill_display_uri
from app.core.trace_middleware import TraceMiddleware
from app.services.infrastructure.resource_platform.sources import resource_navigation
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    NAVIGATION_DEADLINE_HEADER,
    ResourceNavigation,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths


@dataclass
class LocalSkillCase:
    workspace_id: str
    workspace: Path
    client: httpx.AsyncClient

    def file(self, name: str, content: bytes = b"local Skill body") -> Path:
        path = self.workspace / ".boxteam" / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def uri(self, name: str) -> str:
        return skill_display_uri(scope="workspace", scope_id=self.workspace_id, skill_name=name)


@pytest.fixture
async def local_skill_case(empty_workspace_scope: tuple[str, Path], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[LocalSkillCase]:
    from app.agents import skill_catalog
    from app.main import app as workspace_app

    workspace_id, workspace = empty_workspace_scope
    owner = ResourceNavigation(workspace_root=workspace, workspace_id=workspace_id,
        file_paths=WorkspaceFilePaths(workspace=workspace, workspace_id=workspace_id, bindings=None))
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(resources.router, prefix="/api/v1")
    application.add_exception_handler(HTTPException, workspace_app.exception_handlers[HTTPException])
    application.dependency_overrides[get_resource_navigation] = lambda: owner

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("本地发现和普通文件打开不得编译 runtime、登记或刷新来源")

    monkeypatch.setattr(skill_catalog, "build_workspace_skill_catalog", forbidden)
    for method in ("register_skill_directory", "register_file", "refresh"):
        monkeypatch.setattr(WorkspaceFileResourceRegistry, method, forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(application), base_url="http://test", headers={
        "X-Local-Token": "local-dev-token", "X-BoxTeam-Gateway-Id": "gateway_" + "a" * 32,
        "X-Request-ID": "local-skill-discovery",
    }) as client:
        yield LocalSkillCase(workspace_id, workspace, client)


async def test_discovery_uses_package_identity_and_skips_nonentries_without_reading_body(
    local_skill_case: LocalSkillCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = local_skill_case
    case.file("z-skill", b"---\nname: a-different-name\n---\nprivate-frontmatter-body")
    case.file("a-skill", b"\xff")
    root = case.workspace / ".boxteam/skills"
    (root / "README.md").write_text("not a package")
    (root / "missing/nested").mkdir(parents=True)
    (root / "missing/nested/SKILL.md").write_text("not a top-level entry")
    (root / "directory-entry/SKILL.md").mkdir(parents=True)

    def forbidden_read(*args: object, **kwargs: object) -> bytes:
        pytest.fail("metadata 发现不得读取文件正文")

    monkeypatch.setattr(ResourceNavigation, "_read_bytes", forbidden_read)
    response = await case.client.get("/api/v1/resources/local-skills")
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "local-skill-discovery"
    payload = response.json()["data"]
    assert payload["workspace_id"] == case.workspace_id
    assert [entry["name"] for entry in payload["entries"]] == ["a-skill", "z-skill"]
    for entry in payload["entries"]:
        assert entry["node"] == {"uri": case.uri(entry["name"]), "resource_shape": "file",
            "operations": ["describe", "read", "search"], "child_path": None,
            "canonical_uri": None, "stat": None}
    for private in (str(case.workspace), "private-frontmatter-body", "a-different-name", "content", "revision"):
        assert private not in response.text


async def test_reopen_discovers_current_files_and_stale_pick_uses_original_not_found(local_skill_case: LocalSkillCase) -> None:
    case = local_skill_case
    path = case.file("same", "真实只读来源 Ω".encode())
    first = await case.client.get("/api/v1/resources/local-skills")
    node = first.json()["data"]["entries"][0]["node"]
    read = await case.client.post("/api/v1/resources/navigation", json={"uri": node["uri"], "operation": "read"})
    assert read.status_code == 200, read.text
    assert read.json()["data"]["content"] == "真实只读来源 Ω"
    path.unlink()
    case.file("new-entry")
    second = await case.client.get("/api/v1/resources/local-skills")
    assert [entry["name"] for entry in second.json()["data"]["entries"]] == ["new-entry"]
    deleted = await case.client.post("/api/v1/resources/navigation", json={"uri": node["uri"], "operation": "read"})
    assert deleted.status_code == 404
    assert deleted.json()["detail"] == {"owner": "resource_uri_resolver", "code": "unknown_resource", "message": "资源不存在"}
    assert deleted.headers["X-Request-ID"] == deleted.json()["request_id"] == "local-skill-discovery"


async def test_authorized_missing_file_is_file_not_found_but_private_uri_stays_denied(local_skill_case: LocalSkillCase) -> None:
    case = local_skill_case
    root_uri = resource_display_uri(scope="workspace", scope_id=case.workspace_id, kind="files", tail_segments=("root",))
    missing = await case.client.post("/api/v1/resources/navigation", json={
        "uri": root_uri + "/.vscode/settings.json", "operation": "describe",
    })
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"] == {
        "owner": "resource_file_operations", "code": "file_not_found",
        "reason": "not_found", "message": "目标文件不存在",
    }

    private = await case.client.post("/api/v1/resources/navigation", json={
        "uri": root_uri + "/.boxteam/missing.json", "operation": "describe",
    })
    assert private.status_code == 403, private.text
    assert private.json()["detail"]["owner"] == "resource_uri_resolver"
    assert private.json()["detail"]["code"] == "capability_denied"


async def test_missing_fixed_root_is_empty_and_file_in_place_of_root_is_explicit(local_skill_case: LocalSkillCase) -> None:
    case = local_skill_case
    root = case.workspace / ".boxteam/skills"
    assert not root.exists()
    response = await case.client.get("/api/v1/resources/local-skills")
    assert response.json()["data"] == {"workspace_id": case.workspace_id, "entries": []}
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text("not a directory")
    response = await case.client.get("/api/v1/resources/local-skills")
    assert response.status_code == 403
    assert response.json()["detail"]["message"] == "Skill 固定根不是目录"


@pytest.mark.parametrize("location", ["collection", "package", "outside-entry"])
async def test_discovery_keeps_original_symlink_boundary(local_skill_case: LocalSkillCase, location: str) -> None:
    case = local_skill_case
    path = case.file("sample")
    target = case.workspace / "target"
    target.mkdir()
    (target / "SKILL.md").write_text("outside target")
    if location == "collection":
        path.unlink()
        path.parent.rmdir()
        path.parent.parent.rmdir()
        path.parent.parent.symlink_to(target, target_is_directory=True)
    elif location == "package":
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(target, target_is_directory=True)
    else:
        path.unlink()
        path.symlink_to(target / "SKILL.md")
    response = await case.client.get("/api/v1/resources/local-skills")
    assert response.status_code == 403
    assert response.json()["detail"]["owner"] == "resource_uri_resolver"
    assert response.json()["detail"]["code"] == "capability_denied"
    assert str(case.workspace) not in response.text


async def test_entry_symlink_inside_package_follows_existing_read_capability(local_skill_case: LocalSkillCase) -> None:
    case = local_skill_case
    path = case.file("sample")
    path.rename(path.with_name("entry.md"))
    path.symlink_to("entry.md")
    response = await case.client.get("/api/v1/resources/local-skills")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["entries"][0]["node"]["uri"] == case.uri("sample")
    read = await case.client.post("/api/v1/resources/navigation", json={"uri": case.uri("sample"), "operation": "read"})
    assert read.json()["data"]["content"] == "local Skill body"


async def test_invalid_identity_and_existing_http_route_conflict_are_not_silently_skipped(local_skill_case: LocalSkillCase) -> None:
    case = local_skill_case
    invalid = case.file("invalid name")
    response = await case.client.get("/api/v1/resources/local-skills")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_character"
    invalid.unlink()
    case.file("http_conflict")
    original = await case.client.post("/api/v1/resources/navigation", json={"uri": case.uri("http_conflict"), "operation": "read"})
    assert original.status_code == 403, "未装配配置来源reader时，既有导航仍不读取http_本地文件"
    assert original.json()["detail"]["code"] == "capability_denied"
    response = await case.client.get("/api/v1/resources/local-skills")
    assert response.status_code == 403
    assert response.json()["detail"]["message"] == "本地 Skill URI 与配置来源路由冲突，无法按本地来源打开"
    assert set(response.json()) == {"detail", "request_id"}


@pytest.mark.parametrize(("query", "reason"), [("limit=1", "entry_limit"), ("scan_limit=1", "scan_limit"),
                                                   ("max_response_bytes=1024", "response_byte_limit")])
async def test_original_navigation_budgets_fail_explicitly_instead_of_publishing_partial_inventory(
    local_skill_case: LocalSkillCase, query: str, reason: str,
) -> None:
    case = local_skill_case
    for index in range(4):
        case.file(f"skill-{index}-" + "a" * 90)
    response = await case.client.get("/api/v1/resources/local-skills?" + query)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "output_limit_exceeded"
    assert response.json()["detail"]["reason"] == reason
    assert set(response.json()) == {"detail", "request_id"}


async def test_original_deadline_and_authentication_are_enforced(local_skill_case: LocalSkillCase) -> None:
    case = local_skill_case
    denied = await case.client.get("/api/v1/resources/local-skills", headers={"X-Local-Token": "wrong"})
    assert denied.status_code == 401
    response = await case.client.get("/api/v1/resources/local-skills", headers={NAVIGATION_DEADLINE_HEADER: str(time.time() - 1)})
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "deadline"


async def test_exact_entry_budget_succeeds_and_one_more_does_not_publish_a_partial_list(local_skill_case: LocalSkillCase) -> None:
    case = local_skill_case
    case.file("first")
    exact = await case.client.get("/api/v1/resources/local-skills?limit=1")
    assert exact.status_code == 200 and len(exact.json()["data"]["entries"]) == 1
    case.file("second")
    more = await case.client.get("/api/v1/resources/local-skills?limit=1")
    assert more.status_code == 400 and set(more.json()) == {"detail", "request_id"}
    assert more.json()["detail"]["reason"] == "entry_limit"


async def test_timeout_during_directory_enumeration_does_not_publish_earlier_entries(
    local_skill_case: LocalSkillCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = local_skill_case
    case.file("first")
    case.file("second")
    original = resource_navigation.os.scandir

    @contextmanager
    def slow_directory(path: Path):
        with original(path) as entries:
            def delayed():
                for index, entry in enumerate(entries):
                    if index:
                        time.sleep(0.03)
                    yield entry
            yield delayed()

    monkeypatch.setattr(resource_navigation.os, "scandir", slow_directory)
    response = await case.client.get("/api/v1/resources/local-skills?timeout_ms=10")
    assert response.status_code == 400 and set(response.json()) == {"detail", "request_id"}
    assert response.json()["detail"]["reason"] == "deadline"
    await asyncio.sleep(0.04)
