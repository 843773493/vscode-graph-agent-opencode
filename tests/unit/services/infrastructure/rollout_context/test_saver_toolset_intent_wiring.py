"""5.4：真实 Saver seal 是 thread applied ToolSet 的唯一提交边界。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.agents.graph_binding import DEEP_AGENT_GRAPH_BINDING
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_prefixed_id, uuid7_datetime_from_hex
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)

SESSION_ID = "ses_019c38b8b88476a58ce3735618d44bc8"

TOOLS_A = (
    {"type": "function", "function": {"name": "get_goal", "parameters": {}}},
)
TOOLS_B = (
    {"type": "function", "function": {"name": "read_file", "parameters": {}}},
)


def _desired_key(tools: tuple[dict[str, object], ...]) -> str:
    return sha256_jcs({"tools": [dict(tool) for tool in tools]})


@pytest.fixture
def saver(tmp_path: Path, session_bundle_factory) -> RolloutCheckpointSaver:
    session_bundle_factory(tmp_path, SESSION_ID)
    return RolloutCheckpointSaver(sessions_dir=tmp_path)


def _main_thread_id(saver: RolloutCheckpointSaver) -> str:
    return saver._storage._resolve_thread_identity(SESSION_ID, None)


@pytest.fixture
def sealed_toolset_context(frozen_provider_activation, saver, frozen_provider_wire):
    """只包装真实 ingress/admission/prepare，不代写 owner 或 assembly。"""
    @contextmanager
    def enter(*, thread_id=None, name="toolset-contract", accepted=None):
        thread = thread_id or _main_thread_id(saver)
        if accepted is None:
            accepted = saver.accept_turn(
                SESSION_ID, thread_id=thread, accepted_ingress_id=f"{name}-ingress",
                acceptance_idempotency_key=f"{name}-root", job_id=f"{name}-job",
                admission_idempotency_key=f"{name}-admission", payload="工具集边界", payload_kind="text",
            )
        manager = ContextSourceManager(
            owner=ContextSourceOwnerKey(SESSION_ID, thread), control_state_port=saver,
        )
        admission = saver.execution_admission(
            SESSION_ID, thread_id=thread, execution_id=accepted["initial_execution_id"],
        )

        def prepare(tool_set, *, key, supplier=None):
            result = saver.prepare_context_for_provider(
                SESSION_ID, thread, turn_id=accepted["turn_id"],
                execution_id=accepted["initial_execution_id"],
                **frozen_provider_wire("toolset-provider", "responses"),
                context_source_manager=manager,
                tool_set_supplier=supplier if supplier is not None else lambda: tool_set,
                plan_creation_idempotency_key=f"{name}-create-{key}",
                seal_idempotency_key=f"{name}-seal-{key}",
                **frozen_provider_activation(saver, plan_creation_idempotency_key=f'{name}-create-{key}'),
            )
            return result, saver.get_context_assembly(SESSION_ID, thread, assembly_id=result["assembly_id"])

        with (
            session_thread_owner_scope(MutationIntentOwner(SESSION_ID, thread), langgraph_thread_id=thread),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            yield thread, accepted, prepare, manager

    return enter


def test_successful_seal_advances_only_manifest_changes(sealed_toolset_context, saver) -> None:
    """新 call 的同 manifest 复用 epoch，变化只在成功 seal 后 applied。"""
    tools_a = ToolSetSnapshot(selection_revision=_desired_key(TOOLS_A), tools=TOOLS_A)
    same_manifest = ToolSetSnapshot(selection_revision=sha256_jcs({"hidden": False}), tools=TOOLS_A)
    tools_b = ToolSetSnapshot(selection_revision=_desired_key(TOOLS_B), tools=TOOLS_B)
    with sealed_toolset_context() as (thread, accepted, prepare, _manager):
        _, first = prepare(tools_a, key="a")
        _, unchanged = prepare(same_manifest, key="same")
        assert first.seal_binding.applied_toolset_revision == tools_a.revision
        assert first.seal_binding.tool_set_snapshot_id == tools_a.snapshot_id
        assert unchanged.seal_binding.applied_toolset_revision == tools_a.revision
        assert unchanged.seal_binding.selection_revision == same_manifest.selection_revision
        assert unchanged.seal_binding.prefix.state.epoch.ordinal == 1
        _, changed = prepare(tools_b, key="b")
        assert changed.seal_binding.applied_toolset_revision == tools_b.revision
        assert changed.seal_binding.tool_set_snapshot_id == tools_b.snapshot_id
        assert changed.seal_binding.prefix.state.epoch.ordinal == 2
        assert changed.seal_binding.previous_assembly_id == unchanged.assembly_id
        assert first.turn_id == unchanged.turn_id == changed.turn_id == accepted["turn_id"]
        assert saver.list_context_assemblies(SESSION_ID, thread) == (first, unchanged, changed)


def test_prepare_boundary_switches_and_sealed_retry_keeps_applied(frozen_provider_activation,
    frozen_provider_wire, saver: RolloutCheckpointSaver,
) -> None:
    thread_id = _main_thread_id(saver)
    accepted = saver.accept_turn(
        SESSION_ID,
        thread_id=thread_id,
        accepted_ingress_id="toolset-ingress",
        acceptance_idempotency_key="toolset-root",
        job_id="toolset-job",
        admission_idempotency_key="toolset-admission",
        payload="切换工具集",
        payload_kind="text",
    )
    tools_a = ToolSetSnapshot(selection_revision=_desired_key(TOOLS_A), tools=TOOLS_A)
    tools_b = ToolSetSnapshot(selection_revision=_desired_key(TOOLS_B), tools=TOOLS_B)
    options = {
        "turn_id": accepted["turn_id"],
        "execution_id": accepted["initial_execution_id"],
        **frozen_provider_wire("toolset-provider", "responses"),
        "context_source_manager": ContextSourceManager(
            owner=ContextSourceOwnerKey(SESSION_ID, thread_id),
            control_state_port=saver,
        ),
        "tool_set_supplier": lambda: tools_a,
        "plan_creation_idempotency_key": "toolset-create",
        "seal_idempotency_key": "toolset-seal",
    }
    admission = saver.execution_admission(
        SESSION_ID, thread_id=thread_id,
        execution_id=accepted["initial_execution_id"],
    )
    owner = MutationIntentOwner(session_id=SESSION_ID, thread_id=thread_id)
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        first = saver.prepare_context_for_provider(SESSION_ID, thread_id, **options, **frozen_provider_activation(saver, plan_creation_idempotency_key=(options)['plan_creation_idempotency_key']))
        first_snapshot = saver._storage.get_context_assembly(
            SESSION_ID, thread_id, assembly_id=first["assembly_id"],
        )
        binding = first_snapshot.seal_binding
        assert binding.applied_toolset_revision == tools_a.revision
        assert binding.tool_set_snapshot_id == tools_a.snapshot_id
        assert binding.prefix.state.epoch.ordinal == 1

        # 下一次真实 prepare/seal 才使 B 成为 applied，旧 A 请求仍归原 assembly。
        second = saver.prepare_context_for_provider(
            SESSION_ID, thread_id,
            **{**options,
               "tool_set_supplier": lambda: tools_b,
               "plan_creation_idempotency_key": "toolset-create-b",
               "seal_idempotency_key": "toolset-seal-b"},
            **frozen_provider_activation(saver, plan_creation_idempotency_key=({**options, 'tool_set_supplier': lambda: tools_b, 'plan_creation_idempotency_key': 'toolset-create-b', 'seal_idempotency_key': 'toolset-seal-b'})['plan_creation_idempotency_key']),
        )
        second_snapshot = saver._storage.get_context_assembly(
            SESSION_ID, thread_id, assembly_id=second["assembly_id"],
        )
        assert second_snapshot.seal_binding.applied_toolset_revision == tools_b.revision
        assert second_snapshot.seal_binding.tool_set_snapshot_id == tools_b.snapshot_id
        assert second_snapshot.seal_binding.prefix.state.epoch.ordinal == 2
        assert second_snapshot.seal_binding.previous_assembly_id == first["assembly_id"]

        def refuse_supplier():
            pytest.fail("已 sealed 的重试不得重读 ToolSet desired")

        retry = saver.prepare_context_for_provider(
            SESSION_ID, thread_id,
            **{**options, "tool_set_supplier": refuse_supplier},
            **frozen_provider_activation(saver, plan_creation_idempotency_key=({**options, 'tool_set_supplier': refuse_supplier})['plan_creation_idempotency_key']),
        )
    assert retry["assembly_id"] == first["assembly_id"]
    assert saver._storage.get_context_assembly(
        SESSION_ID, thread_id, assembly_id=first["assembly_id"],
    ) == first_snapshot
    with saver._context_reader.open_snapshot(SESSION_ID, thread_id) as snapshot:
        latest = saver._storage.latest_context_assembly(snapshot)
    assert latest.assembly_id == second["assembly_id"]
    assert latest.seal_binding.applied_toolset_revision == tools_b.revision
    assert latest.seal_binding.tool_set_snapshot_id == tools_b.snapshot_id


def test_unregistered_thread_cannot_prepare_or_receive_applied_toolset(
    saver, sealed_toolset_context,
) -> None:
    """canonical 但未发布的 child 不能绕过 catalog 获得 applied binding。"""
    missing = create_prefixed_id("thr")
    with (
        pytest.raises(KeyError, match="thread catalog"),
        sealed_toolset_context(thread_id=missing),
    ):
        pytest.fail("未注册 thread 不应建立 execution admission")
    assert saver.list_context_assemblies(SESSION_ID, _main_thread_id(saver)) == ()
    session_dir = saver._storage._path_resolver.resolve_session_node(SESSION_ID)
    control = SessionControlStore(session_dir / "session-control.sqlite")
    try:
        with pytest.raises(KeyError):
            control.get_thread_owner_binding(missing)
    finally:
        control.close()


def test_sealed_retry_after_saver_restart_keeps_revision_and_bytes(frozen_provider_activation,
    sealed_toolset_context, saver, frozen_provider_wire,
) -> None:
    tools_a = ToolSetSnapshot(selection_revision=_desired_key(TOOLS_A), tools=TOOLS_A)
    with sealed_toolset_context(name="restart") as (thread, accepted, prepare, _manager):
        first_result, first = prepare(tools_a, key="a")
        frozen = first.to_dict()
        with RolloutCheckpointSaver(
            saver._storage.sessions_dir,
        ) as restarted:
            admission = restarted.execution_admission(
                SESSION_ID, thread_id=thread, execution_id=accepted["initial_execution_id"],
            )
            manager = ContextSourceManager(
                owner=ContextSourceOwnerKey(SESSION_ID, thread), control_state_port=restarted,
            )

            def refuse_supplier():
                pytest.fail("跨进程 sealed retry 不得读取新的 ToolSet desired")

            with (
                session_thread_owner_scope(MutationIntentOwner(SESSION_ID, thread), langgraph_thread_id=thread),
                execution_admission_scope(admission, langgraph_thread_id=thread),
            ):
                replay = restarted.prepare_context_for_provider(
                    SESSION_ID, thread, turn_id=accepted["turn_id"],
                    execution_id=accepted["initial_execution_id"],
                    **frozen_provider_wire("toolset-provider", "responses"),
                    context_source_manager=manager, tool_set_supplier=refuse_supplier,
                    plan_creation_idempotency_key="restart-create-a", seal_idempotency_key="restart-seal-a",
                    **frozen_provider_activation(restarted, plan_creation_idempotency_key='restart-create-a'),
                )
            restored = restarted.get_context_assembly(SESSION_ID, thread, assembly_id=replay["assembly_id"])
            assert replay["assembly_id"] == first_result["assembly_id"]
            assert restored.to_dict() == frozen
            assert restored.seal_binding.applied_toolset_revision == tools_a.revision
            assert restarted.list_context_assemblies(SESSION_ID, thread) == (first,)


def test_successful_seal_applies_toolset_to_published_child_thread(
    saver, sealed_toolset_context,
) -> None:
    child_thread_id = create_prefixed_id("thr")
    session_dir = saver._storage._path_resolver.resolve_session_node(SESSION_ID)
    control = SessionControlStore(session_dir / "session-control.sqlite")
    try:
        control.create_or_get_thread_creation_record(
            idempotency_key=f"toolset-child-{child_thread_id}",
            initial_state="idle",
            preimage_hash="b" * 64,
            graph_binding=json.dumps({
                "graph_id": DEEP_AGENT_GRAPH_BINDING.graph_id,
                "graph_revision": DEEP_AGENT_GRAPH_BINDING.graph_revision,
                "graph_schema_hash": DEEP_AGENT_GRAPH_BINDING.graph_schema_hash,
                "capability_profile_hash": DEEP_AGENT_GRAPH_BINDING.capability_profile_hash,
            }),
            capability_profile="{}",
            created_at=uuid7_datetime_from_hex(child_thread_id[4:]),
            thread_id=child_thread_id,
        )
        artifact_manifest = "{}"
        control.freeze_thread_creation_artifact_manifest(
            f"toolset-child-{child_thread_id}",
            artifact_manifest=artifact_manifest,
            artifact_manifest_hash=hashlib.sha256(
                artifact_manifest.encode("utf-8")
            ).hexdigest(),
        )
        published = control.publish_thread_creation_record(
            f"toolset-child-{child_thread_id}", session_id=session_dir.name
        )
        (session_dir / published.final_relative_locator).mkdir(parents=True)
    finally:
        control.close()

    tools_a = ToolSetSnapshot(selection_revision=_desired_key(TOOLS_A), tools=TOOLS_A)
    with sealed_toolset_context(thread_id=child_thread_id, name="child") as (thread, _accepted, prepare, _manager):
        _, child = prepare(tools_a, key="a")
    assert child.thread_id == child_thread_id == thread
    assert child.seal_binding.applied_toolset_revision == tools_a.revision
    assert child.seal_binding.tool_set_snapshot_id == tools_a.snapshot_id
    assert child.seal_binding.prefix.state.epoch.ordinal == 1
    assert saver.list_context_assemblies(SESSION_ID, _main_thread_id(saver)) == ()
    store = SessionControlStore(session_dir / "session-control.sqlite")
    try:
        owner = store.get_thread_owner_binding(child_thread_id)
        assert not hasattr(owner, "applied_toolset_revision")
        assert not hasattr(owner, "toolset_compatibility_key")
    finally:
        store.close()


def test_failed_seal_keeps_previous_applied_and_retries_atomically(
    saver, sealed_toolset_context,
) -> None:
    """真实 SQLite seal 事务失败不推进 applied，重试成功才进入新 epoch。"""
    tools_a = ToolSetSnapshot(selection_revision=_desired_key(TOOLS_A), tools=TOOLS_A)
    tools_b = ToolSetSnapshot(selection_revision=_desired_key(TOOLS_B), tools=TOOLS_B)
    with sealed_toolset_context(name="failure") as (thread, _accepted, prepare, manager):
        _, first = prepare(tools_a, key="a")
        before = first.to_dict()
        pending = manager.prepare_pending()
        with saver._storage._connect(thread, "", session_id=SESSION_ID) as connection:
            connection.execute(
                "CREATE TRIGGER refuse_toolset_seal BEFORE INSERT ON context_assemblies "
                "BEGIN SELECT RAISE(ABORT, 'toolset-seal-injected'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="toolset-seal-injected"):
            prepare(tools_b, key="b")
        assert saver.list_context_assemblies(SESSION_ID, thread) == (first,)
        assert saver.get_context_assembly(SESSION_ID, thread, assembly_id=first.assembly_id).to_dict() == before
        assert manager.prepare_pending() == pending
        with saver._context_reader.open_snapshot(SESSION_ID, thread) as snapshot:
            latest = saver._storage.latest_context_assembly(snapshot)
            assert latest.assembly_id == first.assembly_id
            assert latest.seal_binding.applied_toolset_revision == tools_a.revision
            assert snapshot.connection.execute(
                "SELECT count(*) FROM storage_commits WHERE commit_kind = 'assembly_sealed' AND status = 'committed'"
            ).fetchone()[0] == 1
        with saver._storage._connect(thread, "", session_id=SESSION_ID) as connection:
            connection.execute("DROP TRIGGER refuse_toolset_seal")
        _, changed = prepare(tools_b, key="b")
        assert changed.seal_binding.applied_toolset_revision == tools_b.revision
        assert changed.seal_binding.tool_set_snapshot_id == tools_b.snapshot_id
        assert changed.seal_binding.previous_assembly_id == first.assembly_id
        assert changed.seal_binding.prefix.state.epoch.ordinal == 2
        assert saver.list_context_assemblies(SESSION_ID, thread) == (first, changed)


@pytest.mark.parametrize("change", ["policy_keys", "policy_version", "wire_tool_order"])
def test_complete_toolset_manifest_changes_rebase_after_real_seal(
    sealed_toolset_context, change,
) -> None:
    """仅验证完整 snapshot→Saver→seal；不声称控制面已生产 policy/order 变更。"""
    tools = (*TOOLS_A, *TOOLS_B)
    first_set = ToolSetSnapshot(selection_revision=_desired_key(tools), tools=tools)
    changed_set = ToolSetSnapshot(
        selection_revision=first_set.selection_revision,
        tools=tuple(reversed(tools)) if change == "wire_tool_order" else tools,
        policy_keys=("filesystem_rules",) if change == "policy_keys" else (),
        policy_version="v2" if change == "policy_version" else "v1",
    )
    with sealed_toolset_context(name=change) as (_thread, accepted, prepare, _manager):
        _, first = prepare(first_set, key="before")
        _, changed = prepare(changed_set, key="after")
    assert changed_set.revision != first_set.revision
    assert first.seal_binding.applied_toolset_revision == first_set.revision
    assert changed.seal_binding.applied_toolset_revision == changed_set.revision
    assert changed.seal_binding.tool_set_snapshot_id == changed_set.snapshot_id
    assert changed.seal_binding.prefix.state.epoch.ordinal == 2
    assert changed.seal_binding.previous_assembly_id == first.assembly_id
    assert first.turn_id == changed.turn_id == accepted["turn_id"]
