"""真实 Saver/JSONL/SQLite 的有序消息 group 与重启恢复合同。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProvenanceRelation,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    CANONICAL_GROUP_FIELD,
    message_annotations,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.mutation.mutation_intents import (
    MutationIntentDuplicateConsumption,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
)
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader


@pytest.fixture
def group_case(tmp_path, session_bundle_factory):
    session_id = "ses_019c205391bb7a4b81b6728df70f63e6"
    session_bundle_factory(tmp_path, session_id)
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
                "model_call_id": "model-call-1",
                "phase": "commentary",
                "token_usage": {"input_tokens": 12, "output_tokens": 5},
            },
        ),
        ToolMessage(
            id="result-1",
            content="文件正文",
            tool_call_id="call-1",
            name="read",
            response_metadata={"model_call_id": "model-call-1"},
        ),
        AIMessage(
            id="final-1",
            content="完成",
            response_metadata={
                "phase": "final_answer",
                "model_call_id": "model-call-final",
            },
        ),
    ]
    checkpoint = {
        "id": "checkpoint-group-1",
        "channel_values": {"messages": messages},
        "channel_versions": {"messages": "1"},
        "updated_channels": ["messages"],
    }
    saver = RolloutCheckpointSaver(tmp_path)
    thread_id = saver._storage.storage.main_thread_id(session_id)
    session_dir = saver._storage.storage.root(session_id, thread_id=thread_id).parent
    saver.accept_turn(
        session_id,
        thread_id=thread_id,
        accepted_ingress_id="user-1",
        acceptance_idempotency_key="acceptance:user-1",
        job_id="job-group",
        admission_idempotency_key="admission:job-group",
        turn_id="turn-tool-result",
        root_item_id="item-user-1",
        payload=messages[0].content,
        initial_execution_id="execution-group",
        extensions=message_annotations(messages[0].response_metadata),
    )
    admission = saver.execution_admission(
        session_id,
        thread_id=thread_id,
        execution_id="execution-group",
    )
    with (
        session_thread_owner_scope(
            MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id
        ),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        yield session_id, session_dir, messages, checkpoint


def _save(tmp_path, group_case):
    session_id, _, _, checkpoint = group_case
    saver = RolloutCheckpointSaver(tmp_path)
    config = {
        "configurable": {
            "thread_id": saver._storage.storage.main_thread_id(session_id),
            "checkpoint_ns": "",
        }
    }
    saver.put(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": "1"},
    )
    return saver, config


@pytest.fixture
def opaque_group_case(tmp_path, group_case):
    session_id, _, messages, _ = group_case
    codec = LangChainMessageCodec()
    group = codec.items_for_message(
        messages[1],
        item_sequence=2,
        message_id="call-message-1",
        turn_id="turn-tool-result",
        execution_id="execution-group",
        model_call_id="model-call-1",
        timestamp="2026-10-05T00:00:00+00:00",
    )
    members = tuple(
        replace(
            item,
            extensions={
                **item.extensions,
                "vendor.opaque": {
                    "extension_version": "future-99",
                    "encoding": "opaque-v42",
                    "protection": "public",
                    "value": [index, {"owner": "只保存，不参与归属"}],
                },
            },
            provenance_relations=(
                ItemProvenanceRelation(
                    "derived_from",
                    "canonical_item",
                    f"source-item-{index}",
                    target_session_id="source-session",
                    target_thread_id="source-thread",
                    target_turn_id="source-turn",
                    target_model_call_id=f"source-model-{index}",
                ),
            ),
        )
        for index, item in enumerate(group)
    )
    # carrier 的无正文 manifest 引用已发布的 canonical 组，不能替测试完成首次发布。
    saver = RolloutCheckpointSaver(tmp_path)
    thread_id = saver._storage.storage.main_thread_id(session_id)
    saver.append_items(session_id, members, thread_id=thread_id)
    messages[1] = codec.from_dict(codec.project_message(members))
    return group_case, members


def test_checkpoint_preserves_each_member_opaque_extensions_and_provenance_on_reopen(
    tmp_path,
    opaque_group_case,
):
    group_case, expected = opaque_group_case
    session_id, session_dir, _, _ = group_case
    _, config = _save(tmp_path, group_case)
    path = session_dir / "rollout" / "rollout.jsonl"
    before = path.read_bytes()
    script = """
