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
async def test_fixed_skill_directory_discovers_only_direct_entries_and_publishes_deletion(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    watcher = WorkspaceFileWatchService(workspace_root=workspace)
    registry = WorkspaceFileResourceRegistry(workspace_root=workspace, watch_service=watcher)
    root = workspace / ".boxteam" / "skills"
    initial = registry.register_skill_directory(root=root, scope="workspace", scope_id="test")
    assert initial.available and initial.entries == ()
    subscription = registry.subscribe_observation(label="目录发现验收")
    await registry.start()
    try:
        nested = root / "container" / "nested"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text("不能递归发现\n", encoding="utf-8")
        direct = root / "direct"
        direct.mkdir()
        skill = direct / "SKILL.md"
        skill.write_text("已接受正文\n", encoding="utf-8")
        async with asyncio.timeout(5):
            while not registry.skill_directory_snapshot(initial.directory_id).entries:
                await asyncio.sleep(0.02)
        accepted = registry.skill_directory_snapshot(initial.directory_id)
        assert [entry.name for entry in accepted.entries] == ["direct"]
        entry = accepted.entries[0]
        assert registry.snapshot(entry.uri).content == "已接受正文\n"
        notifications = subscription.pending()
        assert any(item.directory_id == initial.directory_id and item.uri is None for item in notifications)
        skill.unlink()
        async with asyncio.timeout(5):
            while registry.skill_directory_snapshot(initial.directory_id).entries:
                await asyncio.sleep(0.02)
        assert not registry.snapshot(entry.uri).available
        assert registry.snapshot(entry.uri).exists is False
        assert registry.skill_directory_snapshot(initial.directory_id).revision == initial.revision
    finally:
        registry.unsubscribe_observation(subscription)
        await registry.stop()
        await watcher.shutdown()


@pytest.mark.asyncio
async def test_gateway_skills_root_creation_update_delete_and_rebuild_use_shallow_watches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gateway_home = tmp_path / "gateway-home"
    gateway_home.mkdir()
    watcher = WorkspaceFileWatchService(workspace_root=workspace)
    registry = WorkspaceFileResourceRegistry(
        workspace_root=None, gateway_skills_root=gateway_home / "skills", watch_service=watcher,
    )
    root = gateway_home / "skills"
    initial = registry.register_skill_directory(root=root, scope="gateway", scope_id="gateway-test")
    assert initial.available and initial.entries == ()

    await registry.start()
    try:
        home_key = watcher._watcher_key(gateway_home, False, registry._watch_filter)
        assert home_key in watcher._watchers
        assert watcher._watcher_key(gateway_home, True, registry._watch_filter) not in watcher._watchers

        skill = root / "demo" / "SKILL.md"
        entry_key = watcher._watcher_key(skill.parent, False, registry._watch_filter)
        reconciled_directories: list[str] = []
        entry_ready_reconciliations: list[str] = []
        post_ready_reconcile = asyncio.Event()
        gap_update_applied = False
        reconcile = registry._reconcile_skill_directory

        def record_reconcile(directory_id: str, handle):
            nonlocal gap_update_applied
            reconciled_directories.append(directory_id)
            snapshot = reconcile(directory_id, handle)
            entry_watcher = watcher._watchers.get(entry_key)
            if entry_watcher is not None and entry_watcher.ready.is_set():
                entry_ready_reconciliations.append(directory_id)
                post_ready_reconcile.set()
            if snapshot.entries and not gap_update_applied:
                skill.write_text("gateway after root scan\n", encoding="utf-8")
                gap_update_applied = True
            return snapshot

        monkeypatch.setattr(registry, "_reconcile_skill_directory", record_reconcile)
        unrelated = gateway_home / "unrelated.json"
        unrelated.write_text("noise", encoding="utf-8")
        await asyncio.sleep(0.4)
        assert reconciled_directories == []

        skill.parent.mkdir(parents=True)
        skill.write_text("gateway v1\n", encoding="utf-8")
        async with asyncio.timeout(5):
            while not registry.skill_directory_snapshot(initial.directory_id).entries:
                await asyncio.sleep(0.02)
        entry = registry.skill_directory_snapshot(initial.directory_id).entries[0]
        assert watcher._watcher_key(root, False, registry._watch_filter) in watcher._watchers
        async with asyncio.timeout(5):
            await watcher.wait_until_ready(
                (gateway_home, root, skill.parent),
                recursive=False,
                change_filter=registry._watch_filter,
            )
            await post_ready_reconcile.wait()
        assert gap_update_applied
        assert entry_ready_reconciliations == [initial.directory_id]
        assert entry_key in watcher._watchers
        assert registry.snapshot(entry.uri).content == "gateway after root scan\n"

        skill.write_text("gateway v2\n", encoding="utf-8")
        async with asyncio.timeout(5):
            while registry.snapshot(entry.uri).content != "gateway v2\n":
                await asyncio.sleep(0.02)

        skill.unlink()
        skill.parent.rmdir()
        root.rmdir()
        async with asyncio.timeout(5):
            while registry.skill_directory_snapshot(initial.directory_id).entries:
                await asyncio.sleep(0.02)
        assert not registry.snapshot(entry.uri).available

        skill.parent.mkdir(parents=True)
        skill.write_text("gateway v3\n", encoding="utf-8")
        async with asyncio.timeout(5):
            while registry.skill_directory_snapshot(initial.directory_id).entries == ():
                await asyncio.sleep(0.02)
            while registry.snapshot(entry.uri).content != "gateway v3\n":
                await asyncio.sleep(0.02)
        assert registry.snapshot(entry.uri).available
    finally:
        await registry.stop()
        await watcher.shutdown()


def test_inline_directory_reads_only_manifest_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = tmp_path / "distribution"
    root = project / "resources" / "skills"
    declared = root / "declared"
    declared.mkdir(parents=True)
    (declared / "SKILL.md").write_text("manifest 正文\n", encoding="utf-8")
    (root / "undeclared").symlink_to(tmp_path / "outside", target_is_directory=True)
    watcher = WorkspaceFileWatchService(workspace_root=workspace)
    registry = WorkspaceFileResourceRegistry(workspace_root=workspace, project_root=project, watch_service=watcher)
    original_iterdir = Path.iterdir

    def guarded_iterdir(path: Path):
        if path == root:
            raise AssertionError("inline 只能访问 manifest 声明的 entry，不能枚举目录")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    accepted = registry.register_skill_directory(
        root=root, scope="inline", scope_id="test-distribution", declared_entries=("declared",),
    )
    assert accepted.available and [entry.name for entry in accepted.entries] == ["declared"]
    assert registry.snapshot(accepted.entries[0].uri).content == "manifest 正文\n"


def test_skill_directory_rejects_intermediate_escape_before_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "outside"
    (external / "skills" / "secret").mkdir(parents=True)
    (workspace / ".boxteam").symlink_to(external, target_is_directory=True)
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace, watch_service=WorkspaceFileWatchService(workspace_root=workspace),
    )
    enumerated = []
    original_iterdir = Path.iterdir

    def record_enumeration(path):
        enumerated.append(path)
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", record_enumeration)
    snapshot = registry.register_skill_directory(
        root=workspace / ".boxteam" / "skills", scope="workspace", scope_id="test",
    )
    assert not snapshot.available and "允许根" in snapshot.error
    assert snapshot.entries == () and enumerated == []


