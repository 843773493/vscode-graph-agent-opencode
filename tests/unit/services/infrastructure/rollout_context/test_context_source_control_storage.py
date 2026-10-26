"""CSM 控制状态在唯一 RolloutCheckpointSaver/ContextStore owner 上的持久化测试。

覆盖持久化 round-trip、重启恢复后的不重复注入、untrack 状态保留、
main/child thread 隔离，以及幂等写入与乐观版本冲突。
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure.rollout_context.checkpoint.durable.context_source_control import (
    ContextSourceControlStorageMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_source_control_port import (
    ContextSourceControlOwnerMixin,
)
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
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
    _revision,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    ROLLOUT_SCHEMA_VERSION,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.unit.services.infrastructure.rollout_context.conftest import SourceSealCase

SESSION_ID = "ses_019c3dbcb2f6749b85bed7b51dfe889d"
CHILD_SESSION_ID = "ses_019c52c6b01c7733850a1aaf64ac579e"


def _create_session_node(
    sessions_root: Path,
    session_id: str,
    *,
    parent_node_id: str | None = None,
) -> None:
    seed_catalog_session_bundle(
        sessions_root,
        session_id,
        title="控制状态测试",
        parent_node_id=parent_node_id,
    )


def _descriptor() -> ContextSourceDescriptor:
    return ContextSourceDescriptor(
        source_id="skill:debugging",
        source_kind="skill",
        name="debugging",
        description="调试工作流",
        internal_locator="/.boxteam/skills/debugging/SKILL.md",
    )


def _new_manager(
    owner: ContextSourceOwnerKey,
    port: ContextSourceControlOwnerMixin,
) -> ContextSourceManager:
    manager = ContextSourceManager(owner=owner, control_state_port=port)
    manager.register(_descriptor())
    return manager


def _track_skill(manager: ContextSourceManager, seal_sources: Callable, content: str = "v1\n") -> None:
    manager.activate_skill_content("debugging", content)
    _install_debugging_snapshot(manager, content)
    manager.load_skill("debugging", mode="tracked")
    batch = manager.prepare_pending()
    assert batch is not None
    seal_sources(manager)


def _install_debugging_snapshot(
    manager: ContextSourceManager,
    content: str = "v1\n",
) -> None:
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


@pytest.fixture
def sessions_root(tmp_path: Path) -> Path:
    """建立隔离的 sessions 根目录，并注册 main session 与 child thread 节点。

    R18 catalog 模式适配：节点构造经 path_utils 工厂走 SQLite catalog
    链，直连非 catalog 构造会被 fail closed，后续 RolloutContextStore 解析即被拒。
    """
    root = tmp_path / ".boxteam" / "sessions"
    root.mkdir(parents=True)
    _create_session_node(root, SESSION_ID)
    _create_session_node(root, CHILD_SESSION_ID, parent_node_id=SESSION_ID)
    return root


@pytest.fixture
def rollout_storage(sessions_root: Path) -> RolloutContextStore:
    return RolloutContextStore(sessions_root, message_codec=LangChainMessageCodec())


@pytest.fixture
def control_state_port(
    sessions_root: Path,
    rollout_storage: RolloutContextStore,
) -> RolloutCheckpointSaver:
    """唯一 owner 端口：生产 CSM 只会拿到这个 Saver 实例。"""
    return RolloutCheckpointSaver(sessions_root, storage=rollout_storage)


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
                accepted_ingress_id="control-ingress:" + owner.thread_id,
                acceptance_idempotency_key="control-accept:" + owner.thread_id,
                job_id="control-job:" + owner.thread_id,
                admission_idempotency_key="control-admission:" + owner.thread_id,
                payload="控制状态真实 seal 合同",
            )
            cases[owner] = SourceSealCase(
                control_state_port, owner, accepted, frozen_provider_wire, frozen_provider_activation,
            )
        return cases[owner].seal(manager)

    return seal


@pytest.fixture
def owner(rollout_storage: RolloutContextStore) -> ContextSourceOwnerKey:
    return ContextSourceOwnerKey(
        session_id=SESSION_ID,
        thread_id=rollout_storage.storage.main_thread_id(SESSION_ID),
    )


def test_storage_and_saver_mixins_are_wired_into_unique_owners(
    rollout_storage: RolloutContextStore,
    control_state_port: RolloutCheckpointSaver,
) -> None:
    assert isinstance(rollout_storage.checkpoints, ContextSourceControlStorageMixin)
    assert isinstance(control_state_port, ContextSourceControlOwnerMixin)
    assert isinstance(control_state_port, RolloutCheckpointSaver)


def test_control_state_round_trip_persists_through_owner(
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
    seal_sources,
) -> None:
    manager = _new_manager(owner, control_state_port)
    _track_skill(manager, seal_sources)

    (stored,) = control_state_port.load_context_source_control_states(owner)

    assert stored.owner == owner
    assert stored.source_id == "skill:debugging"
    assert stored.source_kind == "skill"
    assert stored.name == "debugging"
    assert stored.tracking_status == "tracked"
    assert stored.latest_visible_committed_revision == stored.latest_revision
    assert stored.state_revision == 3
    assert stored.updated_at is not None


def test_restart_recovery_does_not_reinject_tracked_source(
    rollout_storage: RolloutContextStore,
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
    seal_sources,
) -> None:
    _track_skill(_new_manager(owner, control_state_port), seal_sources)

    # 重启后的新 CSM 只依赖 owner 端口恢复，不看进程内 cache。
    rebuilt = _new_manager(owner, control_state_port)

    assert rebuilt.observe("skill:debugging", "v1\n").status == "baseline_restored"
    assert rebuilt.prepare_pending() is None
    assert rebuilt.metadata() == ({"name": "debugging", "description": "调试工作流"},)
    # 新 storage 实例读回同一事实，证明状态在 SQLite 而不是内存。
    fresh_storage = RolloutContextStore(rollout_storage.storage.sessions_dir)
    (stored,) = fresh_storage.checkpoints.read_context_source_control_states(
        SESSION_ID,
        thread_id=owner.thread_id,
    )
    assert stored.tracking_status == "tracked"
    assert stored.latest_visible_committed_revision == stored.latest_revision


def test_restart_recovery_keeps_untrack_state(
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
    seal_sources,
) -> None:
    manager = _new_manager(owner, control_state_port)
    _track_skill(manager, seal_sources)
    manager.load_skill("debugging", mode="untrack")

    rebuilt = _new_manager(owner, control_state_port)
    (stored,) = control_state_port.load_context_source_control_states(owner)
    _install_debugging_snapshot(rebuilt)

    assert stored.tracking_status == "untracked"
    assert rebuilt.load_skill("debugging").tracked is False
    assert rebuilt.observe("skill:debugging", "v2\n").status == "not_tracked"
    assert rebuilt.prepare_pending() is None


def test_restart_recovery_keeps_threads_isolated(
    rollout_storage: RolloutContextStore,
    control_state_port: RolloutCheckpointSaver,
    seal_sources,
) -> None:
    main_owner = ContextSourceOwnerKey(
        session_id=SESSION_ID,
        thread_id=rollout_storage.storage.main_thread_id(SESSION_ID),
    )
    child_owner = ContextSourceOwnerKey(
        session_id=CHILD_SESSION_ID,
        thread_id=rollout_storage.storage.main_thread_id(CHILD_SESSION_ID),
    )
    main = _new_manager(main_owner, control_state_port)
    _track_skill(main, seal_sources)

    child = ContextSourceManager(
        owner=child_owner, control_state_port=control_state_port
    )
    assert child.metadata() == ()
    child.register(_descriptor())
    _track_skill(child, seal_sources, "child\n")

    (main_state,) = control_state_port.load_context_source_control_states(main_owner)
    (child_state,) = control_state_port.load_context_source_control_states(child_owner)

    assert (
        main_state.latest_visible_committed_revision
        != child_state.latest_visible_committed_revision
    )
    assert main_state.tracking_status == child_state.tracking_status == "tracked"
    rebuilt_child = _new_manager(child_owner, control_state_port)
    assert rebuilt_child.observe("skill:debugging", "child\n").status == "baseline_restored"
    assert rebuilt_child.observe("skill:debugging", "v1\n").status == "accepted"


def test_owner_rejects_bare_session_id_as_thread(
    rollout_storage: RolloutContextStore,
) -> None:
    with pytest.raises(RuntimeError, match="拒绝 thread alias"):
        rollout_storage.checkpoints.read_context_source_control_states(
            SESSION_ID,
            thread_id=SESSION_ID,
        )
    with pytest.raises(RuntimeError, match="拒绝 thread alias"):
        rollout_storage.checkpoints.read_context_source_control_states(
            SESSION_ID,
            thread_id="main",
        )


def test_owner_availability_matches_authoritative_index(
    rollout_storage: RolloutContextStore,
    control_state_port: RolloutCheckpointSaver,
) -> None:
    assert rollout_storage.checkpoints.context_source_control_owner_available(
        SESSION_ID,
        thread_id=rollout_storage.storage.main_thread_id(SESSION_ID),
    )
    assert rollout_storage.checkpoints.context_source_control_owner_available(
        CHILD_SESSION_ID,
        thread_id=rollout_storage.storage.main_thread_id(CHILD_SESSION_ID),
    )
    assert not rollout_storage.checkpoints.context_source_control_owner_available(
        "tools_inspection_session",
        thread_id=rollout_storage.storage.main_thread_id(SESSION_ID),
    )
    assert not control_state_port.context_source_control_owner_available(
        ContextSourceOwnerKey(
            session_id="tools_inspection_session",
            thread_id=rollout_storage.storage.main_thread_id(SESSION_ID),
        )
    )


def test_synthetic_session_has_no_restorable_control_state(
    rollout_storage: RolloutContextStore,
    control_state_port: RolloutCheckpointSaver,
) -> None:
    """合成 session 没有 ContextStore：恢复短路返回空，而不是解析失败。"""
    owner = ContextSourceOwnerKey(
        session_id="tools_inspection_session",
        thread_id=rollout_storage.storage.main_thread_id(SESSION_ID),
    )

    assert (
        rollout_storage.checkpoints.read_context_source_control_states(
            "tools_inspection_session",
            thread_id=owner.thread_id,
        )
        == ()
    )
    assert control_state_port.load_context_source_control_states(owner) == ()
    assert rollout_storage.checkpoints.read_context_source_rebuild_rows(
        owner.session_id, owner.thread_id, checkpoint_ns="",
    ) is None
    assert control_state_port.load_context_source_rebuild_state(owner) is None
    manager = ContextSourceManager(owner=owner, control_state_port=control_state_port)
    manager.register(_descriptor())
    assert manager.metadata() == ({"name": "debugging", "description": "调试工作流"},)
    # 写入仍必须显式失败：没有 durable owner 时不允许伪造持久化成功。
    with pytest.raises(RuntimeError, match="不是 canonical Session 身份"):
        rollout_storage.checkpoints.apply_context_source_control_mutation(
            ContextSourceControlMutation("register", ContextSourceControlState(
                owner=owner,
                source_id="skill:debugging",
                source_kind="skill",
                name="debugging",
                binding_revision=None,
                tracking_status="tracked",
                latest_visible_committed_revision=None,
                latest_revision="sha256:latest",
            ))
        )


def test_read_short_circuits_for_owner_outside_authoritative_index(
    rollout_storage: RolloutContextStore,
) -> None:
    assert (
        rollout_storage.checkpoints.read_context_source_control_states(
            "ses_019c1b918afd7857877ba5414bddf667",
            thread_id=rollout_storage.storage.main_thread_id(SESSION_ID),
        )
        == ()
    )
    # 已登记 Session 下缺失 Thread 必须 fail closed，不能短路成空控制状态。
    with pytest.raises(RuntimeError, match="catalog 中不存在精确 SessionThread owner"):
        rollout_storage.checkpoints.read_context_source_control_states(
            SESSION_ID,
            thread_id="thr_019bd65d75ea759187d180cdef62d7ec",
        )


def test_read_fails_closed_when_indexed_node_directory_is_missing(
    sessions_root: Path,
    rollout_storage: RolloutContextStore,
) -> None:
    """索引已登记但物理节点目录被删除时，读取必须 fail-closed 而不是短路返回空。"""
    resolver = get_session_path_resolver(sessions_root)
    session_dir = resolver.resolve_session_node(SESSION_ID)
    shutil.rmtree(session_dir)

    # 防篡改收敛到物理解析点（fail closed），不扫盘比对。
    with pytest.raises(RuntimeError, match="会话物理目录缺失"):
        rollout_storage.checkpoints.read_context_source_control_states(
            SESSION_ID,
            thread_id=rollout_storage.storage.main_thread_id(SESSION_ID),
        )


def test_owner_rejects_thread_from_other_session(
    rollout_storage: RolloutContextStore,
) -> None:
    # canonical Thread ID 不能跨 Session 重用。
    with pytest.raises(RuntimeError, match="catalog 中不存在精确 SessionThread owner"):
        rollout_storage.checkpoints.read_context_source_control_states(
            CHILD_SESSION_ID,
            thread_id=rollout_storage.storage.main_thread_id(SESSION_ID),
        )


def test_owner_rejects_non_canonical_thread_id_shape(
    rollout_storage: RolloutContextStore,
) -> None:
    """非 ses_ 前缀的非法 thread id 必须落在 canonical 形态验证器上。

    这是「越界 thread」之外的另一条真实拒绝路径：它既不是可寻址 session
    节点，也不是合法 canonical thread id，最终由 validate_thread_id 拒绝。
    """
    with pytest.raises(RuntimeError, match="不是 canonical Thread 身份"):
        rollout_storage.checkpoints.read_context_source_control_states(
            SESSION_ID,
            thread_id="../escape",
        )
    # 非法但形态近似 canonical 的短 thr_ id 同样必须显式拒绝，不能短路成空。
    with pytest.raises(RuntimeError, match="不是 canonical Thread 身份"):
        rollout_storage.checkpoints.read_context_source_control_states(
            SESSION_ID,
            thread_id="thr_short",
        )
    # 未登记的合法 canonical thread id 不得被当作「没有控制状态」短路。
    with pytest.raises(RuntimeError, match="catalog 中不存在精确 SessionThread owner"):
        rollout_storage.checkpoints.read_context_source_control_states(
            SESSION_ID,
            thread_id="thr_019bd65d75ea759187d180cdef62d7ec",
        )


def test_write_is_idempotent_and_does_not_advance_state_revision(
    rollout_storage: RolloutContextStore,
    owner: ContextSourceOwnerKey,
) -> None:
    state = ContextSourceControlState(
        owner=owner,
        source_id="skill:debugging",
        source_kind="skill",
        name="debugging",
        binding_revision=None,
        tracking_status="tracked",
        latest_visible_committed_revision=None,
        latest_revision="sha256:latest",
    )

    first = rollout_storage.checkpoints.apply_context_source_control_mutation(ContextSourceControlMutation("register", state)).state
    second = rollout_storage.checkpoints.apply_context_source_control_mutation(ContextSourceControlMutation("register", state)).state

    assert first.state_revision == 1
    assert second == first


@pytest.mark.parametrize("operation", ["register", "observe", "track", "untrack"])
@pytest.mark.parametrize("existing", [False, True])
def test_control_mutation_cannot_publish_an_applied_revision(
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
    operation: ContextSourceControlOperation,
    existing: bool,
) -> None:
    state = ContextSourceControlState(
        owner=owner, source_id="instruction:agent", source_kind="agent_instructions",
        name="Agent 说明", binding_revision=None, tracking_status="untracked",
        latest_revision="revision-1", latest_visible_committed_revision=None,
    )
    if existing:
        state = control_state_port.apply_context_source_control_mutation(
            ContextSourceControlMutation("register", state),
        ).state
    before = control_state_port.load_context_source_control_states(owner)
    forged = replace(
        state, latest_visible_committed_revision=state.latest_revision,
        tracking_status="tracked" if operation == "track" else "untracked",
    )
    with pytest.raises(RuntimeError, match="applied 只能由 assembly seal 推进"):
        control_state_port.apply_context_source_control_mutation(
            ContextSourceControlMutation(operation, forged),
        )
    assert control_state_port.load_context_source_control_states(owner) == before


def test_control_mutations_keep_observation_and_tracking_separate(
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
) -> None:
    state = control_state_port.apply_context_source_control_mutation(
        ContextSourceControlMutation("register", ContextSourceControlState(
            owner=owner, source_id="instruction:agent", source_kind="agent_instructions",
            name="Agent 说明", binding_revision="catalog-1", tracking_status="tracked",
            latest_revision="revision-1", latest_visible_committed_revision=None,
        )),
    ).state
    changes = (
        ("observe", replace(state, tracking_status="untracked")),
        ("track", replace(state, latest_revision="revision-2")),
        ("observe", replace(state, binding_revision="catalog-2")),
        ("register", replace(state, name="另一个来源")),
    )
    for operation, requested in changes:
        with pytest.raises(RuntimeError, match="context-source-control-state-conflict"):
            control_state_port.apply_context_source_control_mutation(
                ContextSourceControlMutation(operation, requested),
            )
        assert control_state_port.load_context_source_control_states(owner) == (state,)
    observed = control_state_port.apply_context_source_control_mutation(
        ContextSourceControlMutation("observe", replace(state, latest_revision="revision-2")),
    ).state
    untracked = control_state_port.apply_context_source_control_mutation(
        ContextSourceControlMutation("untrack", replace(observed, tracking_status="untracked")),
    ).state
    assert untracked.latest_revision == "revision-2"
    assert untracked.latest_visible_committed_revision is None
    assert untracked.state_revision == state.state_revision + 2


def test_concurrent_control_mutations_share_one_cas_baseline(
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
) -> None:
    state = ContextSourceControlState(
        owner=owner, source_id="instruction:agent", source_kind="agent_instructions",
        name="Agent 说明", binding_revision=None, tracking_status="tracked",
        latest_revision="revision-1", latest_visible_committed_revision=None,
    )
    # 预先物化库，把竞争限制在控制事务，不混入首次建库。
    control_state_port._storage.storage.initialize(owner.session_id, "", thread_id=owner.thread_id)
    barrier = threading.Barrier(2, timeout=5)

    def register() -> ContextSourceControlState:
        barrier.wait()
        return control_state_port.apply_context_source_control_mutation(
            ContextSourceControlMutation("register", state),
        ).state

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(register) for _ in range(2)]
        first, second = [future.result(timeout=5) for future in futures]
    assert first == second
    assert first.state_revision == 1

    def observe(revision: str) -> ContextSourceControlState | RuntimeError:
        barrier.wait()
        try:
            return control_state_port.apply_context_source_control_mutation(
                ContextSourceControlMutation("observe", replace(first, latest_revision=revision)),
            ).state
        except RuntimeError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(observe, revision) for revision in ("revision-2", "revision-3")]
        results = [future.result(timeout=5) for future in futures]
    winners = [result for result in results if isinstance(result, ContextSourceControlState)]
    failures = [result for result in results if isinstance(result, RuntimeError)]
    assert len(winners) == len(failures) == 1
    assert "context-source-control-state-conflict" in str(failures[0])
    assert winners[0].state_revision == 2
    assert control_state_port.load_context_source_control_states(owner) == (winners[0],)


def test_rebind_receipt_replay_requires_both_exact_cas_versions(control_state_port, owner):
    old = control_state_port.apply_context_source_control_mutation(ContextSourceControlMutation(
        "register", ContextSourceControlState(
            owner=owner, source_id="skill:old", source_kind="skill", name="demo",
            binding_revision="old-catalog", tracking_status="tracked",
            latest_revision="old-revision", latest_visible_committed_revision=None,
        ),
    )).state
    target = ContextSourceControlState(
        owner=owner, source_id="skill:new", source_kind="skill", name="demo",
        binding_revision="new-catalog", tracking_status="tracked",
        latest_revision="new-revision", latest_visible_committed_revision=None,
    )
    mutation = ContextSourceControlMutation("track", target, replace(old, tracking_status="untracked"))
    receipt = control_state_port.apply_context_source_control_mutation(mutation)
    assert receipt.replaced_state == replace(
        old, tracking_status="untracked", state_revision=old.state_revision + 1,
        updated_at=receipt.replaced_state.updated_at,
    )
    assert control_state_port.apply_context_source_control_mutation(mutation) == receipt
    before = control_state_port.load_context_source_control_states(owner)
    for bad in (
        replace(mutation, state=replace(target, state_revision=100)),
        replace(mutation, replaced_state=replace(mutation.replaced_state, state_revision=100)),
        replace(mutation, replaced_state=replace(mutation.replaced_state, latest_revision="forged")),
        replace(mutation, state=replace(target, latest_visible_committed_revision="forged")),
    ):
        with pytest.raises(RuntimeError, match="context-source-control-state-conflict"):
            control_state_port.apply_context_source_control_mutation(bad)
        assert control_state_port.load_context_source_control_states(owner) == before


def test_write_rejects_stale_state_revision(
    rollout_storage: RolloutContextStore,
    owner: ContextSourceOwnerKey,
) -> None:
    state = ContextSourceControlState(
        owner=owner,
        source_id="skill:debugging",
        source_kind="skill",
        name="debugging",
        binding_revision=None,
        tracking_status="tracked",
        latest_visible_committed_revision=None,
        latest_revision="sha256:latest",
    )
    stored = rollout_storage.checkpoints.apply_context_source_control_mutation(ContextSourceControlMutation("register", state)).state

    with pytest.raises(RuntimeError, match="context-source-control-state-conflict"):
        rollout_storage.checkpoints.apply_context_source_control_mutation(
            ContextSourceControlMutation("untrack", ContextSourceControlState(
                owner=owner,
                source_id="skill:debugging",
                source_kind="skill",
                name="debugging",
                binding_revision=None,
                tracking_status="untracked",
                latest_visible_committed_revision=None,
                latest_revision="sha256:latest",
                state_revision=stored.state_revision - 1,
            ))
        )


def test_write_does_not_reconcile_uncommitted_jsonl_tail(
    rollout_storage: RolloutContextStore,
    owner: ContextSourceOwnerKey,
) -> None:
    """控制状态写入不得走 initialize() 的尾部回收，否则会截断在途 Turn。"""
    rollout_storage.storage.initialize(SESSION_ID, "")
    jsonl_path = rollout_storage.storage.jsonl_path(SESSION_ID, "")
    with jsonl_path.open("ab") as stream:
        stream.write(b'{"in_flight_item": true}\n')
    size_before = jsonl_path.stat().st_size
    assert size_before > 0

    rollout_storage.checkpoints.apply_context_source_control_mutation(
        ContextSourceControlMutation("register", ContextSourceControlState(
            owner=owner,
            source_id="skill:debugging",
            source_kind="skill",
            name="debugging",
            binding_revision=None,
            tracking_status="tracked",
            latest_visible_committed_revision=None,
            latest_revision="sha256:latest",
        ))
    )

    assert jsonl_path.stat().st_size == size_before
    assert (
        rollout_storage.checkpoints.read_context_source_control_states(
            SESSION_ID,
            thread_id=owner.thread_id,
        )
        != ()
    )


def test_write_does_not_take_rollout_file_lock(
    rollout_storage: RolloutContextStore,
    owner: ContextSourceOwnerKey,
) -> None:
    """控制状态写入不得与 canonical writer 争用同一个 rollout 文件锁。

    该锁同时保护 canonical 提交；在 Turn 进行中取锁会让 item 提交顺序倒置，
    破坏 itemized 投影的 tool protocol closure。此处由另一线程持有锁，写入
    必须立即完成而不是等待锁超时。
    """
    rollout_storage.storage.initialize(SESSION_ID, "")
    lock = rollout_storage.storage._lock(
        rollout_storage.storage.main_thread_id(SESSION_ID), "", session_id=SESSION_ID
    )
    holder_ready = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with lock:
            holder_ready.set()
            release.wait(timeout=30)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert holder_ready.wait(timeout=10)
        stored = rollout_storage.checkpoints.apply_context_source_control_mutation(
            ContextSourceControlMutation("register", ContextSourceControlState(
                owner=owner,
                source_id="skill:debugging",
                source_kind="skill",
                name="debugging",
                binding_revision=None,
                tracking_status="tracked",
                latest_visible_committed_revision=None,
                latest_revision="sha256:latest",
            ))
        ).state
        assert stored.state_revision == 1
    finally:
        release.set()
        holder.join(timeout=10)
    assert not holder.is_alive()


def test_previous_schema_without_checkpoint_source_assembly_is_rejected(
    rollout_storage: RolloutContextStore,
    owner: ContextSourceOwnerKey,
) -> None:
    """旧 Schema 11 在任何来源读写前拒绝，禁止按当前 DDL 隐式补列。"""
    rollout_storage.storage.initialize(owner.session_id, "", thread_id=owner.thread_id)
    index_path = rollout_storage.storage.index_path(owner.session_id, "", thread_id=owner.thread_id)
    with closing(sqlite3.connect(index_path)) as connection, connection:
        connection.execute("ALTER TABLE checkpoint_context_source_snapshots DROP COLUMN source_assembly_id")
        connection.execute("UPDATE database_meta SET schema_version = 11")
    before = index_path.read_bytes()
    expected = f"current=11, target={ROLLOUT_SCHEMA_VERSION}"
    with pytest.raises(RuntimeError, match="schema-incompatible") as read_error:
        rollout_storage.checkpoints.read_context_source_control_states(owner.session_id, thread_id=owner.thread_id)
    assert expected in str(read_error.value)
    with pytest.raises(RuntimeError, match="schema-incompatible") as rebuild_error:
        rollout_storage.checkpoints.read_context_source_rebuild_rows(owner.session_id, owner.thread_id, checkpoint_ns="")
    assert expected in str(rebuild_error.value)
    with pytest.raises(RuntimeError, match="schema-incompatible") as write_error:
        rollout_storage.checkpoints.apply_context_source_control_mutation(ContextSourceControlMutation("register", ContextSourceControlState(
            owner=owner, source_id="skill:debugging", source_kind="skill", name="debugging",
            binding_revision=None, tracking_status="tracked",
            latest_visible_committed_revision=None, latest_revision="sha256:latest",
        )))
    assert expected in str(write_error.value)
    assert index_path.read_bytes() == before
    with closing(sqlite3.connect(index_path)) as connection:
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (11,)
        assert "source_assembly_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(checkpoint_context_source_snapshots)")
        }


def test_current_rollout_without_control_table_is_rejected_without_repair(
    rollout_storage: RolloutContextStore,
    control_state_port: RolloutCheckpointSaver,
    owner: ContextSourceOwnerKey,
) -> None:
    """当前版本缺少来源表是损坏，读写都必须失败且不能静默补表。"""
    rollout_storage.storage.initialize(SESSION_ID, "")
    index_path = rollout_storage.storage.index_path(SESSION_ID, "")
    with closing(sqlite3.connect(index_path)) as connection:
        assert connection.execute("SELECT count(*) FROM context_epoch_transitions").fetchone() == (0,)
        connection.execute("DROP TABLE context_source_control_states")
        connection.commit()

    before = index_path.read_bytes()
    with pytest.raises(sqlite3.OperationalError, match="no such table: context_source_control_states"):
        rollout_storage.checkpoints.read_context_source_control_states(
            SESSION_ID,
            thread_id=owner.thread_id,
        )
    with pytest.raises(sqlite3.OperationalError, match="no such table: context_source_control_states"):
        rollout_storage.checkpoints.read_context_source_rebuild_rows(
            SESSION_ID, owner.thread_id, checkpoint_ns="",
        )
    with pytest.raises(sqlite3.OperationalError, match="no such table: context_source_control_states"):
        control_state_port.load_context_source_rebuild_state(owner)
    with pytest.raises(sqlite3.OperationalError, match="no such table: context_source_control_states"):
        rollout_storage.checkpoints.apply_context_source_control_mutation(
            ContextSourceControlMutation("register", ContextSourceControlState(
                owner=owner,
                source_id="skill:debugging",
                source_kind="skill",
                name="debugging",
                binding_revision=None,
                tracking_status="tracked",
                latest_visible_committed_revision=None,
                latest_revision="sha256:latest",
            ))
        )
    assert index_path.read_bytes() == before
    with closing(sqlite3.connect(index_path)) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_source_control_states'"
        ).fetchone() is None
