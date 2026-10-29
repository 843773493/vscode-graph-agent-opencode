"""固定 Skill 目录失败不得被诊断日志的次错覆盖。"""

from __future__ import annotations

import asyncio
import errno
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

import app.services.infrastructure.workspace_file_watch_service as watch_module
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)


@pytest.fixture(params=[False, True], ids=["logging", "logging-unavailable"])
def diagnostic_sink(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog):
    blocked = tmp_path / "blocked-log-parent"
    blocked.write_text("不是目录", encoding="utf-8")
    original = logging.Logger.callHandlers

    def call_handlers(logger: logging.Logger, record: logging.LogRecord) -> None:
        original(logger, record)
        if request.param and record.name in {
            "app.core.failures",
            watch_module.__name__,
            "app.services.infrastructure.resource_platform.sources.workspace_file_resources",
        }:
            with (blocked / "service.log").open("a", encoding="utf-8"):
                pass

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(logging.Logger, "callHandlers", call_handlers)
    return bool(request.param), blocked


@pytest.mark.parametrize("failure_kind", ["read", "symlink"])
def test_initial_directory_failure_publishes_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog, diagnostic_sink,
    failure_kind: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = workspace / ".boxteam" / "skills"
    root.parent.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    native_error = PermissionError(errno.EACCES, "拒绝读取原目录", str(root))
    original_iterdir = Path.iterdir
    reads: list[Path] = []

    def iterdir(path: Path) -> Iterator[Path]:
        reads.append(path)
        if path == root:
            raise native_error
        return original_iterdir(path)

    if failure_kind == "read":
        root.mkdir()
        monkeypatch.setattr(Path, "iterdir", iterdir)
    else:
        root.symlink_to(external, target_is_directory=True)
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace, watch_service=WorkspaceFileWatchService(workspace_root=workspace),
    )
    subscription = registry.subscribe_observation(label="directory-failure")
    try:
        rejected = registry.register_skill_directory(root=root, scope="workspace", scope_id="test")
        assert registry.skill_directory_snapshot(rejected.directory_id) is rejected
        assert not rejected.available and rejected.entries == ()
        assert rejected.error is not None
        assert str(root) not in rejected.error and str(external) not in rejected.error
        failed_logging, blocked = diagnostic_sink
        assert str(blocked) not in rejected.error
        if failed_logging:
            assert "diagnostic_sink_error=NotADirectoryError" in rejected.error
        observations = subscription.pending()
        assert len(observations) == 1
        assert observations[0].directory_id == rejected.directory_id
        assert observations[0].available is False
        errors = [record.exc_info[1] for record in caplog.records if record.exc_info]
        assert len(errors) == 1
        if failure_kind == "read":
            assert errors[0] is native_error and reads == [root]
            assert "PermissionError" in rejected.error and f"errno={errno.EACCES}" in rejected.error
            monkeypatch.setattr(Path, "iterdir", original_iterdir)
        else:
            assert "Skill 根目录不允许 symlink: scope=workspace, scope_id=test" in rejected.error
            root.unlink()
            root.mkdir()
        if failed_logging:
            assert str(blocked) in "\n".join(errors[0].__notes__)
        entry = root / "demo"
        entry.mkdir()
        (entry / "SKILL.md").write_text("原入口恢复正文\n", encoding="utf-8")
        registry.reconcile()
        recovered = registry.skill_directory_snapshot(rejected.directory_id)
        assert recovered.available and recovered.error is None
        assert len(recovered.entries) == 1
        assert recovered.entries[0].uri == "boxteam://workspace/test/resources/skills/demo/SKILL.md"
        assert registry.snapshot(recovered.entries[0].uri).content == "原入口恢复正文\n"
        assert any(item.directory_id == rejected.directory_id and item.available for item in subscription.pending())
        assert list(external.iterdir()) == []
    finally:
        registry.unsubscribe_observation(subscription)


@pytest.mark.asyncio
async def test_upstream_watch_failure_reaches_file_and_directory_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, diagnostic_sink,
) -> None:
    root = tmp_path / "workspace"
    skill = root / ".boxteam" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("原已接受正文\n", encoding="utf-8")
    failure = PermissionError(errno.EACCES, "底层监听失败", str(root))
    release = asyncio.Event()

    async def failing_watch(_root: Path, **_options):
        yield set()
        await release.wait()
        raise failure

    monkeypatch.setattr(watch_module, "awatch", failing_watch)
    watcher = WorkspaceFileWatchService(workspace_root=root)
    registry = WorkspaceFileResourceRegistry(workspace_root=root, watch_service=watcher, project_root=tmp_path)
    initial = registry.register_skill_directory(root=skill.parent.parent, scope="workspace", scope_id="test")
    original = registry.snapshot(initial.entries[0].uri)
    subscription = registry.subscribe_observation(label="watch-failure")
    try:
        await asyncio.wait_for(registry.start(), timeout=2)
        subscription.pending()
        release.set()
        async with asyncio.timeout(2):
            while registry.snapshot(original.uri).available:
                registry.check_running()
                await asyncio.sleep(0.01)
        registry.check_running()
        file = registry.snapshot(original.uri)
        directory = registry.skill_directory_snapshot(initial.directory_id)
        assert not directory.available and directory.entries == initial.entries
        assert directory.revision == initial.revision
        assert file.content == original.content and file.revision == original.revision
        assert file.error == directory.error
        assert file.error is not None and "PermissionError" in file.error
        broken_logging, blocked = diagnostic_sink
        assert str(root) not in file.error and str(blocked) not in file.error
        if broken_logging:
            assert "diagnostic_sink_error=NotADirectoryError" in file.error
            assert str(blocked) in "\n".join(failure.__notes__)
        observations = subscription.pending()
        assert any(item.uri == original.uri and not item.available for item in observations)
        assert any(item.directory_id == initial.directory_id and not item.available for item in observations)
    finally:
        release.set()
        registry.unsubscribe_observation(subscription)
        await registry.stop()
        await watcher.shutdown()