@pytest.mark.asyncio
async def test_symlink_replacement_publishes_unavailable_without_reading_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "AGENTS.md"
    source.write_text("原始已发布正文\n", encoding="utf-8")
    target = tmp_path / "external-target.md"
    target.write_text("不得读取的外部正文\n", encoding="utf-8")
    uri = "boxteam://workspace/test/resources/agent-spec/root/AGENTS.md"
    watcher = WorkspaceFileWatchService(workspace_root=workspace)
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace,
        watch_service=watcher,
    )
    initial = registry.register_file(uri=uri, path=source)
    reader = registry._reconciler._reader
    read_all = reader._read_all
    read_contents: list[bytes] = []

    def record_read(fd: int, size: int) -> bytes:
        content = read_all(fd, size)
        read_contents.append(content)
        return content

    monkeypatch.setattr(reader, "_read_all", record_read)
    await registry.start()
    try:
        source.unlink()
        source.symlink_to(target)
        for _ in range(100):
            snapshot = registry.snapshot(uri)
            if not snapshot.available:
                break
            await asyncio.sleep(0.02)

        assert not snapshot.available
        assert snapshot.revision == initial.revision
        assert snapshot.content == initial.content == "原始已发布正文\n"
        assert snapshot.error is not None and "符号链接" in snapshot.error
        assert read_contents == []
    finally:
        await registry.stop()
        await watcher.shutdown()


