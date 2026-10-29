"""固定 Skill 目录失败不得被诊断日志的次错覆盖。"""

from __future__ import annotations

import asyncio
import errno
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

import app.services.infrastructure.workspace_file_watch_service as watch_module
from app.services.infrastructure.resource_platform.sources.observed_source import (
    StableSourceReadError,
)
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


@pytest.mark.parametrize("scope", ["workspace", "gateway"])
def test_native_symlink_loop_does_not_publish_filesystem_location(
    tmp_path: Path, caplog, diagnostic_sink, scope: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = workspace / ".boxteam" if scope == "workspace" else tmp_path / "gateway-home"
    parent.symlink_to(parent.name, target_is_directory=True)
    root = parent / "skills"
    scope_id = "test" if scope == "workspace" else "gateway_0123456789abcdef0123456789abcdef"
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace if scope == "workspace" else None,
        gateway_skills_root=root if scope == "gateway" else None,
        watch_service=WorkspaceFileWatchService(workspace_root=workspace), project_root=tmp_path,
    )
    rejected = registry.register_skill_directory(root=root, scope=scope, scope_id=scope_id)
    assert not rejected.available and rejected.error is not None
    assert str(parent) not in rejected.error
    originals = [record.exc_info[1] for record in caplog.records if record.exc_info]
    assert len(originals) == 1 and isinstance(originals[0], RuntimeError)
    assert str(parent) in str(originals[0])
    assert "error=RuntimeError" in rejected.error and "stage=skill-directory-reconcile" in rejected.error
    broken_logging, blocked = diagnostic_sink
    assert str(blocked) not in rejected.error
    if broken_logging:
        assert "diagnostic_sink_error=NotADirectoryError" in rejected.error
        assert str(blocked) in "\n".join(originals[0].__notes__)
    parent.unlink()
    source = root / "demo" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("循环修复后的原来源\n", encoding="utf-8")
    registry.reconcile()
    recovered = registry.skill_directory_snapshot(rejected.directory_id)
    assert recovered.available and recovered.error is None
    assert len(recovered.entries) == 1
    assert recovered.entries[0].uri == f"boxteam://{scope}/{scope_id}/resources/skills/demo/SKILL.md"
    assert registry.snapshot(recovered.entries[0].uri).content == "循环修复后的原来源\n"


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
@pytest.mark.parametrize("origin", ["resolve", "enumerate", "bind"])
def test_unmarked_failures_keep_original_diagnostics_without_public_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog, diagnostic_sink,
    error_type: type[RuntimeError | ValueError], origin: str,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / ".boxteam" / "skills" / "demo" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("应由原入口重新读取\n", encoding="utf-8")
    root = source.parent.parent
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace, watch_service=WorkspaceFileWatchService(workspace_root=workspace),
        project_root=tmp_path,
    )
    # 同一业务前缀不能使下游异常取得公开原文的许可。
    message = f"Skill 根目录不允许 symlink: {root}; C:\\private\\SKILL.md; \\\\server\\share\\SKILL.md"
    failure = error_type(message)
    args = failure.args
    with monkeypatch.context() as faults:
        original_resolve, original_iterdir = Path.resolve, Path.iterdir

        def resolve(path: Path, *arguments, **options):
            if path == root:
                raise failure
            return original_resolve(path, *arguments, **options)

        def enumerate_root(path: Path):
            if path == root:
                raise failure
            return original_iterdir(path)

        def bind(_handle):
            raise failure

        if origin == "resolve":
            faults.setattr(Path, "resolve", resolve)
        elif origin == "enumerate":
            faults.setattr(Path, "iterdir", enumerate_root)
        else:
            faults.setattr(registry._reconciler, "bind_handle", bind)
        rejected = registry.register_skill_directory(root=root, scope="workspace", scope_id="test")
    assert not rejected.available and rejected.entries == ()
    assert rejected.error is not None
    assert str(root) not in rejected.error and "C:\\private" not in rejected.error
    assert "\\\\server\\share" not in rejected.error and "reason=" not in rejected.error
    assert f"error={error_type.__name__}" in rejected.error
    assert any(record.exc_info and record.exc_info[1] is failure for record in caplog.records)
    assert type(failure) is error_type and failure.args == args and str(failure) == message
    broken_logging, blocked = diagnostic_sink
    assert str(blocked) not in rejected.error
    if broken_logging:
        assert "diagnostic_sink_error=NotADirectoryError" in rejected.error
        assert str(blocked) in "\n".join(failure.__notes__)
    registry.reconcile()
    recovered = registry.skill_directory_snapshot(rejected.directory_id)
    assert recovered.available and recovered.error is None and len(recovered.entries) == 1
    accepted = registry.snapshot(recovered.entries[0].uri)
    assert accepted.available and accepted.content == "应由原入口重新读取\n"


