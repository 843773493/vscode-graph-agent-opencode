"""真实 Saver/JSONL/SQLite 的有序消息 group 与重启恢复合同。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
)


@pytest.fixture
def group_case(tmp_path, session_bundle_factory):
    session_id = "session-codec-group"
    session_dir = session_bundle_factory(tmp_path, session_id)
    content = [
        {"type": "text", "text": "先检查", "id": "text-before"},
        {"type": "reasoning", "reasoning": "核对参数", "id": "reasoning-1", "index": 1},
        {"type": "text", "text": "然后调用", "id": "text-after"},
    ]
    messages = [
        HumanMessage(
            id="user-1",
            content="检查文件",
            response_metadata={
                "message_role": "system",
                "message_metadata": {
                    "source": "session_subagent_delegation",
                    "parent_session_id": "parent-1",
                },
            },
        ),
        AIMessage(
            id="call-message-1",
            content=content,
            tool_calls=[{"id": "call-1", "name": "read", "args": {"path": "a.txt"}}],
            response_metadata={
                "phase": "commentary",
                "token_usage": {"input_tokens": 12, "output_tokens": 5},
            },
        ),
        ToolMessage(
            id="result-1", content="文件正文", tool_call_id="call-1", name="read"
        ),
        AIMessage(
            id="final-1", content="完成", response_metadata={"phase": "final_answer"}
        ),
    ]
    checkpoint = {
        "id": "checkpoint-group-1",
        "channel_values": {"messages": messages},
        "channel_versions": {"messages": "1"},
        "updated_channels": ["messages"],
    }
    return session_id, session_dir, messages, checkpoint


def _save(tmp_path, group_case):
    session_id, _, _, checkpoint = group_case
    saver = RolloutCheckpointSaver(tmp_path)
    config = {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}}
    saver.put(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": "1"},
    )
    return saver, config


def test_canonical_group_jsonl_locator_commit_and_fresh_process(tmp_path, group_case):
    session_id, session_dir, messages, _ = group_case
    _save(tmp_path, group_case)
    path = session_dir / "rollout" / "rollout.jsonl"
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    rows = [json.loads(line) for line in lines]
    items = [CanonicalItemRecord.from_dict(row) for row in rows]
    assert [item.semantic_kind for item in items] == [
        "user_input",
        "assistant_output",
        "reasoning",
        "assistant_output",
        "tool_call",
        "tool_result",
        "assistant_output",
    ]
    assert [item.item_sequence for item in items] == list(range(1, 8))
    assert items[0].turn_scope == "turn_root"
    assert all(item.turn_id == items[0].turn_id for item in items)
    assert all(
        line == canonical_json_line(item.to_dict())
        for line, item in zip(lines, items, strict=True)
    )
    group = items[1:5]
    assert all("token_usage" not in item.metadata for item in group[:-1])
    assert len({item.message_group_id for item in group}) == 1
    assert len({json.dumps(item.producer_ref, sort_keys=True) for item in group}) == 1
    assert [item.metadata["projection_group"]["ordinal"] for item in group] == [
        0,
        1,
        2,
        3,
    ]
    assert group[1].payload == "核对参数"
    assert set(group[-1].payload) == {"tool_calls"}
    assert "核对参数" not in json.dumps(group[-1].metadata, ensure_ascii=False)
    assert items[0].metadata["source"] == "session_subagent_delegation"
    assert items[0].metadata["parent_session_id"] == "parent-1"
    assert "message_role" not in items[0].metadata
    assert group[-1].metadata["phase"] == "commentary"
    assert group[-1].metadata["token_usage"] == {"input_tokens": 12, "output_tokens": 5}
    with sqlite3.connect(
        f"file:{session_dir}/rollout/index.sqlite?mode=ro", uri=True
    ) as db:
        catalogs = db.execute(
            "SELECT item_sequence, jsonl_offset, jsonl_length, commit_id FROM item_catalog ORDER BY item_sequence"
        ).fetchall()
        assert len({row[3] for row in catalogs}) == 1
        for sequence, offset, length, _ in catalogs:
            assert raw[offset : offset + length] == lines[sequence - 1]
        assert db.execute(
            "SELECT jsonl_record_count FROM storage_commits WHERE status = 'committed'"
        ).fetchall() == [(7,)]
        anchors = db.execute(
            "SELECT message_sequence, jsonl_offset, jsonl_length FROM messages ORDER BY message_sequence"
        ).fetchall()
        assert [
            json.loads(raw[offset : offset + length])["item_sequence"]
            for _, offset, length in anchors
        ] == [1, 5, 6, 7]
        assert [row[0] for row in anchors] == [1, 2, 3, 4]
        assert (
            db.execute(
                "SELECT COUNT(*) FROM context_view_items WHERE visible = 1"
            ).fetchone()[0]
            == 7
        )
    script = """