def test_duplicate_registration_reuses_latest_snapshot_without_source_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "AGENTS.md"
    source.write_text("v1\n", encoding="utf-8")
    uri = "boxteam://workspace/test/resources/agent-spec/root/AGENTS.md"
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace,
        watch_service=WorkspaceFileWatchService(workspace_root=workspace),
    )
    subscription = registry.subscribe_observation(label="duplicate-registration")
    read_count = 0
    read = registry._reconciler._reader.read

    def counted_read(handle):
        nonlocal read_count
        read_count += 1
        return read(handle)

    monkeypatch.setattr(registry._reconciler._reader, "read", counted_read)
    initial = registry.register_file(uri=uri, path=source)
    assert initial.content == "v1\n"
    assert read_count == 1
    first_observations = subscription.pending()
    assert len(first_observations) == 1
    assert first_observations[0].revision == initial.revision

    def forbid(*_args, **_kwargs):
        raise AssertionError("已发布快照的重复注册不得访问文件或重新发布")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", forbid)
        patch.setattr(Path, "absolute", forbid)
        patch.setattr(Path, "is_file", forbid)
        patch.setattr(Path, "exists", forbid)
        patch.setattr(registry._reconciler, "reconcile", forbid)
        patch.setattr(registry._reconciler._reader, "read", forbid)
        patch.setattr(registry, "_publish_observation", forbid)
        duplicate = registry.register_file(uri=uri, path=source)
    assert duplicate is initial
    assert read_count == 1
    assert subscription.pending() == ()

    source.write_text("v2\n", encoding="utf-8")
    refreshed = registry.refresh(uri)
    assert refreshed.content == "v2\n"
    assert refreshed is not initial
    assert read_count == 2
    refresh_observations = subscription.pending()
    assert len(refresh_observations) == 1
    assert refresh_observations[0].revision == refreshed.revision

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", forbid)
        patch.setattr(Path, "absolute", forbid)
        patch.setattr(Path, "is_file", forbid)
        patch.setattr(Path, "exists", forbid)
        patch.setattr(registry._reconciler, "reconcile", forbid)
        patch.setattr(registry._reconciler._reader, "read", forbid)
        patch.setattr(registry, "_publish_observation", forbid)
        latest_duplicate = registry.register_file(uri=uri, path=source)
    assert latest_duplicate is refreshed
    assert read_count == 2
    assert subscription.pending() == ()
    registry.unsubscribe_observation(subscription)


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


def test_duplicate_optional_absence_reuses_snapshot_and_keeps_handle_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uri = "boxteam://workspace/test/resources/agent-spec/root/AGENTS.md"
    path = tmp_path / "AGENTS.md"
    registry = WorkspaceFileResourceRegistry(
        workspace_root=tmp_path,
        watch_service=WorkspaceFileWatchService(workspace_root=tmp_path),
    )
    absent = registry.register_file(uri=uri, path=path, optional=True)
    assert absent.available and not absent.exists and absent.revision

    def forbid(*_args, **_kwargs):
        raise AssertionError("已发布 optional absence 不得重新读取")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", forbid)
        patch.setattr(Path, "absolute", forbid)
        patch.setattr(Path, "is_file", forbid)
        patch.setattr(Path, "exists", forbid)
        patch.setattr(registry._reconciler, "reconcile", forbid)
        patch.setattr(registry._reconciler._reader, "read", forbid)
        duplicate = registry.register_file(uri=uri, path=path, optional=True)
        assert duplicate is absent
        with pytest.raises(ValueError, match="来源 handle 绑定冲突"):
            registry.register_file(uri=uri, path=path, optional=False)

    path.write_text("actual\n", encoding="utf-8")
    refreshed = registry.refresh(uri)
    assert refreshed.available and refreshed.exists and refreshed.content == "actual\n"
    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", forbid)
        patch.setattr(Path, "absolute", forbid)
        patch.setattr(Path, "is_file", forbid)
        patch.setattr(Path, "exists", forbid)
        patch.setattr(registry._reconciler, "reconcile", forbid)
        patch.setattr(registry._reconciler._reader, "read", forbid)
        duplicate_after_refresh = registry.register_file(uri=uri, path=path, optional=True)
    assert duplicate_after_refresh is refreshed


def test_required_missing_source_does_not_publish_optional_absence(tmp_path: Path) -> None:
    uri = "boxteam://workspace/test/resources/skills/demo/SKILL.md"
    registry = WorkspaceFileResourceRegistry(
        workspace_root=tmp_path, watch_service=WorkspaceFileWatchService(workspace_root=tmp_path),
    )
    snapshot = registry.register_file(uri=uri, path=tmp_path / "SKILL.md")
    assert not snapshot.available and not snapshot.exists and snapshot.revision is None
    assert "不存在" in snapshot.error

    source = tmp_path / "SKILL.md"
    source.write_text("available after retry\n", encoding="utf-8")
    retried = registry.register_file(uri=uri, path=source)
    assert retried.available and retried.exists
    assert retried.content == "available after retry\n"


def test_register_file_rejects_uri_bound_to_a_different_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_path = workspace / "first.md"
    second_path = workspace / "second.md"
    first_path.write_text("first\n", encoding="utf-8")
    second_path.write_text("second\n", encoding="utf-8")
    uri = "boxteam://workspace/test/resources/skills/demo/SKILL.md"
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace,
        watch_service=WorkspaceFileWatchService(workspace_root=workspace),
    )
    first = registry.register_file(uri=uri, path=first_path)

    with pytest.raises(ValueError, match="资源 URI 绑定冲突"):
        registry.register_file(uri=uri, path=second_path)

    assert registry.snapshot(uri) is first
