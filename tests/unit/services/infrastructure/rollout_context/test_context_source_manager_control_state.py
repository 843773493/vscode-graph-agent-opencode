"""CSM 控制状态持久化与重启恢复的单元测试（注入端口替身）。

测试只使用 typed 端口替身，不创建真实 SQLite；真实 owner 的持久化由
``test_context_source_control_storage.py`` 覆盖。
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    MAIN_THREAD_ID,
    ContextSourceControlState,
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDelta,
    ContextSourceDescriptor,
    ContextSourceManager,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
    _revision,
)

SESSION_ID = "ses_0123456789abcdef0123456789abcdef"
CHILD_THREAD_ID = "thr_fedcba9876543210fedcba9876543210"
UPDATED_AT = "2026-09-15T00:00:00+00:00"
V1_REVISION = "sha256:" + hashlib.sha256(b"v1\n").hexdigest()
CHILD_REVISION = "sha256:" + hashlib.sha256(b"child\n").hexdigest()


class InMemoryControlStatePort:
    """测试用最小端口替身：只在内存保存 typed 状态，模拟 owner 的版本推进。"""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str, str], ContextSourceControlState] = {}
        self.writes = 0

    def load_context_source_control_states(
        self,
        owner: ContextSourceOwnerKey,
    ) -> tuple[ContextSourceControlState, ...]:
        return tuple(
            state
            for key, state in self._states.items()
            if key[:2] == (owner.session_id, owner.thread_id)
        )

    def save_context_source_control_state(
        self,
        state: ContextSourceControlState,
    ) -> ContextSourceControlState:
        key = (state.owner.session_id, state.owner.thread_id, state.source_id)
        existing = self._states.get(key)
        if existing is not None and existing.durable_fields() == state.durable_fields():
            return existing
        if existing is not None and state.state_revision != existing.state_revision:
            raise RuntimeError(
                "测试端口拒绝过期控制状态: "
                f"expected={state.state_revision} stored={existing.state_revision}"
            )
        self.writes += 1
        stored = replace(
            state,
            state_revision=1 if existing is None else existing.state_revision + 1,
            updated_at=UPDATED_AT,
        )
        self._states[key] = stored
        return stored


def _descriptor() -> ContextSourceDescriptor:
    return ContextSourceDescriptor(
        source_id="skill:debugging",
        source_kind="skill",
        name="debugging",
        description="调试工作流",
        internal_locator="/.boxteam/skills/debugging/SKILL.md",
    )


def _install_snapshot(manager: ContextSourceManager, content: str = "v1\n") -> None:
    """按正式 typed API 安装与 registered source 一致的冻结 binding。"""
    manager.install_skill_activation_snapshot(
        SkillCatalogActivationSnapshot(
            catalog_revision="sha256:test-catalog",
            entries={
                "debugging": SkillCatalogBinding(
                    name="debugging",
                    resource_id="skill-entry:test:debugging:activation",
                    entry_identity="skill-entry:test:debugging",
                    display_uri="boxteam://workspace/skill/debugging",
                    activation_revision=_revision(content),
                    body=content,
                )
            },
        )
    )


def _commit_model_call_pending(manager: ContextSourceManager) -> tuple[ContextSourceDelta, ...]:
    batch = manager.prepare_pending()
    assert batch is not None
    manager.commit_model_call_pending(batch)
    return batch.deltas


def _track_skill(manager: ContextSourceManager, content: str = "v1\n") -> None:
    manager.activate_skill_content("debugging", content)
    _install_snapshot(manager, content)
    manager.load_skill("debugging", mode="tracked")
    _commit_model_call_pending(manager)


def _new_manager(
    owner: ContextSourceOwnerKey,
    port: InMemoryControlStatePort,
) -> ContextSourceManager:
    """模拟进程重启/agent 重建后的新 CSM，只依赖注入的 owner 端口。"""
    manager = ContextSourceManager(owner=owner, control_state_port=port)
    manager.register(_descriptor())
    return manager


@pytest.fixture
def control_state_port() -> InMemoryControlStatePort:
    return InMemoryControlStatePort()


@pytest.fixture
def owner() -> ContextSourceOwnerKey:
    return ContextSourceOwnerKey(session_id=SESSION_ID, thread_id=MAIN_THREAD_ID)


@pytest.fixture
def manager(
    owner: ContextSourceOwnerKey,
    control_state_port: InMemoryControlStatePort,
) -> ContextSourceManager:
    return _new_manager(owner, control_state_port)


def test_owner_and_port_must_be_provided_together() -> None:
    with pytest.raises(ValueError, match="owner 与 control_state_port"):
        ContextSourceManager(
            owner=ContextSourceOwnerKey(session_id=SESSION_ID, thread_id=MAIN_THREAD_ID)
        )
    with pytest.raises(ValueError, match="owner 与 control_state_port"):
        ContextSourceManager(control_state_port=InMemoryControlStatePort())


def test_unactivated_registration_is_not_persisted(
    manager: ContextSourceManager,
    control_state_port: InMemoryControlStatePort,
    owner: ContextSourceOwnerKey,
) -> None:
    """从未激活/跟踪的目录注册不是需要跨重启保留的控制状态。"""
    manager.register(_descriptor())

    assert control_state_port.writes == 0
    assert control_state_port.load_context_source_control_states(owner) == ()


def test_control_state_round_trip_records_identity_and_revisions(
    manager: ContextSourceManager,
    control_state_port: InMemoryControlStatePort,
    owner: ContextSourceOwnerKey,
) -> None:
    _track_skill(manager)

    (stored,) = control_state_port.load_context_source_control_states(owner)
    assert stored.owner == owner
    assert stored.source_id == "skill:debugging"
    assert stored.source_kind == "skill"
    assert stored.name == "debugging"
    assert stored.tracking_status == "tracked"
    assert stored.latest_revision == V1_REVISION
    assert stored.latest_visible_committed_revision == V1_REVISION
    assert stored.binding_revision is None
    # register/track 与 applied revision 推进各自产生一次持久化写入。
    assert stored.state_revision == 3
    assert stored.updated_at == UPDATED_AT


def test_repeated_register_does_not_rewrite_unchanged_control_state(
    manager: ContextSourceManager,
    control_state_port: InMemoryControlStatePort,
) -> None:
    _track_skill(manager)
    writes = control_state_port.writes

    manager.register(_descriptor())
    manager.register(_descriptor())

    assert control_state_port.writes == writes


def test_restart_recovery_does_not_reinject_tracked_source(
    manager: ContextSourceManager,
    control_state_port: InMemoryControlStatePort,
    owner: ContextSourceOwnerKey,
) -> None:
    _track_skill(manager)
    rebuilt = _new_manager(owner, control_state_port)

    assert rebuilt.observe("skill:debugging", "v1\n") is False
    assert rebuilt.prepare_pending() is None
    assert rebuilt.metadata() == (
        {"name": "debugging", "description": "调试工作流"},
    )


def test_restart_recovery_keeps_untrack_state(
    manager: ContextSourceManager,
    control_state_port: InMemoryControlStatePort,
    owner: ContextSourceOwnerKey,
) -> None:
    _track_skill(manager)
    manager.load_skill("debugging", mode="untrack")

    rebuilt = _new_manager(owner, control_state_port)
    (stored,) = control_state_port.load_context_source_control_states(owner)
    _install_snapshot(rebuilt)

    assert stored.tracking_status == "untracked"
    assert rebuilt.load_skill("debugging").tracked is False
    assert rebuilt.observe("skill:debugging", "v2\n") is False
    assert rebuilt.prepare_pending() is None


def test_restart_recovery_rebuilds_diff_baseline_from_latest_visible_committed_revision(
    manager: ContextSourceManager,
    control_state_port: InMemoryControlStatePort,
    owner: ContextSourceOwnerKey,
) -> None:
    _track_skill(manager)
    rebuilt = _new_manager(owner, control_state_port)

    # 首帧同一 revision 只重建基准；下一 revision 仍按 applied revision 合并 delta。
    assert rebuilt.observe("skill:debugging", "v1\n") is False
    assert rebuilt.observe("skill:debugging", "v2\n") is True
    (delta,) = _commit_model_call_pending(rebuilt)

    assert delta.kind == "delta"
    assert delta.previous_revision == V1_REVISION
    assert "-v1" in delta.content
    assert "+v2" in delta.content


def test_different_threads_do_not_share_control_state(
    control_state_port: InMemoryControlStatePort,
) -> None:
    main_owner = ContextSourceOwnerKey(session_id=SESSION_ID, thread_id=MAIN_THREAD_ID)
    child_owner = ContextSourceOwnerKey(
        session_id=SESSION_ID,
        thread_id=CHILD_THREAD_ID,
    )
    main = _new_manager(main_owner, control_state_port)
    _track_skill(main)

    child = ContextSourceManager(
        owner=child_owner,
        control_state_port=control_state_port,
    )
    assert child.metadata() == ()
    child.register(_descriptor())
    _track_skill(child, "child\n")

    (main_state,) = control_state_port.load_context_source_control_states(main_owner)
    (child_state,) = control_state_port.load_context_source_control_states(child_owner)
    assert main_state.latest_visible_committed_revision == V1_REVISION
    assert child_state.latest_visible_committed_revision == CHILD_REVISION
    rebuilt_child = _new_manager(child_owner, control_state_port)
    assert rebuilt_child.observe("skill:debugging", "child\n") is False
    assert rebuilt_child.observe("skill:debugging", "v1\n") is True


def test_restart_recovery_allows_tracked_registration_awaiting_first_observation():
    # 注册后、首帧观察前重启：tracked + 无 revision 合法，恢复后可继续观察。
    port = InMemoryControlStatePort()
    owner = ContextSourceOwnerKey(session_id=SESSION_ID, thread_id=MAIN_THREAD_ID)
    manager = ContextSourceManager(owner=owner, control_state_port=port)
    manager.register(
        ContextSourceDescriptor(
            source_id="agents:workspace",
            source_kind="workspace_agents",
            name="AGENTS.md",
            description="工作区指令",
            internal_locator="boxteam://workspace/agents",
            resource_uri="boxteam://workspace/agents",
        ),
        tracking_status="tracked",
    )

    rebuilt = ContextSourceManager(owner=owner, control_state_port=port)
    status, latest = rebuilt.source_observation_state("agents:workspace")
    assert status == "tracked"
    assert latest is None
    assert rebuilt.observe("agents:workspace", "v1\n") is True
