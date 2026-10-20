"""rollout 持久化定位的 thread-qualified 合同（OpenSpec 8.3 主项）。

rollout 物理节点、index.sqlite 与 rollout.jsonl 由精确 ``(session_id, thread_id)``
的 thread node 独占：main thread 经 thread catalog 冻结的 ``main_thread_id`` 解析，
非 main durable thread 经其 frozen locator 解析到独立节点。裸 ``main`` 别名与裸
session_id 不是 thread identity，必须经 thread catalog 显式解析，绝不把它们当作
thread_id 拼路径；thread 维度缺省或非法时 fail closed。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex, uuid7_datetime_from_hex
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
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
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from app.services.infrastructure.rollout_history.snapshot import IndexedHistorySnapshots
from tests.support.canonical_id_at import thread_id_at
from tests.support.catalog_session_bundle import (
    seed_catalog_session_bundle,
)

SESSION_ID = f"ses_{create_uuid_hex()}"
CHILD_CREATED_AT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
CHILD_THREAD_ID = thread_id_at(CHILD_CREATED_AT)




def _publish_child_thread(
    session_dir: Path,
    *,
    thread_id: str,
) -> Path:
    """用 R20 store API 发布最小 child，再创建其受检物理目录。"""
    control = SessionControlStore(session_dir / "session-control.sqlite")
    key = f"rollout-locating-{thread_id}"
    try:
        control.create_or_get_thread_creation_record(
            idempotency_key=key,
            initial_state="idle",
            preimage_hash="a" * 64,
            graph_binding=json.dumps(
                {
                    "graph_id": "rollout-locating",
                    "graph_revision": 1,
                    "graph_schema_hash": "sha256:" + "a" * 64,
                    "capability_profile_hash": "sha256:" + "b" * 64,
                }
            ),
            capability_profile=json.dumps({}),
            created_at=uuid7_datetime_from_hex(thread_id[4:]),
            thread_id=thread_id,
        )
        manifest = "{}"
        control.freeze_thread_creation_artifact_manifest(
            key,
            artifact_manifest=manifest,
            artifact_manifest_hash=hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
        )
        published = control.publish_thread_creation_record(key, session_id=session_dir.name)
        locator = published.final_relative_locator
    finally:
        control.close()
    thread_dir = session_dir / locator
    thread_dir.mkdir(parents=True)
    return thread_dir


@pytest.fixture
def storage(tmp_path: Path) -> RolloutContextStore:
    root = tmp_path / ".boxteam" / "sessions"
    root.mkdir(parents=True)
    seed_catalog_session_bundle(root, SESSION_ID, title="thread 定位")
    return RolloutContextStore(
        root,
        serde=JsonPlusSerializer(),
        message_codec=LangChainMessageCodec(),
    )


def test_main_thread_resolves_to_catalog_thread_node_rollout(
    storage: RolloutContextStore,
) -> None:
    main_thread_id = storage.storage._path_resolver.main_thread_id(SESSION_ID)
    session_node = storage.storage._path_resolver.resolve_session_node(SESSION_ID)
    thread_node = storage.storage._path_resolver.resolve_thread_node(SESSION_ID, main_thread_id)
    assert storage.storage.root(SESSION_ID) == thread_node / "rollout"
    assert storage.storage.root(SESSION_ID, thread_id=main_thread_id) == thread_node / "rollout"
    assert storage.storage.index_path(SESSION_ID) == thread_node / "rollout" / "index.sqlite"
    assert storage.storage.jsonl_path(SESSION_ID) == thread_node / "rollout" / "rollout.jsonl"
    assert thread_node == session_node / "threads" / main_thread_id
    assert not (session_node / "rollout").exists()


@pytest.mark.parametrize(("fault", "error_type", "message"), [
    ("missing_meta", RuntimeError, "rollout database_meta 缺失"),
    ("empty_owner", ValueError, "rollout 当前 SessionThread owner 非法"),
    ("recovery_required", ValueError, "rollout 当前 owner 缺失或不是 active"),
])
def test_read_snapshot_rejects_missing_owner_and_inactive_database(
    storage: RolloutContextStore, fault: str, error_type: type[Exception], message: str,
) -> None:
    thread = storage.storage.main_thread_id(SESSION_ID)
    storage.storage.initialize(SESSION_ID, thread_id=thread)
    with storage.storage._connect(thread, "", session_id=SESSION_ID) as connection:
        if fault == "missing_meta":
            connection.execute("DELETE FROM database_meta")
        elif fault == "empty_owner":
            connection.execute("UPDATE database_meta SET thread_id=''")
        else:
            connection.execute("UPDATE database_meta SET database_state='recovery_required'")
    with pytest.raises(error_type, match=message):
        storage.storage.open_read_snapshot(SESSION_ID, thread)


def test_main_thread_owner_resolves_to_one_rollout_lock_and_rejects_session_alias(
    storage: RolloutContextStore,
) -> None:
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    main_lock = storage.storage._lock(thread_id, "", session_id=SESSION_ID)
    assert main_lock is storage.storage._lock(
        thread_id,
        "",
        session_id=SESSION_ID,
    )
    assert main_lock is storage.storage._lock(
        thread_id,
        "branch:parallel",
        session_id=SESSION_ID,
    )
    _publish_child_thread(
        storage.storage._path_resolver.resolve_session_node(SESSION_ID),
        thread_id=CHILD_THREAD_ID,
    )
    assert main_lock is not storage.storage._lock(
        CHILD_THREAD_ID,
        "branch:parallel",
        session_id=SESSION_ID,
    )
    with pytest.raises(RuntimeError, match="拒绝 thread alias"):
        storage.storage._lock(SESSION_ID, "", session_id=SESSION_ID)


def test_initialize_reenters_owner_lock_across_checkpoint_namespaces(
    storage: RolloutContextStore,
) -> None:
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    default_root = storage.storage.root(SESSION_ID, thread_id=thread_id)
    namespace_root = storage.storage.root(
        SESSION_ID,
        "branch:parallel",
        thread_id=thread_id,
    )
    assert namespace_root == default_root
    storage.storage.initialize(SESSION_ID, thread_id=thread_id)
    storage.storage.initialize(
        SESSION_ID,
        "branch:parallel",
        thread_id=thread_id,
    )

    with storage.storage._lock(thread_id, "", session_id=SESSION_ID):
        namespace_manifest = storage.storage.initialize(
            SESSION_ID,
            "branch:parallel",
            thread_id=thread_id,
        )

    assert namespace_manifest.checkpoint_ns == "branch:parallel"


def test_child_thread_resolves_to_own_thread_node_rollout(
    storage: RolloutContextStore,
) -> None:
    session_node = storage.storage._path_resolver.resolve_session_node(SESSION_ID)
    thread_dir = _publish_child_thread(
        session_node,
        thread_id=CHILD_THREAD_ID,
    )
    child_root = storage.storage.root(SESSION_ID, thread_id=CHILD_THREAD_ID)
    assert child_root == thread_dir / "rollout"
    assert child_root != session_node / "rollout"
    assert storage.storage.index_path(SESSION_ID, thread_id=CHILD_THREAD_ID) == (
        thread_dir / "rollout" / "index.sqlite"
    )


def test_unpublished_thread_id_fails_closed(
    storage: RolloutContextStore,
) -> None:
    with pytest.raises(KeyError):
        storage.storage.root(SESSION_ID, thread_id=CHILD_THREAD_ID)


def test_unknown_session_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="非空字符串"):
        storage = RolloutContextStore(tmp_path / "does-not-exist")
        storage.storage.root("")


def test_main_and_child_rollout_stores_are_physically_isolated(
    storage: RolloutContextStore,
) -> None:
    """OpenSpec 8.3 Scenario：main 与 child 的 rollout 落点互不重叠。

    线程级隔离由「每 thread 独立数据库文件」实现：main 与 child 各自的
    `index.sqlite` 与 `rollout.jsonl` 落在不同物理目录，故两库各自的单行
    `database_meta` 与 `committed_jsonl_offset` 不会互相串扰。
    """
    session_node = storage.storage._path_resolver.resolve_session_node(SESSION_ID)
    _publish_child_thread(
        session_node,
        thread_id=CHILD_THREAD_ID,
    )
    main_root = storage.storage.root(SESSION_ID)
    child_root = storage.storage.root(SESSION_ID, thread_id=CHILD_THREAD_ID)
    assert main_root != child_root
    assert main_root.parent != child_root.parent
    assert storage.storage.index_path(SESSION_ID) != storage.storage.index_path(
        SESSION_ID, thread_id=CHILD_THREAD_ID
    )
    assert storage.storage.jsonl_path(SESSION_ID) != storage.storage.jsonl_path(
        SESSION_ID, thread_id=CHILD_THREAD_ID
    )


def test_acceptance_owner_roundtrips_same_local_ids_in_sibling_threads(
    tmp_path: Path,
    session_bundle_factory,
    frozen_provider_wire,
    frozen_provider_activation,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    storage = saver._storage
    session_node = storage.storage._path_resolver.resolve_session_node(SESSION_ID)
    thread_ids = (storage.storage.main_thread_id(SESSION_ID), f"thr_{create_uuid_hex()}")
    _publish_child_thread(session_node, thread_id=thread_ids[1])

    for thread_id in thread_ids:
        saver.accept_turn(
            SESSION_ID,
            thread_id=thread_id,
            accepted_ingress_id="ingress-local-1",
            acceptance_idempotency_key="acceptance-local-1",
            job_id="job-local-1",
            admission_idempotency_key="admission-local-1",
            payload="same local input",
            turn_id="turn-local-1",
            root_item_id="item-root-local-1",
            initial_execution_id="execution-local-1",
        )
        admission = saver.execution_admission(
            SESSION_ID, thread_id=thread_id, execution_id="execution-local-1",
        )
        with (
            session_thread_owner_scope(MutationIntentOwner(SESSION_ID, thread_id), langgraph_thread_id=thread_id),
            execution_admission_scope(admission, langgraph_thread_id=thread_id),
        ):
            prepared = saver.prepare_context_for_provider(
                SESSION_ID, thread_id, turn_id="turn-local-1", execution_id="execution-local-1",
                plan_creation_idempotency_key="owner-plan", seal_idempotency_key="owner-seal",
                context_source_manager=ContextSourceManager(
                    owner=ContextSourceOwnerKey(SESSION_ID, thread_id), control_state_port=saver,
                ),
                tool_set_supplier=lambda: ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
                **frozen_provider_wire("test-provider", "chat_completions"),
                **frozen_provider_activation(saver, plan_creation_idempotency_key="owner-plan",
                                             model_call_id="00000000-0000-4000-8000-000000000001"),
            )
        saver.register_model_call(
            SESSION_ID, thread_id=thread_id, execution_id="execution-local-1",
            model_call_id="00000000-0000-4000-8000-000000000001", attempt=1,
            provider="test-provider", assembly_id=str(prepared["assembly_id"]),
        )

    saver.update_model_call_outcome(
        SESSION_ID, thread_id=thread_ids[0], model_call_id="00000000-0000-4000-8000-000000000001",
        outcome="failed", dispatch_state="failed",
    )

    saver.converge_execution(
        SESSION_ID, thread_id=thread_ids[0], turn_id="turn-local-1",
        execution_id="execution-local-1", outcome="failed", turn_status="failed",
    )

    for thread_id in thread_ids:
        index_path = storage.storage.index_path(SESSION_ID, thread_id=thread_id)
        with sqlite3.connect(index_path) as connection:
            assert connection.execute(
                "SELECT session_id, thread_id FROM database_meta WHERE singleton_id = 1"
            ).fetchone() == (SESSION_ID, thread_id)
            assert connection.execute(
                "SELECT session_id, thread_id, turn_id FROM turn_records"
            ).fetchone() == (SESSION_ID, thread_id, "turn-local-1")
            assert connection.execute(
                "SELECT session_id, thread_id, execution_id FROM executions"
            ).fetchone() == (SESSION_ID, thread_id, "execution-local-1")
            assert connection.execute(
                "SELECT session_id, thread_id, turn_id, execution_id "
                "FROM turn_execution_links"
            ).fetchone() == (
                SESSION_ID,
                thread_id,
                "turn-local-1",
                "execution-local-1",
            )
            assert connection.execute(
                "SELECT item_id FROM item_catalog WHERE item_id = ?",
                ("item-root-local-1",),
            ).fetchone() == ("item-root-local-1",)
            assert connection.execute(
                "SELECT m.session_id,m.thread_id,m.execution_id,e.turn_id,m.outcome "
                "FROM model_calls m JOIN executions e USING(session_id,thread_id,execution_id)",
            ).fetchone() == (
                SESSION_ID, thread_id, "execution-local-1", "turn-local-1",
                "failed" if thread_id == thread_ids[0] else "unknown",
            )
            assert connection.execute("SELECT outcome FROM executions").fetchone() == (
                "failed" if thread_id == thread_ids[0] else "unknown",
            )
            for table in ("executions", "model_calls", "turn_execution_links"):
                columns = {row[1]: row for row in connection.execute(f"PRAGMA table_info({table})")}
                assert columns["thread_id"][3] == 1
                indexes = connection.execute(f"PRAGMA index_list({table})").fetchall()
                indexed = [(row[2], [column[2] for column in connection.execute(f"PRAGMA index_info('{row[1]}')")]) for row in indexes]
                assert any(unique and names[:2] == ["session_id", "thread_id"] for unique, names in indexed)
                assert any(names[:2] == ["session_id", "thread_id"] for _, names in indexed)
                with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
                    connection.execute(f"UPDATE {table} SET thread_id=NULL")


@pytest.mark.parametrize("entry", ["saver", "execution_store"])
@pytest.mark.parametrize("operation", ["accept_turn", "execution_for_turn", "register_model_call", "update_model_call_outcome", "converge_execution"])
@pytest.mark.parametrize("bad_thread", ["missing", None, "", "main", SESSION_ID])
def test_execution_fact_entries_reject_missing_thread_without_writing(
    storage: RolloutContextStore, entry: str, operation: str, bad_thread: str | None,
) -> None:
    main = storage.storage.main_thread_id(SESSION_ID)
    storage.storage.initialize(SESSION_ID, thread_id=main)
    owner = storage.executions if entry == "execution_store" else RolloutCheckpointSaver(storage.storage.sessions_dir)
    parameters = {
        "accept_turn": {"accepted_ingress_id": "bad-ingress", "acceptance_idempotency_key": "bad-key",
                        "job_id": "bad-job", "admission_idempotency_key": "bad-admission", "payload": "bad"},
        "execution_for_turn": {"turn_id": "bad-turn"},
        "register_model_call": {"execution_id": "bad-execution", "model_call_id": "bad-call", "attempt": 1, "provider": "test"},
        "update_model_call_outcome": {"model_call_id": "bad-call", "outcome": "failed"},
        "converge_execution": {"turn_id": "bad-turn", "execution_id": "bad-execution", "outcome": "failed", "turn_status": "failed"},
    }[operation]
    if bad_thread != "missing":
        parameters["thread_id"] = bad_thread
    before = {path: path.read_bytes() for path in storage.storage.root(SESSION_ID, thread_id=main).rglob("*") if path.is_file()}
    with pytest.raises((TypeError, RuntimeError), match="thread_id|thread alias"):
        getattr(owner, operation)(SESSION_ID, **parameters)
    after = {path: path.read_bytes() for path in storage.storage.root(SESSION_ID, thread_id=main).rglob("*") if path.is_file()}
    assert after == before


def test_execution_owner_never_comes_from_checkpoint_namespace(
    storage: RolloutContextStore,
) -> None:
    main = storage.storage.main_thread_id(SESSION_ID)
    child = f"thr_{create_uuid_hex()}"
    _publish_child_thread(storage.storage._path_resolver.resolve_session_node(SESSION_ID), thread_id=child)
    storage.executions.accept_turn(
        SESSION_ID, thread_id=child, checkpoint_ns=main,
        accepted_ingress_id="namespace-ingress", acceptance_idempotency_key="namespace-key",
        job_id="namespace-job", admission_idempotency_key="namespace-admission", payload="child",
        turn_id="namespace-turn", initial_execution_id="namespace-execution",
    )
    with storage.storage._connect(child, main, session_id=SESSION_ID, read_only=True) as connection:
        assert connection.execute("SELECT session_id,thread_id,execution_id FROM executions").fetchall() == [
            (SESSION_ID, child, "namespace-execution"),
        ]
    assert not storage.storage.index_path(SESSION_ID, thread_id=main).exists()


def _write_admitted_checkpoint(
    saver: RolloutCheckpointSaver,
    *,
    session_id: str,
    thread_id: str,
    label: str,
) -> tuple[dict[str, object], str]:
    turn_id = f"turn-{label}"
    execution_id = f"execution-{label}"
    user_message_id = f"user-{label}"
    assistant_message_id = f"assistant-{label}"
    user_message = HumanMessage(content=f"{label} question", id=user_message_id)
    assistant_message = AIMessage(
        content=f"{label} answer",
        id=assistant_message_id,
    )
    saver.accept_turn(
        session_id,
        thread_id=thread_id,
        accepted_ingress_id=f"ingress-{label}",
        acceptance_idempotency_key=f"acceptance-{label}",
        job_id=f"job-{label}",
        admission_idempotency_key=f"admission-{label}",
        payload=f"{label} question",
        payload_kind="text",
        turn_id=turn_id,
        root_item_id=f"item-{user_message_id}",
        initial_execution_id=execution_id,
    )
    admission = saver.execution_admission(
        session_id,
        thread_id=thread_id,
        execution_id=execution_id,
    )
    checkpoint_id = f"checkpoint-{label}"
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": [user_message, assistant_message]}
    checkpoint["channel_versions"] = {"messages": checkpoint_id}
    checkpoint["updated_channels"] = ["messages"]
    owner = MutationIntentOwner(session_id=session_id, thread_id=thread_id)
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        config = saver.put(
            build_checkpoint_config(thread_id),
            checkpoint,
            {"source": "thread-qualified-owner-test"},
            {"messages": checkpoint_id},
        )
    return config, turn_id


def test_main_and_child_checkpoints_restore_through_exact_owner(
    storage: RolloutContextStore,
) -> None:
    session_node = storage.storage._path_resolver.resolve_session_node(SESSION_ID)
    _publish_child_thread(
        session_node,
        thread_id=CHILD_THREAD_ID,
    )
    saver = RolloutCheckpointSaver(storage.storage.sessions_dir, storage=storage)
    main_thread_id = storage.storage.main_thread_id(SESSION_ID)
    main_config, _main_turn_id = _write_admitted_checkpoint(
        saver,
        session_id=SESSION_ID,
        thread_id=main_thread_id,
        label="main-owner",
    )
    child_config, child_turn_id = _write_admitted_checkpoint(
        saver,
        session_id=SESSION_ID,
        thread_id=CHILD_THREAD_ID,
        label="child-owner",
    )

    expected_by_thread = {
        main_thread_id: (
            "main-owner",
            ("item-user-main-owner", "item-assistant-main-owner"),
        ),
        CHILD_THREAD_ID: (
            "child-owner",
            ("item-user-child-owner", "item-assistant-child-owner"),
        ),
    }
    for thread_id, (label, item_ids) in expected_by_thread.items():
        items = storage.storage.read_items(
            SESSION_ID,
            thread_id,
            item_ids=item_ids,
        )
        assert [item.item_id for item in items] == list(item_ids)
        assert [item.payload for item in items] == [
            f"{label} question",
            f"{label} answer",
        ]
        with storage.storage.open_read_snapshot(SESSION_ID, thread_id) as snapshot:
            snapshot_items = storage.storage.read_items(
                SESSION_ID,
                thread_id,
                item_ids=item_ids,
                snapshot=snapshot,
            )
        assert snapshot_items == items

    main_owner = MutationIntentOwner(
        session_id=SESSION_ID,
        thread_id=main_thread_id,
    )
    child_owner = MutationIntentOwner(
        session_id=SESSION_ID,
        thread_id=CHILD_THREAD_ID,
    )
    with session_thread_owner_scope(
        main_owner,
        langgraph_thread_id=main_thread_id,
    ):
        restored_main = saver.get_tuple(main_config)
    with session_thread_owner_scope(
        child_owner,
        langgraph_thread_id=CHILD_THREAD_ID,
    ):
        restored_child = saver.get_tuple(child_config)
        child_list = list(saver.list(build_checkpoint_config(CHILD_THREAD_ID)))
        anchor, anchored_messages = saver.materialize_turn_anchor(
            build_checkpoint_config(CHILD_THREAD_ID),
            turn_id=child_turn_id,
        )

    assert restored_main is not None
    assert restored_child is not None
    assert [
        message.content
        for message in restored_main.checkpoint["channel_values"]["messages"]
    ] == [
        "main-owner question",
        "main-owner answer",
    ]
    assert [
        message.content
        for message in restored_child.checkpoint["channel_values"]["messages"]
    ] == [
        "child-owner question",
        "child-owner answer",
    ]
    assert len(child_list) == 1
    assert child_list[0].config["configurable"]["thread_id"] == CHILD_THREAD_ID
    assert [message.content for message in anchored_messages] == [
        "child-owner question",
        "child-owner answer",
    ]
    assert anchor.turn_id == child_turn_id

    # LangGraph list(config=None) retains its catalog Session enumeration contract;
    # each Session resolves exactly its authoritative main thread, never a child.
    all_sessions = list(saver.list(None))
    assert [item.config["configurable"]["thread_id"] for item in all_sessions] == [
        main_thread_id
    ]
    assert all_sessions[0].checkpoint["channel_values"]["messages"][0].content == (
        "main-owner question"
    )


def test_checkpoint_reads_reject_cross_owner_config_and_snapshot(
    storage: RolloutContextStore,
) -> None:
    session_node = storage.storage._path_resolver.resolve_session_node(SESSION_ID)
    _publish_child_thread(
        session_node,
        thread_id=CHILD_THREAD_ID,
    )
    saver = RolloutCheckpointSaver(storage.storage.sessions_dir, storage=storage)
    main_thread_id = storage.storage.main_thread_id(SESSION_ID)
    main_config, _ = _write_admitted_checkpoint(
        saver,
        session_id=SESSION_ID,
        thread_id=main_thread_id,
        label="cross-main",
    )
    child_config, _ = _write_admitted_checkpoint(
        saver,
        session_id=SESSION_ID,
        thread_id=CHILD_THREAD_ID,
        label="cross-child",
    )

    main_owner = MutationIntentOwner(
        session_id=SESSION_ID,
        thread_id=main_thread_id,
    )
    with session_thread_owner_scope(
        main_owner,
        langgraph_thread_id=main_thread_id,
    ):
        with pytest.raises(RuntimeError, match="owner.*thread_id 冲突"):
            saver.get_tuple(child_config)
        with pytest.raises(ValueError, match="相同 rollout owner"):
            list(
                saver.list(
                    build_checkpoint_config(CHILD_THREAD_ID),
                    before=main_config,
                )
            )

    with (
        storage.storage.open_read_snapshot(SESSION_ID, CHILD_THREAD_ID) as child_snapshot,
        pytest.raises(ValueError, match="目标 rollout 不一致"),
    ):
        storage.checkpoints.latest_checkpoint(
            SESSION_ID,
            main_thread_id,
            "",
            None,
            snapshot=child_snapshot,
        )


def test_indexed_history_cache_keeps_same_local_ids_and_revisions_in_each_thread(
    storage: RolloutContextStore,
) -> None:
    main = storage.storage.main_thread_id(SESSION_ID)
    _publish_child_thread(storage.storage._path_resolver.resolve_session_node(SESSION_ID), thread_id=CHILD_THREAD_ID)
    saver = RolloutCheckpointSaver(storage.storage.sessions_dir, storage=storage)
    for thread in (main, CHILD_THREAD_ID):
        _write_admitted_checkpoint(saver, session_id=SESSION_ID, thread_id=thread, label="cache-local")
    snapshots = IndexedHistorySnapshots(saver._context_reader)
    revisions = []
    entries = {}
    for thread in (main, CHILD_THREAD_ID, main, CHILD_THREAD_ID):
        history = snapshots.read(SESSION_ID, thread)
        assert history is not None
        with history.snapshot as snapshot:
            assert (snapshot.session_id, snapshot.thread_id) == (SESSION_ID, thread)
            assert history.turn_count == 1
            revisions.append((snapshot.manifest.history_view_revision, snapshot.manifest.committed_sequence))
            key = (SESSION_ID, thread)
            cached = snapshots._indexed_history_cache[key]
            if key in entries:
                assert cached is entries[key]
            entries[key] = cached
    assert len(set(revisions)) == 1
    assert set(snapshots._indexed_history_cache) == {(SESSION_ID, main), (SESSION_ID, CHILD_THREAD_ID)}