import json, sys
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
saver = RolloutCheckpointSaver(sys.argv[1])
value = saver.get_tuple({'configurable': {'thread_id': sys.argv[2], 'checkpoint_ns': ''}})
print(json.dumps([{'id': message.id, 'content': message.content, 'metadata': message.response_metadata, 'calls': getattr(message, 'tool_calls', [])} for message in value.checkpoint['channel_values']['messages']], ensure_ascii=False))
"""
    result = subprocess.run(
        ["uv", "run", "python", "-c", script, str(tmp_path), session_id],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=True,
    )
    restored = json.loads(result.stdout)
    assert [row["id"] for row in restored] == [message.id for message in messages]
    assert restored[1]["content"] == messages[1].content
    assert restored[1]["calls"] == messages[1].tool_calls
    assert restored[1]["metadata"]["phase"] == "commentary"
    assert (
        restored[1]["metadata"]["token_usage"]
        == messages[1].response_metadata["token_usage"]
    )
    assert (
        restored[0]["metadata"]["message_metadata"]["source"]
        == "session_subagent_delegation"
    )
    assert (
        restored[0]["metadata"]["message_metadata"]["parent_session_id"] == "parent-1"
    )
    assert path.read_bytes() == raw


def test_group_retry_and_content_conflict_preserve_immutable_jsonl(
    tmp_path, group_case
):
    _, session_dir, messages, checkpoint = group_case
    saver, config = _save(tmp_path, group_case)
    path = session_dir / "rollout" / "rollout.jsonl"
    before = path.read_bytes()
    metadata = {"source": "test", "step": 1, "writes": {}}
    saver.put(config, checkpoint, metadata, {"messages": "1"})
    assert path.read_bytes() == before
    changed = messages[1].model_copy(
        update={"content": [{"type": "text", "text": "冲突正文"}]}
    )
    conflict = {
        **checkpoint,
        "channel_values": {"messages": [messages[0], changed, *messages[2:]]},
    }
    with pytest.raises(ValueError, match="canonical"):
        saver.put(config, conflict, metadata, {"messages": "1"})
    assert path.read_bytes() == before
    restored = RolloutCheckpointSaver(tmp_path).get_tuple(config)
    assert (
        restored.checkpoint["channel_values"]["messages"][1].content
        == messages[1].content
    )


@pytest.mark.parametrize("mutation", ["missing", "reversed", "duplicate"])
def test_group_decoder_rejects_incomplete_or_reordered_items(group_case, mutation):
    message = group_case[2][1]
    codec = LangChainMessageCodec()
    group = codec.items_for_message(
        message,
        item_sequence=1,
        message_id=message.id,
        turn_id="turn-1",
        timestamp="2026-09-08T00:00:00+00:00",
    )
    altered = (
        group[1:]
        if mutation == "missing"
        else tuple(reversed(group))
        if mutation == "reversed"
        else (group[0], *group)
    )
    with pytest.raises(ValueError, match="group"):
        codec.project_message(altered)


@pytest.mark.parametrize("mutation", ["order", "phase", "source", "token_usage"])
def test_repeated_checkpoint_rejects_semantic_changes(tmp_path, group_case, mutation):
    _, session_dir, messages, checkpoint = group_case
    saver, config = _save(tmp_path, group_case)
    before = (session_dir / "rollout" / "rollout.jsonl").read_bytes()
    changed_messages = list(messages)
    if mutation == "order":
        changed_messages[1], changed_messages[2] = (
            changed_messages[2],
            changed_messages[1],
        )
    else:
        index = 0 if mutation == "source" else 1
        metadata = dict(messages[index].response_metadata)
        metadata[mutation] = (
            {"input_tokens": 999} if mutation == "token_usage" else "changed"
        )
        changed_messages[index] = messages[index].model_copy(
            update={"response_metadata": metadata}
        )
    with pytest.raises(ValueError, match="canonical|重复 checkpoint"):
        saver.put(
            config,
            {**checkpoint, "channel_values": {"messages": changed_messages}},
            {"source": "test", "step": 1, "writes": {}},
            {"messages": "1"},
        )
    assert (session_dir / "rollout" / "rollout.jsonl").read_bytes() == before


@pytest.mark.parametrize(
    "part",
    [
        {
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "摘要"}],
            "encrypted_content": "protected-value",
        },
        {"type": "thinking", "thinking": "受保护思考", "signature": "signature-value"},
    ],
)
def test_protected_reasoning_is_separate_valid_canonical_item(
    tmp_path, group_case, part
):
    _, session_dir, messages, _ = group_case
    messages[1] = messages[1].model_copy(
        update={"content": [part, {"type": "text", "text": "调用"}]}
    )
    _, config = _save(tmp_path, group_case)
    rows = [
        json.loads(line)
        for line in (session_dir / "rollout" / "rollout.jsonl").read_text().splitlines()
    ]
    protected = next(
        CanonicalItemRecord.from_dict(row)
        for row in rows
        if row["semantic_kind"] == "reasoning"
    )
    assert protected.payload_kind == "extension"
    assert protected.payload["protection"] == {"visibility": "protected"}
    assert protected.payload["value"] == part
    restored = RolloutCheckpointSaver(tmp_path).get_tuple(config)
    assert (
        restored.checkpoint["channel_values"]["messages"][1].content
        == messages[1].content
    )


def test_group_catalog_corruption_does_not_rewrite_jsonl(tmp_path, group_case):
    _, session_dir, _, _ = group_case
    _, config = _save(tmp_path, group_case)
    path = session_dir / "rollout" / "rollout.jsonl"
    before = path.read_bytes()
    with sqlite3.connect(session_dir / "rollout" / "index.sqlite") as db:
        db.execute(
            "UPDATE item_catalog SET message_group_id = 'foreign-group' WHERE item_sequence = 3"
        )
    with pytest.raises(RuntimeError, match="group|catalog"):
        RolloutCheckpointSaver(tmp_path).get_tuple(config)
    assert path.read_bytes() == before


def test_group_sql_failure_rolls_back_jsonl_catalog_and_locators(tmp_path, group_case):
    session_id, session_dir, messages, checkpoint = group_case
    saver = RolloutCheckpointSaver(tmp_path)
    config = {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}}
    metadata = {"source": "test", "step": 1, "writes": {}}
    saver.put(
        config,
        {
            **checkpoint,
            "id": "checkpoint-root",
            "channel_values": {"messages": messages[:1]},
        },
        metadata,
        {"messages": "1"},
    )
    path = session_dir / "rollout" / "rollout.jsonl"
    before = path.read_bytes()
    database = session_dir / "rollout" / "index.sqlite"
    # 在真实 SQLite 的 reasoning item 插入时制造故障，验证跨存储原子性。
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TRIGGER reject_reasoning BEFORE INSERT ON item_catalog WHEN NEW.semantic_kind = 'reasoning' BEGIN SELECT RAISE(ABORT, 'injected-group-failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected-group-failure"):
        saver.put(config, checkpoint, metadata, {"messages": "1"})
    assert path.read_bytes() == before
    with sqlite3.connect(database) as db:
        for table in ("item_catalog", "messages", "storage_commits", "checkpoints"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        assert db.execute(
            "SELECT last_item_sequence, last_message_sequence, committed_jsonl_offset FROM database_meta"
        ).fetchone() == (1, 1, len(before))
        db.execute("DROP TRIGGER reject_reasoning")
    saver.put(config, checkpoint, metadata, {"messages": "1"})
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["item_sequence"] for row in rows] == list(range(1, 8))
    assert path.read_bytes().startswith(before)


@pytest.mark.parametrize("mutation", ["missing_companions", "phase"])
def test_request_tool_group_rejects_existing_group_semantic_changes(
    tmp_path, group_case, mutation
):
    session_id, session_dir, messages, _ = group_case
    saver, _ = _save(tmp_path, group_case)
    path = session_dir / "rollout" / "rollout.jsonl"
    before = path.read_bytes()
    turn_id = json.loads(before.splitlines()[0])["turn_id"]
    assert saver._storage.ensure_request_tool_result_items(
        session_id, turn_id=turn_id, messages=messages
    ) == ()
    changed = messages[1].model_copy(
        update={"content": messages[1].content[:1]}
        if mutation == "missing_companions"
        else {"response_metadata": {**messages[1].response_metadata, "phase": "changed"}}
    )
    with pytest.raises(RuntimeError, match="canonical.*group"):
        saver._storage.ensure_request_tool_result_items(
            session_id, turn_id=turn_id, messages=[changed]
        )
    assert path.read_bytes() == before


def test_request_tool_groups_are_committed_once_and_reused_by_checkpoint(
    tmp_path, group_case
):
    session_id, session_dir, messages, checkpoint = group_case
    saver = RolloutCheckpointSaver(tmp_path)
    config = {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}}
    metadata = {"source": "test", "step": 1, "writes": {}}
    saver.put(
        config,
        {**checkpoint, "id": "root", "channel_values": {"messages": messages[:1]}},
        metadata,
        {"messages": "1"},
    )
    path = session_dir / "rollout" / "rollout.jsonl"
    turn_id = json.loads(path.read_bytes().splitlines()[0])["turn_id"]
    committed = saver._storage.ensure_request_tool_result_items(
        session_id, turn_id=turn_id, messages=messages[1:3]
    )
    assert committed == (
        "item-call-message-1-content-0",
        "item-call-message-1-content-1",
        "item-call-message-1-content-2",
        "item-call-message-1",
        "item-result-1",
    )
    before = path.read_bytes()
    assert len(before.splitlines()) == 6
    assert saver._storage.ensure_request_tool_result_items(
        session_id, turn_id=turn_id, messages=messages[1:3]
    ) == ()
    assert path.read_bytes() == before
    saver.put(config, checkpoint, metadata, {"messages": "1"})
    after = path.read_bytes()
    assert after.startswith(before)
    rows = [json.loads(line) for line in after.splitlines()]
    assert [row["item_sequence"] for row in rows] == list(range(1, 8))
    with sqlite3.connect(session_dir / "rollout" / "index.sqlite") as db:
        assert db.execute(
            "SELECT COUNT(DISTINCT commit_id) FROM item_catalog WHERE item_sequence BETWEEN 2 AND 6"
        ).fetchone() == (1,)
    restored = RolloutCheckpointSaver(tmp_path).get_tuple(config)
    assert restored.checkpoint["channel_values"]["messages"][1].content == messages[1].content
    assert path.read_bytes() == after


def test_plain_reasoning_content_is_text_semantic_item(tmp_path, group_case):
    _, session_dir, messages, _ = group_case
    part = {"type": "reasoning_content", "reasoning_content": "先核对调用参数"}
    messages[1] = messages[1].model_copy(update={"content": [part]})
    _, config = _save(tmp_path, group_case)
    rows = [
        json.loads(line)
        for line in (session_dir / "rollout" / "rollout.jsonl").read_text().splitlines()
    ]
    reasoning = next(row for row in rows if row["semantic_kind"] == "reasoning")
    assert reasoning["payload_kind"] == "text"
    assert reasoning["payload"] == "先核对调用参数"
    restored = RolloutCheckpointSaver(tmp_path).get_tuple(config)
    assert restored.checkpoint["channel_values"]["messages"][1].content == [part]
