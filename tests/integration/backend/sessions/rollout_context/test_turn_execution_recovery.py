"""真实 Saver、Turn 生命周期与 SQLite/JSONL 的跨模块恢复合同。"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.parts import ContentPart, ContentPartAnchor
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.execution_step.stream_bindings import CanonicalItemSink
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime


def _accept(saver: RolloutCheckpointSaver, session_id: str) -> dict[str, object]:
    return saver.accept_turn(
        session_id,
        accepted_ingress_id="ingress-user-1",
        acceptance_idempotency_key="acceptance-user-1",
        payload="请检查当前状态",
        payload_kind=PayloadKind.TEXT,
        turn_id="turn-user-1",
        root_item_id="item-user-1",
        initial_execution_id="execution-user-1",
    )


def _rollout_db(sessions_dir: Path, session_id: str) -> Path:
    return (
        get_session_path_resolver(sessions_dir).resolve_session_node(session_id)
        / "rollout"
        / "index.sqlite"
    )


def _assistant_item(
    item_id: str,
    *,
    metadata: dict[str, object] | None = None,
) -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=1,
        item_id=item_id,
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "provider-1",
            "invocation_id": "execution-user-1",
        },
        payload="一个待写入的 assistant item",
        metadata=metadata or {},
        turn_id="turn-user-1",
        turn_scope=TurnScope.TURN_MEMBER,
        message_group_id="message-group-1",
        wire_role="assistant",
    )


@pytest.mark.parametrize(
    ("column", "value_factory", "match"),
    [
        ("status", lambda row: "prepared", "尚未 committed|未收敛"),
        ("metadata_json", lambda row: "[]", "metadata_json"),
        (
            "jsonl_offset_after",
            lambda row: int(row) + 1,
            "offset 字段不一致|与 end 冲突",
        ),
    ],
)
def test_acceptance_retry_rejects_corrupted_idempotency_ledger(
    tmp_path: Path,
    session_bundle_factory,
    column: str,
    value_factory,
    match: str,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    _accept(saver, "session_1")
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        current = connection.execute(
            f"SELECT {column} FROM storage_commits WHERE commit_kind = 'acceptance'"
        ).fetchone()[0]
        connection.execute(
            f"UPDATE storage_commits SET {column} = ? WHERE commit_kind = 'acceptance'",
            (value_factory(current),),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match=match):
        _accept(RolloutCheckpointSaver(sessions_dir), "session_1")


@pytest.mark.parametrize(
    "metadata",
    [
        {"source_revision": 1},
        {"block_id": "block-1", "block_index": "0"},
        {"block_id": "block-1", "block_index": -1},
        {"block_index": 0},
    ],
)
def test_item_commit_rejects_non_canonical_locator_metadata(
    tmp_path: Path,
    session_bundle_factory,
    metadata: dict[str, object],
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    _accept(saver, "session_1")
    rollout_path = saver._storage.jsonl_path("session_1")
    before = rollout_path.read_bytes()

    with pytest.raises(ItemSchemaError, match="source_revision|block_id|block_index"):
        saver._storage.append_item(
            "session_1",
            _assistant_item("item-invalid-storage-metadata", metadata=metadata),
        )

    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM item_catalog WHERE item_id = ?",
                ("item-invalid-storage-metadata",),
            ).fetchone()[0]
            == 0
        )
    assert rollout_path.read_bytes() == before


def test_item_commit_uses_canonical_writer_for_block_part_projection(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    _accept(saver, "session_1")

    item = _assistant_item(
        "item-block-projection",
        metadata={"block_id": "block-1", "block_index": 0},
    )
    commit_id = saver._storage.append_item("session_1", item)
    db_path = _rollout_db(sessions_dir, "session_1")
    with sqlite3.connect(db_path) as connection:
        part = connection.execute(
            "SELECT item_id, part_id, part_ordinal, content_hash, line_hash "
            "FROM item_parts WHERE item_id = ?",
            (item.item_id,),
        ).fetchone()
        locator = connection.execute(
            "SELECT jsonl_offset, jsonl_length, commit_id FROM item_catalog "
            "WHERE item_id = ?",
            (item.item_id,),
        ).fetchone()
    assert part[:4] == (item.item_id, "block-1", 0, item.content_hash)
    assert locator[2] == commit_id
    raw = saver._storage.jsonl_path("session_1").read_bytes()
    assert (
        part[4] == hashlib.sha256(raw[locator[0] : locator[0] + locator[1]]).hexdigest()
    )


def _source_overlay_for_test(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "overlay_id": "overlay-contract",
        "session_id": "session_1",
        "checkpoint_ns": "",
        "source_kind": "workspace_policy",
        "source_revision": "policy-1",
        "source_overlay_epoch": 0,
        "base_ref": "policy-base",
        "delta_ref": None,
        "base_source_revision": "policy-base-rev-1",
        "base_content_length": None,
        "base_content_hash": None,
        "base_redacted_stable_digest": None,
        "delta_source_revision": None,
        "delta_content_length": None,
        "delta_content_hash": None,
        "delta_redacted_stable_digest": None,
        "delta_from_revision": None,
        "delta_to_revision": None,
        "delta_diff_hash": None,
        "supersedes_overlay_id": None,
        "materializes_overlay_id": None,
        "status": "active",
        "idempotency_key": "overlay-contract-key",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_acceptance_and_provider_retry_keep_one_real_user_root(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)

    accepted = _accept(saver, "session_1")
    assert accepted["turn_id"] == "turn-user-1"
    assert accepted["root_input_item_id"] == "item-user-1"
    assert accepted["initial_execution_id"] == "execution-user-1"
    assert accepted["idempotent"] is False
    execution_id = str(accepted["initial_execution_id"])
    assembly_1 = saver.seal_context_for_dispatch(
        "session_1",
        turn_id="turn-user-1",
        execution_id=execution_id,
        model_call_id="model-call-attempt-1",
        provider_version="test-provider-v1",
        target_format="chat_completions",
    )
    saver.register_model_call(
        "session_1",
        execution_id=execution_id,
        model_call_id="model-call-attempt-1",
        attempt=1,
        provider="test-provider",
        assembly_id=assembly_1,
    )
    saver.update_model_call_outcome(
        "session_1",
        model_call_id="model-call-attempt-1",
        outcome="failed",
        dispatch_state="failed",
    )
    saver.register_model_call(
        "session_1",
        execution_id=execution_id,
        model_call_id="model-call-attempt-2",
        attempt=2,
        provider="test-provider",
        retry_of_model_call_id="model-call-attempt-1",
        assembly_id=assembly_1,
    )
    saver.update_model_call_outcome(
        "session_1",
        model_call_id="model-call-attempt-2",
        outcome="completed_empty",
        dispatch_state="completed",
    )
    saver.converge_execution(
        "session_1",
        turn_id="turn-user-1",
        execution_id=execution_id,
        outcome="completed_empty",
        turn_status="completed_empty",
    )

    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        root_count, turn_status, execution_outcome = connection.execute(
            "SELECT (SELECT COUNT(*) FROM item_catalog WHERE semantic_kind = 'user_input'), "
            "(SELECT status FROM turn_records WHERE turn_id = 'turn-user-1'), "
            "(SELECT outcome FROM executions WHERE execution_id = 'execution-user-1')"
        ).fetchone()
        model_calls = connection.execute(
            "SELECT model_call_id, attempt, retry_of_model_call_id, outcome "
            "FROM model_calls ORDER BY attempt"
        ).fetchall()
        assembly_model_call_id = connection.execute(
            "SELECT model_call_id FROM context_assemblies WHERE assembly_id = ?",
            (assembly_1,),
        ).fetchone()[0]
    assert (root_count, turn_status, execution_outcome) == (
        1,
        "completed_empty",
        "completed_empty",
    )
    assert model_calls == [
        ("model-call-attempt-1", 1, None, "failed"),
        ("model-call-attempt-2", 2, "model-call-attempt-1", "completed_empty"),
    ]
    assert assembly_model_call_id == "model-call-attempt-2"


def test_turn_projection_uses_canonical_tool_identity_before_message_projection(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    _accept(saver, "session_1")
    common = {
        "turn_id": "turn-user-1",
        "turn_scope": TurnScope.TURN_MEMBER,
        "wire_role": "assistant",
        "status": CanonicalItemStatus.COMPLETED,
    }
    live_call = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-model-call-1-block-call-1",
        semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind=PayloadKind.TOOL_CALL,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "model-call-1",
            "invocation_id": "model-call-1",
        },
        payload={"tool_call_id": "call-1", "name": "get_goal", "args": {}},
        metadata={"block_id": "call-1", "block_index": 0},
        message_group_id="message-model-call-1",
        **common,
    )
    checkpoint_shadow = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-lc_run--model-call-1",
        semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind=PayloadKind.TOOL_CALL,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "lc_run--model-call-1",
            "invocation_id": "turn-user-1",
        },
        payload={
            "tool_calls": [
                {"id": "call-1", "name": "get_goal", "args": {}}
            ]
        },
        metadata={
            "projection_message_id": "lc_run--model-call-1",
            "projection_group": {"content_form": "str", "ordinal": 0, "size": 1},
        },
        message_group_id="message-lc_run--model-call-1",
        **common,
    )
    result = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-result-1",
        semantic_kind=SemanticKind.TOOL_RESULT,
        payload_kind=PayloadKind.TOOL_RESULT,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "result-1",
            "invocation_id": "turn-user-1",
        },
        payload={
            "tool_call_id": "call-1",
            "result_id": "result-1",
            "name": "get_goal",
            "content": "{}",
            "tool_outcome": "success",
        },
        metadata={"tool_call_id": "call-1", "execution_confirmed": True},
        message_group_id="message-result-1",
        wire_role="tool",
        turn_id="turn-user-1",
        turn_scope=TurnScope.TURN_MEMBER,
        status=CanonicalItemStatus.COMPLETED,
    )
    saver.append_items("session_1", (live_call,))
    saver.append_items("session_1", (checkpoint_shadow, result))

    with saver._storage.open_read_snapshot("session_1") as snapshot:
        projection = saver._storage.read_turn_projections(
            snapshot, ("turn-user-1",)
        )["turn-user-1"]

    assert projection["activity_stats"]["item_count"] == 2
    assert [item["kind"] for item in projection["activity_items"]] == [
        "tool_call",
        "tool_result",
    ]
    assert {
        item["tool_call_id"] for item in projection["activity_items"]
    } == {"call-1"}


def test_item_catalog_reads_fail_closed_on_missing_item_and_view_reference(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    _accept(saver, "session_1")

    with pytest.raises(KeyError, match="canonical item catalog 缺少请求的 item"):
        saver._storage.read_items("session_1", item_ids=("item-does-not-exist",))

    rollout_db = _rollout_db(sessions_dir, "session_1")
    with sqlite3.connect(rollout_db) as connection:
        view_id = str(
            connection.execute(
                "SELECT head_view_id FROM branches WHERE status = 'active' LIMIT 1"
            ).fetchone()[0]
        )
        next_ordinal = int(
            connection.execute(
                "SELECT COALESCE(MAX(logical_item_ordinal), 0) + 1 "
                "FROM context_view_items WHERE view_id = ?",
                (view_id,),
            ).fetchone()[0]
        )
        connection.execute(
            "INSERT INTO context_view_items(view_id, item_id, logical_item_ordinal, "
            "visible, source_kind) VALUES (?, ?, ?, 1, 'canonical')",
            (view_id, "item-does-not-exist", next_ordinal),
        )
        connection.commit()

    with (
        saver._storage.open_read_snapshot("session_1") as snapshot,
        pytest.raises(KeyError, match="canonical item catalog 缺少请求的 item"),
    ):
        saver._storage.read_items_for_view(snapshot, view_id)


def test_index_validation_rejects_catalog_sequence_gaps_even_when_jsonl_matches(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    _accept(saver, "session_1")
    rollout_root = _rollout_db(sessions_dir, "session_1").parent

    raw = (rollout_root / "rollout.jsonl").read_bytes()
    assert raw.count(b'"item_sequence":1') == 1
    (rollout_root / "rollout.jsonl").write_bytes(
        raw.replace(b'"item_sequence":1', b'"item_sequence":7', 1)
    )
    with sqlite3.connect(rollout_root / "index.sqlite") as connection:
        connection.execute(
            "UPDATE item_catalog SET item_sequence = 7 WHERE item_id = ?",
            ("item-user-1",),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="item_sequence 不连续"):
        saver._storage.validate_index("session_1")


def test_item_projection_read_rejects_catalog_hash_drift(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    _accept(saver, "session_1")
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        connection.execute(
            "UPDATE item_projections SET content_hash = 'corrupted' WHERE item_id = ?",
            ("item-user-1",),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="item projection 与 canonical catalog"):
        saver._storage.read_item_projections("session_1", item_ids=("item-user-1",))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("status", "failed"),
        ("content_length", 999),
        ("content_truncated", 2),
        ("projection_version", "broken"),
        ("id", "projection-alias"),
    ],
)
def test_item_projection_read_rejects_typed_or_identity_drift(
    tmp_path: Path,
    session_bundle_factory,
    column: str,
    value: object,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    _accept(saver, "session_1")
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        connection.execute(
            f"UPDATE item_projections SET {column} = ? WHERE item_id = ?",
            (value, "item-user-1"),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="item_projections|item projection"):
        saver._storage.read_item_projections("session_1", item_ids=("item-user-1",))


def test_execution_lost_resume_creates_execution_without_a_new_user_root(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    accepted = _accept(saver, "session_1")
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        accepted_offset = connection.execute(
            "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
        ).fetchone()[0]

    lost = saver.mark_execution_lost(
        "session_1",
        turn_id="turn-user-1",
        execution_id=str(accepted["initial_execution_id"]),
        reason="provider response lost before durable result commit",
    )
    resumed = saver.resume_turn("session_1", turn_id="turn-user-1")
    assert lost["status"] == "unknown"
    assert lost["outcome"] == "execution_lost"
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        metadata_only = connection.execute(
            "SELECT commit_mode, jsonl_offset_before, jsonl_offset_after, "
            "jsonl_record_count, outcome FROM storage_commits "
            "WHERE commit_id = ?",
            (lost["commit_id"],),
        ).fetchone()
        lost_offset = connection.execute(
            "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
        ).fetchone()[0]
    assert metadata_only == (
        "metadata_only",
        accepted_offset,
        accepted_offset,
        0,
        "execution_lost",
    )
    assert lost_offset == accepted_offset
    assert resumed["execution_id"] != accepted["initial_execution_id"]
    assert resumed["attempt"] == 2
    saver.converge_execution(
        "session_1",
        turn_id="turn-user-1",
        execution_id=str(resumed["execution_id"]),
        outcome="completed_empty",
        turn_status="completed_empty",
    )

    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        root_count = connection.execute(
            "SELECT COUNT(*) FROM item_catalog WHERE semantic_kind = 'user_input'"
        ).fetchone()[0]
        turns = connection.execute(
            "SELECT turn_id, status, initial_execution_id, last_execution_id "
            "FROM turn_records"
        ).fetchall()
        executions = connection.execute(
            "SELECT execution_id, attempt, outcome FROM executions ORDER BY attempt"
        ).fetchall()
    assert root_count == 1
    assert turns == [
        ("turn-user-1", "completed_empty", "execution-user-1", resumed["execution_id"])
    ]
    assert executions == [
        ("execution-user-1", 1, "execution_lost"),
        (resumed["execution_id"], 2, "completed_empty"),
    ]


def test_interrupted_turn_resumes_after_restart_without_new_user_root(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    accepted = _accept(saver, "session_1")
    saver.converge_execution(
        "session_1",
        turn_id="turn-user-1",
        execution_id=str(accepted["initial_execution_id"]),
        outcome="interrupted",
        turn_status="interrupted",
    )

    restarted = RolloutCheckpointSaver(sessions_dir)
    resumed = restarted.resume_turn("session_1", turn_id="turn-user-1")

    assert resumed["execution_id"] != accepted["initial_execution_id"]
    assert resumed["attempt"] == 2
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        root_count = connection.execute(
            "SELECT COUNT(*) FROM item_catalog WHERE semantic_kind = 'user_input'"
        ).fetchone()[0]
        turn_row = connection.execute(
            "SELECT turn_id, status, initial_execution_id, last_execution_id "
            "FROM turn_records"
        ).fetchone()
        executions = connection.execute(
            "SELECT execution_id, attempt, outcome FROM executions ORDER BY attempt"
        ).fetchall()
    assert root_count == 1
    assert turn_row == (
        "turn-user-1",
        "active",
        "execution-user-1",
        resumed["execution_id"],
    )
    assert executions == [
        ("execution-user-1", 1, "interrupted"),
        (resumed["execution_id"], 2, "unknown"),
    ]

    restarted.converge_execution(
        "session_1",
        turn_id="turn-user-1",
        execution_id=str(resumed["execution_id"]),
        outcome="completed_empty",
        turn_status="completed_empty",
    )


@pytest.mark.asyncio
async def test_partial_stream_item_and_anchor_survive_runtime_restart(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    """真实 stream/runtime 写入的 partial item 与定位 anchor 可跨进程恢复。"""
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    _accept(saver, "session_1")
    resolver = get_session_path_resolver(sessions_dir)
    stream_store = MessageStreamStore(path_resolver=resolver)
    writer = await stream_store.open(
        session_id="session_1",
        turn_id="turn-user-1",
    )
    item_sink = CanonicalItemSink(
        saver,
        session_id="session_1",
        checkpoint_ns="",
    )
    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=item_sink.append,
        canonical_turn_id="turn-user-1",
    )
    await runtime.start_model("model-call-partial", "test-provider")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content=[
                {
                    "id": "answer-partial",
                    "index": 0,
                    "type": "text",
                    "text": "流式前半",
                }
            ]
        )
    )
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content=[
                {
                    "id": "answer-partial",
                    "index": 0,
                    "type": "text",
                    "text": "流式后半",
                }
            ]
        )
    )
    await runtime.fail_model(
        code="user_interrupt",
        message="用户在 provider 输出中断开流",
        outcome="user_interrupt",
        retryable=False,
    )
    await writer.close_interrupted("interrupt-partial-restart")

    partial_items = [
        item
        for item in saver._storage.read_items("session_1")
        if item.status == CanonicalItemStatus.PARTIAL.value
    ]
    assert len(partial_items) == 1
    partial_item = partial_items[0]
    assert partial_item.item_id == "item-model-call-partial-block-answer-partial"
    assert partial_item.payload == "流式前半流式后半"
    assert partial_item.producer_ref == {
        "producer_kind": "provider",
        "producer_id": "model-call-partial",
        "invocation_id": "model-call-partial",
    }

    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        view_id, branch_id = connection.execute(
            "SELECT v.view_id, v.branch_id FROM context_views AS v "
            "JOIN branches AS b ON b.head_view_id = v.view_id "
            "WHERE b.status = 'active'"
        ).fetchone()
        part_row = connection.execute(
            "SELECT part_id, part_ordinal, content_hash, locator_json "
            "FROM item_parts WHERE item_id = ?",
            (partial_item.item_id,),
        ).fetchone()
    assert part_row is not None
    part_id, part_ordinal, content_hash, locator_json = part_row
    assert (part_id, part_ordinal, content_hash) == (
        "answer-partial",
        0,
        partial_item.content_hash,
    )
    assert locator_json == canonical_json_bytes(
        {
            "encoding": "jcs:v1",
            "json_pointer": "/payload",
            "length": len("流式前半流式后半".encode()),
            "offset": 0,
        }
    ).decode("utf-8")
    anchor = ContentPartAnchor(
        anchor_id="anchor-partial-restart",
        item_id=partial_item.item_id,
        part_id=part_id,
        mode="inclusive",
        view_id=view_id,
        branch_id=branch_id,
        capability="content_part",
        content_hash=content_hash,
    )
    saver.register_content_part_anchor("session_1", anchor)

    restarted_saver = RolloutCheckpointSaver(sessions_dir)
    restarted_items = restarted_saver._storage.read_items("session_1")
    restored_item = next(
        item for item in restarted_items if item.item_id == partial_item.item_id
    )
    assert restored_item == partial_item
    assert (
        restarted_saver.resolve_content_part_anchor(
            "session_1",
            anchor_id=anchor.anchor_id,
        )
        == anchor
    )

    restarted_stream_store = MessageStreamStore(path_resolver=resolver)
    restarted_writer = await restarted_stream_store.open_existing(
        session_id="session_1",
        turn_id="turn-user-1",
        turn_stream_id=writer.turn_stream_id,
    )
    stream_state = await restarted_stream_store.get_state(
        restarted_writer.turn_stream_id
    )
    assert stream_state["stream_status"] == "interrupted"
    assert len(stream_state["model_calls"]) == 1
    model_call = stream_state["model_calls"][0]
    assert {
        key: model_call[key]
        for key in ("model_call_id", "attempt", "outcome", "status")
    } == {
        "model_call_id": "model-call-partial",
        "attempt": 1,
        "outcome": "user_interrupt",
        "status": "failed",
    }
    assert len(stream_state["blocks"]) == 1
    block = stream_state["blocks"][0]
    assert {
        key: block[key]
        for key in (
            "block_id",
            "block_index",
            "carrier_type",
            "status",
            "text",
            "model_call_id",
            "projection",
            "completion_reason",
            "partial",
        )
    } == {
        "block_id": "answer-partial",
        "block_index": 0,
        "carrier_type": "text",
        "status": "completed",
        "text": "流式前半流式后半",
        "model_call_id": "model-call-partial",
        "projection": "streaming",
        "completion_reason": "user_interrupt",
        "partial": True,
    }


def test_checkpoint_first_root_creates_one_based_turn_ordinal(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    """checkpoint 先到时也必须建立合法的 v2 Turn root/ordinal。"""
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "checkpoint-first-root"
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(
                content="checkpoint 先到的用户输入",
                id="checkpoint-first-user",
                response_metadata={
                    "message_metadata": {"turn_id": "turn-checkpoint-first"}
                },
            )
        ]
    }
    checkpoint["channel_versions"] = {"messages": "1"}
    checkpoint["updated_channels"] = ["messages"]

    saver.put(
        build_checkpoint_config("session_1"),
        checkpoint,
        {"source": "checkpoint-first-test", "step": 1},
        {"messages": "1"},
    )

    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        turn = connection.execute(
            "SELECT turn_id, turn_ordinal, root_input_item_id, status FROM turn_records"
        ).fetchone()
        root = connection.execute(
            "SELECT item_id, semantic_kind, turn_scope, turn_id FROM item_catalog"
        ).fetchone()
    assert turn == (
        "turn-checkpoint-first",
        1,
        "item-checkpoint-first-user",
        "active",
    )
    assert root == (
        "item-checkpoint-first-user",
        "user_input",
        "turn_root",
        "turn-checkpoint-first",
    )


def test_committed_overlay_selection_is_epoch_stable_after_restart(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    """active overlay 的 base/delta 顺序不能依赖 registry 或内存插入顺序。"""
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    _accept(saver, "session_1")

    def overlay(epoch: int) -> SimpleNamespace:
        return SimpleNamespace(
            overlay_id=f"overlay-{epoch}",
            session_id="session_1",
            checkpoint_ns="",
            source_kind="workspace_policy",
            source_revision=f"policy-{epoch}",
            source_overlay_epoch=epoch,
            base_ref=f"policy-base-{epoch}",
            delta_ref=f"policy-delta-{epoch}",
            base_source_revision=f"policy-base-rev-{epoch}",
            base_content_length=None,
            base_content_hash=None,
            base_redacted_stable_digest=None,
            delta_source_revision=f"policy-delta-rev-{epoch}",
            delta_content_length=None,
            delta_content_hash=None,
            delta_redacted_stable_digest=None,
            delta_from_revision=f"policy-base-rev-{epoch}",
            delta_to_revision=f"policy-delta-rev-{epoch}",
            delta_diff_hash=f"diff-{epoch}",
            supersedes_overlay_id=None,
            materializes_overlay_id=None,
            status="active",
            idempotency_key=f"overlay-key-{epoch}",
        )

    # 把创建时间改成逆序，验证 composition 依据 source_overlay_epoch 重新
    # 建立 selection，而不是让时间戳或 Python ledger 顺序决定语义。
    for epoch in (2, 3):
        current = overlay(epoch)
        saver.register_source_overlay(
            current,
            base_content={"policy": f"base-{epoch}"},
            delta_content={"policy": f"delta-{epoch}"},
        )
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        connection.execute(
            "UPDATE source_overlays SET created_at = CASE overlay_id "
            "WHEN 'overlay-2' THEN '2026-09-07T00:00:03+00:00' "
            "WHEN 'overlay-3' THEN '2026-09-07T00:00:02+00:00' END"
        )
        connection.commit()

    def ref_ids(checkpoint_saver: RolloutCheckpointSaver) -> list[str]:
        plan = checkpoint_saver.compose_committed_context_plan(
            "session_1",
            plan_id="plan-overlay-order",
        )
        return [ref.ref_id for ref in plan.refs]

    expected = [
        "item-user-1",
        "policy-base-2",
        "policy-delta-2",
        "policy-base-3",
        "policy-delta-3",
    ]
    assert ref_ids(saver) == expected
    assert ref_ids(RolloutCheckpointSaver(sessions_dir)) == expected


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("source_overlay_epoch", True),
        ("source_overlay_epoch", "0"),
        ("checkpoint_ns", 1),
        ("base_content_length", "not-a-length"),
        ("base_content_hash", 123),
        ("base_source_revision", 123),
        ("idempotency_key", 123),
        ("status", "corrupted"),
    ],
)
def test_source_overlay_registration_rejects_coerced_manifest_values(
    tmp_path: Path,
    session_bundle_factory,
    field_name: str,
    value: object,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    _accept(saver, "session_1")
    overlay = _source_overlay_for_test(**{field_name: value})

    with pytest.raises((TypeError, ValueError)):
        saver.register_source_overlay(
            overlay,
            base_content={"policy": "must not be accepted"},
        )


def test_source_overlay_registration_rejects_ref_identity_collision(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    _accept(saver, "session_1")
    overlay = _source_overlay_for_test(
        delta_ref="policy-base",
        delta_source_revision="policy-delta-rev-1",
        delta_from_revision="policy-base-rev-1",
        delta_to_revision="policy-delta-rev-1",
        delta_diff_hash="diff-1",
    )

    with pytest.raises(ValueError, match="不能复用同一 ref"):
        saver.register_source_overlay(
            overlay,
            base_content={"policy": "base"},
            delta_content={"policy": "delta"},
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [("status", "corrupted"), ("source_overlay_epoch", "not-an-epoch")],
)
def test_source_overlay_restore_rejects_corrupted_registry_rows(
    tmp_path: Path,
    session_bundle_factory,
    column: str,
    value: object,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    _accept(saver, "session_1")
    saver.register_source_overlay(
        _source_overlay_for_test(),
        base_content={"policy": "persisted"},
    )
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        connection.execute(
            f"UPDATE source_overlays SET {column} = ? WHERE overlay_id = ?",
            (value, "overlay-contract"),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="source overlay"):
        RolloutCheckpointSaver(sessions_dir).list_source_overlays("session_1")


def test_item_bearing_terminal_convergence_materializes_output_and_restart(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    accepted = _accept(saver, "session_1")
    execution_id = str(accepted["initial_execution_id"])
    output = CanonicalItemRecord.create(
        item_sequence=99,
        item_id="item-output-without-projection",
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "model-call-1",
            "invocation_id": execution_id,
        },
        payload="没有对应 message projection",
        turn_id="turn-user-1",
        turn_scope=TurnScope.TURN_MEMBER,
        message_group_id="message-group-1",
        wire_role="assistant",
    )
    commit_id = saver.converge_execution(
        "session_1",
        turn_id="turn-user-1",
        execution_id=execution_id,
        outcome="completed",
        turn_status="completed",
        items=(output,),
        final_item_id=output.item_id,
    )

    restarted = RolloutCheckpointSaver(sessions_dir)
    assert [item.item_id for item in restarted._storage.read_items("session_1")] == [
        "item-user-1",
        output.item_id,
    ]
    rollout_root = _rollout_db(sessions_dir, "session_1").parent
    with sqlite3.connect(rollout_root / "index.sqlite") as connection:
        status, final_item_id, item_count = connection.execute(
            "SELECT tr.status, tr.final_item_id, "
            "(SELECT COUNT(*) FROM item_catalog) "
            "FROM turn_records AS tr WHERE tr.turn_id = 'turn-user-1'"
        ).fetchone()
        commit = connection.execute(
            "SELECT commit_id, commit_mode, jsonl_record_count, "
            "first_message_sequence, last_message_sequence, status "
            "FROM storage_commits WHERE commit_id = ?",
            (commit_id,),
        ).fetchone()
        messages = connection.execute(
            "SELECT message_sequence, message_id, turn_id, role FROM messages "
            "ORDER BY message_sequence"
        ).fetchall()
        projected_turn = connection.execute(
            "SELECT first_message_sequence, last_message_sequence, "
            "user_message_sequence, final_message_sequence, final_message_id, status "
            "FROM turns WHERE turn_id = 'turn-user-1'"
        ).fetchone()
        view_turn = connection.execute(
            "SELECT user_message_sequence, final_message_sequence "
            "FROM context_view_turns WHERE turn_id = 'turn-user-1'"
        ).fetchone()
    assert (status, final_item_id, item_count) == (
        "completed",
        output.item_id,
        2,
    )
    # acceptance 已在独立 commit 中投影 sequence 1 的用户 root；终态 commit
    # 只拥有本事务新增的 assistant sequence 2，不能把旧消息计入自身边界。
    assert commit == (commit_id, "item_bearing", 1, 2, 2, "committed")
    assert messages == [
        (1, "user-1", "turn-user-1", "user"),
        (2, "output-without-projection", "turn-user-1", "assistant"),
    ]
    assert projected_turn == (1, 2, 1, 2, "output-without-projection", "completed")
    assert view_turn == (1, 2)
    latest, cursor, projection_epoch = restarted._history_reader.bootstrap("session_1")
    assert latest is not None
    assert latest.turn_id == "turn-user-1"
    assert latest.response_preview == "没有对应 message projection"
    assert cursor is not None
    assert projection_epoch == 1
    assert (
        restarted.converge_execution(
            "session_1",
            turn_id="turn-user-1",
            execution_id=execution_id,
            outcome="completed",
            turn_status="completed",
            items=(output,),
            final_item_id=output.item_id,
        )
        == commit_id
    )
    conflicting_output = CanonicalItemRecord.create(
        item_sequence=99,
        item_id=output.item_id,
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "model-call-1",
            "invocation_id": execution_id,
        },
        payload="另一个终态正文",
        turn_id="turn-user-1",
        turn_scope=TurnScope.TURN_MEMBER,
        message_group_id="message-group-1",
        wire_role="assistant",
    )
    with pytest.raises(ValueError, match="幂等键冲突"):
        restarted.converge_execution(
            "session_1",
            turn_id="turn-user-1",
            execution_id=execution_id,
            outcome="completed",
            turn_status="completed",
            items=(conflicting_output,),
            final_item_id=conflicting_output.item_id,
        )


def test_item_bearing_terminal_projection_failure_rolls_back_jsonl_and_sqlite(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    accepted = _accept(saver, "session_1")
    execution_id = str(accepted["initial_execution_id"])
    output = CanonicalItemRecord.create(
        item_sequence=99,
        item_id="item-output-with-invalid-wire-role",
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "model-call-1",
            "invocation_id": execution_id,
        },
        payload="projection should fail",
        turn_id="turn-user-1",
        turn_scope=TurnScope.TURN_MEMBER,
        message_group_id="message-group-1",
        wire_role="user",
    )
    with pytest.raises(ValueError, match="wire_role 与 message projection 不匹配"):
        saver.converge_execution(
            "session_1",
            turn_id="turn-user-1",
            execution_id=execution_id,
            outcome="completed",
            turn_status="completed",
            items=(output,),
            final_item_id=output.item_id,
        )

    restarted = RolloutCheckpointSaver(sessions_dir)
    assert [item.item_id for item in restarted._storage.read_items("session_1")] == [
        "item-user-1"
    ]
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        item_count, message_count, turn_status, terminal_commits = connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM item_catalog), "
            "(SELECT COUNT(*) FROM messages), "
            "(SELECT status FROM turn_records WHERE turn_id = 'turn-user-1'), "
            "(SELECT COUNT(*) FROM storage_commits WHERE commit_kind = 'terminal_convergence')"
        ).fetchone()
    # 失败只回滚终态事务；acceptance 的 canonical root 与 message projection
    # 已经提交，必须继续保留，不能把跨事务清空误当作回滚成功。
    assert (item_count, message_count, turn_status, terminal_commits) == (
        1,
        1,
        "active",
        0,
    )


def test_cancelled_turn_rejects_original_dispatch_and_replays_as_new_turn(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    accepted = _accept(saver, "session_1")
    execution_id = str(accepted["initial_execution_id"])

    saver.converge_execution(
        "session_1",
        turn_id="turn-user-1",
        execution_id=execution_id,
        outcome="cancelled",
        turn_status="cancelled",
    )

    with pytest.raises(ValueError, match="turn_not_resumable"):
        saver.resume_turn("session_1", turn_id="turn-user-1")
    with pytest.raises(ValueError, match="turn_not_resumable"):
        saver.dispatch_replay("session_1", turn_id="turn-user-1")

    replayed = saver.replay_as_new_turn(
        "session_1",
        source_turn_id="turn-user-1",
        acceptance_idempotency_key="replay-acceptance-1",
    )
    assert replayed["turn_id"] != "turn-user-1"
    assert replayed["replay_of_turn_id"] == "turn-user-1"

    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        turns = connection.execute(
            "SELECT turn_id, turn_ordinal, root_input_item_id, status, replay_of_turn_id "
            "FROM turn_records ORDER BY turn_ordinal"
        ).fetchall()
        roots = connection.execute(
            "SELECT item_id, turn_id FROM item_catalog WHERE semantic_kind = 'user_input' "
            "ORDER BY item_sequence"
        ).fetchall()
        acceptances = connection.execute(
            "SELECT turn_id, accepted_ingress_id, acceptance_idempotency_key "
            "FROM turn_acceptances ORDER BY created_at, turn_id"
        ).fetchall()
        view_turns = connection.execute(
            "SELECT turn_id, logical_turn_ordinal FROM context_view_turns "
            "WHERE turn_id IN (?, ?) ORDER BY logical_turn_ordinal",
            ("turn-user-1", str(replayed["turn_id"])),
        ).fetchall()

    assert turns[0] == (
        "turn-user-1",
        1,
        "item-user-1",
        "cancelled",
        None,
    )
    assert turns[1][0] == replayed["turn_id"]
    assert turns[1][1] > turns[0][1]
    assert turns[1][2] != turns[0][2]
    assert turns[1][3] == "active"
    assert turns[1][4] == "turn-user-1"
    assert len(roots) == 2
    assert roots[0][0] != roots[1][0]
    assert roots[1][1] == replayed["turn_id"]
    assert len(acceptances) == 2
    assert acceptances[0][2] != acceptances[1][2]
    assert [row[0] for row in view_turns] == ["turn-user-1", replayed["turn_id"]]


@pytest.mark.parametrize(
    ("turn_status", "outcome"),
    [("completed_empty", "completed_empty"), ("failed", "failed")],
)
def test_terminal_turn_rejects_resume_and_original_dispatch_without_mutation(
    tmp_path: Path,
    session_bundle_factory,
    turn_status: str,
    outcome: str,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    accepted = _accept(saver, "session_1")
    saver.converge_execution(
        "session_1",
        turn_id="turn-user-1",
        execution_id=str(accepted["initial_execution_id"]),
        outcome=outcome,
        turn_status=turn_status,
    )

    db_path = _rollout_db(sessions_dir, "session_1")
    with sqlite3.connect(db_path) as connection:
        before_turn = connection.execute(
            "SELECT status, last_execution_id, final_item_id FROM turn_records "
            "WHERE turn_id = 'turn-user-1'"
        ).fetchone()
        before_execution_count = connection.execute(
            "SELECT COUNT(*) FROM executions WHERE turn_id = 'turn-user-1'"
        ).fetchone()[0]

    with pytest.raises(ValueError, match="turn_not_resumable"):
        saver.resume_turn("session_1", turn_id="turn-user-1")
    with pytest.raises(ValueError, match="turn_not_resumable"):
        saver.dispatch_replay("session_1", turn_id="turn-user-1")

    with sqlite3.connect(db_path) as connection:
        after_turn = connection.execute(
            "SELECT status, last_execution_id, final_item_id FROM turn_records "
            "WHERE turn_id = 'turn-user-1'"
        ).fetchone()
        after_execution_count = connection.execute(
            "SELECT COUNT(*) FROM executions WHERE turn_id = 'turn-user-1'"
        ).fetchone()[0]

    assert after_turn == before_turn
    assert after_execution_count == before_execution_count == 1


def test_replay_as_new_turn_is_idempotent_after_saver_restart(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    _accept(saver, "session_1")

    first = saver.replay_as_new_turn(
        "session_1",
        source_turn_id="turn-user-1",
        acceptance_idempotency_key="replay-acceptance-restart-1",
    )
    restarted = RolloutCheckpointSaver(sessions_dir)
    second = restarted.replay_as_new_turn(
        "session_1",
        source_turn_id="turn-user-1",
        acceptance_idempotency_key="replay-acceptance-restart-1",
    )

    assert second["idempotent"] is True
    for field in (
        "turn_id",
        "root_input_item_id",
        "initial_execution_id",
        "commit_id",
        "replay_of_turn_id",
        "operation",
    ):
        assert second[field] == first[field]

    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        turn_count, root_count, acceptance_count, lineage_count = connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM turn_records), "
            "(SELECT COUNT(*) FROM item_catalog WHERE semantic_kind = 'user_input'), "
            "(SELECT COUNT(*) FROM turn_acceptances), "
            "(SELECT COUNT(*) FROM item_relations WHERE relation = 'replay_input')"
        ).fetchone()
    assert (turn_count, root_count, acceptance_count, lineage_count) == (2, 2, 2, 1)


def test_partial_content_part_anchor_survives_restart_and_rejects_unreachable_view(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    accepted = _accept(saver, "session_1")
    root = "请检查当前状态"
    part = ContentPart.create(
        part_id="part-user-1",
        part_ordinal=0,
        part_semantic_kind="user_input",
        content=root,
    )
    saver.register_content_part(
        "session_1",
        item_id=str(accepted["root_input_item_id"]),
        part=part,
        locator={
            "json_pointer": "/payload",
            "encoding": "jcs:v1",
            "offset": 0,
            "length": len(canonical_json_bytes(root)),
        },
    )
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "checkpoint-anchor"
    checkpoint["channel_values"] = {
        "messages": [HumanMessage(content=root, id="user-1")]
    }
    checkpoint["channel_versions"] = {"messages": "1"}
    checkpoint["updated_channels"] = ["messages"]
    saver.put(
        build_checkpoint_config("session_1"),
        checkpoint,
        {"source": "anchor-test", "step": 1, "parents": {}},
        {"messages": "1"},
    )
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        view_id, branch_id = connection.execute(
            "SELECT v.view_id, v.branch_id FROM context_views AS v "
            "JOIN branches AS b ON b.head_view_id = v.view_id "
            "WHERE b.status = 'active'"
        ).fetchone()
    anchor = ContentPartAnchor(
        anchor_id="anchor-user-part-1",
        item_id=str(accepted["root_input_item_id"]),
        part_id=part.part_id,
        mode="inclusive",
        view_id=view_id,
        branch_id=branch_id,
        capability="content_part",
        content_hash=sha256_jcs(root),
    )
    saver.register_content_part_anchor("session_1", anchor)

    restarted = RolloutCheckpointSaver(sessions_dir)
    resolved = restarted.resolve_content_part_anchor(
        "session_1",
        anchor_id=anchor.anchor_id,
    )
    assert resolved == anchor
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        persisted = connection.execute(
            "SELECT item_id, part_id, part_ordinal, line_hash FROM item_parts"
        ).fetchone()
        raw_line = (
            _rollout_db(sessions_dir, "session_1").parent / "rollout.jsonl"
        ).read_bytes()
        item_offset, item_length = connection.execute(
            "SELECT jsonl_offset, jsonl_length FROM item_catalog WHERE item_id = ?",
            (accepted["root_input_item_id"],),
        ).fetchone()
    assert persisted[:3] == (accepted["root_input_item_id"], part.part_id, 0)
    assert (
        persisted[3]
        == hashlib.sha256(raw_line[item_offset : item_offset + item_length]).hexdigest()
    )
    assert (
        restarted._storage.read_items("session_1")[0].item_id
        == accepted["root_input_item_id"]
    )

    unreachable = ContentPartAnchor(
        anchor_id="anchor-unreachable-view",
        item_id=str(accepted["root_input_item_id"]),
        part_id=part.part_id,
        mode="before",
        view_id="view-not-in-lineage",
        branch_id=branch_id,
        capability="content_part",
        content_hash=part.content_hash,
    )
    with pytest.raises(ValueError, match="不在指定 view"):
        restarted.register_content_part_anchor("session_1", unreachable)

    inactive_view_id = "view-inactive-anchor"
    inactive_branch_id = "branch-inactive-anchor"
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        connection.execute(
            "INSERT INTO branches(branch_id, branch_kind, status, head_view_id, "
            "head_checkpoint_id, parent_branch_id, created_at, updated_at) "
            "VALUES (?, 'fork', 'active', ?, NULL, NULL, ?, ?)",
            (
                inactive_branch_id,
                inactive_view_id,
                "2026-09-07T00:00:00+00:00",
                "2026-09-07T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO context_views(view_id, branch_id, parent_view_id, view_kind, "
            "head_turn_id, head_message_sequence, logical_turn_count, created_at) "
            "VALUES (?, ?, NULL, 'checkpoint', 'turn-user-1', 1, 1, ?)",
            (
                inactive_view_id,
                inactive_branch_id,
                "2026-09-07T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO context_view_items(view_id, item_id, logical_item_ordinal, "
            "visible, source_kind) VALUES (?, ?, 1, 1, 'canonical')",
            (inactive_view_id, accepted["root_input_item_id"]),
        )
        connection.commit()
    inactive_anchor = ContentPartAnchor(
        anchor_id="anchor-inactive-branch",
        item_id=str(accepted["root_input_item_id"]),
        part_id=part.part_id,
        mode="inclusive",
        view_id=inactive_view_id,
        branch_id=inactive_branch_id,
        capability="content_part",
        content_hash=part.content_hash,
    )
    with pytest.raises(ValueError, match="active branch lineage"):
        restarted.register_content_part_anchor("session_1", inactive_anchor)

    jsonl_path = _rollout_db(sessions_dir, "session_1").parent / "rollout.jsonl"
    original = jsonl_path.read_bytes()
    jsonl_path.write_bytes(original.replace(b"user-1", b"user-2", 1))
    with pytest.raises(ValueError, match="line hash 不匹配"):
        restarted.resolve_content_part_anchor(
            "session_1",
            anchor_id=anchor.anchor_id,
        )


def test_sealed_selection_drives_langchain_and_provider_projection_after_restart(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    accepted = _accept(saver, "session_1")
    tool_snapshot = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取文件",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assembly_id = saver.seal_context_for_dispatch(
        "session_1",
        turn_id=str(accepted["turn_id"]),
        execution_id=str(accepted["initial_execution_id"]),
        model_call_id="model-call-projection-1",
        provider_version="provider-v2",
        tool_snapshot=tool_snapshot,
        target_format="chat_completions",
    )
    snapshot = saver.get_context_assembly(
        "session_1",
        assembly_id=assembly_id,
    )
    plan = snapshot.as_sealed_plan()
    first_messages, first_tools, first_losses = saver.project_context_plan_to_provider(
        "session_1",
        plan,
        target_format="chat_completions",
    )
    assert [entry.plan_ordinal for entry in plan.selection] == list(
        range(len(plan.selection))
    )
    assert [message.content for message in first_messages] == ["请检查当前状态"]
    assert [tool["function"]["name"] for tool in first_tools] == ["read_file"]
    assert first_losses == ()
    history_messages = saver.project_context_plan_to_messages(
        "session_1",
        plan,
    )
    history_projection = saver.project_context_plan_to_history(
        "session_1",
        plan,
    )
    diagnostic_messages, diagnostic_losses = (
        saver.project_context_plan_with_diagnostics("session_1", plan)
    )
    assert [message.content for message in history_messages] == [
        message.content for message in first_messages
    ]
    assert [message.id for message in history_messages] == [
        message.id for message in first_messages
    ]
    assert [message.content for message in history_projection] == [
        message.content for message in history_messages
    ]
    assert [message.id for message in history_projection] == [
        message.id for message in history_messages
    ]
    assert [message.content for message in diagnostic_messages] == [
        message.content for message in first_messages
    ]
    assert diagnostic_losses == first_losses

    restarted = RolloutCheckpointSaver(sessions_dir)
    restored_snapshot = restarted.get_context_assembly(
        "session_1",
        assembly_id=assembly_id,
    )
    second_messages, second_tools, second_losses = (
        restarted.project_context_plan_to_provider(
            "session_1",
            restored_snapshot.as_sealed_plan(),
            target_format="chat_completions",
        )
    )
    assert restored_snapshot.selection == snapshot.selection
    assert [message.content for message in second_messages] == [
        message.content for message in first_messages
    ]
    assert second_tools == first_tools
    assert second_losses == first_losses


def test_optional_omitted_selection_skips_detail_body_and_provider_tools(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    accepted = _accept(saver, "session_1")
    body = {"policy": "只在需要时注入", "scope": "optional"}
    contribution = ContextContribution(
        contribution_id="optional-policy-1",
        source_kind="workspace_policy",
        source_revision="optional-policy-rev-1",
        content_hash=contribution_content_hash("prompt", body),
        body=body,
        metadata={"source_ordinal": 0},
    )
    saver.register_context_contribution(
        "session_1",
        contribution,
        request_content=body,
    )

    assembly_id = saver.seal_context_for_dispatch(
        "session_1",
        turn_id=str(accepted["turn_id"]),
        execution_id=str(accepted["initial_execution_id"]),
        model_call_id="model-call-optional-omission",
        provider_version="provider-v2",
        target_format="chat_completions",
        omitted_ref_ids=(contribution.contribution_id,),
    )
    snapshot = saver.get_context_assembly(
        "session_1",
        assembly_id=assembly_id,
    )
    omitted = next(
        entry
        for entry in snapshot.selection
        if entry.ref.ref_id == contribution.contribution_id
    )
    assert omitted.included is False
    assert omitted.detail_ref is None
    assert omitted.contribution_id is None
    assert omitted.content_length == contribution.content_length
    assert omitted.loss == ("selection_omitted",)

    messages, tools, losses = saver.project_context_plan_to_provider(
        "session_1",
        snapshot.as_sealed_plan(),
        target_format="chat_completions",
    )
    assert [message.content for message in messages] == ["请检查当前状态"]
    assert tools == []
    assert losses == ("selection_omitted",)
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM context_plan_details").fetchone()[
                0
            ]
            == 0
        )


def test_request_only_detail_and_selection_restore_without_memory_body(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    accepted = _accept(saver, "session_1")
    body = {"policy": "先读取工作区配置", "scope": "request"}
    contribution = ContextContribution(
        contribution_id="policy-contribution-1",
        source_kind="workspace_policy",
        source_revision="policy-revision-1",
        content_hash=contribution_content_hash("prompt", body),
        body=body,
        content_length=len(canonical_json_bytes(body)),
        metadata={"source_ordinal": 0},
    )

    prepared = saver.prepare_context_for_provider(
        "session_1",
        turn_id=str(accepted["turn_id"]),
        prompt_contributions=(contribution,),
        tool_snapshot=({"type": "function", "function": {"name": "read_file"}},),
        provider_version="provider-v2",
        target_format="chat_completions",
        plan_creation_idempotency_key="test_turn_execution_recovery:1282:create",
        seal_idempotency_key="test_turn_execution_recovery:1282:seal",
    )
    assembly_id = str(prepared["assembly_id"])
    plan = prepared["plan"]
    assert isinstance(plan, ContextRequestPlan)
    assert plan.plan_state == "sealed"
    assert [entry.plan_ordinal for entry in plan.selection] == list(
        range(len(plan.selection))
    )
    assert any(
        entry.selection_kind == "request_only"
        and entry.included
        and entry.detail_ref
        and entry.contribution_id == contribution.contribution_id
        for entry in plan.selection
    )
    first_messages = prepared["messages"]
    first_tools = prepared["tools"]
    assert [message.content for message in first_messages] == [
        [{"policy": "先读取工作区配置", "scope": "request"}],
        "请检查当前状态",
    ]
    assert first_tools == [{"type": "function", "function": {"name": "read_file"}}]

    restarted = RolloutCheckpointSaver(sessions_dir)
    restored = restarted.get_context_assembly(
        "session_1",
        assembly_id=assembly_id,
    )
    restored_plan = restored.as_sealed_plan()
    messages, tools, losses = restarted.project_context_plan_to_provider(
        "session_1",
        restored_plan,
        target_format="chat_completions",
    )
    assert restored_plan.selection == plan.selection
    assert [message.content for message in messages] == [
        message.content for message in first_messages
    ]
    assert tools == first_tools
    assert losses == ()
    history_messages = restarted.project_context_plan_to_history(
        "session_1",
        restored_plan,
    )
    assert [message.content for message in history_messages] == ["请检查当前状态"]
    assert all(
        message.id != contribution.contribution_id for message in history_messages
    )


def test_protected_request_only_detail_projects_after_restart_with_injected_key(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    key = b"0123456789abcdef0123456789abcdef"
    saver = RolloutCheckpointSaver(
        sessions_dir,
        protected_detail_key=key,
    )
    accepted = _accept(saver, "session_1")
    body = {"secret_policy": "只允许 provider 使用"}
    contribution = ContextContribution(
        contribution_id="protected-policy-1",
        source_kind="workspace_secret",
        source_revision="protected-policy-revision-1",
        content_hash=contribution_content_hash("prompt", body),
        body=body,
        content_length=len(canonical_json_bytes(body)),
        visibility="private",
        protection="protected",
        metadata={"source_ordinal": 0},
    )

    prepared = saver.prepare_context_for_provider(
        "session_1",
        turn_id=str(accepted["turn_id"]),
        prompt_contributions=(contribution,),
        provider_version="provider-v2",
        target_format="chat_completions",
        plan_creation_idempotency_key="test_turn_execution_recovery:1364:create",
        seal_idempotency_key="test_turn_execution_recovery:1364:seal",
    )
    plan = prepared["plan"]
    assert isinstance(plan, ContextRequestPlan)
    protected_entries = [
        entry
        for entry in plan.selection
        if entry.ref.ref_id == contribution.contribution_id
    ]
    assert len(protected_entries) == 1
    assert protected_entries[0].protection == "protected"
    assert protected_entries[0].detail_ref is not None

    restarted = RolloutCheckpointSaver(
        sessions_dir,
        protected_detail_key=key,
    )
    restored = restarted.get_context_assembly(
        "session_1",
        assembly_id=str(prepared["assembly_id"]),
    )
    restored_plan = restored.as_sealed_plan()
    messages, _, losses = restarted.project_context_plan_to_provider(
        "session_1",
        restored_plan,
        target_format="chat_completions",
    )
    assert losses == ()
    assert [message.content for message in messages] == [
        [body],
        "请检查当前状态",
    ]
    assert (
        restarted.read_context_plan_detail(
            "session_1",
            detail_ref=protected_entries[0].detail_ref,
            include_sensitive=True,
        )["detail"]
        == body
    )


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("context_assemblies", "status", 1),
        ("context_assemblies", "history_view_revision", -1),
        ("assembly_item_refs", "content_length", -1),
    ],
)
def test_sealed_assembly_restore_rejects_coerced_sqlite_manifest_values(
    tmp_path: Path,
    session_bundle_factory,
    table: str,
    column: str,
    value: object,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    accepted = _accept(saver, "session_1")
    assembly_id = saver.seal_context_for_dispatch(
        "session_1",
        turn_id=str(accepted["turn_id"]),
        execution_id=str(accepted["initial_execution_id"]),
        model_call_id="model-call-corrupt-manifest",
        provider_version="provider-v2",
        target_format="chat_completions",
    )
    where = "assembly_id = ?"
    parameters: tuple[object, ...] = (value, assembly_id)
    if table == "assembly_item_refs":
        where = "assembly_id = ? AND ref_ordinal = 0"
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        connection.execute(
            f"UPDATE {table} SET {column} = ? WHERE {where}",
            parameters,
        )
        connection.commit()

    with pytest.raises((RuntimeError, TypeError), match="assembly manifest|manifest"):
        saver.get_context_assembly("session_1", assembly_id=assembly_id)


def test_sealed_assembly_restore_rejects_missing_detail_manifest(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    accepted = _accept(saver, "session_1")
    body = {"policy": "只能在本次请求使用"}
    contribution = ContextContribution(
        contribution_id="restore-detail-missing",
        source_kind="workspace_policy",
        source_revision="restore-detail-revision",
        content_hash=contribution_content_hash("prompt", body),
        body=body,
        content_length=len(canonical_json_bytes(body)),
        metadata={"source_ordinal": 0},
    )
    prepared = saver.prepare_context_for_provider(
        "session_1",
        turn_id=str(accepted["turn_id"]),
        prompt_contributions=(contribution,),
        provider_version="provider-v2",
        target_format="chat_completions",
        plan_creation_idempotency_key="test_turn_execution_recovery:1471:create",
        seal_idempotency_key="test_turn_execution_recovery:1471:seal",
    )
    with sqlite3.connect(_rollout_db(sessions_dir, "session_1")) as connection:
        connection.execute(
            "DELETE FROM context_plan_details WHERE assembly_id = ?",
            (prepared["assembly_id"],),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="detail-unavailable"):
        saver.get_context_assembly(
            "session_1",
            assembly_id=str(prepared["assembly_id"]),
        )
