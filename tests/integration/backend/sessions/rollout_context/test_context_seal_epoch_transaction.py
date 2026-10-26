"""来源、ToolSet 与 PrefixEpoch 随唯一 Saver seal 事务提交的集成证据。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.prefix_epoch import (
    AppendedItemRef,
    EpochReason,
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
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    payload_content_length,
    sha256_jcs,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.plans.contributions import ContextContribution, SourceBinding
from app.domain.itemized.plans.request import ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlMutation,
    ContextSourceControlState,
    ContextSourceOwnerKey,
)
from app.services.mapping.itemized.prefix_frames import project_canonical_prefix_frames
from tests.integration.backend.sessions.rollout_context.test_context_epoch_transition import (
    epoch_db as registry_db,
)

__all__ = ["registry_db"]


@pytest.fixture
def seal_inputs(registry_db):
    saver, session, _connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    tools = ToolSetSnapshot(
        selection_revision=sha256_jcs({"read_file": True}),
        tools=({"type": "function", "function": {"name": "read_file", "description": "读文件"}},),
        policy_keys=("filesystem_rules",),
    )
    source_specs = (
        ("instruction:R01", "agent_instructions", "Agent 说明", "本地编码助手"),
        ("instruction:R02", "runtime_identity", "运行身份", "当前用户的本地工作区"),
    )
    owner = ContextSourceOwnerKey(session, thread)
    for identity, kind, name, content in source_specs:
        revision = sha256_jcs(content)
        saver.register_context_contribution(session, thread, ContextContribution(
            contribution_id="source-root:" + identity, source_kind=kind,
            source_revision=revision, body=content,
            content_hash=contribution_content_hash("prompt", content),
            content_length=payload_content_length("structured_content", content),
            source_binding=SourceBinding(source_ref=identity), root_placement="root_eligible",
        ), request_content=content)
        saver.apply_context_source_control_mutation(ContextSourceControlMutation("register", ContextSourceControlState(
            owner=owner, source_id=identity, source_kind=kind,
            name=name, binding_revision=None, tracking_status="tracked",
            latest_visible_committed_revision=None, latest_revision=revision,
        )))

    def build(name, *, previous=None, reason=None, selected_tools=tools):
        transition = None
        if previous is not None and reason in {None, EpochReason.COMPACTION, EpochReason.REWIND}:
            checkpoint = empty_checkpoint()
            checkpoint["id"] = "checkpoint:" + name
            messages = [HumanMessage(
                content="真实用户输入", id=str(accepted["root_input_item_id"]).removeprefix("item-"),
            )]
            if reason is None:
                messages.append(AIMessage(content="下一次请求前的真实回答", id="seal-input:" + name))
            checkpoint["channel_values"] = {"messages": messages}
            if reason == EpochReason.COMPACTION:
                checkpoint["channel_values"]["_summarization_event"] = {
                    "cutoff_index": len(messages),
                    "cutoff_message_id": messages[-1].id,
                    "summary_message": HumanMessage(content="已完成对话的真实摘要", id="summary:" + name),
                }
            checkpoint["channel_versions"] = {
                key: checkpoint["id"] for key in checkpoint["channel_values"]
            }
            admission = saver.execution_admission(
                session, thread_id=thread, execution_id=str(accepted["initial_execution_id"]),
            )
            with (
                session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
                execution_admission_scope(admission, langgraph_thread_id=thread),
            ):
                config = saver.put(
                    {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}, checkpoint,
                    {"source": "compaction" if reason == EpochReason.COMPACTION else "loop", "step": 1},
                    checkpoint["channel_versions"],
                )
                if reason == EpochReason.REWIND:
                    saver.rewind(config, checkpoint_id=checkpoint["id"])
            if reason is not None:
                rebuild = saver.load_context_source_rebuild_state(owner)
                assert rebuild is not None and rebuild.transition.reason == reason
                transition = rebuild.transition
        with saver._context_reader.open_snapshot(session, thread, "") as read_snapshot:
            plan = saver.compose_committed_context_plan(
                session, thread, plan_id=name, tool_set=selected_tools, read_snapshot=read_snapshot,
            )
            items = saver._storage.checkpoints.committed_context_items(
                read_snapshot, predecessor=saver._storage.assemblies.latest_context_assembly(read_snapshot),
            )
        by_id = {source.contribution_id: source for source in plan.contributions}
        sources = tuple(
            RootCandidateSource(
                source_identity=identity, source_kind=kind, name=source_name,
                revision=sha256_jcs(content), content=content,
                source_ordinal=by_id["source-root:" + identity].source_ordinal,
                root_placement="root_eligible", included_reason="initial",
            )
            for identity, kind, source_name, content in source_specs
        )
        ref = plan.tool_set_refs[0]
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
        frames = []
        inherited = set()
        if previous is None or reason is not None:
            frames.append(ProviderProfileItemFrame(
                1, state.provider_profile, "system", "text", root.content,
                (AppendedItemRef("source_item", "root:" + name, "root:" + name, root.frame_hash(), "tracked"),),
            ))
        else:
            inherited = {
                entry.ref.ref_id for entry in previous.selection
                if entry.included and entry.ref.ref_type == "canonical_item"
            }
        canonical_frames, _selected_ids = project_canonical_prefix_frames(
            tuple(item for item in items if item.item_id not in inherited),
            provider_profile=state.provider_profile, start_ordinal=len(state.frames) + len(frames),
        )
        frames.extend(canonical_frames)
        state = append_items(
            state, frames, desired_provider_profile=state.provider_profile,
            desired_tool_compatibility_key=selected_tools.compatibility_key(ref),
        )
        preparation = ContextSealPreparation(
            prefix_state=state, root=root, tool_set=selected_tools,
            previous_assembly_id=None if previous is None else previous.assembly_id,
            source_revisions=applied, transition=transition,
        )
        admission = saver.execution_admission(session, thread_id=thread, execution_id=str(accepted["initial_execution_id"]))
        with saver._storage.storage.read_connection(session, thread, "") as connection:
            notice_ref, _selection = saver._storage.executions.runtime_notice_acceptance(admission, connection=connection)
        preparation = replace(preparation, notice_acceptance=notice_ref,
                              transition=None if transition is None else replace(transition, notice_acceptance=notice_ref))
        registered = saver.create_context_plan(session, replace(
            plan, plan_creation_idempotency_key="create:" + name,
        ))
        return registered.draft, preparation

    return build


def _seal(registry_db, inputs, *, saver=None):
    owner, session, _connection, accepted = registry_db
    plan, preparation = inputs
    return (saver or owner).seal_context_plan(
        session, plan.thread_id, plan, wire_profile=None, wire_params=None, turn_id=accepted["turn_id"],
        execution_id=accepted["initial_execution_id"], provider_version="epoch-provider",
        seal_idempotency_key="seal:" + plan.plan_id, target_format="chat_completions",
        context_preparation=preparation,
    )


def _source_rows(connection):
    return connection.execute(
        "SELECT source_id, latest_revision, latest_visible_committed_revision, state_revision "
        "FROM context_source_control_states ORDER BY source_id"
    ).fetchall()


def _sealed_facts(saver, connection):
    """失败前后比较完整事实与全部 detail；合法 unsealed draft 与失败审计可保留。"""
    queries = (
        "SELECT * FROM item_catalog ORDER BY item_sequence",
        "SELECT * FROM item_projections ORDER BY item_sequence",
        "SELECT * FROM context_source_control_states ORDER BY source_id",
        "SELECT * FROM context_epoch_transitions ORDER BY transition_id",
        "SELECT * FROM context_assemblies ORDER BY assembly_id",
        "SELECT * FROM assembly_item_refs ORDER BY assembly_id, ref_ordinal",
        "SELECT * FROM context_assembly_contributions ORDER BY assembly_id, contribution_ordinal",
        "SELECT * FROM context_assembly_selections ORDER BY assembly_id, plan_ordinal",
        "SELECT * FROM context_plan_details ORDER BY detail_ref",
        "SELECT * FROM context_contributions ORDER BY assembly_id, contribution_id",
        "SELECT * FROM context_plans ORDER BY session_id, thread_id, plan_id",
        "SELECT * FROM context_plan_refs ORDER BY session_id, thread_id, plan_id, ref_type, ref_id",
        "SELECT * FROM context_plan_contributions ORDER BY session_id, thread_id, plan_id, contribution_id",
        "SELECT * FROM tool_set_snapshots ORDER BY session_id, thread_id, plan_id, tool_set_snapshot_id",
        "SELECT * FROM storage_commits ORDER BY commit_id",
        "SELECT * FROM database_meta ORDER BY singleton_id",
        "SELECT * FROM model_calls ORDER BY model_call_id",
        "SELECT * FROM checkpoint_context_source_snapshots ORDER BY checkpoint_id",
        "SELECT * FROM checkpoint_context_source_states ORDER BY checkpoint_id, source_id",
    )
    return (
        tuple(connection.execute(query).fetchall() for query in queries),
        tuple((key, tuple(item["assembly_id"] for item in queue))
              for key, queue in sorted(saver._prepared_dispatches.items())),
        tuple(
            (path.relative_to(saver._storage.storage.sessions_dir).as_posix(), path.read_bytes())
            for path in sorted(saver._storage.storage.sessions_dir.rglob("*"))
            if path.is_file() and (
                "context-plan-details" in path.parts
            )
        ),
    )


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

    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        monkeypatch.setattr(
            "app.services.infrastructure.rollout_context.checkpoint.seal.lifecycle.uuid4",
            refuse_allocation,
        )
        monkeypatch.setattr(restarted._detail_store, "write", refuse_allocation)
        monkeypatch.setattr(restarted._detail_store, "prepare", refuse_allocation)
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
    committed_facts = _sealed_facts(saver, connection)
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
        original_commit = saver._storage.storage._commit_connection

        def refuse_commit(candidate):
            if candidate.execute("SELECT count(*) FROM context_assemblies").fetchone() == (1,):
                assert all(row[1] == row[2] for row in _source_rows(candidate))
                raise RuntimeError("injected final commit failure")
            # 失败 control outcome 是独立事务，仍应正常提交并暴露原始失败。
            original_commit(candidate)

        monkeypatch.setattr(saver._storage.storage, "_commit_connection", refuse_commit)
    with pytest.raises((RuntimeError, sqlite3.IntegrityError), match="injected"):
        _seal(registry_db, inputs)
    _assert_unsealed(connection, inputs[0], before, before_commits)
    assert _sealed_facts(saver, connection) == committed_facts
    assert not inputs[1].prefix_state.epoch_applied


@pytest.mark.parametrize("failure", ["publish", "commit"])
def test_first_sensitive_header_failure_leaves_no_detail_or_key(
    registry_db, monkeypatch, failure,
):
    saver, session, connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    plan = saver.create_context_plan(session, ContextRequestPlan(
        session_id=session, thread_id=thread, plan_id="sensitive-header",
        refs=(), plan_creation_idempotency_key="sensitive-header:create",
    )).draft
    snapshot = saver._composer_for(session, thread, "").assembly(
        plan=plan, session_id=session, thread_id=thread, assembly_id="sensitive-header:assembly",
        turn_id=accepted["turn_id"], execution_id=accepted["initial_execution_id"],
        provider_version="header-provider",
    )
    options = {
        "seal_idempotency_key": "sensitive-header:seal",
        "seal_input_hash": sha256_jcs("sensitive-header:input"),
        "detail": {"secret": "只允许在成功 seal 后留下详情"},
        "sensitive_detail": True,
    }
    before = _sealed_facts(saver, connection)
    with monkeypatch.context() as injected:
        if failure == "publish":
            def refuse_publish(*_args, **_kwargs):
                raise RuntimeError("injected detail publish failure")

            injected.setattr(saver._detail_store._files, "publish", refuse_publish)
        else:
            original_commit = saver._storage.storage._commit_connection

            def refuse_commit(candidate):
                if candidate.execute("SELECT count(*) FROM context_assemblies").fetchone() == (1,):
                    raise RuntimeError("injected detail commit failure")
                original_commit(candidate)

            injected.setattr(saver._storage.storage, "_commit_connection", refuse_commit)
        with pytest.raises(RuntimeError, match="injected detail"):
            saver.seal_context_assembly(session, thread, snapshot, **options)
    assert _sealed_facts(saver, connection) == before
    assert connection.execute(
        "SELECT plan_state, assembly_id FROM context_plans WHERE plan_id=?", (plan.plan_id,),
    ).fetchone() == ("unsealed", None)
    saver.seal_context_assembly(session, thread, snapshot, **options)
    assert saver._storage.assemblies.get_context_assembly(
        session, thread, assembly_id=snapshot.assembly_id,
    ).to_dict() == snapshot.to_dict()
    assert connection.execute(
        "SELECT count(*) FROM context_plan_details WHERE assembly_id=?", (snapshot.assembly_id,),
    ).fetchone() == (1,)


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
    if changed_revision == "observed":
        saver.apply_context_source_control_mutation(ContextSourceControlMutation("observe", changed))
    else:
        # 直接注入损坏，验证 seal 拒绝错误的 applied；正常控制端口禁止这种写入。
        connection.execute(
            "UPDATE context_source_control_states SET latest_visible_committed_revision=? "
            "WHERE session_id=? AND thread_id=? AND source_id=?",
            (changed.latest_visible_committed_revision, stored.owner.session_id,
             stored.owner.thread_id, stored.source_id),
        )
        connection.commit()
    before = _source_rows(connection)
    with pytest.raises(RuntimeError, match="context-source-seal-conflict"):
        _seal(registry_db, (plan, preparation))
    _assert_unsealed(connection, plan, before, [])


def test_source_applied_requires_a_selected_root_or_tail_frame(registry_db, seal_inputs):
    saver, _session, connection, _accepted = registry_db
    plan, preparation = seal_inputs("unselected-source")
    revision = sha256_jcs("不存在于本次请求")
    saver.apply_context_source_control_mutation(ContextSourceControlMutation("register", ContextSourceControlState(
        owner=ContextSourceOwnerKey(plan.session_id, plan.thread_id),
        source_id="instruction:unselected", source_kind="runtime_identity", name="未选来源",
        binding_revision=None, tracking_status="tracked",
        latest_visible_committed_revision=None, latest_revision=revision,
    )))
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
    saver, session, connection, _accepted = registry_db
    first = _seal(registry_db, seal_inputs("first"))
    second = _seal(registry_db, seal_inputs("same-epoch", previous=first))
    assert second.seal_binding.root == first.seal_binding.root
    assert second.seal_binding.prefix.parent == first.seal_binding.prefix.state.parent
    assert second.seal_binding.prefix.prefix_byte_length > first.seal_binding.prefix.prefix_byte_length
    inputs = seal_inputs("rebuild", previous=second, reason=reason)
    transition = inputs[1].transition
    before = _source_rows(connection)
    pending = connection.execute("SELECT * FROM context_epoch_transitions").fetchall()
    committed_facts = _sealed_facts(saver, connection)
    old_snapshots = tuple(canonical_json_bytes(snapshot.to_dict()) for snapshot in (first, second))
    connection.execute(
        "CREATE TRIGGER refuse_epoch_consumption BEFORE UPDATE ON context_epoch_transitions "
        "WHEN NEW.status='consumed' BEGIN SELECT RAISE(ABORT, 'injected transition failure'); END"
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected transition failure"):
        _seal(registry_db, inputs)
    assert _source_rows(connection) == before
    assert connection.execute("SELECT * FROM context_epoch_transitions").fetchall() == pending
    assert _sealed_facts(saver, connection) == committed_facts
    assert connection.execute("SELECT assembly_id FROM context_assemblies ORDER BY rowid").fetchall() == [
        (first.assembly_id,), (second.assembly_id,),
    ]
    assert connection.execute("SELECT plan_state, assembly_id FROM context_plans WHERE plan_id='rebuild'").fetchone() == ("unsealed", None)
    connection.execute("DROP TRIGGER refuse_epoch_consumption")
    connection.commit()
    rebuilt = _seal(registry_db, inputs)
    assert rebuilt.seal_binding.prefix.state.epoch.ordinal == 2
    assert rebuilt.seal_binding.prefix.state.epoch.reason == reason
    assert rebuilt.seal_binding.prefix.consumed_transition.consumed
    assert not transition.consumed
    assert _source_rows(connection) == before
    assert connection.execute(
        "SELECT status, consumed_assembly_id FROM context_epoch_transitions WHERE transition_id=?",
        (transition.transition_id,),
    ).fetchone() == ("consumed", rebuilt.assembly_id)
    assert tuple(canonical_json_bytes(saver.get_context_assembly(
        session, snapshot.thread_id, assembly_id=snapshot.assembly_id,
    ).to_dict()) for snapshot in (first, second)) == old_snapshots


def test_tail_source_delta_applies_the_exact_prior_revision_with_same_root(registry_db, seal_inputs):
    saver, session, connection, _accepted = registry_db
    first = _seal(registry_db, seal_inputs("first"))
    owner = ContextSourceOwnerKey(session, first.thread_id)
    stored = saver.load_context_source_control_states(owner)[0]
    revision = sha256_jcs("最新的 Agent 说明")
    saver.apply_context_source_control_mutation(ContextSourceControlMutation("observe", replace(stored, latest_revision=revision)))
    delta_content = "Agent 说明的新 delta"
    saver.register_context_contribution(session, first.thread_id, ContextContribution(
        contribution_id="source:delta", source_kind=stored.source_kind,
        source_revision=revision, contribution_kind="notice", body=delta_content,
        content_hash=contribution_content_hash("notice", delta_content),
        content_length=payload_content_length("structured_content", delta_content),
        source_binding=SourceBinding(source_ref=stored.source_id),
    ), request_content=delta_content)
    plan, preparation = seal_inputs("delta", previous=first)
    frame = ProviderProfileItemFrame(
        len(preparation.prefix_state.frames) + 1, preparation.prefix_state.provider_profile,
        "user", "text", delta_content,
        (AppendedItemRef("source_item", "source:delta", stored.source_id, revision, "tracked"),),
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
