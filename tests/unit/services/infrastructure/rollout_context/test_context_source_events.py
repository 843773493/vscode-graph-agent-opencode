"""CSM commit/untrack 成功边界发布 context.source/* 轻量事件的接线测试（3.8-C）。

事件只是内存通知：验证发布内容只含 identity/revision/kind 与 owner 定位字段，
不携带正文；未注入 publisher 时 CSM 行为与之前完全一致。
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing

import pytest

from app.services.infrastructure.events.channel_events import ContextSourceEvent
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
)


def _revision(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _install_snapshot(manager: ContextSourceManager, content: str) -> None:
    """按正式 typed API 安装与 registered source 一致的冻结 binding。"""
    manager.install_skill_activation_snapshot(
        SkillCatalogActivationSnapshot(
            catalog_revision="sha256:test-catalog",
            entries={
                "demo": SkillCatalogBinding(
                    name="demo",
                    resource_id="skill-entry:test:demo:activation",
                    entry_identity="skill-entry:test:demo",
                    display_uri="boxteam://workspace/test/resources/skills/demo/SKILL.md",
                    activation_revision=_revision(content),
                    body=content,
                )
            },
        )
    )


def _descriptor(source_id: str = "src_skill_1", name: str = "demo") -> ContextSourceDescriptor:
    return ContextSourceDescriptor(
        source_id=source_id,
        source_kind="skill",
        name=name,
        description="demo skill",
        internal_locator=".boxteam/skills/demo/SKILL.md",
        resource_uri="boxteam://workspace/test/resources/skills/demo/SKILL.md",
    )


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[ContextSourceEvent] = []

    def __call__(self, event: ContextSourceEvent) -> None:
        self.events.append(event)


def test_real_seal_publishes_committed_event_per_source(source_seal_case):
    sink = RecordingSink()
    manager = source_seal_case.manager(lifecycle_event_sink=sink)
    manager.register(_descriptor())
    manager.activate_skill_content("demo", "hello world")
    source_seal_case.seal(manager)
    assert len(sink.events) == 1
    event = sink.events[-1]
    assert event.source_id == "src_skill_1"
    assert event.source_kind == "skill"
    assert event.kind == "committed"
    assert event.revision == _revision("hello world")
    assert not hasattr(event, "content")
    assert not hasattr(event, "diff")


def test_real_seal_publishes_one_event_per_delta_with_exact_owner(source_seal_case):
    sink = RecordingSink()
    manager = source_seal_case.manager(lifecycle_event_sink=sink)
    manager.register(_descriptor("src_a", "a"))
    manager.register(_descriptor("src_b", "b"))
    manager.activate_skill_content("a", "alpha")
    manager.activate_skill_content("b", "beta")
    source_seal_case.seal(manager)
    assert [(event.source_id, event.kind) for event in sink.events] == [
        ("src_a", "committed"), ("src_b", "committed"),
    ]
    assert all(event.session_id == source_seal_case.owner.session_id for event in sink.events)
    assert all(event.thread_id == source_seal_case.owner.thread_id for event in sink.events)


def test_untrack_publishes_after_real_seal_with_latest_revision(source_seal_case):
    sink = RecordingSink()
    manager = source_seal_case.manager(lifecycle_event_sink=sink)
    manager.register(_descriptor())
    manager.activate_skill_content("demo", "hello world")
    _install_snapshot(manager, "hello world")
    manager.load_skill("demo", mode="tracked")
    source_seal_case.seal(manager)
    receipt = manager.load_skill("demo", mode="untrack")
    assert receipt.tracked is False
    assert len(sink.events) == 2
    assert [event.kind for event in sink.events] == ["committed", "untracked"]
    event = sink.events[-1]
    assert event.source_id == "src_skill_1"
    assert event.revision == _revision("hello world")


def test_manager_without_sink_still_confirms_real_seal_and_untracks(source_seal_case):
    manager = source_seal_case.manager()
    manager.register(_descriptor())
    manager.activate_skill_content("demo", "hello world")
    receipt, _ = source_seal_case.seal(manager)
    assert receipt.deltas[0].revision == _revision("hello world")
    assert manager.load_skill("demo", mode="untrack").tracked is False


def test_publisher_failure_is_not_silently_swallowed_after_seal(source_seal_case):
    def exploding_sink(event: ContextSourceEvent) -> None:
        raise RuntimeError("channel unavailable")

    manager = source_seal_case.manager(lifecycle_event_sink=exploding_sink)
    manager.register(_descriptor())
    manager.activate_skill_content("demo", "hello world")
    with pytest.raises(RuntimeError, match="channel unavailable"):
        source_seal_case.seal(manager)
    (stored,) = source_seal_case.saver.load_context_source_control_states(source_seal_case.owner)
    assert stored.latest_visible_committed_revision == _revision("hello world")


def test_failed_real_seal_publishes_no_committed_event(source_seal_case):
    sink = RecordingSink()
    manager = source_seal_case.manager(lifecycle_event_sink=sink)
    manager.register(_descriptor())
    manager.activate_skill_content("demo", "hello world")
    batch = manager.prepare_pending()
    owner, saver = source_seal_case.owner, source_seal_case.saver
    before = saver.load_context_source_control_states(owner)
    with closing(saver._storage.storage._connect(owner.thread_id, session_id=owner.session_id)) as connection:
        connection.execute(
            "CREATE TRIGGER refuse_source_event BEFORE INSERT ON context_assemblies "
            "BEGIN SELECT RAISE(ABORT, 'injected assembly failure'); END"
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="injected assembly failure"):
            source_seal_case.seal(manager)
    assert sink.events == []
    assert manager.prepare_pending() == batch
    assert saver.load_context_source_control_states(owner) == before


def test_untrack_publishes_only_after_persistence_succeeds(source_seal_case, monkeypatch):
    sink = RecordingSink()
    manager = source_seal_case.manager(lifecycle_event_sink=sink)
    manager.register(_descriptor())
    manager.activate_skill_content("demo", "hello world")
    _install_snapshot(manager, "hello world")
    manager.load_skill("demo", mode="tracked")
    before = source_seal_case.saver.load_context_source_control_states(source_seal_case.owner)

    def fail_persistence(*args, **kwargs):
        raise RuntimeError("control state storage unavailable")

    monkeypatch.setattr(source_seal_case.saver, "apply_context_source_control_mutation", fail_persistence)
    with pytest.raises(RuntimeError, match="control state storage unavailable"):
        manager.load_skill("demo", mode="untrack")
    assert sink.events == []
    assert source_seal_case.saver.load_context_source_control_states(source_seal_case.owner) == before
