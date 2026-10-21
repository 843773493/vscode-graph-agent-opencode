"""通过正式 canonical owner 验证替换来源、历史冻结与消息索引迁移。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Annotated, TypedDict
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from app.core.checkpoint_config import build_checkpoint_config
from app.domain.itemized.identity.item_projection import ItemProvenanceRelation
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    MessagesDeltaChannel,
    checkpoint_messages,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    canonical_message_anchor,
)
from app.services.infrastructure.rollout_context.checkpoint.mutation.mutation_intents import (
    MutationIntentConsumptionRejected,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    MESSAGES_VERSION_MIGRATION_SQL,
)
from tests.integration.backend.sessions.rollout_context._channel_version_fixtures import (
    restore_schema20,
)
from tests.integration.backend.sessions.rollout_context.test_rollout_checkpoint_saver import (
    SESSION_ID,
    _accept_user_execution,
    _checkpoint,
    _execution_scope,
)
from tests.integration.backend.sessions.rollout_context.test_rollout_checkpoint_saver import (
    start_model_call as model_call_fixture,
)

start_model_call = model_call_fixture


@pytest.fixture
def revision_case(tmp_path, session_bundle_factory, start_model_call):
    sessions = tmp_path / "sessions"
    session_bundle_factory(sessions, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions)
    thread = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="保留原始输入", id="revision-user")
    admission = _accept_user_execution(saver, SESSION_ID, thread, user, turn_id="revision-turn")
    index = saver._storage.storage.root(SESSION_ID, thread_id=thread) / "index.sqlite"
    with _execution_scope(admission):
        root_checkpoint = _checkpoint(str(uuid4()), [user])
        root_checkpoint["channel_versions"] = {"messages": "1"}
        root = saver.put(build_checkpoint_config(thread), root_checkpoint, {"source": "input", "step": -1}, {})
        start_model_call(saver, admission, user)
        with sqlite3.connect(index) as connection:
            model_call_id = connection.execute("SELECT model_call_id FROM model_calls").fetchone()[0]
        answer = AIMessage(content="原始正文", id="revision-answer", response_metadata={"model_call_id": model_call_id})
        source_checkpoint = _checkpoint(str(uuid4()), [user, answer])
        source_checkpoint["channel_versions"] = {"messages": "2"}
        child = saver.put(root, source_checkpoint, {"source": "loop", "step": 0}, {})
        carrier = saver.get_tuple(child).checkpoint["channel_values"]["messages"][-1]
        anchor = canonical_message_anchor(saver._storage.storage._codec().to_dict(carrier))
        source, = saver._storage.storage.read_items(SESSION_ID, thread, item_ids=(anchor,))
    revision = CanonicalItemRecord.create(
        item_sequence=source.item_sequence + 1, item_id="revision-new-anchor",
        semantic_kind=source.semantic_kind, payload_kind=source.payload_kind,
        status=source.status, producer_ref=source.producer_ref, payload="独立新正文",
        turn_id=source.turn_id, turn_scope=source.turn_scope, wire_role=source.wire_role,
        message_group_id="revision-new-group", projection_identity=source.projection_identity,
        provenance_relations=(ItemProvenanceRelation(
            relation="replaces", target_kind="canonical_item", target_id=source.item_id,
            target_session_id=SESSION_ID, target_thread_id=thread,
            target_turn_id=source.turn_id, target_model_call_id=model_call_id,
        ),),
    )
    return saver, admission, index, source, revision, child


def test_canonical_owner_publishes_revision_and_cold_reader_checks_absent_selection_source(revision_case):
    saver, admission, _index, source, revision, child = revision_case
    with _execution_scope(admission):
        saver.append_items(SESSION_ID, (revision,), thread_id=admission.thread_id)
    cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
    # 只读新正文时，来源仍由同一 catalog 的精确 anchor 校验。
    stored, = cold._storage.storage.read_items(SESSION_ID, admission.thread_id, item_ids=(revision.item_id,))
    assert stored.payload == "独立新正文"
    assert stored.provenance_relations == revision.provenance_relations
    original, = cold._storage.storage.read_items(SESSION_ID, admission.thread_id, item_ids=(source.item_id,))
    assert original.to_dict() == source.to_dict()
    with _execution_scope(admission):
        assert cold.get_tuple(child).checkpoint["channel_values"]["messages"][-1].content == "原始正文"


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_graph_same_id_replacement_publishes_new_canonical_version(revision_case, asynchronous):
    saver, admission, index, source, _revision, child = revision_case

    class State(TypedDict):
        messages: Annotated[list[AnyMessage], MessagesDeltaChannel(snapshot_frequency=2)]

    graph = StateGraph(State)
    graph.add_node("replace", lambda _: {"messages": [AIMessage(
        id="revision-answer", content="从真实图替换的正文",
        response_metadata={"model_call_id": source.projection_identity.model_call_id},
    )]})
    graph.add_edge(START, "replace")
    graph.add_edge("replace", END)
    compiled = graph.compile(checkpointer=saver)
    with _execution_scope(admission):
        if asynchronous:
            result = await compiled.ainvoke({"messages": []}, child)
        else:
            result = compiled.invoke({"messages": []}, child)
        assert result["messages"][-1].content == "从真实图替换的正文"
        cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
        latest = cold.get_tuple(build_checkpoint_config(admission.thread_id))
        messages = checkpoint_messages(cold, latest)
        assert messages[-1].content == "从真实图替换的正文"
        assert checkpoint_messages(cold, cold.get_tuple(child))[-1].content == "原始正文"
        new_anchor = canonical_message_anchor(saver._storage.storage._codec().to_dict(messages[-1]))
        assert new_anchor != source.item_id
        stored, = cold._storage.storage.read_items(admission.session_id, admission.thread_id, item_ids=(new_anchor,))
        assert stored.provenance_relations[-1].target_id == source.item_id
        assert stored.provenance_relations[-1].target_model_call_id == source.projection_identity.model_call_id
        # 同一输入再次执行只复用新版本，不再制造相同 logical ID 正文。
        if asynchronous:
            await compiled.ainvoke({"messages": []}, latest.config)
        else:
            compiled.invoke({"messages": []}, latest.config)
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT count(*) FROM messages WHERE message_id='revision-answer'").fetchone() == (2,)
    with _execution_scope(admission):
        # 同库的新版本不能改变旧 checkpoint 的完整 Turn；回退依赖冻结成员。
        cold.rewind(
            build_checkpoint_config(admission.thread_id),
            checkpoint_id=child["configurable"]["checkpoint_id"],
        )
        with cold._context_reader.open_snapshot(admission.session_id, admission.thread_id) as snapshot:
            head = snapshot.connection.execute(
                "SELECT head_view_id FROM branches WHERE branch_id=?",
                (snapshot.manifest.active_branch_id,),
            ).fetchone()[0]
            assert snapshot.connection.execute(
                "SELECT turn_id FROM context_view_turns WHERE view_id=?", (head,),
            ).fetchall() == [(admission.turn_id,)]
            source_view = snapshot.connection.execute(
                "SELECT view_id FROM checkpoints WHERE checkpoint_id=?",
                (child["configurable"]["checkpoint_id"],),
            ).fetchone()[0]
            source_items = snapshot.connection.execute(
                "SELECT item_id FROM context_view_items WHERE view_id=? AND visible=1 ORDER BY logical_item_ordinal",
                (source_view,),
            ).fetchall()
            assert snapshot.connection.execute(
                "SELECT item_id FROM context_view_items WHERE view_id=? AND visible=1 ORDER BY logical_item_ordinal",
                (head,),
            ).fetchall() == source_items
        # 真正截掉来源 view 必需的回答后，不能把不完整 Turn 登记为完整历史。
        cold.rewind(
            build_checkpoint_config(admission.thread_id),
            checkpoint_id=child["configurable"]["checkpoint_id"],
            source_anchor="revision-user",
        )
        with cold._context_reader.open_snapshot(admission.session_id, admission.thread_id) as snapshot:
            assert snapshot.connection.execute(
                "SELECT count(*) FROM context_view_turns WHERE view_id=("
                "SELECT head_view_id FROM branches WHERE branch_id=?)",
                (snapshot.manifest.active_branch_id,),
            ).fetchone() == (0,)


def test_independent_graph_branches_can_reuse_channel_version(revision_case):
    saver, admission, index, source, _revision, parent = revision_case

    class State(TypedDict):
        messages: Annotated[list[AnyMessage], MessagesDeltaChannel()]

    frozen = []
    with _execution_scope(admission):
        for text in ("分支 A 正文", "分支 B 正文"):
            graph = StateGraph(State)
            graph.add_node("replace", lambda _, text=text: {"messages": [AIMessage(
                id="revision-answer", content=text,
                response_metadata={"model_call_id": source.projection_identity.model_call_id},
            )]})
            graph.add_edge(START, "replace")
            graph.add_edge("replace", END)
            graph.compile(checkpointer=saver).invoke({"messages": []}, parent)
            frozen.append(saver.get_tuple(build_checkpoint_config(admission.thread_id)).config)
        cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
        assert [checkpoint_messages(cold, cold.get_tuple(config))[-1].content for config in frozen] == ["分支 A 正文", "分支 B 正文"]
        assert checkpoint_messages(cold, cold.get_tuple(parent))[-1].content == "原始正文"
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages WHERE message_id='revision-answer'").fetchone() == (3,)


@pytest.mark.parametrize("invalid", ["missing", "owner", "root", "no-relation"])
def test_owner_rejects_invalid_revision_without_publishing_canonical_bytes(revision_case, invalid):
    saver, admission, index, source, revision, _child = revision_case
    relation = revision.provenance_relations[0]
    if invalid == "missing":
        relation = replace(relation, target_id="missing-source")
        error = "source item 缺失"
    elif invalid == "owner":
        relation = replace(relation, target_session_id="foreign-session")
        error = "历史 SessionThread"
    elif invalid == "root":
        relation = replace(relation, target_id="item-revision-user")
        error = "accepted root"
    else:
        error = "新版本缺少精确 replaces 来源"
    revision = replace(revision, provenance_relations=() if invalid == "no-relation" else (relation,))
    jsonl = index.with_name("rollout.jsonl")
    before = jsonl.read_bytes()
    with _execution_scope(admission), pytest.raises(MutationIntentConsumptionRejected, match=error):
        saver.append_items(SESSION_ID, (revision,), thread_id=admission.thread_id)
    assert jsonl.read_bytes() == before
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT 1 FROM item_catalog WHERE item_id=?", (revision.item_id,)).fetchone() is None
    original, = saver._storage.storage.read_items(SESSION_ID, admission.thread_id, item_ids=(source.item_id,))
    assert original.to_dict() == source.to_dict()


@pytest.mark.parametrize("reader", ["items", "locator"])
def test_cold_read_rejects_corrupt_source_ownership_without_repair(revision_case, reader):
    saver, admission, index, _source, revision, _child = revision_case
    with _execution_scope(admission):
        saver.append_items(SESSION_ID, (revision,), thread_id=admission.thread_id)
    with sqlite3.connect(index) as connection:
        connection.execute("UPDATE model_calls SET thread_id='wrong-thread'")
    cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
    with pytest.raises(ValueError, match="source ModelCall/assembly owner"):
        if reader == "items":
            cold._storage.storage.read_items(SESSION_ID, admission.thread_id, item_ids=(revision.item_id,))
        else:
            with cold._storage.storage.read_connection(SESSION_ID, admission.thread_id, "") as connection:
                cold._storage.storage._read_canonical_item_from_catalog(
                    connection, session_id=SESSION_ID, thread_id=admission.thread_id,
                    checkpoint_ns="", item_id=revision.item_id,
                )
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT thread_id FROM model_calls").fetchone() == ("wrong-thread",)


def test_schema20_upgrade_preserves_all_locators_then_allows_explicit_revision(revision_case):
    saver, admission, index, source, revision, child = revision_case
    with sqlite3.connect(index) as connection:
        restore_schema20(connection)
        rows_before = connection.execute("SELECT * FROM messages ORDER BY message_sequence").fetchall()
    before = index.with_name("rollout.jsonl").read_bytes()
    saver._storage.storage.migrate_schema(
        admission.thread_id, session_id=SESSION_ID, to_version=21,
        migration_name="canonical_message_versions", migration_sql=MESSAGES_VERSION_MIGRATION_SQL,
    )
    assert index.with_name("rollout.jsonl").read_bytes() == before
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT * FROM messages ORDER BY message_sequence").fetchall() == rows_before
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (21, "active")
        assert connection.execute("SELECT from_version,to_version,status FROM schema_migrations ORDER BY migration_id").fetchall() == [(0, 20, "completed"), (20, 21, "completed")]
    cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
    with _execution_scope(admission):
        cold.append_items(SESSION_ID, (revision,), thread_id=admission.thread_id)
        assert cold.get_tuple(child).checkpoint["channel_values"]["messages"][-1].content == source.payload
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT count(*) FROM messages WHERE message_id='revision-answer'").fetchone() == (2,)


def test_interrupted_message_migration_rolls_back_schema_and_retains_backup(revision_case, monkeypatch):
    from app.services.infrastructure.rollout_context.storage import (
        rollout_maintenance_owner,
    )

    saver, admission, index, _source, _revision, _child = revision_case
    with sqlite3.connect(index) as connection:
        restore_schema20(connection)
        rows_before = connection.execute("SELECT * FROM messages ORDER BY message_sequence").fetchall()
        ddl_before = connection.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()
    before = index.with_name("rollout.jsonl").read_bytes()

    def fail_during_schema(connection, _script):
        connection.execute("ALTER TABLE messages RENAME TO messages_v20")
        connection.execute("DROP INDEX messages_turn_index")
        raise RuntimeError("消息结构迁移中断")

    monkeypatch.setattr(rollout_maintenance_owner, "execute_atomic_schema_sql", fail_during_schema)
    with pytest.raises(RuntimeError, match="消息结构迁移中断"):
        saver._storage.storage.migrate_schema(
            admission.thread_id, session_id=SESSION_ID, to_version=21,
            migration_name="canonical_message_versions", migration_sql=MESSAGES_VERSION_MIGRATION_SQL,
        )
    assert index.with_name("rollout.jsonl").read_bytes() == before
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT * FROM messages ORDER BY message_sequence").fetchall() == rows_before
        assert connection.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone() == ddl_before
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (20, "recovery_required")
        assert connection.execute("SELECT status FROM schema_migrations ORDER BY migration_id DESC LIMIT 1").fetchone() == ("failed",)
    backups = list(index.parent.glob("index.sqlite.migration-*.backup"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT * FROM messages ORDER BY message_sequence").fetchall() == rows_before