import json, sys
from pathlib import Path
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from tests.support.session_catalog_owner import fixture_catalog_scope
with fixture_catalog_scope():
    saver = RolloutCheckpointSaver(Path(sys.argv[1]))
    thread_id = saver._storage.storage.main_thread_id(sys.argv[2])
    with session_thread_owner_scope(MutationIntentOwner(sys.argv[2], thread_id), langgraph_thread_id=thread_id):
        restored = saver.get_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
    print(json.dumps(restored.checkpoint["channel_values"]["messages"][1].response_metadata["canonical_item_manifests"], ensure_ascii=False))
"""
    fresh = subprocess.run(
        [
            "timeout",
            "30",
            "bash",
            "-c",
            'ulimit -d 4194304; exec "$@"',
            "bash",
            "uv",
            "run",
            "python",
            "-c",
            script,
            str(tmp_path),
            session_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert fresh.returncode == 0, fresh.stderr
    assert json.loads(fresh.stdout) == [
        {"item_id": item.item_id, **item.projection_manifest()} for item in expected
    ]
    with RolloutCheckpointSaver(tmp_path) as reopened:
        restored = reopened.get_tuple(config)
        assert restored is not None
        messages = restored.checkpoint["channel_values"]["messages"]
        manifests = messages[1].response_metadata[CANONICAL_GROUP_FIELD]
        assert manifests == [
            {"item_id": item.item_id, **item.projection_manifest()} for item in expected
        ]
        items = reopened._storage.storage.read_items(
            session_id, config["configurable"]["thread_id"]
        )
        actual = [
            item
            for item in items
            if item.message_group_id == expected[-1].message_group_id
        ]
        assert [item.projection_manifest() for item in actual] == [
            item.projection_manifest() for item in expected
        ]
        reopened.put(
            config,
            {**restored.checkpoint, "id": "checkpoint-opaque-retry", "channel_versions": {"messages": "2"}},
            {"source": "test", "step": 2, "writes": {}},
            {"messages": "2"},
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["extensions", "provenance_relations"])
def test_same_body_checkpoint_rejects_changed_opaque_or_member_provenance(
    tmp_path,
    opaque_group_case,
    mutation,
):
    group_case, _ = opaque_group_case
    _, session_dir, _, checkpoint = group_case
    saver, config = _save(tmp_path, group_case)
    messages = list(saver.get_tuple(config).checkpoint["channel_values"]["messages"])
    metadata = deepcopy(messages[1].response_metadata)
    if mutation == "extensions":
        metadata[CANONICAL_GROUP_FIELD][0]["extensions"]["vendor.opaque"]["value"] = (
            "changed"
        )
    else:
        metadata[CANONICAL_GROUP_FIELD][0]["provenance_relations"][0]["target_id"] = (
            "other-source-item"
        )
    messages[1] = messages[1].model_copy(update={"response_metadata": metadata})
    path = session_dir / "rollout" / "rollout.jsonl"
    before = path.read_bytes()
    with sqlite3.connect(session_dir / "rollout" / "index.sqlite") as database:
        catalog_before = database.execute(
            "SELECT * FROM item_catalog ORDER BY item_sequence"
        ).fetchall()
        commits_before = database.execute(
            "SELECT * FROM storage_commits ORDER BY commit_id"
        ).fetchall()
    with pytest.raises(ValueError, match="typed provenance/extensions"):
        saver.put(
            config,
            {
                **checkpoint,
                "id": "checkpoint-opaque-conflict",
                "channel_versions": {"messages": "2"},
                "channel_values": {"messages": messages},
            },
            {"source": "test", "step": 2, "writes": {}},
            {"messages": "2"},
        )
    assert path.read_bytes() == before
    with sqlite3.connect(session_dir / "rollout" / "index.sqlite") as database:
        assert (
            database.execute(
                "SELECT * FROM item_catalog ORDER BY item_sequence"
            ).fetchall()
            == catalog_before
        )
        assert (
            database.execute(
                "SELECT * FROM storage_commits ORDER BY commit_id"
            ).fetchall()
            == commits_before
        )


@pytest.mark.parametrize("model_call", ["model-call-1", "model-call-2"])
@pytest.mark.parametrize("carrier", ["provider_raw", "canonical_scoped"])
def test_indexed_reader_keeps_reused_provider_tool_call_within_requested_model_scope(
    tmp_path,
    group_case,
    model_call,
    carrier,
):
    session_id, _, messages, _ = group_case
    first_id = (
        "model-call-1:tool-call:call-1" if carrier == "canonical_scoped" else "call-1"
    )
    messages[1] = messages[1].model_copy(
        update={
            "tool_calls": [{**messages[1].tool_calls[0], "id": first_id}],
        }
    )
    messages[2] = messages[2].model_copy(update={"tool_call_id": first_id})
    second_id = (
        "model-call-2:tool-call:call-1" if carrier == "canonical_scoped" else "call-1"
    )
    messages[3:3] = [
        AIMessage(
            id="call-message-2",
            content="",
            response_metadata={"model_call_id": "model-call-2"},
            tool_calls=[{"id": second_id, "name": "read", "args": {"path": "b.txt"}}],
        ),
        ToolMessage(
            id="result-2",
            content="另一调用的结果",
            tool_call_id=second_id,
            name="read",
            response_metadata={"model_call_id": "model-call-2"},
        ),
    ]
    saver, config = _save(tmp_path, group_case)
    with saver._storage.storage.open_read_snapshot(
        session_id, config["configurable"]["thread_id"]
    ) as snapshot:
        records = saver._storage.checkpoints.read_indexed_records_batch(
            snapshot,
            turn_ids=("turn-tool-result",),
            message_roles=(),
            tool_kinds=("tool_result",),
            tool_call_ids=(f"{model_call}:tool-call:call-1",),
        )["turn-tool-result"]
    suffix = "1" if model_call == "model-call-1" else "2"
    assert {record["message_id"] for record in records} == {
        f"call-message-{suffix}",
        f"result-{suffix}",
    }
    assert all(
        record["message"]["data"]["response_metadata"]["model_call_id"] == model_call
        for record in records
    )

    page = RolloutHistoryReader(RolloutContextReader(saver._storage)).load(
        session_id,
        config["configurable"]["thread_id"],
        TurnHistoryLoadRequest(
            turn_ids=["turn-tool-result"],
            tool_call_ids=[f"{model_call}:tool-call:call-1"],
            include=["tool_call", "tool_result", "tool_summary"],
        ),
    )
    detail = page.items[0]
    assert [part.kind for part in detail.response_parts] == ["tool_call", "tool_result"]
    expected_id = f"{model_call}:tool-call:call-1" if carrier == "canonical_scoped" else "call-1"
    assert {part.tool_call_id for part in detail.response_parts} == {expected_id}
    assert json.loads(detail.response_parts[0].arguments)["path"] == (
        "a.txt" if suffix == "1" else "b.txt"
    )
    assert detail.response_parts[1].result == (
        "文件正文" if suffix == "1" else "另一调用的结果"
    )
    assert len(detail.items) == 2
    # 同一 Turn 的完整 DTO 必须经过真实 Web 合并，不能只验单个 ModelCall 的筛选结果。
    reader = RolloutHistoryReader(RolloutContextReader(saver._storage))
    summary = reader.load(
        session_id, config["configurable"]["thread_id"],
        TurnHistoryLoadRequest(turn_ids=["turn-tool-result"], include=["tool_summary"]),
    ).items[0]
    complete = reader.load(
        session_id, config["configurable"]["thread_id"],
        TurnHistoryLoadRequest(
            turn_ids=["turn-tool-result"],
            include=["tool_call", "tool_result", "tool_summary"],
        ),
    ).items[0]
    assert len(complete.items) == 4
    scoped_ids = {f"model-call-{index}:tool-call:call-1" for index in (1, 2)}
    assert {item.part_id for item in complete.items} == scoped_ids
    assert {item.tool_call_id for item in summary.tool_summary} == scoped_ids
    assert {item.tool_call_id for item in complete.tool_summary} == scoped_ids
    assert {part.tool_call_id for part in complete.response_parts} == {first_id, second_id}
    merged = subprocess.run(
        ["bun", "-e", """
