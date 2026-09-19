"""CSM commit/untrack 成功边界发布 context.source/* 轻量事件的接线测试（3.8-C）。

事件只是内存通知：验证发布内容只含 identity/revision/kind 与 owner 定位字段，
不携带正文；未注入 publisher 时 CSM 行为与之前完全一致。
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from app.services.infrastructure.events.channel_events import ContextSourceEvent
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlState,
    ContextSourceOwnerKey,
)
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
                    display_uri="boxteam://workspace/skills/demo",
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
        resource_uri="boxteam://workspace/skills/demo",
    )


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[ContextSourceEvent] = []

    def __call__(self, event: ContextSourceEvent) -> None:
        self.events.append(event)


class _FakeControlStatePort:
    """内存控制状态端口：只为验证事件携带 owner 定位字段。"""

    def __init__(self) -> None:
        self.saved: list[ContextSourceControlState] = []

    def load_context_source_control_states(
        self, owner: ContextSourceOwnerKey
    ) -> tuple[ContextSourceControlState, ...]:
        return ()

    def save_context_source_control_state(
        self, snapshot: ContextSourceControlState
    ) -> ContextSourceControlState:
        self.saved.append(snapshot)
        return dataclasses.replace(
            snapshot, state_revision=snapshot.state_revision + 1
        )


def test_commit_model_call_pending_publishes_committed_event_per_source() -> None:
    sink = RecordingSink()
    manager = ContextSourceManager(
        owner=None,
        control_state_port=None,
        lifecycle_event_sink=sink,
    )
    manager.register(_descriptor())
    manager.activate_skill_content("demo", "hello world")
    batch = manager.prepare_pending()
    assert batch is not None
    manager.commit_model_call_pending(batch)

    assert len(sink.events) == 1
    event = sink.events[-1]
    assert event.source_id == "src_skill_1"
    assert event.source_kind == "skill"
    assert event.kind == "committed"
    assert event.revision == _revision("hello world")
    # 轻量红线：事件值对象不携带正文/diff 字段。
    assert not hasattr(event, "content")
    assert not hasattr(event, "diff")


def test_commit_model_call_pending_publishes_one_event_per_delta_with_owner_identity() -> None:
    sink = RecordingSink()
    owner = ContextSourceOwnerKey(session_id="ses_1", thread_id="thr_1")
    manager = ContextSourceManager(
        owner=owner,
        control_state_port=_FakeControlStatePort(),
        lifecycle_event_sink=sink,
    )
    manager.register(_descriptor("src_a", "a"))
    manager.register(_descriptor("src_b", "b"))
    manager.activate_skill_content("a", "alpha")
    manager.activate_skill_content("b", "beta")
    batch = manager.prepare_pending()
    assert batch is not None
    manager.commit_model_call_pending(batch)

    assert [(event.source_id, event.kind) for event in sink.events] == [
        ("src_a", "committed"),
        ("src_b", "committed"),
    ]
    assert all(event.session_id == "ses_1" for event in sink.events)
    assert all(event.thread_id == "thr_1" for event in sink.events)


def test_untrack_publishes_untracked_event_with_latest_revision() -> None:
    sink = RecordingSink()
    manager = ContextSourceManager(
        owner=None,
        control_state_port=None,
        lifecycle_event_sink=sink,
    )
    manager.register(_descriptor())
    manager.activate_skill_content("demo", "hello world")
    _install_snapshot(manager, "hello world")
    manager.load_skill("demo", mode="tracked")
    batch = manager.prepare_pending()
    assert batch is not None
    manager.commit_model_call_pending(batch)
    receipt = manager.load_skill("demo", mode="untrack")
    assert receipt.tracked is False

    # commit 边界先发布 committed 事件；untracked 是最后一个事件。
    assert len(sink.events) == 2
    event = sink.events[-1]
    assert event.kind == "untracked"
    assert event.source_id == "src_skill_1"
    assert event.revision == _revision("hello world")


def test_manager_without_sink_unchanged_and_no_events() -> None:
    """未注入 publisher 时行为与之前一致（默认参数，零事件）。"""
    manager = ContextSourceManager()
    manager.register(_descriptor())
    manager.activate_skill_content("demo", "hello world")
    batch = manager.prepare_pending()
    assert batch is not None
    manager.commit_model_call_pending(batch)
    receipt = manager.load_skill("demo", mode="untrack")
    assert receipt.tracked is False


def test_publisher_failure_is_not_silently_swallowed() -> None:
    """发布出口抛错时不被 CSM 吞掉（程序绝不默默失败）。"""

    def exploding_sink(event: ContextSourceEvent) -> None:
        raise RuntimeError("channel unavailable")

    manager = ContextSourceManager(
        owner=None,
        control_state_port=None,
        lifecycle_event_sink=exploding_sink,
    )
    manager.register(_descriptor())
    manager.activate_skill_content("demo", "hello world")
    batch = manager.prepare_pending()
    assert batch is not None
    with pytest.raises(RuntimeError, match="channel unavailable"):
        manager.commit_model_call_pending(batch)


class _FailingControlStatePort(_FakeControlStatePort):
    """第 N 次保存才抛错的端口：验证「持久化成功后才发布」的顺序守卫（R2b 审查 M2）。

    activate/observe 队列 delta 时会先成功保存一次；commit/untrack 边界的
    保存是第 2 次。让第 2 次失败即可精确模拟「commit 时刻持久化失败」。
    """

    def __init__(self, fail_on_save_number: int = 2) -> None:
        super().__init__()
        self._fail_on_save_number = fail_on_save_number
        self._save_count = 0

    def save_context_source_control_state(
        self, snapshot: ContextSourceControlState
    ) -> ContextSourceControlState:
        self._save_count += 1
        if self._save_count >= self._fail_on_save_number:
            raise RuntimeError("control state storage unavailable")
        return super().save_context_source_control_state(snapshot)


def test_commit_publishes_only_after_persistence_succeeds() -> None:
    """持久化端口抛错时 commit 不得发布任何事件（发布必须在持久化成功之后）。

    save 序列：activate 队列 delta=1、commit 推进 applied revision=2；让第 2 次
    失败即精确模拟「commit 边界持久化失败」。
    """
    sink = RecordingSink()
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(session_id="ses_1", thread_id="thr_1"),
        control_state_port=_FailingControlStatePort(),
        lifecycle_event_sink=sink,
    )
    manager.register(_descriptor())
    manager.activate_skill_content("demo", "hello world")
    batch = manager.prepare_pending()
    assert batch is not None
    with pytest.raises(RuntimeError, match="control state storage unavailable"):
        manager.commit_model_call_pending(batch)
    assert sink.events == []


def test_untrack_publishes_only_after_persistence_succeeds() -> None:
    """持久化端口抛错时 untrack 不得发布任何事件。

    save 序列：activate=1、tracked=2、untrack=3；让第 3 次失败即精确模拟
    「untrack 边界持久化失败」。untrack 未跟踪来源时 durable fields 不变，
    会被 dedup 守卫跳过，因此必须先 tracked 再 untrack。
    """
    sink = RecordingSink()
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(session_id="ses_1", thread_id="thr_1"),
        control_state_port=_FailingControlStatePort(fail_on_save_number=3),
        lifecycle_event_sink=sink,
    )
    manager.register(_descriptor())
    manager.activate_skill_content("demo", "hello world")
    _install_snapshot(manager, "hello world")
    manager.load_skill("demo", mode="tracked")
    with pytest.raises(RuntimeError, match="control state storage unavailable"):
        manager.load_skill("demo", mode="untrack")
    assert sink.events == []
