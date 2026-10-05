"""rollout 持久化定位的 thread-qualified 合同（OpenSpec 8.3 主项）。

rollout 物理节点、index.sqlite 与 rollout.jsonl 由精确 ``(session_id, thread_id)``
的 thread node 独占：main thread 经 thread catalog 冻结的 ``main_thread_id`` 解析，
非 main durable thread 经其 frozen locator 解析到独立节点。裸 ``main`` 别名与裸
session_id 不是 thread identity，必须经 thread catalog 显式解析，绝不把它们当作
thread_id 拼路径；thread 维度缺省或非法时 fail closed。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex, uuid7_datetime_from_hex
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage
from tests.support.canonical_id_at import thread_id_at
from tests.support.catalog_session_bundle import (
    seed_catalog_session_bundle,
)

SESSION_ID = f"ses_{create_uuid_hex()}"
CHILD_CREATED_AT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
CHILD_THREAD_ID = thread_id_at(CHILD_CREATED_AT)


def test_full_copy_staging_owner_accepts_thread_qualified_root_signature(tmp_path: Path) -> None:
    """私有 fork staging 支持真实 main owner 并拒绝 non-main。"""
    from app.services.infrastructure.rollout_context.fork.full_copy.staging import (
        FullCopyStagingStorage,
    )

    target = f"ses_{create_uuid_hex()}"
    parameters = inspect.signature(FullCopyStagingStorage.root).parameters
    assert "thread_id" in parameters
    assert parameters["thread_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["thread_id"].default is None
    probe_root = tmp_path / "staging-root-signature-probe"
    main_thread_id = f"thr_{create_uuid_hex()}"
    instance = object.__new__(FullCopyStagingStorage)
    instance._path_resolver = SimpleNamespace(main_thread_id=lambda _session_id: main_thread_id)
    instance._source = f"ses_{create_uuid_hex()}"
    instance._target = target
    instance._stage_root = probe_root
    assert instance.root(target, "", thread_id=None) == probe_root
    assert instance.root(target, "", thread_id=main_thread_id) == probe_root
    with pytest.raises(ValueError, match="不接受其它 thread_id"):
        instance.root(target, "", thread_id=CHILD_THREAD_ID)


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
            graph_binding=json.dumps({"graph_id": "rollout-locating"}),
            capability_profile=json.dumps({}),
            created_at=uuid7_datetime_from_hex(thread_id[4:]),
            thread_id=thread_id,
        )
        manifest = "{}"
        control.freeze_thread_creation_artifact_manifest(
            key,
            artifact_manifest=manifest,
            artifact_manifest_hash=hashlib.sha256(
                manifest.encode("utf-8")
            ).hexdigest(),
        )
        published = control.publish_thread_creation_record(key)
        locator = published.final_relative_locator
    finally:
        control.close()
    thread_dir = session_dir / locator
    thread_dir.mkdir(parents=True)
    return thread_dir


@pytest.fixture
def storage(tmp_path: Path) -> RolloutStorage:
    root = tmp_path / ".boxteam" / "sessions"
    root.mkdir(parents=True)
    seed_catalog_session_bundle(root, SESSION_ID, title="thread 定位")
    return RolloutStorage(
        root,
        serde=JsonPlusSerializer(),
        message_codec=LangChainMessageCodec(),
    )


def test_main_thread_resolves_to_session_node_rollout(
    storage: RolloutStorage,
) -> None:
    main_thread_id = storage._path_resolver.main_thread_id(SESSION_ID)
    session_node = storage._path_resolver.resolve_session_node(SESSION_ID)
    thread_node = storage._path_resolver.resolve_thread_node(
        SESSION_ID, main_thread_id
    )
    assert storage.root(SESSION_ID) == thread_node / "rollout"
    assert storage.root(SESSION_ID, thread_id=main_thread_id) == thread_node / "rollout"
    assert storage.index_path(SESSION_ID) == thread_node / "rollout" / "index.sqlite"
    assert storage.jsonl_path(SESSION_ID) == thread_node / "rollout" / "rollout.jsonl"
    assert thread_node == session_node


def test_session_and_main_thread_resolve_to_one_rollout_lock(
    storage: RolloutStorage,
) -> None:
    assert storage._lock(SESSION_ID, "", session_id=SESSION_ID) is storage._lock(
        SESSION_ID,
        "",
        session_id=SESSION_ID,
    )


def test_child_thread_resolves_to_own_thread_node_rollout(
    storage: RolloutStorage,
) -> None:
    session_node = storage._path_resolver.resolve_session_node(SESSION_ID)
    thread_dir = _publish_child_thread(
        session_node,
        thread_id=CHILD_THREAD_ID,
    )
    child_root = storage.root(SESSION_ID, thread_id=CHILD_THREAD_ID)
    assert child_root == thread_dir / "rollout"
    assert child_root != session_node / "rollout"
    assert storage.index_path(SESSION_ID, thread_id=CHILD_THREAD_ID) == (
        thread_dir / "rollout" / "index.sqlite"
    )


def test_unpublished_thread_id_fails_closed(
    storage: RolloutStorage,
) -> None:
    with pytest.raises(KeyError):
        storage.root(SESSION_ID, thread_id=CHILD_THREAD_ID)


def test_unknown_session_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="非空字符串"):
        storage = RolloutStorage(tmp_path / "does-not-exist")
        storage.root("")


def test_main_and_child_rollout_stores_are_physically_isolated(
    storage: RolloutStorage,
) -> None:
    """OpenSpec 8.3 Scenario：main 与 child 的 rollout 落点互不重叠。

    线程级隔离由「每 thread 独立数据库文件」实现：main 与 child 各自的
    `index.sqlite` 与 `rollout.jsonl` 落在不同物理目录，故两库各自的单行
    `database_meta` 与 `committed_jsonl_offset` 不会互相串扰。
    """
    session_node = storage._path_resolver.resolve_session_node(SESSION_ID)
    _publish_child_thread(
        session_node,
        thread_id=CHILD_THREAD_ID,
    )
    main_root = storage.root(SESSION_ID)
    child_root = storage.root(SESSION_ID, thread_id=CHILD_THREAD_ID)
    assert main_root != child_root
    assert main_root.parent != child_root.parent
    assert storage.index_path(SESSION_ID) != storage.index_path(
        SESSION_ID, thread_id=CHILD_THREAD_ID
    )
    assert storage.jsonl_path(SESSION_ID) != storage.jsonl_path(
        SESSION_ID, thread_id=CHILD_THREAD_ID
    )


def test_acceptance_owner_roundtrips_same_local_ids_in_sibling_threads(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    storage = saver._storage
    session_node = storage._path_resolver.resolve_session_node(SESSION_ID)
    thread_ids = (f"thr_{create_uuid_hex()}", f"thr_{create_uuid_hex()}")
    for thread_id in thread_ids:
        _publish_child_thread(
            session_node,
            thread_id=thread_id,
        )

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

    for thread_id in thread_ids:
        index_path = storage.index_path(SESSION_ID, thread_id=thread_id)
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
    storage: RolloutStorage,
) -> None:
    session_node = storage._path_resolver.resolve_session_node(SESSION_ID)
    _publish_child_thread(
        session_node,
        thread_id=CHILD_THREAD_ID,
    )
    saver = RolloutCheckpointSaver(storage.sessions_dir, storage=storage)
    main_thread_id = storage.main_thread_id(SESSION_ID)
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
        items = storage.read_items(
            SESSION_ID,
            thread_id,
            item_ids=item_ids,
        )
        assert [item.item_id for item in items] == list(item_ids)
        assert [item.payload for item in items] == [
            f"{label} question",
            f"{label} answer",
        ]
        with storage.open_read_snapshot(SESSION_ID, thread_id) as snapshot:
            snapshot_items = storage.read_items(
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
    assert [message.content for message in restored_main.checkpoint["channel_values"]["messages"]] == [
        "main-owner question",
        "main-owner answer",
    ]
    assert [message.content for message in restored_child.checkpoint["channel_values"]["messages"]] == [
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
    storage: RolloutStorage,
) -> None:
    session_node = storage._path_resolver.resolve_session_node(SESSION_ID)
    _publish_child_thread(
        session_node,
        thread_id=CHILD_THREAD_ID,
    )
    saver = RolloutCheckpointSaver(storage.sessions_dir, storage=storage)
    main_thread_id = storage.main_thread_id(SESSION_ID)
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
        storage.open_read_snapshot(SESSION_ID, CHILD_THREAD_ID) as child_snapshot,
        pytest.raises(ValueError, match="目标 rollout 不一致"),
    ):
        storage.latest_checkpoint(
            SESSION_ID,
            main_thread_id,
            "",
            None,
            snapshot=child_snapshot,
        )
