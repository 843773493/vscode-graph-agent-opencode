"""协作同库 mutation、cold pending、恢复与真实 activation owner 联验。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.graph_binding import compute_capability_profile_hash
from app.core.execution_admission_context import execution_admission_scope
from app.core.lifecycle import LifetimeScope
from app.core.path_utils import (
    get_session_creation_service,
    get_session_path_resolver,
    workspace_context,
)
from app.core.session_control_store import CollaborationMemberInput
from app.core.session_thread_context import session_thread_owner_scope
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
from app.prompting import PromptSection, internal_message_factory
from app.schemas.internal_v2.job import JobDispatchSnapshotDTO
from app.schemas.internal_v2.team import TeamBoardDTO, TeamEventDTO
from app.services.business.resource_derivations.team import (
    TEAM_EVENT_SOURCE_ID,
    TEAM_FACETS,
    TEAM_LEDGER_SOURCE_ID,
    TEAM_RESOURCE_IDS,
    TeamFacetLoader,
)
from app.services.business.session_service import SessionService
from app.services.business.team.service import TeamCoordinationService
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.activation.body import (
    ResourceActivationBodyStore,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.team.store import TeamStore
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)
from app.services.orchestration.resource_activation import (
    ResourceActivationCoordinator,
    ResourceActivationError,
)
from app.services.orchestration.runtime_source_dispatcher import RuntimeSourceDispatcher
from app.services.orchestration.team.fanout_service import TeamFanoutService
from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.unit.services.business.session_service_test_support import (
    SessionServiceConfigStub,
    prepare_session_test_workspace,
)

SESSION = "ses_019c3df7f8527d568e834acf3c0f6612"

FANOUT_PROCESS = r'''
import asyncio,os,sys
from pathlib import Path
from threading import RLock
from app.core.path_utils import get_session_creation_service,get_session_path_resolver,workspace_context
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.business.session_service import SessionService
from app.services.business.team.service import TeamCoordinationService
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.orchestration.owner_thread_creation_factory import OwnerThreadCreationFactory
from app.services.orchestration.team.fanout_service import TeamFanoutService
from tests.support.session_catalog_owner import fixture_catalog_scope
from tests.unit.services.business.session_service_test_support import SessionServiceConfigStub
workspace,session,mode=sys.argv[1:]
workspace=Path(workspace)
root=workspace/".boxteam/sessions"
with fixture_catalog_scope(),workspace_context(load_or_create_workspace_id(workspace),workspace):
    resolver=get_session_path_resolver(root)
    saver=RolloutCheckpointSaver(root)
    sessions=SessionService(config_service=SessionServiceConfigStub(),trace_event_store=TraceEventStore(sessions_dir=root),
        workspace_id=resolver.workspace_id,path_resolver=resolver,creation_service=get_session_creation_service(root),
        initialize_thread_storage=saver.initialize_thread_storage)
    factory=OwnerThreadCreationFactory(sessions_root=root,workspace_id=resolver.workspace_id,path_resolver=resolver)
    fanout=TeamFanoutService(factory=factory,sessions=sessions,saver=saver,dispatcher=None,publication_lock=RLock())
    control=factory.for_owner_session(session).control_store
    record=control.list_collaboration_fanouts(pending_only=True)[-1]
    if mode=="zero":
        saver.receive_team_source_observation=lambda *args,**kwargs: os._exit(81)
    else:
        finish=control.finish_collaboration_delivery
        count=0
        def receipt(*args,**kwargs):
            global count
            finish(*args,**kwargs)
            count+=1
            if count==(1 if mode=="partial" else len(record.recipients)):
                os._exit(82)
        control.finish_collaboration_delivery=receipt
    asyncio.run(fanout.reconcile(session))
raise AssertionError("fanout退出注入未命中")
'''


class RejectDispatcher:
    async def submit(self, *args, **kwargs):
        raise AssertionError("cold collaboration pending 不得派生 runtime wakeup")


@pytest.fixture
async def team_case(request):
    context, _, workspace = prepare_session_test_workspace(test_file=Path(request.node.path), node_id=request.node.nodeid)
    workspace_id = load_or_create_workspace_id(workspace)
    sessions_root = workspace / ".boxteam/sessions"
    bundle = seed_catalog_session_bundle(sessions_root, SESSION)
    with workspace_context(workspace_id, workspace):
        resolver = get_session_path_resolver(sessions_root)
        saver = RolloutCheckpointSaver(sessions_root)
        sessions = SessionService(config_service=SessionServiceConfigStub(), trace_event_store=TraceEventStore(sessions_dir=sessions_root),
                                  workspace_id=workspace_id, path_resolver=resolver, creation_service=get_session_creation_service(sessions_root),
                                  initialize_thread_storage=saver.initialize_thread_storage)
        factory = OwnerThreadCreationFactory(sessions_root=sessions_root, workspace_id=workspace_id, path_resolver=resolver)
        lock = RLock()
        fanout = TeamFanoutService(factory=factory, sessions=sessions, saver=saver, dispatcher=RejectDispatcher(), publication_lock=lock)
        store = TeamStore(factory=factory, write_admission=sessions.session_write_admission)
        control = factory.for_owner_session(SESSION).control_store
        main = resolver.main_thread_id(SESSION)
        created = resolver.catalog_store.get_node(SESSION).created_at
        board = TeamBoardDTO(team_id="team_" + "a" * 32, name="协作验证", session_id=SESSION, coordinator_thread_id=main,
                             version=1, created_at=created, updated_at=created)
        case = SimpleNamespace(context=context, workspace=workspace, root=sessions_root, main=main, sessions=sessions,
                               factory=factory, saver=saver, fanout=fanout, store=store, control=control, board=board,
                               lock=lock, scopes=[], bundle=bundle)
        await commit_board(case)
        yield case
        await fanout.stop()
        for scope in case.scopes:
            await scope.close()
        control.close()
        resolver.catalog_store.close()
    context.close()


async def commit_board(case, *, name=None, wakeup=None):
    board = case.board
    if name is not None:
        board = board.model_copy(update={"version": board.version + 1, "name": name})
    event = TeamEventDTO(event_id="tevt_" + uuid4().hex, team_id=board.team_id, session_id=SESSION,
                         type="team.created" if board.version == 1 else "team.updated", actor_thread_id=case.main,
                         created_at=board.created_at, payload={"name": board.name})
    revision = await case.store.commit(board, event, wakeup=wakeup)
    case.board = board
    return revision


async def create_member(case, name):
    profile = {"goal_enabled": False}
    result = await case.factory.for_owner_session(SESSION).create(
        idempotency_key="delegate-" + name, session_id=SESSION, thread_id=None, delegation_id="delegate-" + name,
        initial_state="running", artifact_manifest={},
        session_metadata={"graph_binding": {"graph_id": "deep-agent", "graph_revision": 1, "graph_schema_hash": "sha256:" + "a" * 64,
                                             "capability_profile_hash": compute_capability_profile_hash(capability_profile=profile)},
                          "capability_profile": profile, "task_seed": {"trusted_context": {"team_id": case.board.team_id,
                              "coordinator_thread_id": case.main, "work_mode": "read_only"}, "untrusted_instructions": "只读验证"},
                          "task_reference": {"parent_session_id": SESSION, "parent_thread_id": case.main,
                                             "delegation_id": "delegate-" + name}},
        collaboration_member={"role": "reviewer", "subagent_type": "general-purpose", "title": name},
    )
    return result.child_thread_id


async def test_member_abort_after_fence_close_settles_without_new_fanout(team_case):
    case = team_case
    record = case.control.create_or_get_thread_creation_record(
        idempotency_key="closed-member", delegation_id="closed-member", initial_state="running",
        preimage_hash="b" * 64,
        graph_binding=json.dumps({"graph_id": "deep-agent", "graph_revision": 1,
                                  "graph_schema_hash": "sha256:" + "a" * 64,
                                  "capability_profile_hash": compute_capability_profile_hash(capability_profile={"goal_enabled": False})}),
        capability_profile='{"goal_enabled": false}', task_seed="{}",
        task_reference=json.dumps({"parent_session_id": SESSION, "parent_thread_id": case.main,
                                   "delegation_id": "closed-member"}),
        owner_session_id=SESSION,
        collaboration_member=CollaborationMemberInput(
            coordinator_session_id=SESSION, coordinator_thread_id=case.main, role="reviewer",
            subagent_type="general-purpose", title="删除期间中止",
        ),
    )
    revision = case.control.get_collaboration_ledger_revision()
    original_fanouts = case.control.list_collaboration_fanouts()
    original_lease_count = case.control.connection.execute("SELECT COUNT(*) FROM session_operation_leases").fetchone()[0]
    case.control.cas_fence_transition(1, "deleting")
    aborted = case.control.abort_thread_creation_record(record.thread_creation_idempotency_key, "session_deletion_pending")
    assert aborted.state == "aborted"
    assert case.control.get_collaboration_member("closed-member").state == "cancelled"
    assert case.control.get_collaboration_ledger_revision() == revision + 1
    assert case.control.list_collaboration_fanouts() == original_fanouts
    assert case.control.connection.execute("SELECT COUNT(*) FROM session_operation_leases").fetchone()[0] == original_lease_count
    async with case.fanout.deletion_holder.lock_for(SESSION), case.sessions._session_lifecycle_gate.exclusive(SESSION):
        await case.fanout.drain_session(SESSION)
    assert all(fanout.state == "cancelled" for fanout in case.control.list_collaboration_fanouts())
    assert case.control.abort_thread_creation_record("closed-member", "其他原因") == aborted


def subscribe(case, *, thread=None, fanout=None):
    registry = ResourceRegistry(publication_lock=case.lock)
    scope = LifetimeScope("team-generation")
    case.scopes.append(scope)
    (fanout or case.fanout).subscribe(SESSION, thread or case.main, registry, scope)
    return registry, scope


def observation_rows(case, thread):
    index = case.saver._storage.storage.index_path(SESSION, "", thread_id=thread)
    with sqlite3.connect(index.as_uri() + "?mode=ro", uri=True) as connection:
        return connection.execute("SELECT ledger_revision,event_hash,payload_json FROM team_source_observations ORDER BY ledger_revision").fetchall()


def admission(case, ordinal):
    key = str(ordinal)
    case.saver.accept_turn(SESSION, thread_id=case.main, accepted_ingress_id="ingress-" + key, acceptance_idempotency_key="accept-" + key,
        job_id="job-" + key, admission_idempotency_key="admit-" + key, payload="验证", turn_id="turn-" + key, root_item_id="root-" + key)
    execution = case.saver.execution_for_turn(SESSION, thread_id=case.main, turn_id="turn-" + key)
    return case.saver.execution_admission(SESSION, thread_id=case.main, execution_id=execution)


def coordinator(case, registry):
    return ResourceActivationCoordinator(registry=registry, saver=case.saver,
        body_store=ResourceActivationBodyStore(case.saver._storage, case.saver._detail_store), wait_seconds=0.025, poll_seconds=0.002)


def prepare(case, active, frozen, key, wire):
    manager = ContextSourceManager(owner=ContextSourceOwnerKey(SESSION, case.main), control_state_port=case.saver, mutation_intent_port=case.saver)
    with session_thread_owner_scope(MutationIntentOwner(SESSION, case.main), langgraph_thread_id=case.main), execution_admission_scope(active, langgraph_thread_id=case.main):
        return case.saver.prepare_context_for_provider(SESSION, case.main, turn_id=active.turn_id, execution_id=active.execution_id,
            plan_creation_idempotency_key=key, seal_idempotency_key="seal:" + key,
            context_source_manager=manager, tool_set_supplier=lambda: ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
            model_call_id=frozen.model_call_id or key, activation_snapshot=frozen, **wire("fixture-model", "chat_completions"))


@pytest.mark.asyncio
async def test_partial_fanout_cold_restart_only_recovers_pending_and_rejects_old_token(team_case, monkeypatch):
    case = team_case
    children = [await create_member(case, name) for name in ("one", "two")]
    original = case.saver.receive_team_source_observation
    calls = Counter()

    def fail_one(session_id, thread_id, **kwargs):
        calls[thread_id] += 1
        if thread_id == children[0]:
            original(session_id, thread_id, **kwargs)
            raise OSError("observation 已提交但 delivery receipt 前失败")
        original(session_id, thread_id, **kwargs)

    monkeypatch.setattr(case.saver, "receive_team_source_observation", fail_one)
    with pytest.raises(ExceptionGroup, match="部分投递失败"):
        await case.fanout.reconcile(SESSION)
    records = case.control.list_collaboration_fanouts(pending_only=True)
    assert records and all(any(item.thread_id == children[0] and item.error for item in row.recipients) for row in records)
    tokens = {record.ledger_revision: case.control.get_lease(record.lease_id).fencing_token for record in records}
    first_observations = observation_rows(case, children[0])
    monkeypatch.setattr(case.saver, "receive_team_source_observation", original)
    recovered = TeamFanoutService(factory=case.factory, sessions=case.sessions, saver=case.saver, dispatcher=RejectDispatcher(), publication_lock=case.lock)
    await recovered.start()
    try:
        assert case.control.list_collaboration_fanouts(pending_only=True) == ()
        assert observation_rows(case, children[0]) == first_observations
        assert all(item.available for item in subscribe(case, thread=children[0], fanout=recovered)[0].snapshots())
        for row in records:
            with pytest.raises(RuntimeError, match="lease/token"):
                case.control.finish_collaboration_delivery(row.ledger_revision, children[0], fencing_token=tokens[row.ledger_revision], outcome="delivered")
        for child in children:
            assert case.saver._storage.checkpoints.read_context_source_control_states(SESSION, thread_id=child) == ()
            with sqlite3.connect(case.saver._storage.storage.index_path(SESSION, "", thread_id=child)) as connection:
                assert connection.execute("SELECT count(*) FROM item_catalog").fetchone()[0] == 0
                assert connection.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
    finally:
        await recovered.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["turn", "model_call"])
async def test_real_activation_freezes_latest_active_boundary_and_never_reads_ledger(team_case, monkeypatch, boundary):
    case = team_case
    await case.fanout.start()
    registry, scope = subscribe(case)
    parent = await coordinator(case, registry).freeze_execution(admission=admission(case, 1),
        policy=ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={"team_state": boundary}))
    first = await coordinator(case, registry).commit_model_call(await coordinator(case, registry).capture_model_call(parent=parent, model_call_id=str(uuid4())))
    await commit_board(case, name="后续变化")
    await case.fanout.reconcile(SESSION)
    await case.fanout.stop()
    snapshots = registry.snapshots()
    previous = {item.resource_id: item for item in first.bindings}
    assert previous[TEAM_RESOURCE_IDS["membership"]].revision == registry.snapshot(TEAM_RESOURCE_IDS["membership"]).revision
    assert previous[TEAM_RESOURCE_IDS["tasks"]].revision != registry.snapshot(TEAM_RESOURCE_IDS["tasks"]).revision

    def no_ledger(*args, **kwargs):
        raise AssertionError("activation request 禁止读取 collaboration ledger")

    monkeypatch.setattr(case.factory, "for_owner_session", no_ledger)
    second = await coordinator(case, registry).commit_model_call(await coordinator(case, registry).capture_model_call(parent=parent, model_call_id=str(uuid4())))
    assert second.bindings == first.bindings if boundary == "turn" else second.bindings != first.bindings
    assert registry.snapshots() == snapshots
    await scope.close()
    assert (SESSION, case.main) not in case.fanout._subscribers


@pytest.mark.asyncio
async def test_required_unready_revision_blocks_dispatch_and_ready_without_source_rewrite(team_case):
    case = team_case
    await case.fanout.start()
    registry, _scope = subscribe(case)
    await case.fanout.stop()
    valid = {snapshot.resource_id: snapshot for snapshot in registry.snapshots()}
    latest = await commit_board(case, name="连续变化一")
    latest = await commit_board(case, name="连续变化二")
    assert not all(snapshot.available for snapshot in registry.snapshots())
    for snapshot in registry.snapshots():
        previous = valid[snapshot.resource_id]
        assert snapshot.payload is None and snapshot.retained_revision == previous.revision
        assert snapshot.revision == previous.revision and snapshot.source_lineage == previous.source_lineage
        assert registry.last_valid_snapshot(snapshot.resource_id) is previous
    with pytest.raises(ResourceActivationError, match="source-unavailable"):
        await coordinator(case, registry).freeze_execution(admission=admission(case, 1),
            policy=ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={}))
    await case.fanout.reconcile(SESSION)
    assert max(revision for revision, _hash, _payload in observation_rows(case, case.main)) == latest
    frozen = await coordinator(case, registry).freeze_execution(admission=case.saver.execution_admission(SESSION, thread_id=case.main,
        execution_id=case.saver.execution_for_turn(SESSION, thread_id=case.main, turn_id="turn-1")),
        policy=ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={}))
    assert frozen.registry_generation == max(snapshot.generation for snapshot in registry.snapshots())
    record = case.control.get_collaboration_fanout(latest)
    for binding in frozen.bindings:
        assert binding.captured_registry_generation == registry.snapshot(binding.resource_id).generation
        assert binding.source_lineage_ref is not None
        assert binding.source_lineage_ref.sources == tuple(sorted((
            (TEAM_LEDGER_SOURCE_ID, str(latest)), (TEAM_EVENT_SOURCE_ID, record.event_hash),
        )))
    assert case.saver._storage.checkpoints.read_context_source_control_states(SESSION, thread_id=case.main) == ()


@pytest.mark.asyncio
async def test_fence_closed_and_recipient_deleted_finish_without_late_source_append(team_case):
    case = team_case
    child = await create_member(case, "deleted")
    original_revision = await commit_board(case, name="待删除")
    case.control.connection.execute("DELETE FROM thread_catalog WHERE thread_id=?", (child,))
    case.control.cas_fence_transition(expected_generation=1, new_state="deleting")
    await case.fanout.reconcile(SESSION)
    record = case.control.get_collaboration_fanout(original_revision)
    assert record.state == "cancelled"
    assert {item.state for item in record.recipients} == {"fence_closed"}
    assert not case.saver._storage.storage.index_path(SESSION, "", thread_id=case.main).exists()
    assert case.control.list_non_terminal_leases() == ()


@pytest.mark.asyncio
async def test_delete_drain_never_waits_live_holder_with_session_gate_held(team_case):
    case = team_case
    resolver = case.sessions.path_resolver
    resolver.bind_session_drain_callback(case.fanout.drain_session, holders=(case.fanout.deletion_holder,))
    async with case.fanout.deletion_holder.lock_for(SESSION):
        deleting = asyncio.create_task(resolver.delete_subtree(idempotency_key="live-team-delete", root_node_id=SESSION))
        try:
            async with asyncio.timeout(2):
                while resolver.catalog_store.get_node(SESSION).state != "deleting":
                    await asyncio.sleep(0)
            async with asyncio.timeout(2), case.sessions._session_lifecycle_gate.exclusive(SESSION):
                assert not deleting.done()
                assert case.control.get_fence() == ("active", 1)
                assert case.control.list_non_terminal_leases()
        except BaseException:
            deleting.cancel()
            await asyncio.gather(deleting, return_exceptions=True)
            raise
    assert (await asyncio.wait_for(deleting, 2)).record_state == "completed"
    assert case.control.list_collaboration_fanouts()[0].state == "cancelled"
    assert case.control.list_non_terminal_leases() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["event", "recipient", "payload", "lease"])
async def test_frozen_fanout_damage_fails_closed(team_case, damage):
    case = team_case
    record = case.control.get_collaboration_fanout(1)
    if damage == "event":
        case.control.connection.execute("UPDATE collaboration_fanouts SET event_hash=?", ("f" * 64,))
    elif damage == "recipient":
        case.control.connection.execute("DELETE FROM collaboration_deliveries")
    elif damage == "payload":
        payload = json.loads(record.payload_json)
        payload["boards"][0]["name"] = "损坏"
        case.control.connection.execute("UPDATE collaboration_fanouts SET payload_json=?", (json.dumps(payload),))
    else:
        case.control.connection.execute("UPDATE session_operation_leases SET preimage_hash=? WHERE lease_id=?", ("f" * 64, record.lease_id))
    with pytest.raises(RuntimeError, match="collaboration fanout"):
        case.control.get_collaboration_fanout(1)


@pytest.mark.asyncio
async def test_hidden_registering_member_does_not_change_facet_revision(team_case):
    case = team_case
    await case.fanout.start()
    registry, _scope = subscribe(case)
    await case.fanout.stop()
    original = {snapshot.facet: snapshot for snapshot in registry.snapshots()}
    async with case.sessions.session_write_admission(SESSION):
        case.control.create_or_get_thread_creation_record(
            idempotency_key="hidden", delegation_id="hidden", initial_state="running", preimage_hash="c" * 64,
            graph_binding=json.dumps({"graph_id": "deep-agent", "graph_revision": 1,
                "graph_schema_hash": "sha256:" + "a" * 64,
                "capability_profile_hash": compute_capability_profile_hash(capability_profile={"goal_enabled": False})}),
            capability_profile='{"goal_enabled": false}',
            task_seed=json.dumps({"trusted_context": {"team_id": case.board.team_id}}),
            task_reference=json.dumps({"parent_session_id": SESSION, "parent_thread_id": case.main,
                                       "delegation_id": "hidden"}), owner_session_id=SESSION,
            collaboration_member=CollaborationMemberInput(
                coordinator_session_id=SESSION, coordinator_thread_id=case.main, role="reviewer",
                subagent_type="general-purpose", title="隐藏成员",
            ),
        )
    await case.fanout.reconcile(SESSION)
    record = case.control.get_collaboration_fanout(2)
    for snapshot in registry.snapshots():
        assert snapshot.available
        assert snapshot.revision == original[snapshot.facet].revision
        assert snapshot.payload == original[snapshot.facet].payload
        assert snapshot.source_lineage == tuple(sorted((
            (TEAM_LEDGER_SOURCE_ID, "2"), (TEAM_EVENT_SOURCE_ID, record.event_hash),
        )))


@pytest.mark.asyncio
async def test_real_fanout_uses_versioned_graph_loaders_and_accepted_inputs_only(team_case, monkeypatch):
    case = team_case
    await case.fanout.start()
    calls = []
    load = TeamFacetLoader.__call__

    def capture(loader, inputs):
        calls.append({"facet": loader.facet, "inputs": {
            source_id: {"revision": source.revision, "content": source.content}
            for source_id, source in inputs.items()
        }})
        return load(loader, inputs)

    def no_ledger(*args, **kwargs):
        raise AssertionError("Team graph 发布只能消费已接受输入")

    monkeypatch.setattr(TeamFacetLoader, "__call__", capture)
    with monkeypatch.context() as accepted_only:
        accepted_only.setattr(case.factory, "for_owner_session", no_ledger)
        registry, _scope = subscribe(case)
    assert [call["facet"] for call in calls] == list(TEAM_FACETS)
    await case.fanout.stop()
    original = {snapshot.facet: snapshot for snapshot in registry.snapshots()}
    revision = await commit_board(case, name="图接线任务变化")
    assert len(calls) == len(TEAM_FACETS)
    await case.fanout.reconcile(SESSION)
    record = case.control.get_collaboration_fanout(revision)
    changed_tasks = {snapshot.facet: snapshot for snapshot in registry.snapshots()}
    assert changed_tasks["membership"].revision == original["membership"].revision
    assert changed_tasks["tasks"].revision != original["tasks"].revision
    for call in calls[-len(TEAM_FACETS):]:
        assert call["inputs"] == {
            TEAM_LEDGER_SOURCE_ID: {"revision": str(revision), "content": record.payload_json},
            TEAM_EVENT_SOURCE_ID: {"revision": record.event_hash, "content": record.event_hash},
        }
    await create_member(case, "graph-member")
    await case.fanout.reconcile(SESSION)
    changed_members = {snapshot.facet: snapshot for snapshot in registry.snapshots()}
    assert changed_members["membership"].revision != changed_tasks["membership"].revision
    assert changed_members["tasks"].revision == changed_tasks["tasks"].revision
    for descriptor in registry.descriptors():
        assert descriptor.source_ids == (TEAM_LEDGER_SOURCE_ID, TEAM_EVENT_SOURCE_ID)
        assert descriptor.loader_version == TeamFacetLoader.version
    (case.context.artifacts_dir / "team-loader-actual.json").write_text(json.dumps({
        "loader_calls": calls,
        "observations": observation_rows(case, case.main),
        "snapshots": [{"resource_id": snapshot.resource_id, "revision": snapshot.revision,
                       "generation": snapshot.generation, "payload": snapshot.payload,
                       "source_lineage": snapshot.source_lineage} for snapshot in registry.snapshots()],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["turn", "model_call"])
async def test_tail_guidance_seal_and_restart_keep_old_item_bytes_and_independent_facets(team_case, frozen_provider_wire, boundary):
    case = team_case
    await case.fanout.start()
    registry, _scope = subscribe(case)
    active = admission(case, 1)
    parent = await coordinator(case, registry).freeze_execution(admission=active,
        policy=ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={"team_state": boundary}))
    call_one = str(uuid4())
    frozen = await coordinator(case, registry).commit_model_call(await coordinator(case, registry).capture_model_call(parent=parent, model_call_id=call_one))
    first = prepare(case, active, frozen, call_one, frozen_provider_wire)
    first_assembly = case.saver.get_context_assembly(SESSION, case.main, assembly_id=first["assembly_id"])
    index = case.saver._storage.storage.index_path(SESSION, "", thread_id=case.main)
    with sqlite3.connect(index) as connection:
        before = connection.execute("SELECT * FROM item_catalog ORDER BY item_sequence").fetchall()
        source_count = connection.execute("SELECT count(*) FROM context_source_control_states").fetchone()[0]
    assert source_count == 0
    await commit_board(case, name="未seal中间变化")
    await commit_board(case, name="只激活最新变化")
    await case.fanout.reconcile(SESSION)
    second_key = str(uuid4())
    second_frozen = await coordinator(case, registry).commit_model_call(await coordinator(case, registry).capture_model_call(parent=parent, model_call_id=second_key))
    second = prepare(case, active, second_frozen, second_key, frozen_provider_wire)
    with sqlite3.connect(index) as connection:
        after = connection.execute("SELECT * FROM item_catalog ORDER BY item_sequence").fetchall()
        assert after[:len(before)] == before
        assert connection.execute("SELECT count(*) FROM context_source_control_states").fetchone()[0] == 0
    assert after == before
    second_assembly = case.saver.get_context_assembly(SESSION, case.main, assembly_id=second["assembly_id"])
    first_frames = first_assembly.seal_binding.prefix.state.frames
    second_frames = second_assembly.seal_binding.prefix.state.frames
    assert second_frames[:len(first_frames)] == first_frames
    assert len(second_frames) == len(first_frames) if boundary == "turn" else len(second_frames) == len(first_frames) + 1
    with RolloutCheckpointSaver(case.root) as restored:
        original = restored.load_sealed_resource_activation(SESSION, thread_id=case.main, assembly_id=first_assembly.assembly_id,
            plan_hash=first_assembly.plan_hash, request_hash=first_assembly.request_hash)
        assert original.to_dict() == frozen.to_dict()
        assert restored.get_context_assembly(SESSION, case.main, assembly_id=second["assembly_id"]).seal_binding is not None


@pytest.mark.asyncio
async def test_queue_acceptance_does_not_freeze_team_before_active_slot(team_case):
    case = team_case
    await case.fanout.start()
    registry, _scope = subscribe(case)
    first = admission(case, 1)
    queued = admission(case, 2)
    latest = await commit_board(case, name="排队后激活前变化")
    await case.fanout.reconcile(SESSION)
    assert case.saver._storage.executions.mark_job_terminal_status(session_id=SESSION, thread_id=case.main, job_id=first.job_id, status="cancelled")
    frozen = await coordinator(case, registry).freeze_execution(admission=queued,
        policy=ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={}))
    assert frozen.registry_generation == max(snapshot.generation for snapshot in registry.snapshots())
    latest_record = case.control.get_collaboration_fanout(latest)
    for binding in frozen.bindings:
        assert binding.captured_registry_generation == registry.snapshot(binding.resource_id).generation
        assert binding.source_lineage_ref is not None
        assert binding.source_lineage_ref.sources == tuple(sorted((
            (TEAM_LEDGER_SOURCE_ID, str(latest)), (TEAM_EVENT_SOURCE_ID, latest_record.event_hash),
        )))
    task_binding = next(binding for binding in frozen.bindings if binding.resource_id == TEAM_RESOURCE_IDS["tasks"])
    assert "排队后激活前变化" in case.saver.read_resource_activation_payload(
        frozen, task_binding, storage_owner=(SESSION, case.main),
    )["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["zero", "partial", "all"])
async def test_process_exit_after_zero_partial_all_delivery_recovers_same_original_fanout(team_case, monkeypatch, mode):
    case = team_case
    children = [await create_member(case, name) for name in ("crash-one", "crash-two")]
    await case.fanout.reconcile(SESSION)
    revision = await commit_board(case, name="固定退出窗口")
    record = case.control.get_collaboration_fanout(revision)
    old_token = case.control.get_lease(record.lease_id).fencing_token
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", FANOUT_PROCESS, str(case.workspace), SESSION, mode,
        cwd=case.context.project_root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output, _ = await asyncio.wait_for(process.communicate(), 20)
    (case.context.artifacts_dir / f"fanout-crash-{mode}.log").write_bytes(output)
    assert process.returncode == (81 if mode == "zero" else 82), output.decode()
    interrupted = case.control.get_collaboration_fanout(revision)
    pending = {item.thread_id for item in interrupted.recipients if item.state == "pending"}
    assert len(pending) == {"zero": 3, "partial": 2, "all": 0}[mode]
    assert interrupted.state == "pending"
    counts = Counter()
    receive = case.saver.receive_team_source_observation

    def observed(session_id, thread_id, **kwargs):
        counts[thread_id] += 1
        receive(session_id, thread_id, **kwargs)

    monkeypatch.setattr(case.saver, "receive_team_source_observation", observed)
    recovered = TeamFanoutService(factory=case.factory, sessions=case.sessions, saver=case.saver, dispatcher=RejectDispatcher(), publication_lock=case.lock)
    await recovered.start()
    try:
        assert set(counts) == pending
        assert all(count == 1 for count in counts.values())
        finished = case.control.get_collaboration_fanout(revision)
        assert finished.payload_json == record.payload_json and finished.event_hash == record.event_hash
        assert finished.state == "completed"
        assert case.control.get_lease(record.lease_id).state == "completed"
        for thread in (case.main, *children):
            assert sum(row[0] == revision for row in observation_rows(case, thread)) == 1
        with pytest.raises(RuntimeError, match="lease/token"):
            case.control.finish_collaboration_delivery(revision, case.main, fencing_token=old_token, outcome="delivered")
    finally:
        await recovered.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_point", ["receipt", "task_binding"])
async def test_original_wakeup_receipt_recovers_without_second_admission_or_job(team_case, crash_point):
    case = team_case
    child = await create_member(case, "receipt-member")
    intent = case.control.get_initial_execution_intent("delegate-receipt-member")
    claimed = case.control.claim_initial_execution_intent(intent.admission_idempotency_key,
        claim_owner="receipt-fixture", claim_generation=1)
    case.control.mark_initial_execution_bound(claimed.admission_idempotency_key,
        execution_binding_id=claimed.execution_binding_id, job_id=claimed.job_id,
        claim_owner=claimed.claim_owner, claim_generation=claimed.claim_generation)
    calls = []

    async def start_job(session_id, _content, **kwargs):
        calls.append(kwargs)
        return JobDispatchSnapshotDTO(session_id=session_id, thread_id=kwargs["thread_id"], job_id=kwargs["reserved_job_id"],
            job_status="queued", queued_jobs_ahead=0, queued_job_count=1, pending_job_count=1)

    dispatcher = RuntimeSourceDispatcher(saver=case.saver, session_service=case.sessions,
        job_service=SimpleNamespace(start_job=start_job))
    case.fanout._dispatcher = dispatcher
    await case.fanout.start()
    await case.fanout.stop()
    service = TeamCoordinationService(store=case.store, session_service=case.sessions, fanout=case.fanout,
        session_subagent_service=None, thread_creation_factory=case.factory)

    async def failed_finalize(record, job_id):
        if crash_point == "task_binding":
            await service.finalize_wakeup(record, job_id)
        raise OSError("回执已提交，业务finalizer收口前退出")

    case.fanout.bind_wakeup_finalizer(failed_finalize)
    with pytest.raises(RuntimeError, match="目标线程调度失败"):
        await service.assign_task(requester_session_id=SESSION, requester_thread_id=case.main,
            team_id=case.board.team_id, assignee_thread_id=child, title="原始任务", description="恢复原通知回执",
            phase="test", cycle=1, depends_on_task_ids=[], start_assignee=True)
    original = next(record for record in case.control.list_collaboration_fanouts() if record.wakeup_json is not None)
    assert original.wakeup_receipt_json is not None and original.state == "pending"
    original_task = case.store.get(SESSION, case.board.team_id).tasks[0]
    assert original_task.status == "failed"
    await service._boards.set_task_dispatch_failure(session_id=SESSION, team_id=case.board.team_id,
        task_id=original_task.task_id, actor_thread_id=case.main, error="同一原操作再次未收口")
    # 超过展示页的无关任务事件不能抹掉原任务 dispatch_failed 的恢复因果。
    for index in range(25):
        await service.assign_task(requester_session_id=SESSION, requester_thread_id=case.main,
            team_id=case.board.team_id, assignee_thread_id=case.main, title=f"无关任务{index}", description="不触发通知",
            phase="test", cycle=1, depends_on_task_ids=[], start_assignee=False)
    recovered = TeamFanoutService(factory=case.factory, sessions=case.sessions, saver=case.saver,
        dispatcher=dispatcher, publication_lock=case.lock)
    fresh_store = TeamStore(factory=case.factory, write_admission=case.sessions.session_write_admission)
    fresh = TeamCoordinationService(store=fresh_store, session_service=case.sessions, fanout=recovered,
        session_subagent_service=None, thread_creation_factory=case.factory)
    recovered.bind_wakeup_finalizer(fresh.finalize_wakeup)
    await recovered.start()
    try:
        finished = case.control.get_collaboration_fanout(original.ledger_revision)
        job_id = json.loads(original.wakeup_receipt_json)["job_id"]
        assert finished.state == "completed" and finished.wakeup_receipt_json == original.wakeup_receipt_json
        board = await fresh.get_board(requester_session_id=SESSION, requester_thread_id=case.main, team_id=case.board.team_id)
        restored = next(task for task in board.tasks if task.task_id == original_task.task_id)
        assert (restored.assigned_job_id, restored.status, restored.summary, restored.error) == (job_id, "in_progress", None, None)
        assert await recovered.dispatch(SESSION, original.ledger_revision) == job_id
        assert fresh_store.get(SESSION, case.board.team_id).version == board.version
        assert len(calls) == 1 and calls[0]["thread_id"] == child
        with pytest.raises(RuntimeError, match="其它 Job"):
            await fresh.finalize_wakeup(finished, "replacement-job")
        with sqlite3.connect(case.saver._storage.storage.index_path(SESSION, "", thread_id=child)) as connection:
            assert connection.execute("SELECT count(*) FROM executions").fetchone()[0] == 1
    finally:
        await recovered.stop()



@pytest.mark.asyncio
async def test_deleted_other_recipient_does_not_redirect_original_wakeup(team_case):
    case = team_case
    child = await create_member(case, "terminal-recipient")
    await case.fanout.reconcile(SESSION)
    message = internal_message_factory.build(kind="team_task_update", control="原协调者通知",
        sections=(PromptSection("control_context", {"team_id": case.board.team_id}), PromptSection("team_task_update", {"status": "completed"})),
        metadata={"source": "team_task_update"})
    wakeup = {"target_thread_id": case.main, "source_id": "task-update", "idempotency_key": "task-update",
              "content": message.content, "metadata": dict(message.metadata)}
    revision = await commit_board(case, name="一recipient终结", wakeup=wakeup)
    case.control.connection.execute("DELETE FROM thread_catalog WHERE thread_id=?", (child,))
    await case.fanout.reconcile(SESSION)
    record = case.control.get_collaboration_fanout(revision)
    assert next(item for item in record.recipients if item.thread_id == child).state == "recipient_deleted"
    assert next(item for item in record.recipients if item.thread_id == case.main).state == "delivered"
    assert record.state == "pending" and json.loads(record.wakeup_json)["target_thread_id"] == case.main
