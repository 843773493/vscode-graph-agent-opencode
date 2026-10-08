from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)


@pytest.mark.asyncio
async def test_registered_file_is_refreshed_by_shared_watcher(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agents_path = workspace / "AGENTS.md"
    agents_path.write_text("v1\n", encoding="utf-8")
    watcher = WorkspaceFileWatchService(workspace_root=workspace)
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace,
        watch_service=watcher,
    )
    initial = registry.register_file(
        uri="boxteam://workspace/test/resources/agent-spec/root/AGENTS.md",
        path=agents_path,
    )
    assert initial.content == "v1\n"

    await registry.start()
    try:
        agents_path.write_text("v2\n", encoding="utf-8")
        for _ in range(100):
            if registry.snapshot("boxteam://workspace/test/resources/agent-spec/root/AGENTS.md").content == "v2\n":
                break
            await asyncio.sleep(0.02)
        assert registry.snapshot("boxteam://workspace/test/resources/agent-spec/root/AGENTS.md").content == "v2\n"
    finally:
        await registry.stop()
        await watcher.shutdown()


@pytest.mark.asyncio
async def test_registered_internal_skill_is_refreshed_by_resource_subscription(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    skill_path = workspace / ".boxteam" / "skills" / "demo" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("v1\n", encoding="utf-8")
    watcher = WorkspaceFileWatchService(workspace_root=workspace)
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace,
        watch_service=watcher,
    )
    registry.register_file(
        uri="boxteam://workspace/test/resources/skills/demo/SKILL.md",
        path=skill_path,
    )

    await registry.start()
    try:
        skill_path.write_text("v2\n", encoding="utf-8")
        for _ in range(100):
            if registry.snapshot("boxteam://workspace/test/resources/skills/demo/SKILL.md").content == "v2\n":
                break
            await asyncio.sleep(0.02)
        assert registry.snapshot("boxteam://workspace/test/resources/skills/demo/SKILL.md").content == "v2\n"
    finally:
        await registry.stop()
        await watcher.shutdown()


def test_unavailable_refresh_retains_previous_valid_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "AGENTS.md"
    source.write_text("valid\n", encoding="utf-8")
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace,
        watch_service=WorkspaceFileWatchService(workspace_root=workspace),
    )
    registry.register_file(uri="boxteam://workspace/test/resources/agent-spec/root/AGENTS.md", path=source)

    source.write_bytes(b"\xff\xfe")
    snapshot = registry.refresh("boxteam://workspace/test/resources/agent-spec/root/AGENTS.md")

    assert snapshot.available is False
    assert snapshot.content == "valid\n"
    assert snapshot.revision is not None


def test_optional_agents_absence_empty_body_and_deletion_have_distinct_revisions(tmp_path: Path) -> None:
    uri = "boxteam://workspace/test/resources/agent-spec/root/AGENTS.md"
    watcher = WorkspaceFileWatchService(workspace_root=tmp_path)
    registry = WorkspaceFileResourceRegistry(workspace_root=tmp_path, watch_service=watcher)
    path = tmp_path / "AGENTS.md"
    absent = registry.register_file(uri=uri, path=path, optional=True)
    assert absent.available and not absent.exists and absent.revision and absent.content == ""
    path.write_text("")
    empty = registry.refresh(uri)
    assert empty.available and empty.exists and empty.content == ""
    path.write_text("实际正文\n")
    body = registry.refresh(uri)
    assert body.available and body.exists and body.content == "实际正文\n"
    path.unlink()
    deleted = registry.refresh(uri)
    assert deleted == absent
    assert len({absent.revision, empty.revision, body.revision}) == 3


def test_required_missing_source_does_not_publish_optional_absence(tmp_path: Path) -> None:
    uri = "boxteam://workspace/test/resources/skills/demo/SKILL.md"
    registry = WorkspaceFileResourceRegistry(
        workspace_root=tmp_path, watch_service=WorkspaceFileWatchService(workspace_root=tmp_path),
    )
    snapshot = registry.register_file(uri=uri, path=tmp_path / "SKILL.md")
    assert not snapshot.available and not snapshot.exists and snapshot.revision is None
    assert "不存在" in snapshot.error
