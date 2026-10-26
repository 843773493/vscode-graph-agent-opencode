"""真实 Saver 控制状态端口与 CSM 重建合同；applied 只由 assembly seal 推进。"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime

import pytest

from app.core.identifier import uuid7_datetime_from_hex
from app.core.path_utils import get_session_path_resolver
from app.core.session_control_store import SessionControlStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlMutation,
    ContextSourceControlOperation,
    ContextSourceControlState,
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
    ContextSourceTrackingStateConflict,
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
                    display_uri="boxteam://workspace/test/resources/skills/debugging/SKILL.md",
                    activation_revision=_revision(content),
                    body=content,
                )
            },
        )
    )


def _track_skill(manager: ContextSourceManager, seal_sources: Callable, content: str = "v1\n") -> None:
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
    frozen_provider_activation: Callable[..., dict[str, object]],
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
                control_state_port, owner, accepted, frozen_provider_wire, frozen_provider_activation,
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
    # 显式激活共同接受正文与 mode，之后只有 seal 推进 applied。
    assert stored.state_revision == 2
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

    assert rebuilt.observe("skill:debugging", "v1\n").status == "baseline_restored"
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
    assert rebuilt.observe("skill:debugging", "v2\n").status == "not_tracked"
    assert rebuilt.prepare_pending() is None


def test_activation_refresh_accepts_external_untrack_without_losing_pending_or_visible(
    manager: ContextSourceManager,
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
    seal_sources,
) -> None:
    _track_skill(manager, seal_sources)
    assert manager.observe("skill:debugging", "v2\n").status == "accepted"
    pending = manager.prepare_pending()
    assert pending is not None
    frozen = manager.prepare_sources()
    external = ContextSourceManager(owner=owner, control_state_port=control_state_port)
    assert external.load_skill("debugging", mode="untrack").tracked is False
    committed = control_state_port.load_context_source_control_states(owner)

    manager.refresh_control_states()
    refreshed = manager.prepare_sources()
    by_source = {state.source_id: state for state in committed}
    assert refreshed.deltas == tuple(
        replace(delta, control_revision=by_source[delta.source_id].state_revision)
        for delta in frozen.deltas
    )
    assert refreshed.deltas[0].control_revision > frozen.deltas[0].control_revision
    assert [(item.content, item.revision, item.applied_revision) for item in refreshed.sources] == [
        (item.content, item.revision, item.applied_revision) for item in frozen.sources
    ]
    assert all(item.tracked is False for item in refreshed.sources)
    assert manager.observe("skill:debugging", "v3\n").status == "not_tracked"
    manager.refresh_control_states()
    assert manager.prepare_sources() == refreshed
    assert control_state_port.load_context_source_control_states(owner) == committed


def test_seal_cas_sync_rejects_external_tracking_change(
    manager: ContextSourceManager,
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
    seal_sources,
) -> None:
    _track_skill(manager, seal_sources)
    external = ContextSourceManager(owner=owner, control_state_port=control_state_port)
    external.load_skill("debugging", mode="untrack")
    committed = control_state_port.load_context_source_control_states(owner)
    with pytest.raises(RuntimeError, match="seal 确认不得吸收"):
        manager._sync_persisted_control_states({"skill:debugging"})
    assert control_state_port.load_context_source_control_states(owner) == committed
    manager.refresh_control_states()
    assert manager.observe("skill:debugging", "v2\n").status == "not_tracked"


def test_restart_recovery_rebuilds_diff_baseline_from_latest_visible_committed_revision(
    manager: ContextSourceManager,
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
    seal_sources,
) -> None:
    _track_skill(manager, seal_sources)
    rebuilt = _new_manager(owner, control_state_port)

    # 首帧同一 revision 只重建基准；下一 revision 仍按 applied revision 合并 delta。
    assert rebuilt.observe("skill:debugging", "v1\n").status == "baseline_restored"
    assert rebuilt.observe("skill:debugging", "v2\n").status == "accepted"
    (delta,) = seal_sources(rebuilt)[0].deltas

    assert delta.kind == "delta"
    assert delta.previous_revision == V1_REVISION
    assert "-v1" in delta.content
    assert "+v2" in delta.content


@pytest.mark.parametrize("current_body", ["v2\n", ""], ids=["replace", "delete"])
@pytest.mark.parametrize("pending_before_restart", [False, True], ids=["applied", "observed-later"])
def test_cold_baseline_restoration_preserves_control_and_uses_applied_content(
    manager: ContextSourceManager,
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
    seal_sources: Callable,
    current_body: str,
    pending_before_restart: bool,
) -> None:
    _track_skill(manager, seal_sources)
    if pending_before_restart:
        manager.observe("skill:debugging", "uncommitted\n")
    before = control_state_port.load_context_source_control_states(owner)
    rebuilt = _new_manager(owner, control_state_port)
    reads: list[str] = []

    def read_committed(revision: str) -> str:
        reads.append(revision)
        assert revision == V1_REVISION
        return "v1\n"

    rebuilt.restore_committed_baseline("skill:debugging", read_committed)
    assert control_state_port.load_context_source_control_states(owner) == before
    assert rebuilt.prepare_pending() is None
    assert rebuilt.source_observation_state("skill:debugging") == ("tracked", before[0].latest_revision)
    assert rebuilt.observe("skill:debugging", current_body).status == "accepted"
    batch = rebuilt.prepare_pending()
    assert batch is not None
    rebuilt.restore_committed_baseline("skill:debugging", read_committed)
    assert reads == [V1_REVISION]
    assert rebuilt.prepare_pending() == batch
    delta, = batch.deltas
    expected = f"--- debugging@{V1_REVISION}\n+++ debugging@{_revision(current_body)}\n"
    expected += "@@ -1 +1 @@\n-v1\n+v2\n" if current_body else "@@ -1 +0,0 @@\n-v1\n"
    assert delta.content == expected
    assert delta.previous_revision == V1_REVISION
    assert delta.observation_provenance == (_revision(current_body),)
    assert seal_sources(rebuilt)[0].deltas == batch.deltas


def test_different_threads_do_not_share_control_state(
    control_state_port: RolloutCheckpointSaver,
    source_seal_case,
    seal_sources,
) -> None:
    main_owner = source_seal_case.owner
    resolver = get_session_path_resolver(control_state_port._storage.storage.sessions_dir)
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
        child = control.publish_thread_creation_record(key, session_id=main_owner.session_id)
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
    assert rebuilt_child.observe("skill:debugging", "child\n").status == "baseline_restored"
    assert rebuilt_child.observe("skill:debugging", "v1\n").status == "accepted"


def test_restart_recovery_allows_tracked_registration_awaiting_first_observation(source_seal_case):
    # 注册后、首帧观察前重启：tracked + 无 revision 合法，恢复后可继续观察。
    port = source_seal_case.saver
    owner = source_seal_case.owner
    manager = ContextSourceManager(owner=owner, control_state_port=port)
    manager.register(
        ContextSourceDescriptor(
            source_id="workspace:rules",
            source_kind="workspace_rules",
            name="RULES.md",
            description="工作区指令",
            internal_locator="boxteam://workspace/test/resources/files/root/RULES.md",
            resource_uri="boxteam://workspace/test/resources/files/root/RULES.md",
        ),
        tracking_status="tracked",
    )

    rebuilt = ContextSourceManager(owner=owner, control_state_port=port)
    status, latest = rebuilt.source_observation_state("workspace:rules")
    assert status == "tracked"
    assert latest is None
    assert rebuilt.observe("workspace:rules", "v1\n").status == "accepted"


@pytest.mark.parametrize("operation", ["register", "track", "activate_skill"])
def test_competing_skill_tracking_has_one_durable_winner(source_seal_case, operation: ContextSourceControlOperation):
    """不同 source 行不能绕过同一 owner/name 的唯一 tracking；冲突方保持原控制行。"""
    port, owner = source_seal_case.saver, source_seal_case.owner
    candidates = [ContextSourceControlState(
        owner=owner, source_id=f"skill:{index}", source_kind="skill", name=name,
        binding_revision=None, tracking_status="tracked",
        latest_revision=f"revision-{index}", latest_visible_committed_revision=None,
    ) for index, name in enumerate(("demo", " demo "))]
    if operation == "track":
        candidates = [replace(port.apply_context_source_control_mutation(ContextSourceControlMutation(
            "register", replace(state, tracking_status="untracked"),
        )).state, tracking_status="tracked") for state in candidates]
    before = {state.source_id: state for state in port.load_context_source_control_states(owner)}
    barrier = threading.Barrier(2)

    def track(state: ContextSourceControlState) -> ContextSourceControlState | ContextSourceTrackingStateConflict:
        barrier.wait(timeout=5)
        try:
            return port.apply_context_source_control_mutation(ContextSourceControlMutation(operation, state)).state
        except ContextSourceTrackingStateConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=10) for future in [pool.submit(track, state) for state in candidates]]
    winners = [state for state in results if isinstance(state, ContextSourceControlState)]
    conflicts = [state for state in results if isinstance(state, ContextSourceTrackingStateConflict)]
    assert len(winners) == len(conflicts) == 1
    assert conflicts[0].code == "tracking-state-conflict"
    states = {state.source_id: state for state in port.load_context_source_control_states(owner)}
    loser = next(state for state in candidates if state.source_id != winners[0].source_id)
    assert states[winners[0].source_id] == winners[0]
    assert states.get(loser.source_id) == before.get(loser.source_id)
    assert [state.source_id for state in states.values() if state.tracking_status == "tracked"] == [winners[0].source_id]
    # 冷 manager 必须能恢复唯一 winner，而不是在下一次调用才发现脏 tracking。
    restored = source_seal_case.manager()
    assert restored.source_observation_state(winners[0].source_id)[0] == "tracked"
