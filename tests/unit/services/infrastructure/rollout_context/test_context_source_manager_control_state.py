"""真实 Saver 控制状态端口与 CSM 重建合同；applied 只由 assembly seal 推进。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import closing
from datetime import datetime

import pytest

from app.core.identifier import uuid7_datetime_from_hex
from app.core.path_utils import get_session_path_resolver
from app.core.session_control_store import SessionControlStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
    _revision,
)
from tests.support.graph_binding import TEST_THREAD_GRAPH_BINDING_JSON
from tests.unit.services.infrastructure.rollout_context.conftest import SourceSealCase

SESSION_ID = "ses_019bfb6b5f3c72178c7e8393ce89e7a6"
CHILD_THREAD_ID = "thr_019c58e117a67ec489710c058b3e22a3"
V1_REVISION = "sha256:" + hashlib.sha256(b"v1\n").hexdigest()
CHILD_REVISION = "sha256:" + hashlib.sha256(b"child\n").hexdigest()


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


def _track_skill(manager: ContextSourceManager, seal_sources: Callable, content: str = "v1\n") -> None:
    manager.activate_skill_content("debugging", content)
    _install_snapshot(manager, content)
    manager.load_skill("debugging", mode="tracked")
    seal_sources(manager)


def _new_manager(
    owner: ContextSourceOwnerKey,
    port: RolloutCheckpointSaver,
) -> ContextSourceManager:
    """模拟进程重启/agent 重建后的新 CSM，只依赖注入的 owner 端口。"""
    manager = ContextSourceManager(owner=owner, control_state_port=port)
    manager.register(_descriptor())
    return manager


@pytest.fixture
def control_state_port(source_seal_case) -> RolloutCheckpointSaver:
    return source_seal_case.saver


@pytest.fixture
def owner(source_seal_case) -> ContextSourceOwnerKey:
    return source_seal_case.owner


@pytest.fixture
def seal_sources(
    control_state_port: RolloutCheckpointSaver,
    frozen_provider_wire: Callable[[str, str], dict[str, object]],
) -> Callable:
    cases: dict[ContextSourceOwnerKey, SourceSealCase] = {}

    def seal(manager: ContextSourceManager):
        owner = manager._owner
        assert owner is not None
        if owner not in cases:
            accepted = control_state_port.accept_turn(
                owner.session_id, thread_id=owner.thread_id,
                accepted_ingress_id="control-state-ingress:" + owner.thread_id,
                acceptance_idempotency_key="control-state-accept:" + owner.thread_id,
                job_id="control-state-job:" + owner.thread_id,
                admission_idempotency_key="control-state-admission:" + owner.thread_id,
                payload="控制状态重建真实 seal",
            )
            cases[owner] = SourceSealCase(
                control_state_port, owner, accepted, frozen_provider_wire,
            )
        return cases[owner].seal(manager)

    return seal


@pytest.fixture
def manager(
    owner: ContextSourceOwnerKey,
    control_state_port: RolloutCheckpointSaver,
) -> ContextSourceManager:
    return _new_manager(owner, control_state_port)


def test_owner_and_port_must_be_provided_together() -> None:
    with pytest.raises(ValueError, match="owner 与 control_state_port"):
        ContextSourceManager(
            owner=ContextSourceOwnerKey(session_id=SESSION_ID, thread_id=CHILD_THREAD_ID)
        )
    with pytest.raises(ValueError, match="owner 与 control_state_port"):
        ContextSourceManager(control_state_port=object())


def test_unactivated_registration_is_not_persisted(
    manager: ContextSourceManager,
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
) -> None:
    """从未激活/跟踪的目录注册不是需要跨重启保留的控制状态。"""
    manager.register(_descriptor())

    assert control_state_port.load_context_source_control_states(owner) == ()


def test_control_state_round_trip_records_identity_and_revisions(
    manager: ContextSourceManager,
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
    seal_sources,
) -> None:
    _track_skill(manager, seal_sources)

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
    assert datetime.fromisoformat(stored.updated_at).tzinfo is not None


def test_repeated_register_does_not_rewrite_unchanged_control_state(
    manager: ContextSourceManager,
    control_state_port: RolloutCheckpointSaver,
    seal_sources,
) -> None:
    _track_skill(manager, seal_sources)
    before = control_state_port.load_context_source_control_states(manager._owner)

    manager.register(_descriptor())
    manager.register(_descriptor())

    assert control_state_port.load_context_source_control_states(manager._owner) == before


def test_restart_recovery_does_not_reinject_tracked_source(
    manager: ContextSourceManager,
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
    seal_sources,
) -> None:
    _track_skill(manager, seal_sources)
    rebuilt = _new_manager(owner, control_state_port)

    assert rebuilt.observe("skill:debugging", "v1\n") is False
    assert rebuilt.prepare_pending() is None
    assert rebuilt.metadata() == (
        {"name": "debugging", "description": "调试工作流"},
    )


def test_restart_recovery_keeps_untrack_state(
    manager: ContextSourceManager,
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
    seal_sources,
) -> None:
    _track_skill(manager, seal_sources)
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
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
    seal_sources,
) -> None:
    _track_skill(manager, seal_sources)
    rebuilt = _new_manager(owner, control_state_port)

    # 首帧同一 revision 只重建基准；下一 revision 仍按 applied revision 合并 delta。
    assert rebuilt.observe("skill:debugging", "v1\n") is False
    assert rebuilt.observe("skill:debugging", "v2\n") is True
    (delta,) = seal_sources(rebuilt)[0].deltas

    assert delta.kind == "delta"
    assert delta.previous_revision == V1_REVISION
    assert "-v1" in delta.content
    assert "+v2" in delta.content


def test_different_threads_do_not_share_control_state(
    control_state_port: RolloutCheckpointSaver,
    source_seal_case,
    seal_sources,
) -> None:
    main_owner = source_seal_case.owner
    resolver = get_session_path_resolver(control_state_port._storage.sessions_dir)
    session_root = resolver.resolve_session_node(main_owner.session_id)
    with closing(SessionControlStore(session_root / "session-control.sqlite")) as control:
        key = "control-state-child"
        control.create_or_get_thread_creation_record(
            idempotency_key=key, initial_state="idle", preimage_hash="a" * 64,
            graph_binding=TEST_THREAD_GRAPH_BINDING_JSON, capability_profile=json.dumps({}),
            created_at=uuid7_datetime_from_hex(CHILD_THREAD_ID[4:]),
            thread_id=CHILD_THREAD_ID,
        )
        manifest = "{}"
        control.freeze_thread_creation_artifact_manifest(
            key, artifact_manifest=manifest,
            artifact_manifest_hash=hashlib.sha256(manifest.encode()).hexdigest(),
        )
        child = control.publish_thread_creation_record(key)
    (session_root / child.final_relative_locator).mkdir(parents=True)
    child_owner = ContextSourceOwnerKey(
        session_id=main_owner.session_id, thread_id=CHILD_THREAD_ID,
    )
    main = _new_manager(main_owner, control_state_port)
    _track_skill(main, seal_sources)

    child = ContextSourceManager(
        owner=child_owner,
        control_state_port=control_state_port,
    )
    assert child.metadata() == ()
    child.register(_descriptor())
    _track_skill(child, seal_sources, "child\n")

    (main_state,) = control_state_port.load_context_source_control_states(main_owner)
    (child_state,) = control_state_port.load_context_source_control_states(child_owner)
    assert main_state.latest_visible_committed_revision == V1_REVISION
    assert child_state.latest_visible_committed_revision == CHILD_REVISION
    rebuilt_child = _new_manager(child_owner, control_state_port)
    assert rebuilt_child.observe("skill:debugging", "child\n") is False
    assert rebuilt_child.observe("skill:debugging", "v1\n") is True


def test_restart_recovery_allows_tracked_registration_awaiting_first_observation(source_seal_case):
    # 注册后、首帧观察前重启：tracked + 无 revision 合法，恢复后可继续观察。
    port = source_seal_case.saver
    owner = source_seal_case.owner
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
