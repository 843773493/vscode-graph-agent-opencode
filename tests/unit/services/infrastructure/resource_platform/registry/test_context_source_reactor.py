"""ContextSourceReactor 的事件驱动接线与生命周期释放单元测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from app.core.lifecycle import LifetimeScope
from app.domain.itemized.epoch.prefix_epoch import (
    EpochReason,
    PendingPrefixEpochTransition,
)
from app.services.infrastructure import workspace_file_watch_service as watch_module
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservation,
    ResourceObservationChannel,
)
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
    WorkspaceFileResourceSnapshot,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
    ContextSourceRebuildState,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
    _revision,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileChange,
    WorkspaceFileChangeBatch,
    WorkspaceFileWatchService,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 正式 SQLite fixture 按当前测试路径隔离
)
from tests.unit.services.infrastructure.rollout_context.conftest import (
    SourceSealCase,
)
from tests.unit.services.infrastructure.rollout_context.conftest import (
    source_seal_case as source_seal_case,  # noqa: PLC0414 - 复用真实 Saver seal fixture
)

SKILL_URI = "boxteam://workspace/test/resources/skills/demo/SKILL.md"
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
        source_id="skill-source:workspace:demo",
        entry_identity="skill-entry:workspace:demo",
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
        _install_snapshot(manager, "demo", SKILL_URI, registration.content)
        assert manager.load_skill("demo", mode="snapshot").tracked is False
        assert manager.load_skill("demo", mode="tracked").tracked is True

        # 首次接线必须对账；消费后没有通知时不再重复排观察。
        assert reactor.sync_sources() == (SKILL_URI,)
        assert reactor.subscribed_uri_count == 1
        assert manager.pending_observation_count() == 1
        initial = tuple(reactor.drain())
        assert len(initial) == 1 and initial[0].revision == registration.revision
        assert reactor.content_for(initial[0]) == registration.content
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
        _install_snapshot(manager, "demo", SKILL_URI, registration.content)
        manager.load_skill("demo", mode="tracked")
        reactor.sync_sources()
        assert registry.observation_channel.subscriber_ids
        assert len(tuple(reactor.drain())) == 1
        assert manager.pending_observation_count() == 0

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
async def test_failed_unsubscribe_stays_owned_and_fenced_until_scope_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, registry, watch_service, reactor, scope = _reactor_fixture(tmp_path)
    unsubscribe = registry.unsubscribe_observation
    attempts = 0

    def fail_once(subscription):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("真实订阅首次释放失败")
        return unsubscribe(subscription)

    monkeypatch.setattr(registry, "unsubscribe_observation", fail_once)
    try:
        reactor.sync_sources()
        subscription = reactor._subscription
        assert subscription is not None
        initial_pending = manager.pending_observation_count()
        with pytest.raises(ExceptionGroup, match="LifetimeScope close"):
            await scope.close()
        assert scope.state == "close_failed" and reactor.released
        assert reactor._subscription is subscription
        assert registry.observation_channel.subscriber_ids
        assert reactor.ingest_notifications() == 0
        assert manager.pending_observation_count() == initial_pending
        await scope.close()
        assert attempts == 2 and scope.state == "closed"
        assert reactor._subscription is None
        assert registry.observation_channel.subscriber_ids == ()
    finally:
        await scope.close()
        await watch_service.shutdown()


@pytest.mark.asyncio
async def test_watcher_event_reaches_csm_pending_observation(tmp_path: Path) -> None:
    """真实 watchfiles 事件 → registry 快照更新 → CSM pending observation。"""
    manager, registry, watch_service, reactor, lifetime_scope = _reactor_fixture(tmp_path)
    workspace = tmp_path / "workspace"
    try:
        registration = registry.snapshot(SKILL_URI)
        assert registration.revision is not None
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
        uri = f"boxteam://workspace/test/resources/skills/{name}/SKILL.md"
        registry.register_file(
            uri=uri,
            path=skill_path,
            source_id=f"skill-source:workspace:{name}",
            entry_identity=f"skill-entry:workspace:{name}",
        )
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


@pytest.fixture
async def raw_overflow_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[
    ContextSourceManager, WorkspaceFileResourceRegistry, WorkspaceFileWatchService,
    ContextSourceReactor, list[str], asyncio.Event,
]]:
    async def quiet_awatch(_root: Path, **_options: object):
        # 原生事件源在本例保持静止，防止真实 OS 通知替漏掉 overflow 的错误实现补救。
        yield set()
        await asyncio.Event().wait()

    monkeypatch.setattr(watch_module, "awatch", quiet_awatch)
    monkeypatch.setattr(watch_module, "FILE_WATCH_QUEUE_SIZE", 1)
    (tmp_path / "workspace").mkdir()
    manager, registry, watcher, reactor, scope, uris = _overflow_fixture(
        tmp_path, skill_count=3, max_queue_size=1,
    )
    overflow_seen = asyncio.Event()
    next_event = registry._next_watch_event

    async def record_overflow():
        batch = await next_event()
        if batch is not None and batch.overflow:
            overflow_seen.set()
        return batch

    monkeypatch.setattr(registry, "_next_watch_event", record_overflow)
    await registry.start()
    try:
        yield manager, registry, watcher, reactor, uris, overflow_seen
    finally:
        await scope.close()
        await registry.stop()
        await watcher.shutdown()


@pytest.mark.asyncio
async def test_raw_watcher_overflow_reconciles_registered_sources_through_csm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_overflow_case,
) -> None:
    manager, registry, watcher, reactor, uris, overflow_seen = raw_overflow_case
    assert len(reactor.sync_sources()) == 3
    assert len(tuple(reactor.drain())) == 3
    before = {uri: registry.snapshot(uri).revision for uri in uris}
    shared = next(iter(watcher._watchers.values()))
    path = tmp_path / "workspace" / ".boxteam" / "skills" / "demo0" / "SKILL.md"
    batch = WorkspaceFileChangeBatch(changes=(WorkspaceFileChange("edit", str(path)),))
    for index in range(3):
        skill = tmp_path / "workspace" / ".boxteam" / "skills" / f"demo{index}" / "SKILL.md"
        skill.write_text(f"overflow-{index}\n", encoding="utf-8")
    watcher._publish(shared.subscribers, batch)
    watcher._publish(shared.subscribers, batch)
    async with asyncio.timeout(5):
        await overflow_seen.wait()
        while any(registry.snapshot(uri).revision == before[uri] for uri in uris):
            await asyncio.sleep(0)

    observed_events: list[ResourceObservation] = []
    handle_event = reactor._handle_event

    def record_event(event: ResourceObservation) -> int:
        observed_events.append(event)
        return handle_event(event)

    monkeypatch.setattr(reactor, "_handle_event", record_event)
    assert reactor.ingest_notifications() >= 3
    assert any(event.gap for event in observed_events)
    assert manager.pending_observation_count() == 3
    observed = tuple(reactor.drain())
    assert {item.source_id: reactor.content_for(item) for item in observed} == {
        f"skill:demo{index}": f"overflow-{index}\n" for index in range(3)
    }
    assert {item.revision for item in observed} == {registry.snapshot(uri).revision for uri in uris}
    assert manager.pending_observation_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("directory_gap", [False, True])
async def test_channel_gap_marks_all_sources_and_recovers(tmp_path: Path, directory_gap: bool) -> None:
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
        assert len(tuple(reactor.drain())) == skill_count
        assert manager.pending_observation_count() == 0

        for index in range(skill_count):
            skill_path = (
                tmp_path / "workspace" / ".boxteam" / "skills" / f"demo{index}" / "SKILL.md"
            )
            skill_path.write_text(f"changed-{index}\n", encoding="utf-8")
            registry.refresh(uris[index])
            if directory_gap and index == 0:
                # gap 即使由目录通知承载，也不能漏掉普通文件的已登记消费者。
                registry.observation_channel.notify(ResourceObservation(
                    uri=None, directory_id="skill-directory:" + "0" * 64,
                    revision=None, available=True,
                ))

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


@pytest.mark.asyncio
async def test_optional_file_reactor_and_rebuild_consume_only_published_revisions(
    tmp_path: Path, registry_db, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺失、空文件、正文与删除都先由原 reader 发布；CSM 消费与重建不读源。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "GUIDE.md"
    uri = "boxteam://workspace/test/resources/files/root/GUIDE.md"
    watch = WorkspaceFileWatchService(workspace_root=workspace)
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace, watch_service=watch, project_root=tmp_path,
    )
    saver, session, _connection, _accepted = registry_db
    owner = ContextSourceOwnerKey(session, saver._storage.storage.main_thread_id(session))
    manager = ContextSourceManager(owner=owner, control_state_port=saver)
    manager.register(
        ContextSourceDescriptor(
            source_id="workspace-guide", source_kind="workspace_instructions",
            name="GUIDE.md", description="可选工作区指引", internal_locator="GUIDE.md",
            resource_uri=uri,
        ), tracking_status="tracked",
    )
    scope = LifetimeScope("optional-guide-rebuild")
    reactor = ContextSourceReactor(
        sources=registry, context_sources=manager, lifetime_scope=scope,
        reactor_id="optional-guide",
    )
    manager.bind_published_revision_supplier(reactor.freeze_published_sources)
    revisions = []

    def forbid_source_io(*_args, **_kwargs):
        raise AssertionError("reactor/CSM rebuild 只能消费原 owner 的已发布内存 revision")

    try:
        for index, body in enumerate((None, "", "实际 指引 正文\n", None)):
            if body is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(body, encoding="utf-8")
            published = (
                registry.register_file(
                    uri=uri,
                    path=path,
                    source_id="workspace-guide-source:optional",
                    entry_identity="workspace-guide-entry:optional",
                    optional=True,
                )
                if index == 0 else registry.refresh(uri)
            )
            assert published.available and published.revision
            assert published.exists is (body is not None)
            assert published.content == (body or "")
            revisions.append(published.revision)
            with monkeypatch.context() as patch:
                patch.setattr(registry._reconciler._reader, "read", forbid_source_io)
                patch.setattr(registry._reconciler, "reconcile", forbid_source_io)
                patch.setattr(registry, "refresh", forbid_source_io)
                reactor.sync_sources()
                observed = tuple(reactor.drain())
                assert len(observed) == 1 and observed[0].revision == published.revision
                assert reactor.content_for(observed[0]) == published.content
                assert manager.observe(
                    observed[0].source_id, reactor.content_for(observed[0]),
                    revision=observed[0].revision,
                ).status == "accepted"
                assert manager.source_observation_state("workspace-guide") == (
                    "tracked", published.revision,
                )
                frozen = reactor.freeze_published_sources(manager.descriptors())
                assert [(item.revision, item.content) for item in frozen] == [
                    (published.revision, published.content),
                ]
                rebuild = ContextSourceRebuildState(
                    transition=PendingPrefixEpochTransition(
                        transition_id=f"optional-rebuild-{index}", reason=EpochReason.REWIND,
                    ), states=saver.load_context_source_control_states(owner),
                )
                batch = manager.prepare_rebuild_sources(rebuild)
                assert [(item.revision, item.content) for item in batch.sources] == [
                    (published.revision, published.content),
                ]
                assert len(batch.deltas) == 1
                assert batch.deltas[0].kind == "rebuild"
                assert batch.deltas[0].revision == published.revision
        assert revisions[0] == revisions[3]
        assert len(set(revisions[:3])) == 3
    finally:
        await scope.close()
        await watch.shutdown()


@dataclass(frozen=True)
class ColdFileSourceCase:
    manager: ContextSourceManager
    registry: WorkspaceFileResourceRegistry
    reactor: ContextSourceReactor
    published: WorkspaceFileResourceSnapshot
    baseline_revision: str
    seal_case: SourceSealCase


@pytest.fixture
async def cold_file_source_case(
    request: pytest.FixtureRequest, tmp_path: Path, source_seal_case: SourceSealCase,
) -> AsyncIterator[ColdFileSourceCase]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "GUIDE.md"
    path.write_text("冷启动前的 指引 正文\n", encoding="utf-8")
    descriptor = ContextSourceDescriptor(
        source_id="file:workspace-guide", source_kind="workspace_guide", name="GUIDE.md",
        description="冷启动指引", internal_locator="GUIDE.md",
        resource_uri="boxteam://workspace/test/resources/files/root/GUIDE.md",
        root_placement="root_eligible",
    )
    watch = WorkspaceFileWatchService(workspace_root=workspace)
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace, watch_service=watch, project_root=tmp_path,
    )
    baseline = registry.register_file(
        uri=descriptor.resource_uri,
        path=path,
        source_id="workspace-guide-source:cold-rebuild",
        entry_identity="workspace-guide-entry:cold-rebuild",
        optional=True,
    )
    assert baseline.available and baseline.revision
    manager = source_seal_case.manager()
    manager.register(descriptor, tracking_status="tracked")
    scope = LifetimeScope("guide-before-restart")
    reactor = ContextSourceReactor(
        sources=registry, context_sources=manager, lifetime_scope=scope, reactor_id="guide-before",
    )
    try:
        reactor.sync_sources()
        for observation in reactor.drain():
            assert manager.observe(
                observation.source_id, reactor.content_for(observation), revision=observation.revision,
            ).status == "accepted"
        _receipt, binding = source_seal_case.seal(manager)
        assert [(item.source_id, item.previous_revision, item.revision) for item in binding.source_revisions] == [
            (descriptor.source_id, None, baseline.revision),
        ]
    finally:
        await scope.close()
        await watch.shutdown()

    variant = request.param
    if variant == "changed":
        path.write_text("冷启动后的 指引 新正文\n", encoding="utf-8")
    elif variant in {"absent", "required_unavailable"}:
        path.unlink()
    watch = WorkspaceFileWatchService(workspace_root=workspace)
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace, watch_service=watch, project_root=tmp_path,
    )
    # 注册发布发生在新订阅之前，不能靠通知重放取得这次停机变化。
    published = registry.register_file(
        uri=descriptor.resource_uri,
        path=path,
        source_id="workspace-guide-source:cold-rebuild",
        entry_identity="workspace-guide-entry:cold-rebuild",
        optional=variant != "required_unavailable",
    )
    manager = source_seal_case.manager()
    manager.register(descriptor, tracking_status="tracked")
    assert manager.source_observation_state(descriptor.source_id) == ("tracked", baseline.revision)
    assert manager.prepare_sources().sources == ()
    scope = LifetimeScope("guide-after-restart")
    reactor = ContextSourceReactor(
        sources=registry, context_sources=manager, lifetime_scope=scope, reactor_id="guide-after",
    )
    try:
        yield ColdFileSourceCase(manager, registry, reactor, published, baseline.revision, source_seal_case)
    finally:
        await scope.close()
        await watch.shutdown()


@pytest.mark.parametrize("cold_file_source_case", ["unchanged", "changed", "absent"], indirect=True)
@pytest.mark.asyncio
async def test_cold_file_first_binding_reconciles_published_snapshot(
    cold_file_source_case: ColdFileSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = cold_file_source_case
    source_id = "file:workspace-guide"
    published = case.published
    assert published.available and published.revision
    before = case.seal_case.saver.load_context_source_control_states(case.seal_case.owner)

    def forbid_source_io(*_args, **_kwargs):
        raise AssertionError("首绑只排 reconcile；消费与 prepare 只能使用已发布内存 snapshot")

    with monkeypatch.context() as patch:
        patch.setattr(case.registry, "snapshot", forbid_source_io)
        assert case.reactor.sync_sources() == (published.uri,)
        assert case.reactor.sync_sources() == ()
        assert case.manager.pending_observation_count() == 1
    with monkeypatch.context() as patch:
        patch.setattr(case.registry._reconciler._reader, "read", forbid_source_io)
        patch.setattr(case.registry._reconciler, "reconcile", forbid_source_io)
        patch.setattr(case.registry, "refresh", forbid_source_io)
        observed = tuple(case.reactor.drain())
        assert len(observed) == 1 and observed[0].revision == published.revision
        result = case.manager.observe(
            source_id, case.reactor.content_for(observed[0]), revision=observed[0].revision,
        )
        batch = case.manager.prepare_sources()
        assert [(source.revision, source.content, source.applied_revision) for source in batch.sources] == [
            (published.revision, published.content, case.baseline_revision),
        ]
        pending_control = case.seal_case.saver.load_context_source_control_states(case.seal_case.owner)
        assert pending_control[0].latest_visible_committed_revision == case.baseline_revision
        assert pending_control[0].latest_revision == published.revision
        if published.revision == case.baseline_revision:
            assert result.status == "baseline_restored" and not result.pending
            assert batch.deltas == ()
            assert case.manager.prepare_pending() is None
            assert pending_control == before
        else:
            assert result.status == "accepted" and result.pending
            assert len(batch.deltas) == 1
            delta = batch.deltas[0]
            assert (delta.kind, delta.previous_revision, delta.revision, delta.content) == (
                "delta", case.baseline_revision, published.revision, published.content,
            )
            receipt, binding = case.seal_case.seal(case.manager)
            assert receipt.deltas == batch.deltas
            assert [(item.previous_revision, item.revision) for item in binding.source_revisions] == [
                (case.baseline_revision, published.revision),
            ]
            committed = case.seal_case.saver.load_context_source_control_states(case.seal_case.owner)
            assert committed[0].latest_visible_committed_revision == committed[0].latest_revision == published.revision
            assert case.manager.prepare_pending() is None
        case.reactor.sync_sources()
        assert case.manager.pending_observation_count() == 0
        assert tuple(case.reactor.drain()) == ()


@pytest.mark.parametrize("cold_file_source_case", ["required_unavailable", "missing_revision"], indirect=True)
@pytest.mark.asyncio
async def test_first_binding_rejects_unusable_published_snapshot(
    cold_file_source_case: ColdFileSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = cold_file_source_case
    before = case.seal_case.saver.load_context_source_control_states(case.seal_case.owner)
    if case.published.available:
        snapshot = replace(case.published, revision=None)
        monkeypatch.setattr(case.registry, "snapshot", lambda _uri: snapshot)
        expected_error = "来源快照缺少 revision"
    else:
        assert case.published.revision is None
        expected_error = "来源快照不可用"
    assert case.reactor.sync_sources() == (case.published.uri,)
    assert case.manager.pending_observation_count() == 1
    with pytest.raises(RuntimeError, match=expected_error) as failure:
        tuple(case.reactor.drain())
    assert "source_id=file:workspace-guide" in str(failure.value)
    assert case.published.uri in str(failure.value)
    assert case.manager.prepare_pending() is None
    assert case.seal_case.saver.load_context_source_control_states(case.seal_case.owner) == before
