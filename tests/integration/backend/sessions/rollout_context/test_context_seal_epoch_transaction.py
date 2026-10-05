"""来源、ToolSet 与 PrefixEpoch 随唯一 Saver seal 事务提交的集成证据。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from app.domain.itemized.epoch.prefix_epoch import (
    AppendedItemRef,
    EpochReason,
    PendingPrefixEpochTransition,
    ProviderProfileItemFrame,
    append_items,
    initial_epoch_state,
    open_rebuild_epoch,
)
from app.domain.itemized.epoch.root_compilation import (
    RootCandidateSource,
    compile_root_item,
)
from app.domain.itemized.epoch.seal_binding import (
    AppliedSourceRevision,
    ContextSealPreparation,
)
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlState,
    ContextSourceOwnerKey,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - fixture 产物按当前测试路径隔离
)


@pytest.fixture
def seal_inputs(registry_db):
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.main_thread_id(session)
    tools = ToolSetSnapshot(
        selection_revision=sha256_jcs({"read_file": True}),
        tools=({"type": "function", "function": {"name": "read_file", "description": "读文件"}},),
        policy_keys=("filesystem_rules",),
    )
    sources = tuple(
        RootCandidateSource(
            source_identity=identity, source_kind=kind, name=name,
            revision=sha256_jcs(content), content=content, source_ordinal=ordinal,
            root_placement="root_eligible", included_reason="initial",
        )
        for ordinal, (identity, kind, name, content) in enumerate((
            ("instruction:R01", "agent_instructions", "Agent 说明", "本地编码助手"),
            ("instruction:R02", "runtime_identity", "运行身份", "当前用户的本地工作区"),
        ))
    )
    owner = ContextSourceOwnerKey(session, thread)
    for source in sources:
        saver.save_context_source_control_state(ContextSourceControlState(
            owner=owner, source_id=source.source_identity, source_kind=source.source_kind,
            name=source.name, binding_revision=None, tracking_status="tracked",
            latest_visible_committed_revision=None, latest_revision=source.revision,
        ))

    def build(name, *, previous=None, reason=None, selected_tools=tools):
        ref = selected_tools.bind(session_id=session, thread_id=thread, plan_id=name)
        transition = None
        if previous is None:
            state = initial_epoch_state(
                provider_profile="openai-chat:v1",
                tool_compatibility_key=selected_tools.compatibility_key(ref),
            )
            root = compile_root_item(state, sources)
            applied = tuple(AppliedSourceRevision(
                source.source_identity, source.revision, "decision:" + source.source_identity,
            ) for source in sources)
        elif reason is not None:
            if reason in {EpochReason.COMPACTION, EpochReason.REWIND}:
                transition = PendingPrefixEpochTransition("transition:" + name, reason)
            state = open_rebuild_epoch(
                previous.seal_binding.prefix.state, reason=reason, transition=transition,
                provider_profile=previous.seal_binding.prefix.state.provider_profile,
                tool_compatibility_key=selected_tools.compatibility_key(ref),
            )
            root = compile_root_item(state, sources)
            applied = ()
        else:
            state = previous.seal_binding.prefix.state
            root = previous.seal_binding.root
            applied = ()
        if previous is None or reason is not None:
            frame = ProviderProfileItemFrame(
                1, state.provider_profile, "system", "text", root.content,
                AppendedItemRef("source_item", "root:" + name, "root:" + name, root.frame_hash(), "tracked"),
            )
        else:
            frame = ProviderProfileItemFrame(
                len(state.frames) + 1, state.provider_profile, "user", "text", "下一次请求",
                AppendedItemRef("canonical_item", "user:" + name),
            )
        state = append_items(
            state, (frame,), desired_provider_profile=state.provider_profile,
            desired_tool_compatibility_key=selected_tools.compatibility_key(ref),
        )
        preparation = ContextSealPreparation(
            prefix_state=state, root=root, tool_set=selected_tools,
            previous_assembly_id=None if previous is None else previous.assembly_id,
            source_revisions=applied, transition=transition,
        )
        plan = ContextRequestPlan(
            session_id=session, thread_id=thread, plan_id=name, refs=(),
            tool_set_refs=(ref,), tool_snapshot=selected_tools.tools,
            plan_creation_idempotency_key="create:" + name,
        )
        registered = saver.create_context_plan(session, plan)
        return registered.draft, preparation

    return build


def _seal(registry_db, inputs, *, saver=None):
    owner, session, _connection, accepted = registry_db
    plan, preparation = inputs
    return (saver or owner).seal_context_plan(
        session, plan.thread_id, plan, turn_id=accepted["turn_id"],
        execution_id=accepted["initial_execution_id"], provider_version="epoch-provider",
        seal_idempotency_key="seal:" + plan.plan_id, target_format="chat_completions",
        context_preparation=preparation,
    )


def _source_rows(connection):
    return connection.execute(
        "SELECT source_id, latest_revision, latest_visible_committed_revision, state_revision "
        "FROM context_source_control_states ORDER BY source_id"
    ).fetchall()


def _assert_unsealed(connection, plan, source_rows, before_commits):
    assert _source_rows(connection) == source_rows
    assert connection.execute(
        "SELECT plan_state, assembly_id FROM context_plans WHERE plan_id=?", (plan.plan_id,),
    ).fetchone() == ("unsealed", None)
    assert connection.execute(
        "SELECT assembly_id FROM tool_set_snapshots WHERE plan_id=?", (plan.plan_id,),
    ).fetchone() == (None,)
    for table in ("context_assemblies", "context_assembly_selections", "assembly_item_refs"):
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert connection.execute(
        "SELECT commit_id FROM storage_commits WHERE commit_kind='assembly_sealed'"
    ).fetchall() == before_commits


def test_success_applies_sources_tools_epoch_once_and_restart_retry_reuses_them(
    registry_db, seal_inputs, monkeypatch,
):
    saver, session, connection, _accepted = registry_db
    inputs = seal_inputs("first")
    plan, preparation = inputs
    before = _source_rows(connection)
    assert all(row[2] is None for row in before)
    assert not preparation.prefix_state.epoch_applied
    snapshot = _seal(registry_db, inputs)
    assert snapshot.seal_binding.prefix.state.epoch_applied
    assert snapshot.seal_binding.applied_toolset_revision == preparation.tool_set.revision
    after = _source_rows(connection)
    assert all(row[1] == row[2] and row[3] == old[3] + 1 for row, old in zip(after, before))
    assert not preparation.prefix_state.epoch_applied
    assert connection.execute(
        "SELECT count(*) FROM storage_commits WHERE commit_kind='assembly_sealed'"
    ).fetchone() == (1,)

    def refuse_allocation(*_args, **_kwargs):
        pytest.fail("重启重试不得分配 assembly/detail 或再编译准备值")

    with RolloutCheckpointSaver(saver._storage.sessions_dir) as restarted:
        monkeypatch.setattr(
            "app.services.infrastructure.rollout_context.checkpoint.seal.lifecycle.uuid4",
            refuse_allocation,
        )
        monkeypatch.setattr(restarted._detail_store, "write", refuse_allocation)
        monkeypatch.setattr(ContextSealPreparation, "bind", refuse_allocation)
        restored = restarted.get_context_plan_registration(
            session, thread_id=plan.thread_id, plan_id=plan.plan_id,
        )
        retry = _seal(registry_db, (restored.draft, preparation), saver=restarted)
    assert retry.to_dict() == snapshot.to_dict()
    assert _source_rows(connection) == after
    assert connection.execute(
        "SELECT count(*) FROM storage_commits WHERE commit_kind='assembly_sealed'"
    ).fetchone() == (1,)


@pytest.mark.parametrize("failure", ["second_source", "commit"])
def test_failure_after_source_update_rolls_back_every_applied_fact(
    registry_db, seal_inputs, monkeypatch, failure,
):
    saver, _session, connection, _accepted = registry_db
    inputs = seal_inputs("rollback")
    before = _source_rows(connection)
    before_commits = connection.execute(
        "SELECT commit_id FROM storage_commits WHERE commit_kind='assembly_sealed'"
    ).fetchall()
    if failure == "second_source":
        connection.execute(
            "CREATE TRIGGER refuse_second_source BEFORE UPDATE ON context_source_control_states "
            "WHEN NEW.source_id='instruction:R02' "
            "BEGIN SELECT RAISE(ABORT, 'injected second-source failure'); END"
        )
        connection.commit()
    else:
        original_commit = saver._storage._commit_connection

        def refuse_commit(candidate):
            if candidate.execute("SELECT count(*) FROM context_assemblies").fetchone() == (1,):
                assert all(row[1] == row[2] for row in _source_rows(candidate))
                raise RuntimeError("injected final commit failure")
            # 失败 control outcome 是独立事务，仍应正常提交并暴露原始失败。
            original_commit(candidate)

        monkeypatch.setattr(saver._storage, "_commit_connection", refuse_commit)
    with pytest.raises((RuntimeError, sqlite3.IntegrityError), match="injected"):
        _seal(registry_db, inputs)
    _assert_unsealed(connection, inputs[0], before, before_commits)
    assert not inputs[1].prefix_state.epoch_applied


@pytest.mark.parametrize("changed_revision", ["observed", "applied"])
def test_stale_source_revision_rejects_and_rolls_back_the_whole_seal(
    registry_db, seal_inputs, changed_revision,
):
    saver, _session, connection, _accepted = registry_db
    plan, preparation = seal_inputs("stale-source")
    stored = saver.load_context_source_control_states(
        ContextSourceOwnerKey(plan.session_id, plan.thread_id)
    )[0]
    changed = (
        replace(stored, latest_revision=sha256_jcs("new desired"))
        if changed_revision == "observed"
        else replace(stored, latest_visible_committed_revision=stored.latest_revision)
    )
    saver.save_context_source_control_state(changed)
    before = _source_rows(connection)
    with pytest.raises(RuntimeError, match="context-source-seal-conflict"):
        _seal(registry_db, (plan, preparation))
    _assert_unsealed(connection, plan, before, [])


def test_source_applied_requires_a_selected_root_or_tail_frame(registry_db, seal_inputs):
    saver, _session, connection, _accepted = registry_db
    plan, preparation = seal_inputs("unselected-source")
    revision = sha256_jcs("不存在于本次请求")
    saver.save_context_source_control_state(ContextSourceControlState(
        owner=ContextSourceOwnerKey(plan.session_id, plan.thread_id),
        source_id="instruction:unselected", source_kind="runtime_identity", name="未选来源",
        binding_revision=None, tracking_status="tracked",
        latest_visible_committed_revision=None, latest_revision=revision,
    ))
    preparation = replace(preparation, source_revisions=(
        AppliedSourceRevision("instruction:unselected", revision, "decision:unselected"),
    ))
    before = _source_rows(connection)
    with pytest.raises(ValueError, match="没有真实 selected frame"):
        _seal(registry_db, (plan, preparation))
    _assert_unsealed(connection, plan, before, [])


def test_prepared_competing_assembly_uses_exact_predecessor_cas(registry_db, seal_inputs):
    _saver, _session, connection, _accepted = registry_db
    first = _seal(registry_db, seal_inputs("first"))
    next_inputs = seal_inputs("next", previous=first)
    stale_inputs = seal_inputs("stale", previous=first)
    second = _seal(registry_db, next_inputs)
    before = _source_rows(connection)
    commits = connection.execute(
        "SELECT commit_id FROM storage_commits WHERE commit_kind='assembly_sealed'"
    ).fetchall()
    with pytest.raises(RuntimeError, match="context-seal-predecessor-conflict"):
        _seal(registry_db, stale_inputs)
    assert _source_rows(connection) == before
    assert connection.execute(
        "SELECT commit_id FROM storage_commits WHERE commit_kind='assembly_sealed'"
    ).fetchall() == commits
    assert connection.execute(
        "SELECT assembly_id FROM context_assemblies ORDER BY rowid"
    ).fetchall() == [(first.assembly_id,), (second.assembly_id,)]
    assert connection.execute(
        "SELECT plan_state, assembly_id FROM context_plans WHERE plan_id='stale'"
    ).fetchone() == ("unsealed", None)


@pytest.mark.parametrize("reason", [EpochReason.COMPACTION, EpochReason.REWIND])
def test_rebuild_consumes_transition_only_in_successful_seal(registry_db, seal_inputs, reason):
    _saver, _session, connection, _accepted = registry_db
    first = _seal(registry_db, seal_inputs("first"))
    second = _seal(registry_db, seal_inputs("same-epoch", previous=first))
    assert second.seal_binding.root == first.seal_binding.root
    assert second.seal_binding.prefix.parent == first.seal_binding.prefix.state.parent
    assert second.seal_binding.prefix.prefix_byte_length > first.seal_binding.prefix.prefix_byte_length
    inputs = seal_inputs("rebuild", previous=second, reason=reason)
    transition = inputs[1].transition
    before = _source_rows(connection)
    rebuilt = _seal(registry_db, inputs)
    assert rebuilt.seal_binding.prefix.state.epoch.ordinal == 2
    assert rebuilt.seal_binding.prefix.state.epoch.reason == reason
    assert rebuilt.seal_binding.prefix.consumed_transition.consumed
    assert not transition.consumed
    assert _source_rows(connection) == before


def test_tail_source_delta_applies_the_exact_prior_revision_with_same_root(registry_db, seal_inputs):
    saver, session, connection, _accepted = registry_db
    first = _seal(registry_db, seal_inputs("first"))
    plan, preparation = seal_inputs("delta", previous=first)
    owner = ContextSourceOwnerKey(session, plan.thread_id)
    stored = saver.load_context_source_control_states(owner)[0]
    revision = sha256_jcs("最新的 Agent 说明")
    saver.save_context_source_control_state(replace(stored, latest_revision=revision))
    frame = ProviderProfileItemFrame(
        len(preparation.prefix_state.frames) + 1, preparation.prefix_state.provider_profile,
        "user", "text", "Agent 说明的新 delta",
        AppendedItemRef("source_item", "source:delta", stored.source_id, revision, "tracked"),
    )
    state = append_items(
        preparation.prefix_state, (frame,),
        desired_provider_profile=preparation.prefix_state.provider_profile,
        desired_tool_compatibility_key=preparation.prefix_state.tool_compatibility_key,
    )
    preparation = replace(preparation, prefix_state=state, source_revisions=(
        AppliedSourceRevision(stored.source_id, revision, "decision:delta", stored.latest_revision),
    ))
    before = _source_rows(connection)
    snapshot = _seal(registry_db, (plan, preparation))
    assert snapshot.seal_binding.root == first.seal_binding.root
    assert snapshot.seal_binding.prefix.state.frames[-1].role == "user"
    after = _source_rows(connection)
    assert after[0][1] == after[0][2] == revision
    assert after[0][3] == before[0][3] + 1
    assert after[1] == before[1]


def test_provider_visible_tool_change_rebases_and_applies_only_by_seal(registry_db, seal_inputs):
    first = _seal(registry_db, seal_inputs("first"))
    changed = ToolSetSnapshot(
        selection_revision=sha256_jcs({"read_file": True, "write_file": True}),
        tools=(
            {"type": "function", "function": {"name": "read_file", "description": "读文件"}},
            {"type": "function", "function": {"name": "write_file", "description": "写文件"}},
        ),
        policy_keys=("filesystem_rules",),
    )
    plan, preparation = seal_inputs(
        "changed-tools", previous=first, reason=EpochReason.TOOLSET_CHANGED,
        selected_tools=changed,
    )
    assert not preparation.prefix_state.epoch_applied
    assert first.seal_binding.applied_toolset_revision != changed.revision
    snapshot = _seal(registry_db, (plan, preparation))
    assert snapshot.seal_binding.prefix.state.epoch.ordinal == 2
    assert snapshot.seal_binding.prefix.state.epoch.reason == EpochReason.TOOLSET_CHANGED
    assert snapshot.seal_binding.applied_toolset_revision == changed.revision
    assert snapshot.seal_binding.root != first.seal_binding.root
    assert first.seal_binding.prefix.state.epoch.ordinal == 1
    assert not preparation.prefix_state.epoch_applied
