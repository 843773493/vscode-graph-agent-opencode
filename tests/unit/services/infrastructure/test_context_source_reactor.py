"""ContextSourceReactor 的事件驱动接线与生命周期释放单元测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservationChannel,
)
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
    _revision,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)

SKILL_URI = "boxteam://workspace/skill/demo"
SKILL_VIRTUAL_PATH = "/.boxteam/skills/demo/SKILL.md"


def _write_skill(workspace: Path, content: str) -> Path:
    skill_path = workspace / ".boxteam" / "skills" / "demo" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(content, encoding="utf-8")
    return skill_path


def _reactor_fixture(
    tmp_path: Path,
) -> tuple[
    ContextSourceManager,
    WorkspaceFileResourceRegistry,
    WorkspaceFileWatchService,
    ContextSourceReactor,
    LifetimeScope,
]:
    workspace = tmp_path / "workspace"
    _write_skill(workspace, "v1\n")
    watch_service = WorkspaceFileWatchService(workspace_root=workspace)
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace,
        watch_service=watch_service,
        project_root=tmp_path,
    )
    skill_path = _write_skill(workspace, "v1\n")
    registry.register_file(
        uri=SKILL_URI,
        path=skill_path,
    )
    manager = ContextSourceManager()
    manager.register(
        ContextSourceDescriptor(
            source_id="skill:demo",
            source_kind="skill",
            name="demo",
            description="事件驱动测试 Skill",
            internal_locator=SKILL_VIRTUAL_PATH,
            resource_uri=SKILL_URI,
        )
    )
    lifetime_scope = LifetimeScope("test-agent")
    reactor = ContextSourceReactor(
        sources=registry,
        context_sources=manager,
        lifetime_scope=lifetime_scope,
        reactor_id="test",
    )
    return manager, registry, watch_service, reactor, lifetime_scope


def _install_snapshot(
    manager: ContextSourceManager,
    name: str,
    display_uri: str,
    content: str,
) -> None:
    """按正式 typed API 安装与 registered source 一致的冻结 binding。"""
    manager.install_skill_activation_snapshot(
        SkillCatalogActivationSnapshot(
            catalog_revision="sha256:test-catalog",
            entries={
                name: SkillCatalogBinding(
                    name=name,
                    resource_id=f"skill-entry:test:{name}:activation",
                    entry_identity=f"skill-entry:test:{name}",
                    display_uri=display_uri,
                    activation_revision=_revision(content),
                    body=content,
                )
            },
        )
    )


@pytest.mark.asyncio
async def test_reactor_marks_pending_observation_from_registry_event(
    tmp_path: Path,
) -> None:
    manager, registry, watch_service, reactor, lifetime_scope = _reactor_fixture(tmp_path)
    try:
        # 先按 Skill 既有路径激活并进入 tracked 状态，事件接线只负责后续变化。
        registration = registry.snapshot(SKILL_URI)
        assert registration.revision is not None
        manager.activate_skill_content("demo", registration.content)
        _install_snapshot(manager, "demo", SKILL_URI, registration.content)
        assert manager.load_skill("demo", mode="snapshot").tracked is False
        assert manager.load_skill("demo", mode="tracked").tracked is True

        # 首次接线建立订阅；已观察 revision 不产生多余 pending。
        assert reactor.sync_sources() == (SKILL_URI,)
        assert reactor.subscribed_uri_count == 1
        assert manager.pending_observation_count() == 0

        # 来源变化：watcher 侧定点刷新发布轻量 change 通知，reactor 只登记
        # 「有新 revision」，不读取正文、不做 I/O。
        _write_skill(tmp_path / "workspace", "v2\n")
        changed = registry.refresh(SKILL_URI)
        assert changed.revision != registration.revision

        # 事件回调路径只登记待观察标记，不读取正文。
        assert reactor.ingest_notifications() == 1
        assert manager.pending_observation_count() == 1
        # 没有新 revision 时重复对账不会重复排队。
        reactor.sync_sources()
        assert manager.pending_observation_count() == 1

        observations = tuple(reactor.drain())
        assert [item.source_id for item in observations] == ["skill:demo"]
        assert observations[0].revision == changed.revision
        # 正文只在消费时从 registry 的权威内存快照读取。
        assert reactor.content_for(observations[0]) == "v2\n"
        assert manager.pending_observation_count() == 0
        assert lifetime_scope.snapshot().resource_count == 1
    finally:
        await watch_service.shutdown()


@pytest.mark.asyncio
async def test_reactor_release_stops_delivery_after_scope_close(tmp_path: Path) -> None:
    manager, registry, watch_service, reactor, lifetime_scope = _reactor_fixture(tmp_path)
    try:
        registration = registry.snapshot(SKILL_URI)
        assert registration.revision is not None
        manager.activate_skill_content("demo", registration.content)
        _install_snapshot(manager, "demo", SKILL_URI, registration.content)
        manager.load_skill("demo", mode="tracked")
        reactor.sync_sources()
        assert registry.observation_channel.subscriber_ids

        await lifetime_scope.close()
        assert reactor.released is True
        assert reactor.subscribed_uri_count == 0
        assert registry.observation_channel.subscriber_ids == ()
        # 释放后不允许再绑定新来源。
        with pytest.raises(RuntimeError, match="已释放"):
            reactor.sync_sources()

        # 释放之后来源变化不再到达 CSM，也不会被消费。
        _write_skill(tmp_path / "workspace", "v2\n")
        changed = registry.refresh(SKILL_URI)
        assert changed.revision != registration.revision
        assert reactor.ingest_notifications() == 0
        assert tuple(reactor.drain()) == ()
        assert manager.pending_observation_count() == 0
    finally:
        await watch_service.shutdown()


@pytest.mark.asyncio
async def test_watcher_event_reaches_csm_pending_observation(tmp_path: Path) -> None:
    """真实 watchfiles 事件 → registry 快照更新 → CSM pending observation。"""
    manager, registry, watch_service, reactor, lifetime_scope = _reactor_fixture(tmp_path)
    workspace = tmp_path / "workspace"
    try:
        registration = registry.snapshot(SKILL_URI)
        assert registration.revision is not None
        manager.activate_skill_content("demo", registration.content)
        _install_snapshot(manager, "demo", SKILL_URI, registration.content)
        manager.load_skill("demo", mode="tracked")
        reactor.sync_sources()

        await registry.start()
        try:
            skill_path = _write_skill(workspace, "v2\n")
            updated = None
            for _ in range(150):
                if registry.snapshot(SKILL_URI).content == "v2\n":
                    updated = registry.snapshot(SKILL_URI)
                    break
                await asyncio.sleep(0.02)
            assert updated is not None, f"watcher 未刷新 {skill_path}"
            assert updated.revision != registration.revision

            # 事件已进入 reactor；回调只登记 pending，不读取正文。
            for _ in range(50):
                if reactor.ingest_notifications():
                    break
                await asyncio.sleep(0.02)
            assert manager.pending_observation_count() == 1

            observations = tuple(reactor.drain())
            assert [item.revision for item in observations] == [updated.revision]
            assert reactor.content_for(observations[0]) == "v2\n"
        finally:
            await registry.stop()
    finally:
        await lifetime_scope.close()
        await watch_service.shutdown()


def _overflow_fixture(
    tmp_path: Path,
    *,
    skill_count: int,
    max_queue_size: int,
) -> tuple[
    ContextSourceManager,
    WorkspaceFileResourceRegistry,
    WorkspaceFileWatchService,
    ContextSourceReactor,
    LifetimeScope,
    list[str],
]:
    """建立多个 tracked Skill 来源，并把通道队列压到可溢出的上限。"""
    workspace = tmp_path / "workspace"
    watch_service = WorkspaceFileWatchService(workspace_root=workspace)
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace,
        watch_service=watch_service,
        project_root=tmp_path,
        observation_channel=ResourceObservationChannel(max_queue_size=max_queue_size),
    )
    manager = ContextSourceManager()
    uris: list[str] = []
    for index in range(skill_count):
        name = f"demo{index}"
        virtual_path = f"/.boxteam/skills/{name}/SKILL.md"
        skill_path = workspace / ".boxteam" / "skills" / name / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(f"v{index}\n", encoding="utf-8")
        uri = f"boxteam://workspace/skill/{name}"
        registry.register_file(uri=uri, path=skill_path)
        manager.register(
            ContextSourceDescriptor(
                source_id=f"skill:{name}",
                source_kind="skill",
                name=name,
                description=f"溢出测试 Skill {name}",
                internal_locator=virtual_path,
                resource_uri=uri,
            )
        )
        snapshot = registry.snapshot(uri)
        manager.activate_skill_content(name, snapshot.content)
        _install_snapshot(manager, name, uri, snapshot.content)
        manager.load_skill(name, mode="tracked")
        uris.append(uri)
    lifetime_scope = LifetimeScope("test-agent")
    reactor = ContextSourceReactor(
        sources=registry,
        context_sources=manager,
        lifetime_scope=lifetime_scope,
        reactor_id="test-overflow",
    )
    return manager, registry, watch_service, reactor, lifetime_scope, uris


@pytest.mark.asyncio
async def test_channel_gap_marks_all_sources_and_recovers(tmp_path: Path) -> None:
    """M1 回归：一次溢出不得让 thread 上下文永久静默陈旧。

    审查复现场景：某个来源的连续通知塞满订阅者队列后，旧实现把 gap 标记置位
    却永不复位，此后所有事件被静默丢弃。修复后必须满足：
    ①溢出时全部已绑定来源被标记待观察；②消费后可继续观察；③后续事件恢复投递。
    """
    skill_count = 5
    manager, registry, watch_service, reactor, lifetime_scope, uris = _overflow_fixture(
        tmp_path,
        skill_count=skill_count,
        max_queue_size=1,
    )
    try:
        # 队列容量为 1，绑定后为每个来源写入新内容再定点刷新 → 覆盖全部来源的溢出。
        assert len(reactor.sync_sources()) == skill_count
        subscription = reactor._subscription
        assert subscription is not None

        for index in range(skill_count):
            skill_path = (
                tmp_path / "workspace" / ".boxteam" / "skills" / f"demo{index}" / "SKILL.md"
            )
            skill_path.write_text(f"changed-{index}\n", encoding="utf-8")
            registry.refresh(uris[index])

        # ①溢出：reactor 消费 gap 后必须标记全部已绑定来源（可能还叠加一条
        # 正常通知的定点标记），而不是只标记 gap 事件自带的单一来源。
        marked = reactor.ingest_notifications()
        assert marked >= skill_count
        assert manager.pending_observation_count() == skill_count

        observed = tuple(reactor.drain())
        assert sorted(item.source_id for item in observed) == sorted(
            f"skill:demo{index}" for index in range(skill_count)
        )
        assert manager.pending_observation_count() == 0

        # ③后续事件恢复投递：gap 被消费之后，新的变化必须正常进入 CSM，
        # 而不是被永久静默丢弃（旧实现在这里会永久返回 0）。
        (
            tmp_path / "workspace" / ".boxteam" / "skills" / "demo0" / "SKILL.md"
        ).write_text("after-gap-0\n", encoding="utf-8")
        staged = registry.refresh(uris[0])
        marked_after = reactor.ingest_notifications()
        assert marked_after >= 1
        observed_after = tuple(reactor.drain())
        assert any(
            item.source_id == "skill:demo0" and item.revision == staged.revision
            for item in observed_after
        )
    finally:
        await lifetime_scope.close()
        await watch_service.shutdown()