import assert from 'node:assert/strict';
import {createSessionTurnTimeline, upsertTurn} from './src/clients/web/src/state/session/turnTimeline.ts';
import {responsePartsToTimelineItems} from './src/clients/web/src/state/timeline/responseParts.ts';
const {summary, complete} = JSON.parse(await Bun.stdin.text());
let timeline = upsertTurn(createSessionTurnTimeline('history-contract'), summary);
timeline = upsertTurn(timeline, complete);
timeline = upsertTurn(timeline, complete);
const items = timeline.turnsById[complete.turn_id].items;
const keys = items.map(item => `${item.part_id ?? item.event_id}:${item.type}`);
console.log(JSON.stringify({before: complete.items.length, after: items.length, keys}));
assert.equal(items.length, 4);
assert.equal(new Set(keys).size, 4);
const tools = responsePartsToTimelineItems(timeline.turnsById[complete.turn_id].response_parts)
  .filter(item => item.kind === 'aggregated_tool');
assert.equal(tools.length, 2);
assert.deepEqual(tools.map(item => item.resultText), ['文件正文', '另一调用的结果']);
"""],
        input=json.dumps({
            "summary": summary.model_dump(mode="json"),
            "complete": complete.model_dump(mode="json"),
        }),
        text=True, capture_output=True, cwd=Path.cwd(), timeout=30, check=False,
    )
    assert merged.returncode == 0, merged.stdout + merged.stderr
    # Trace 与摘要用于跨 ModelCall 聚合；canonical payload 与 Provider raw ID 各守原表示。
    scoped_id = f"{model_call}:tool-call:call-1"
    assert {item.part_id for item in detail.items} == {scoped_id}
    assert {item.tool_call_id for item in detail.tool_summary} == {scoped_id}
    assert len(detail.tool_summary) == 2
    with saver._storage.storage.open_read_snapshot(
        session_id, config["configurable"]["thread_id"]
    ) as snapshot:
        assert saver._storage.checkpoints.read_indexed_records_batch(
            snapshot,
            turn_ids=("turn-tool-result",),
            message_roles=(),
            tool_kinds=("tool_call", "tool_result"),
            tool_call_ids=("unknown-model:tool-call:call-1",),
        )["turn-tool-result"] == []


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
    for line, item in zip(lines, items, strict=True):
        restored = item.to_dict()
        original = json.loads(line)
        assert line == canonical_json_line(restored), {
            key: (original.get(key), restored.get(key))
            for key in original.keys() | restored.keys()
            if original.get(key) != restored.get(key)
            or (key in original) != (key in restored)
        }
    group = items[1:5]
    assert all(
        "token_usage"
        not in item.extensions.get("boxteam.checkpoint.annotations", {}).get(
            "value", {}
        )
        for item in group[:-1]
    )
    assert len({item.message_group_id for item in group}) == 1
    assert len({json.dumps(item.producer_ref, sort_keys=True) for item in group}) == 1
    assert [item.projection_group.ordinal for item in group] == [
        0,
        1,
        2,
        3,
    ]
    assert group[1].payload == "核对参数"
    assert set(group[-1].payload) == {"tool_calls"}
    assert "核对参数" not in json.dumps(
        group[-1].projection_manifest(), ensure_ascii=False
    )
    assert (
        items[0].extensions["boxteam.checkpoint.annotations"]["value"]["source"]
        == "session_subagent_delegation"
    )
    assert (
        items[0].extensions["boxteam.checkpoint.annotations"]["value"][
            "parent_session_id"
        ]
        == "parent-1"
    )
    assert (
        "message_role"
        not in items[0].extensions["boxteam.checkpoint.annotations"]["value"]
    )
    assert group[-1].projection_identity.phase == "commentary"
    assert group[-1].extensions["boxteam.checkpoint.annotations"]["value"][
        "token_usage"
    ] == {"input_tokens": 12, "output_tokens": 5}
    with sqlite3.connect(
        f"file:{session_dir}/rollout/index.sqlite?mode=ro", uri=True
    ) as db:
        catalogs = db.execute(
            "SELECT item_sequence, jsonl_offset, jsonl_length, commit_id FROM item_catalog ORDER BY item_sequence"
        ).fetchall()
        assert len({row[3] for row in catalogs[1:]}) == 1
        assert catalogs[0][3] != catalogs[1][3]
        for sequence, offset, length, _ in catalogs:
            assert raw[offset : offset + length] == lines[sequence - 1]
        assert db.execute(
            "SELECT jsonl_record_count FROM storage_commits WHERE status = 'committed' ORDER BY commit_id"
        ).fetchall() == [(1,), (6,)]
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
                "SELECT COUNT(*) FROM context_view_items AS i "
                "JOIN branches AS b ON b.head_view_id = i.view_id "
                "JOIN checkpoint_namespace_state AS ns ON ns.active_branch_id = b.branch_id "
                "WHERE ns.checkpoint_ns = '' AND i.visible = 1"
            ).fetchone()[0]
            == 7
        )
    script = """
