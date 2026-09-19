"""ContextSourceManager 待观察（pending observation）标记的直接单元测试。"""

from __future__ import annotations

import pytest

from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
    PendingSourceObservation,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
)

DEMO_CONTENT = "# Demo\n\nv1\n"
OTHER_CONTENT = "# Other\n\nv1\n"


def _manager(*names: str) -> ContextSourceManager:
    manager = ContextSourceManager()
    for name in names:
        manager.register(
            ContextSourceDescriptor(
                source_id=f"skill:{name}",
                source_kind="skill",
                name=name,
                description=f"{name} Skill",
                internal_locator=f"/.boxteam/skills/{name}/SKILL.md",
                resource_uri=f"boxteam://workspace/skill/{name}",
            )
        )
    return manager


def _install_snapshot(manager: ContextSourceManager, name: str, body: str) -> None:
    """安装与手工 activation revision 一致的冻结 snapshot(测试辅助)。"""
    binding = SkillCatalogBinding(
        name=name,
        resource_id=f"skill-entry:test:{name}:activation",
        entry_identity=f"skill-entry:test:{name}",
        display_uri=f"boxteam://workspace/skill/{name}",
        activation_revision=_revision_of(body),
        body=body,
    )
    manager.install_skill_activation_snapshot(
        SkillCatalogActivationSnapshot(
            catalog_revision="sha256:test-catalog",
            entries={name: binding},
        )
    )


def _tracked_manager(*names: str) -> ContextSourceManager:
    manager = _manager(*names)
    for name in names:
        body = f"# {name}\n\nv1\n"
        manager.activate_skill_content(name, f"# {name}\n\nv1\n")
        _install_snapshot(manager, name, body)
        manager.load_skill(name, mode="tracked")
    return manager


def _revision_of(content: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_pending_observation_keeps_only_latest_revision_per_source() -> None:
    manager = _tracked_manager("demo")

    assert (
        manager.mark_pending_observation("skill:demo", _revision_of(DEMO_CONTENT)) is True
    )
    assert manager.pending_observation_count() == 1
    assert (
        manager.mark_pending_observation("skill:demo", _revision_of(OTHER_CONTENT))
        is True
    )
    # 同一来源只保留最新 revision，不堆积多条 observation。
    assert manager.pending_observation_count() == 1

    pending = manager.next_pending_observation()
    assert pending is not None
    assert pending.source_id == "skill:demo"
    assert pending.revision == _revision_of(OTHER_CONTENT)
    assert manager.pending_observation_count() == 0
    assert manager.next_pending_observation() is None


def test_pending_observation_dedups_known_and_latest_visible_committed_revisions() -> None:
    manager = _tracked_manager("demo")
    applied = _revision_of("# demo\n\nv1\n")
    assert manager.source_observation_state("skill:demo") == ("tracked", applied)

    # 已 applied 的 revision 不再排队（否则会重复注入）。
    assert manager.mark_pending_observation("skill:demo", applied) is False
    assert manager.pending_observation_count() == 0

    new_revision = _revision_of(DEMO_CONTENT)
    assert manager.mark_pending_observation("skill:demo", new_revision) is True
    # 重复标记同一 revision 是幂等的。
    assert manager.mark_pending_observation("skill:demo", new_revision) is False
    assert manager.pending_observation_count() == 1


def test_untracked_marker_is_discarded_on_consume() -> None:
    manager = _manager("demo")
    revision = _revision_of(DEMO_CONTENT)

    # 尚未 tracked 也可以登记（首次 activation 与后续 delta 同一条路径）。
    assert manager.mark_pending_observation("skill:demo", revision) is True
    assert manager.pending_observation_count() == 1

    # 消费时未 tracked → 丢弃，不产生 observation。
    assert manager.next_pending_observation() is None
    assert manager.pending_observation_count() == 0


def test_reconcile_marker_allows_unknown_revision() -> None:
    manager = _manager("demo", "other")

    # 非 reconcile 标记必须带 revision。
    with pytest.raises(ValueError, match="必须提供 revision"):
        manager.mark_pending_observation("skill:demo", None)

    # reconcile 标记允许 revision=None，表示「按权威快照重新对账」。
    assert manager.mark_pending_observation("skill:demo", None, reconcile=True) is True
    assert manager.pending_observation_count() == 1

    observed: list[str] = []
    while True:
        pending = manager.next_pending_observation()
        if pending is None:
            break
        observed.append(pending.source_id)
    # 未 tracked 的 reconcile 标记同样在消费时丢弃。
    assert observed == []


def test_reconcile_marker_on_tracked_source_yields_observation() -> None:
    """gap 后的 reconcile 标记必须能驱动已 tracked 来源重新对账。"""
    manager = _tracked_manager("demo", "other")

    assert manager.mark_all_pending_observations() == ("skill:demo", "skill:other")
    assert manager.next_pending_observation() == PendingSourceObservation(
        source_id="skill:demo",
        revision=None,
    )
    assert manager.next_pending_observation() == PendingSourceObservation(
        source_id="skill:other",
        revision=None,
    )
    assert manager.next_pending_observation() is None


def test_mark_all_pending_observations_scopes_to_requested_sources() -> None:
    manager = _tracked_manager("demo", "other")

    # 全量 reconcile：所有已注册来源都被标记（reconcile 标记 revision 为空）。
    assert manager.mark_all_pending_observations() == ("skill:demo", "skill:other")
    assert manager.pending_observation_count() == 2
    # 重复全量标记幂等。
    assert manager.mark_all_pending_observations() == ()
    assert manager.pending_observation_count() == 2

    # 只对指定来源做全量 reconcile 时不得波及其它来源。
    assert manager.mark_all_pending_observations(("skill:other",)) == ()
    assert manager.pending_observation_count() == 2

    # 消费掉全部标记后，单独标记新变化。
    while manager.next_pending_observation() is not None:
        pass
    assert manager.pending_observation_count() == 0
    assert manager.mark_pending_observation("skill:demo", _revision_of(DEMO_CONTENT))
    assert manager.pending_observation_count() == 1

    with pytest.raises(KeyError, match="Context source 不存在"):
        manager.mark_all_pending_observations(("skill:missing",))


def test_source_observation_state_reports_tracking_and_revision() -> None:
    manager = _manager("demo")
    assert manager.source_observation_state("skill:demo") == ("untracked", None)

    manager.activate_skill_content("demo", DEMO_CONTENT)
    revision = _revision_of(DEMO_CONTENT)
    assert manager.source_observation_state("skill:demo") == ("untracked", revision)

    _install_snapshot(manager, "demo", DEMO_CONTENT)
    manager.load_skill("demo", mode="tracked")
    assert manager.source_observation_state("skill:demo") == ("tracked", revision)

    manager.load_skill("demo", mode="untrack")
    assert manager.source_observation_state("skill:demo") == ("untracked", revision)

    with pytest.raises(KeyError, match="Context source 不存在"):
        manager.source_observation_state("skill:missing")


def test_pending_observation_requires_known_source() -> None:
    manager = _manager("demo")
    with pytest.raises(KeyError, match="Context source 不存在"):
        manager.mark_pending_observation("skill:missing", _revision_of(DEMO_CONTENT))
    with pytest.raises(ValueError, match="非空字符串"):
        manager.mark_pending_observation("skill:demo", "")