@pytest.mark.parametrize(("case", "reason", "error_type"), [
    ("root-file", "Skill 根路径不是目录: scope=workspace, scope_id=test", RuntimeError),
    ("inline-root-missing", "发行包内置 Skill 根目录不存在: scope=inline, scope_id=test", RuntimeError),
    ("inline-entry-missing", "发行包内置 Skill 组目录不存在: scope=inline, scope_id=test, skill=demo", RuntimeError),
    ("inline-skill-missing", "发行包内置 Skill 组缺少 SKILL.md: scope=inline, scope_id=test, skill=demo", RuntimeError),
    ("entry-symlink", "Skill entry 不允许 symlink: scope=workspace, scope_id=test, skill=demo", RuntimeError),
    ("skill-symlink", "SKILL.md 不允许 symlink: scope=workspace, scope_id=test, skill=demo", RuntimeError),
    ("root-escape", "Skill 固定根越出已知允许根: scope=workspace, scope_id=test", ValueError),
    ("scope-missing", "gateway scope 的 scope_id 需要真实的 gateway_id，当前来源 owner 尚未绑定", RuntimeError),
    ("identity-conflict", "Skill 文件来源 owner identity 绑定冲突: uri=boxteam://workspace/test/resources/skills/demo/SKILL.md", ValueError),
    ("handle-conflict", "来源 handle 绑定冲突: source_id=skill-source:workspace:demo", ValueError),
    ("handle-escape", "来源路径越过允许根目录: uri=boxteam://workspace/test/resources/skills/demo/SKILL.md", StableSourceReadError),
    ("uri-grammar", "URI 结构或身份段不得使用百分号编码", ValueError),
])
def test_owner_rejections_preserve_rules_and_exception_categories(
    tmp_path: Path, diagnostic_sink, caplog, monkeypatch: pytest.MonkeyPatch,
    case: str, reason: str,
    error_type: type[RuntimeError | ValueError],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    distribution = tmp_path / "distribution"
    inline = case.startswith("inline-")
    scope = "inline" if inline else "workspace"
    root = distribution / "resources" / "skills" if inline else workspace / ".boxteam" / "skills"
    root.mkdir(parents=True)
    entry = root / "demo"
    source = entry / "SKILL.md"
    external = tmp_path / "external"
    external.mkdir()
    if case == "root-file":
        root.rmdir()
        root.write_text("不是目录", encoding="utf-8")
    elif case == "inline-root-missing":
        root.rmdir()
    elif case == "inline-entry-missing":
        pass
    elif case == "entry-symlink":
        entry.symlink_to(external, target_is_directory=True)
    elif case == "root-escape":
        root.rmdir()
        root.parent.rmdir()
        root.parent.symlink_to(external, target_is_directory=True)
        root.mkdir()
    else:
        entry.mkdir()
        if case == "skill-symlink":
            target = external / "secret.md"
            target.write_text("不得读取的正文", encoding="utf-8")
            source.symlink_to(target)
        elif case != "inline-skill-missing":
            source.write_text("合法来源正文\n", encoding="utf-8")
    if case == "uri-grammar":
        entry.rename(root / "bad%name")
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace, project_root=distribution,
        watch_service=WorkspaceFileWatchService(workspace_root=workspace),
    )
    if case == "identity-conflict":
        registry.register_file(
            uri="boxteam://workspace/test/resources/skills/demo/SKILL.md", path=source,
            source_id="conflicting-source", entry_identity="conflicting-entry",
        )
    elif case == "handle-conflict":
        registry.register_file(
            uri="boxteam://workspace/test/resources/files/root/alternate.md", path=source,
            source_id="skill-source:workspace:demo", entry_identity="alternate-entry",
        )
    elif case == "handle-escape":
        target = external / "secret.md"
        target.write_text("不得作为原来源发布", encoding="utf-8")
        original_resolve = Path.resolve
        calls = 0

        def replace_before_handle_validation(path: Path, *arguments, **options):
            nonlocal calls
            if path == source:
                calls += 1
                if calls == 2:
                    source.unlink()
                    source.symlink_to(target)
            return original_resolve(path, *arguments, **options)

        monkeypatch.setattr(Path, "resolve", replace_before_handle_validation)
    rejected = registry.register_skill_directory(
        root=root, scope=scope, scope_id=None if case == "scope-missing" else "test",
        declared_entries=("demo",) if inline else None,
    )
    assert not rejected.available and rejected.error is not None
    assert reason in rejected.error
    assert str(workspace) not in rejected.error and str(distribution) not in rejected.error
    assert str(external) not in rejected.error
    originals = [record.exc_info[1] for record in caplog.records if record.exc_info]
    assert len(originals) == 1 and isinstance(originals[0], error_type)
    if case != "uri-grammar":
        assert type(originals[0]) is error_type
    assert str(originals[0]) == reason
    broken_logging, blocked = diagnostic_sink
    assert str(blocked) not in rejected.error
    if broken_logging:
        assert "diagnostic_sink_error=NotADirectoryError" in rejected.error
        assert str(blocked) in "\n".join(originals[0].__notes__)