import json, sys
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from tests.support.session_catalog_owner import fixture_catalog_scope
with fixture_catalog_scope():
    saver = RolloutCheckpointSaver(sys.argv[1])
    thread_id = saver._storage.storage.main_thread_id(sys.argv[2])
    with session_thread_owner_scope(MutationIntentOwner(sys.argv[2], thread_id), langgraph_thread_id=thread_id):
        value = saver.get_tuple({'configurable': {'thread_id': thread_id, 'checkpoint_ns': ''}})
    print(json.dumps([{'id': message.id, 'content': message.content, 'metadata': message.response_metadata, 'calls': getattr(message, 'tool_calls', [])} for message in value.checkpoint['channel_values']['messages']], ensure_ascii=False))
"""
    result = subprocess.run(
        [
            "timeout",
            "30",
            "bash",
            "-c",
            'ulimit -d 4194304; exec "$@"',
            "bash",
            "uv",
            "run",
            "python",
            "-c",
            script,
            str(tmp_path),
            session_id,
        ],
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
    config = {
        "configurable": {
            "thread_id": saver._storage.storage.main_thread_id(session_id),
            "checkpoint_ns": "",
        }
    }
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
    assert (
        saver.ensure_request_items(
            session_id,
            saver._storage.storage.main_thread_id(session_id),
            execution_id="execution-group",
            turn_id=turn_id,
            messages=messages,
        )
        == ()
    )
    changed = messages[1].model_copy(
        update={"content": messages[1].content[:1]}
        if mutation == "missing_companions"
        else {
            "response_metadata": {**messages[1].response_metadata, "phase": "changed"}
        }
    )
    with pytest.raises(RuntimeError, match="canonical.*group"):
        saver.ensure_request_items(
            session_id,
            saver._storage.storage.main_thread_id(session_id),
            execution_id="execution-group",
            turn_id=turn_id,
            messages=[changed],
        )
    assert path.read_bytes() == before


def test_request_source_metadata_cannot_append_a_canonical_message(tmp_path, group_case):
    session_id, session_dir, _messages, _checkpoint = group_case
    saver, _config = _save(tmp_path, group_case)
    path = session_dir / "rollout" / "rollout.jsonl"
    before = path.read_bytes()
    forged_source = HumanMessage(
        id="unregistered-source",
        content="未经 typed source owner 登记的输入",
        response_metadata={
            "context_source_kind": "skill",
            "context_source_id": "skill:forged",
            "context_revision": "forged-revision",
        },
    )
    assert saver.ensure_request_items(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        execution_id="execution-group",
        turn_id="turn-tool-result",
        messages=[forged_source],
    ) == ()
    assert path.read_bytes() == before
    assert message_annotations(forged_source.response_metadata) == {}
    with sqlite3.connect(session_dir / "rollout" / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_catalog WHERE item_id=?",
            ("item-unregistered-source",),
        ).fetchone() == (0,)


def test_request_tool_groups_are_committed_once_and_reused_by_checkpoint(
    tmp_path, group_case
):
    session_id, session_dir, messages, checkpoint = group_case
    saver = RolloutCheckpointSaver(tmp_path)
    config = {
        "configurable": {
            "thread_id": saver._storage.storage.main_thread_id(session_id),
            "checkpoint_ns": "",
        }
    }
    metadata = {"source": "test", "step": 1, "writes": {}}
    saver.put(
        config,
        {**checkpoint, "id": "root", "channel_values": {"messages": messages[:1]}},
        metadata,
        {"messages": "1"},
    )
    path = session_dir / "rollout" / "rollout.jsonl"
    turn_id = json.loads(path.read_bytes().splitlines()[0])["turn_id"]
    committed = saver.ensure_request_items(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        execution_id="execution-group",
        turn_id=turn_id,
        messages=messages[1:3],
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
    assert (
        saver.ensure_request_items(
            session_id,
            saver._storage.storage.main_thread_id(session_id),
            execution_id="execution-group",
            turn_id=turn_id,
            messages=messages[1:3],
        )
        == ()
    )
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
    assert (
        restored.checkpoint["channel_values"]["messages"][1].content
        == messages[1].content
    )
    assert path.read_bytes() == after


def test_tool_result_shadow_preserves_distinct_carriers_with_different_lifecycle_ids(
    tmp_path, group_case
):
    session_id, session_dir, messages, _ = group_case
    saver = RolloutCheckpointSaver(tmp_path)
    # 结果正文去重不能只依赖裸 provider tool_call_id；测试消息显式携带
    # 所属 model call，覆盖同一 Turn 内 provider 重用短 ID 的边界。
    tool_result_message = messages[2].model_copy(
        update={"response_metadata": {"model_call_id": "model-call-1"}}
    )
    shadow_ids = saver.ensure_request_items(
        session_id,
        saver._storage.storage.main_thread_id(session_id),
        execution_id="execution-group",
        turn_id="turn-tool-result",
        messages=[tool_result_message],
    )
    assert shadow_ids == ("item-result-1",)
    result = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-tool-execution-result-call-1",
        semantic_kind="tool_result",
        payload_kind="tool_result",
        status="completed",
        producer_ref={
            "producer_kind": "tool",
            "producer_id": "tool-execution-1",
            "invocation_id": "model-call-1",
        },
        payload={
            "tool_call_id": "model-call-1:tool-call:call-1",
            "tool_invocation_id": "tool-invocation-1",
            "tool_attempt_id": "tool-execution-1",
            "result_id": "tool-execution-1",
            "name": "read",
            "content": "文件正文",
            "tool_outcome": "success",
            "execution_confirmed": True,
        },
        projection_identity=ItemProjectionIdentity(model_call_id="model-call-1"),
        turn_id="turn-tool-result",
        turn_scope="turn_member",
        message_group_id="message-tool-execution-1",
        wire_role="tool",
    )
    before = (session_dir / "rollout" / "rollout.jsonl").read_bytes()
    saver.append_items(
        session_id, (result,), thread_id=saver._storage.storage.main_thread_id(session_id)
    )
    path = session_dir / "rollout" / "rollout.jsonl"
    after = path.read_bytes()
    assert after.startswith(before)
    # request 与 live 载体保留各自身份；吞掉任意一方会丢失后续请求投影
    # 或工具执行证据。同一 facade 拒绝重复消费且不能产生第二份正文。
    added, = [json.loads(line) for line in after[len(before):].splitlines()]
    assert added["item_id"] == result.item_id
    with pytest.raises(MutationIntentDuplicateConsumption):
        saver.append_items(
            session_id, (result,), thread_id=saver._storage.storage.main_thread_id(session_id)
        )
    assert path.read_bytes() == after
    with RolloutCheckpointSaver(tmp_path) as cold:
        results = [item for item in cold._storage.storage.read_items(
            session_id, cold._storage.storage.main_thread_id(session_id),
        ) if item.semantic_kind == "tool_result"]
    assert [item.item_id for item in results] == [*shadow_ids, result.item_id]
    assert [item.projection_identity.projection_message_id for item in results] == ["result-1", None]
    assert all(item.projection_identity.model_call_id == "model-call-1" for item in results)
    assert results[-1].payload == result.payload


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
