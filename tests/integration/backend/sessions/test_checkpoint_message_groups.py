"""真实 Saver/JSONL/SQLite 的有序消息 group 与重启恢复合同。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.execution_checkpoint import finalize_assistant_checkpoint
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProvenanceRelation,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    checkpoint_messages,
)
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
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
)
from app.services.infrastructure.rollout_history.errors import InvalidTurnCursorError
from app.services.infrastructure.rollout_history.reader import RolloutHistoryReader
from app.services.mapping.itemized.canonical_langchain import project_canonical_items
from app.services.mapping.itemized.prefix_frames import project_canonical_prefix_frames
from app.services.orchestration.execution_step.model_call import StepModelCallAdapter
from tests.support.rollout.view_mutations import mutate_view_members


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
    assert [part.kind for part in detail.response_items] == ["tool_call", "tool_result"]
    expected_id = f"{model_call}:tool-call:call-1" if carrier == "canonical_scoped" else "call-1"
    assert {part.tool_call_id for part in detail.response_items} == {expected_id}
    assert json.loads(detail.response_items[0].arguments)["path"] == (
        "a.txt" if suffix == "1" else "b.txt"
    )
    assert detail.response_items[1].result == (
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
    assert {part.tool_call_id for part in complete.response_items} == {first_id, second_id}
    merged = subprocess.run(
        ["bun", "-e", """
import assert from 'node:assert/strict';
import {createSessionTurnTimeline, upsertTurn} from './src/clients/web/src/state/session/turnTimeline.ts';
import {responseItemsToTimelineItems} from './src/clients/web/src/state/timeline/responseItems.ts';
const {summary, complete} = JSON.parse(await Bun.stdin.text());
let timeline = upsertTurn(createSessionTurnTimeline('history-contract'), summary);
timeline = upsertTurn(timeline, complete);
timeline = upsertTurn(timeline, complete);
const items = timeline.turnsById[complete.turn_id].items;
const keys = items.map(item => `${item.part_id ?? item.event_id}:${item.type}`);
console.log(JSON.stringify({before: complete.items.length, after: items.length, keys}));
assert.equal(items.length, 4);
assert.equal(new Set(keys).size, 4);
const tools = responseItemsToTimelineItems(timeline.turnsById[complete.turn_id].response_items)
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
    thread_id = saver._storage.storage.main_thread_id(session_id)
    call_ids = saver.ensure_request_items(
        session_id, thread_id, execution_id="execution-group",
        turn_id="turn-tool-result", messages=[messages[1]],
    )
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
    history = cold.load_history(session_id, thread_id, TurnHistoryLoadRequest(
        turn_ids=["turn-tool-result"], include=["metadata", "tool_summary"],
    ))
    calls = [part for part in history.items[0].response_items if part.kind == "tool_call"]
    assert [part.source.item_id for part in calls] == [call_ids[-1]]
    assert calls[0].tool_outcome == "success"


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


@pytest.mark.parametrize("with_tools", [False, True])
def test_semantic_content_group_roundtrips_with_and_without_tools(tmp_path, group_case, with_tools):
    _, session_dir, messages, _ = group_case
    content = [
        {"type": "thinking", "thinking": "核对", "signature": "protected-signature"},
        {"type": "text", "text": "说明"},
        {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}},
        {"type": "input_audio", "input_audio": {"data": "YXVkaW8=", "format": "wav"}},
        {"type": "video_url", "video_url": {"url": "https://example.invalid/video.mp4"}},
        {"type": "provider_widget", "data": {"rows": [1, {"name": "保留原结构"}]}},
        {"type": "text", "text": "结论"},
    ]
    calls = [{"id": "call-1", "name": "read", "args": {"paths": ["a", "b"]}},
             {"id": "call-2", "name": "read", "args": {"path": "c"}}] if with_tools else []
    invalid = [{"id": "bad-call", "name": "read", "args": "{", "error": "invalid JSON", "type": "invalid_tool_call"}]
    message_index = 1 if with_tools else 3
    original = AIMessage(id=messages[message_index].id, content=content, tool_calls=calls,
                         invalid_tool_calls=invalid, response_metadata=messages[message_index].response_metadata)
    messages[message_index] = original
    _, config = _save(tmp_path, group_case)
    records = [CanonicalItemRecord.from_dict(json.loads(line))
               for line in (session_dir / "rollout" / "rollout.jsonl").read_text().splitlines()]
    group = [item for item in records if item.producer_ref["producer_id"] == original.id]
    assert [item.semantic_kind for item in group] == ["reasoning"] + ["assistant_output"] * 6 + (["tool_call", "tool_call"] if with_tools else [])
    assert [item.projection_group.ordinal for item in group] == list(range(len(group)))
    assert all(item.projection_group.size == len(group) for item in group)
    assert all("invalid_tool_calls" not in item.payload for item in group[:-1])
    assert group[-1].payload["invalid_tool_calls"] == invalid
    assert all("bad-call" not in json.dumps(item.projection_manifest()) for item in group)
    assert [len(item.payload["tool_calls"]) for item in group if item.semantic_kind == "tool_call"] == ([1, 1] if with_tools else [])
    restored = RolloutCheckpointSaver(tmp_path).get_tuple(config).checkpoint["channel_values"]["messages"][message_index]
    assert restored.content == content
    assert restored.tool_calls == original.tool_calls
    assert restored.invalid_tool_calls == original.invalid_tool_calls


def test_tool_summary_keeps_group_source_without_loading_siblings_or_body(tmp_path, group_case, monkeypatch):
    session_id, _, messages, _ = group_case
    messages[1] = AIMessage(id=messages[1].id, content=[
        {"type": "thinking", "thinking": "核对来源"}, {"type": "text", "text": "读取文件"},
    ], tool_calls=[
        {"id": "call-1", "name": "read", "args": {"paths": ["A"]}},
        {"id": "call-2", "name": "read", "args": {"paths": ["B"]}},
    ], response_metadata=messages[1].response_metadata)
    saver, config = _save(tmp_path, group_case)
    thread = config["configurable"]["thread_id"]
    # cold reader从原索引读取来源；只选B，不能偷读A或完整参数。
    cold = RolloutCheckpointSaver(tmp_path)
    group = [item for item in saver._storage.storage.read_items(session_id, thread)
             if item.producer_ref["producer_id"] == messages[1].id]
    assert len(group) == 4
    page = cold.load_history(session_id, thread, TurnHistoryLoadRequest(
        turn_ids=["turn-tool-result"], include=["tool_summary"],
    ))

    def reject_body_read(*args, **kwargs):
        pytest.fail("单调用摘要不得读取canonical正文或其它组成员")

    monkeypatch.setattr(cold._context_reader, "read_canonical_items", reject_body_read)
    selected = cold.load_history(session_id, thread, TurnHistoryLoadRequest(
        turn_ids=["turn-tool-result"], item_ids=[group[-1].item_id], include=["tool_summary"],
        source_view_id=page.source_view_id, source_view_revision=page.source_view_revision,
    ))
    parts = selected.items[0].response_items
    assert len(parts) == 1
    assert parts[0].source.item_id == group[-1].item_id
    assert parts[0].source.model_call_id == "model-call-1"
    assert parts[0].source.message_group.model_dump() == {
        "message_group_id": group[-1].message_group_id, "ordinal": 3, "size": 4,
    }
    assert parts[0].arguments is None and parts[0].result is None
    assert parts[0].projection == "summary"


def test_tool_result_matches_real_id_when_distinct_messages_share_call_index(tmp_path, group_case):
    """两个原消息都只有第0项，raw回执必须关联真实scoped调用，而非最后的index=0。"""
    session_id, _, messages, checkpoint = group_case
    model_call_id = "model-call-1"
    messages[1:3] = [
        AIMessage(id=f"declaration-{call_id}", content="", tool_calls=[{
            "id": f"{model_call_id}:tool-call:{call_id}", "name": name, "args": {"path": path},
        }], response_metadata={"model_call_id": model_call_id})
        for call_id, name, path in (("a", "read_file", "A"), ("b", "list_directory", "B"))
    ] + [ToolMessage(id="result-a", content="A的结果", tool_call_id="a", name="read_file",
                     response_metadata={"model_call_id": model_call_id})]
    checkpoint["channel_values"]["messages"] = messages
    _saver, config = _save(tmp_path, group_case)
    thread_id = config["configurable"]["thread_id"]
    cold = RolloutCheckpointSaver(tmp_path)
    page = cold.load_history(session_id, thread_id, TurnHistoryLoadRequest(
        turn_ids=["turn-tool-result"], include=["tool_call", "tool_result"],
    ))
    parts = page.items[0].response_items
    calls = {part.tool_call_id: part for part in parts if part.kind == "tool_call"}
    result, = [part for part in parts if part.kind == "tool_result"]
    a, b = (calls[f"{model_call_id}:tool-call:{call_id}"] for call_id in ("a", "b"))
    assert a.source.call_index == b.source.call_index == 0
    assert a.source.item_id != b.source.item_id
    assert result.tool_call_id == "a" and result.tool_name == "read_file"
    assert result.source.assistant_message_sequence == a.source.assistant_message_sequence
    assert result.source.assistant_message_sequence != b.source.assistant_message_sequence
    assert result.result == "A的结果"
    assert '"A"' in a.arguments and '"B"' in b.arguments
    assert result.source.item_id != a.source.item_id
    assert result.source.message_group is None


@pytest.mark.parametrize("excluded", ["group_tail", "declarations", "result"])
def test_tool_coordinates_follow_selected_view_without_reading_sibling_bodies(tmp_path, group_case, excluded, monkeypatch):
    """组尾隐藏不破坏成员配对；只保留调用或回执时不泄漏被排除对象。"""
    session_id, _, messages, _ = group_case
    messages[1] = AIMessage(id="two-declarations", content="", tool_calls=[
        {"id": "call-1", "name": "read", "args": {"path": "A"}},
        {"id": "call-2", "name": "write", "args": {"path": "B"}},
    ], response_metadata={"model_call_id": "model-call-1"})
    messages[2] = messages[2].model_copy(update={"status": "error"})
    saver, config = _save(tmp_path, group_case)
    thread_id = config["configurable"]["thread_id"]
    storage = saver._storage.storage
    canonical = storage.read_items(session_id, thread_id)
    calls = [item for item in canonical if item.semantic_kind == "tool_call"]
    result, = [item for item in canonical if item.semantic_kind == "tool_result"]
    assert result.status == "completed" and result.payload["tool_outcome"] == "failure"
    assert result.tool_execution is None
    hidden = [calls[-1]] if excluded == "group_tail" else calls if excluded == "declarations" else [result]
    with storage._connect(thread_id, session_id=session_id) as connection:
        view_id = storage._active_context_view_id(connection, "")
        for item in hidden:
            mutate_view_members(connection,
                "UPDATE context_view_items SET visible=0 WHERE view_id=? AND item_id=?",
                (view_id, item.item_id))
        connection.commit()
    cold = RolloutCheckpointSaver(tmp_path)
    selector = result.item_id if excluded == "declarations" else calls[0].item_id
    page = cold.load_history(session_id, thread_id, TurnHistoryLoadRequest(
        turn_ids=["turn-tool-result"], include=["metadata"],
    ))

    read_bodies = cold._context_reader.read_canonical_items

    def reject_body_read(*args, **kwargs):
        pytest.fail("工具摘要不应补读参数、结果或原消息组正文")
    monkeypatch.setattr(cold._context_reader, "read_canonical_items", reject_body_read)

    selected = cold.load_history(session_id, thread_id, TurnHistoryLoadRequest(
        turn_ids=["turn-tool-result"], item_ids=[selector],
        include=["tool_summary"],
        source_view_id=page.source_view_id, source_view_revision=page.source_view_revision,
    ))
    projected, = selected.items[0].response_items
    assert projected.source.item_id == selector and projected.tool_call_id == "call-1"
    assert projected.tool_execution is None
    assert projected.tool_outcome == (None if excluded == "result" else "failure")
    assert projected.model_dump(mode="json")["tool_outcome"] == projected.tool_outcome
    assert projected.arguments is None and projected.result is None
    if excluded == "declarations":
        assert projected.source.assistant_message_sequence is None
        monkeypatch.setattr(cold._context_reader, "read_canonical_items", read_bodies)
        detail = cold.load_history(session_id, thread_id, TurnHistoryLoadRequest(
            turn_ids=["turn-tool-result"], item_ids=[selector], include=["tool_result"],
            source_view_id=page.source_view_id, source_view_revision=page.source_view_revision,
        )).items[0].response_items[0]
        assert detail.result == "文件正文" and detail.tool_outcome == "failure"
    else:
        assert projected.source.assistant_message_sequence is not None
        assert projected.tool_name == "read"
        assert (projected.source.result_message_sequence is None) == (excluded == "result")
        assert projected.arguments is None and projected.result is None


@pytest.mark.parametrize("content", ["", [], ["单项文本"],
    [{"type": "thinking", "thinking": "仅思考"}],
    [{"type": "thinking", "thinking": "仅思考", "signature": "signed"}]])
@pytest.mark.parametrize("with_tools", [False, True])
def test_single_semantic_member_preserves_message_shape(tmp_path, group_case, content, with_tools):
    _, _, messages, _ = group_case
    original = messages[1].model_copy(update={"content": content,
        "tool_calls": messages[1].tool_calls if with_tools else [],
        "invalid_tool_calls": [{"id": "invalid", "name": "read", "args": "{",
                                "error": "invalid JSON", "type": "invalid_tool_call"}]})
    messages[1] = original
    _, config = _save(tmp_path, group_case)
    with RolloutCheckpointSaver(tmp_path) as cold:
        restored = cold.get_tuple(config).checkpoint["channel_values"]["messages"][1]
    assert restored.content == original.content
    assert restored.tool_calls == original.tool_calls
    assert restored.invalid_tool_calls == original.invalid_tool_calls


@pytest.mark.parametrize("with_tools", [False, True])
def test_same_model_call_does_not_merge_distinct_response_messages(tmp_path, group_case, with_tools):
    session_id, _, messages, checkpoint = group_case
    metadata = {"model_call_id": "same-model-call"}
    declarations = [
        [{"id": call_id, "name": "read", "args": {"path": call_id}} for call_id in ("A", "B")],
        [{"id": "C", "name": "read", "args": {"path": "C"}}],
    ] if with_tools else [[], []]
    checkpoint["channel_values"]["messages"] = [messages[0],
        AIMessage(id="response-one", content=[{"type": "text", "text": "第一条"}],
                  tool_calls=declarations[0], response_metadata=metadata),
        AIMessage(id="response-two", content=[{"type": "text", "text": "第二条"}],
                  tool_calls=declarations[1], response_metadata=metadata)]
    _save(tmp_path, group_case)
    with RolloutCheckpointSaver(tmp_path) as cold:
        items = cold._storage.storage.read_items(session_id, cold._storage.storage.main_thread_id(session_id))
        restored = project_canonical_items(items, preserve_order=True)
        frames, selected_ids = project_canonical_prefix_frames(items, provider_profile="test-profile", start_ordinal=0)
        page = cold.load_history(session_id, cold._storage.storage.main_thread_id(session_id),
            TurnHistoryLoadRequest(turn_ids=["turn-tool-result"], include=["tool_summary"]))
    assert [message.id for message in restored] == [messages[0].id, "response-one", "response-two"]
    assert [message.content for message in restored[1:]] == [[{"type": "text", "text": "第一条"}],
                                                            [{"type": "text", "text": "第二条"}]]
    assert [message.tool_calls for message in restored[1:]] == [
        AIMessage(content="", tool_calls=calls).tool_calls for calls in declarations
    ]
    assert [frame.content["message"]["data"]["id"] for frame in frames] == [message.id for message in restored]
    expected_members = [[item.item_id for item in items if item.producer_ref["producer_id"] == message.id]
                        for message in restored]
    assert [frame.content["item_ids"] for frame in frames] == expected_members
    assert [frame.content["message"]["data"].get("tool_calls", []) for frame in frames[1:]] == [
        message.tool_calls for message in restored[1:]
    ]
    assert selected_ids == tuple(item.item_id for item in items)
    calls = [part for part in page.items[0].response_items if part.kind == "tool_call"]
    if with_tools:
        assert [part.source.item_id for part in calls] == [item.item_id for item in items if item.semantic_kind == "tool_call"]
        first, second, third = [part.source for part in calls]
        assert first.model_call_id == second.model_call_id == third.model_call_id == metadata["model_call_id"]
        assert first.message_group.message_group_id == second.message_group.message_group_id
        assert first.message_group.message_group_id != third.message_group.message_group_id
        assert [(source.message_group.ordinal, source.message_group.size) for source in (first, second, third)] == [(1, 3), (2, 3), (1, 2)]
        assert all(part.arguments is None and part.result is None and part.tool_execution is None for part in calls)
    else:
        assert calls == []


def test_runtime_commit_indexes_reasoning_only_message(tmp_path, group_case, monkeypatch):
    session_id, _, _, _ = group_case
    saver, _ = _save(tmp_path, group_case)
    thread_id = saver._storage.storage.main_thread_id(session_id)
    original = AIMessage(id="reasoning-only", content=[
        {"type": "thinking", "thinking": " \t只思考\n", "signature": "private-signature"},
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "复杂思考"}],
         "summary": [{"type": "summary_text", "text": "同源摘要"}], "encrypted_content": "private-cipher"},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "独立摘要"}]},
    ],
                         response_metadata={"model_call_id": "reasoning-only-call"})
    group = LangChainMessageCodec().items_for_message(original, item_sequence=1, message_id=original.id,
        turn_id="turn-tool-result", execution_id="execution-group", timestamp=datetime.now(UTC).isoformat())
    saver.append_items(session_id, group, thread_id=thread_id)
    with saver._storage.storage._connect(thread_id, "", session_id=session_id, read_only=True) as connection:
        row = connection.execute("SELECT message.role, projection.has_reasoning, projection.has_encrypted_reasoning FROM messages message "
            "JOIN message_projections projection USING(message_sequence) WHERE message.message_id=?",
            (original.id,)).fetchone()
        indexed = connection.execute(
            "SELECT item_id,reasoning_text,summary_text,signature_present,encrypted_length FROM reasoning_blocks "
            "WHERE item_id IN (?,?,?) ORDER BY item_id", tuple(item.item_id for item in group),
        ).fetchall()
    assert row == ("assistant", 1, 1)
    assert {entry[0] for entry in indexed} == {item.item_id for item in group}
    assert {entry[1:] for entry in indexed} == {
        (" \t只思考\n", None, 1, None), ("复杂思考", "同源摘要", 0, len("private-cipher")),
        (None, "独立摘要", 0, None),
    }
    with RolloutCheckpointSaver(tmp_path) as cold:
        original_read = cold._storage.checkpoints._read_record_envelopes

        def reject_body_read(session_id, thread_id, checkpoint_ns, rows, *, connection=None):
            rows = list(rows)
            assert not rows, "思考安全投影不得读取JSONL正文"
            return original_read(session_id, thread_id, checkpoint_ns, rows, connection=connection)

        monkeypatch.setattr(cold._storage.checkpoints, "_read_record_envelopes", reject_body_read)
        page = cold.load_history(session_id, thread_id, TurnHistoryLoadRequest(
            turn_ids=["turn-tool-result"], include=["thinking"],
        ))
        parts = [part for part in page.items[0].response_items if part.source.item_id in {item.item_id for item in group}]
        assert [(part.source.item_id, part.text) for part in parts] == [
            (group[0].item_id, " \t只思考\n"), (group[1].item_id, "复杂思考"), (group[2].item_id, "独立摘要"),
        ]
        assert "private-signature" not in page.model_dump_json()
        assert "private-cipher" not in page.model_dump_json()
        summaries = cold.load_history(session_id, thread_id, TurnHistoryLoadRequest(
            turn_ids=["turn-tool-result"], include=["reasoning_summary"],
        ))
        summary_parts = [part for part in summaries.items[0].response_items if part.source.item_id in {item.item_id for item in group}]
        assert [(part.source.item_id, part.kind, part.text) for part in summary_parts] == [
            (group[1].item_id, "reasoning_summary", "同源摘要"),
            (group[2].item_id, "reasoning_summary", "独立摘要"),
        ]
        assert summary_parts[0].id == parts[1].id


@pytest.fixture
def complete_final_group(tmp_path, group_case, frozen_provider_activation, frozen_provider_wire):
    async def complete():
        session_id, _, messages, _ = group_case
        message = messages[-1]
        model_call_id = "02058462-55ec-4bb3-a26d-6daaeec14ee6"
        message.response_metadata["model_call_id"] = model_call_id
        saver, config = _save(tmp_path, group_case)
        thread = saver._storage.storage.main_thread_id(session_id)
        jsonl = saver._storage.storage.jsonl_path(session_id, "", thread_id=thread)
        original_body = jsonl.read_bytes()
        adapter = StepModelCallAdapter(checkpointer=saver, session_id=session_id,
                                      thread_id=thread, job_id="job-group", checkpoint_ns="")
        key = "mixed-final:create"
        activation = frozen_provider_activation(saver, plan_creation_idempotency_key=key,
                                                model_call_id=model_call_id)
        manager = ContextSourceManager(owner=ContextSourceOwnerKey(session_id, thread), control_state_port=saver)
        saver.prepare_context_for_provider(
            session_id, thread, turn_id="turn-tool-result", execution_id="execution-group",
            plan_creation_idempotency_key=key, seal_idempotency_key="mixed-final:seal",
            context_source_manager=manager,
            tool_set_supplier=lambda: ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
            **frozen_provider_wire("test-provider", "chat_completions"), **activation,
        )
        await adapter.register(model_call_id, 1, "test-provider")
        finalize_assistant_checkpoint(checkpointer=saver, thread_id=thread,
            message_id=message.id, on_finalize=adapter.converge_final_message)
        assert jsonl.read_bytes() == original_body
        await adapter.converge_final_checkpoint(message.id)
        return saver, config
    return complete


@pytest.mark.asyncio
@pytest.mark.parametrize("media_only", [False, True])
async def test_final_group_reuses_original_items_and_survives_cold_read(
    tmp_path, group_case, media_only, complete_final_group, monkeypatch,
):
    session_id, _, messages, _ = group_case
    model_call_id = "02058462-55ec-4bb3-a26d-6daaeec14ee6"
    messages[-1].response_metadata["model_call_id"] = model_call_id
    content = [{"type": "image_url", "image_url": {"url": "https://example.invalid/result.png"}}]
    if not media_only:
        content = [{"type": "text", "text": "第一段"}, *content,
                   {"type": "text", "text": "第二段"},
                   {"type": "thinking", "thinking": "末尾思考", "signature": "signed"}]
    messages[-1].id = "mixed-final"
    messages[-1].content = content
    saver, config = await complete_final_group()
    thread = saver._storage.storage.main_thread_id(session_id)
    with RolloutCheckpointSaver(tmp_path) as cold:
        checkpoint = cold.get_tuple(config)
        assert checkpoint is not None
        final_message = checkpoint_messages(cold, checkpoint)[-1]
        assert final_message.id == "mixed-final"
        assert final_message.content == content
        items = cold.read_canonical_items(session_id, thread)
        group = [item for item in items if item.message_group_id == "message-mixed-final"]
        assert len(group) == len(content)
        output = [item for item in group if item.semantic_kind == "assistant_output"]
        with cold._storage.storage.open_read_snapshot(session_id, thread) as snapshot:
            terminal = snapshot.connection.execute(
                "SELECT status,final_item_id FROM turn_records WHERE turn_id=?", ("turn-tool-result",),
            ).fetchone()
        assert terminal == ("completed", output[-1].item_id)
        reader = RolloutHistoryReader(RolloutContextReader(cold._storage))

        original_read = cold._storage.checkpoints._read_record_envelopes

        def reject_body_read(session_id, thread_id, checkpoint_ns, rows, *, connection=None):
            rows = list(rows)
            assert not rows, "final摘要不得读取JSONL正文"
            return original_read(session_id, thread_id, checkpoint_ns, rows, connection=connection)

        monkeypatch.setattr(cold._storage.checkpoints, "_read_record_envelopes", reject_body_read)
        page = reader.load(session_id, thread, TurnHistoryLoadRequest(
            turn_ids=["turn-tool-result"], include=["final_response"],
        ))
        assert page.items[0].final_response == ("" if media_only else "第一段第二段")
        final_members = [part for part in page.items[0].response_items if part.final]
        assert [part.id for part in final_members] == [item.item_id for item in output]
        media_item = next(item for item in output if item.output_block_type == "image_url")
        media_summary = next(part for part in final_members if part.id == media_item.item_id)
        assert (media_summary.kind, media_summary.carrier_type, media_summary.projection) == ("content", "image_url", "summary")
        assert media_summary.content is None
        if not media_only:
            final_text_items = [part for part in final_members if part.kind == "final_text"]
            assert [(part.source.item_id, part.text) for part in final_text_items] == [
                (output[0].item_id, "第一段"), (output[2].item_id, "第二段"),
            ]
            assert [part.source.message_group.ordinal for part in final_text_items] == [0, 2]
            assert all(part.source.message_group.size == len(content) for part in final_text_items)
            selected = reader.load(session_id, thread, TurnHistoryLoadRequest(
                turn_ids=["turn-tool-result"], item_ids=[output[2].item_id],
                include=["final_response"], source_view_id=page.source_view_id,
                source_view_revision=page.source_view_revision,
            ))
            assert selected.items[0].final_response == "第二段"
            assert [(part.source.item_id, part.text) for part in selected.items[0].response_items] == [
                (output[2].item_id, "第二段"),
            ]
        # 媒体正文只能由同一view的显式Item定点读取取得，摘要不得暗中补正文。
        monkeypatch.setattr(cold._storage.checkpoints, "_read_record_envelopes", original_read)
        media_page = reader.load(session_id, thread, TurnHistoryLoadRequest(
            turn_ids=["turn-tool-result"], item_ids=[media_item.item_id], include=["final_response"],
            source_view_id=page.source_view_id, source_view_revision=page.source_view_revision,
        ))
        media_detail, = media_page.items[0].response_items
        assert media_detail.id == media_summary.id
        assert media_detail.final and media_detail.projection == "detail"
        assert media_detail.content == {"type": "image_url", "image_url": {"url": "https://example.invalid/result.png"}}
        assert media_detail.source.message_group == media_summary.source.message_group
        monkeypatch.setattr(cold._storage.checkpoints, "_read_record_envelopes", reject_body_read)
        outline = reader.load(session_id, thread, TurnHistoryLoadRequest(
            turn_ids=["turn-tool-result"], include=["activity_outline"],
            source_view_id=page.source_view_id, source_view_revision=page.source_view_revision,
        ))
        final_output_ids = {item.item_id for item in output}
        assert not any(
            part.source.item_id in final_output_ids and not part.final
            for part in outline.items[0].response_items
        )
        if not media_only:
            with cold._storage.storage._connect(thread, "", session_id=session_id) as connection:
                mutate_view_members(connection,
                    "UPDATE context_view_items SET visible=0 WHERE view_id=? AND item_id=?",
                    (page.source_view_id, output[0].item_id))
            visible = reader.load(session_id, thread, TurnHistoryLoadRequest(
                turn_ids=["turn-tool-result"], include=["final_response"],
            ))
            assert visible.items[0].final_response == "第二段"
            assert [(part.source.item_id, part.text) for part in visible.items[0].response_items if part.kind == "final_text"] == [
                (output[2].item_id, "第二段"),
            ]
            assert any(part.id == media_item.item_id and part.final for part in visible.items[0].response_items)
            assert all(part.source.item_id != output[0].item_id for part in visible.items[0].response_items)


def test_activity_continuation_rejects_new_view_after_result_append(tmp_path, group_case):
    session_id, _, messages, _ = group_case
    messages[1].content = ""
    result = messages[2]
    messages[:] = [messages[0], messages[1], AIMessage(
        id="progress-before-result",
        content=[{"type": "text", "text": f"进度{index}"} for index in range(300)],
        response_metadata={"model_call_id": "progress-model"},
    )]
    saver, config = _save(tmp_path, group_case)
    thread = config["configurable"]["thread_id"]
    initial = saver.load_history(session_id, thread, TurnHistoryLoadRequest())
    binding = {"source_view_id": initial.source_view_id, "source_view_revision": initial.source_view_revision}
    first = saver.load_history(session_id, thread, TurnHistoryLoadRequest(
        turn_ids=["turn-tool-result"], include=["activity_outline"], **binding,
    ))
    assert len(first.items[0].response_items) == 256 and first.before_cursor
    assert first.items[0].activity_stats.item_count == 301
    call, = [item for item in saver.read_canonical_items(session_id, thread) if item.semantic_kind == "tool_call"]
    saver.append_items(session_id, LangChainMessageCodec().items_for_message(
        result, item_sequence=1, message_id=result.id,
        turn_id="turn-tool-result", execution_id="execution-group",
        timestamp=datetime.now(UTC).isoformat(),
    ), thread_id=thread)

    with RolloutCheckpointSaver(tmp_path) as cold:
        with pytest.raises(InvalidTurnCursorError, match="详情来源不属于当前 context view"):
            cold.load_history(session_id, thread, TurnHistoryLoadRequest(
                direction="before", cursor=first.before_cursor,
                turn_ids=["turn-tool-result"], include=["activity_outline"], **binding,
            ))
        current = cold.load_history(session_id, thread, TurnHistoryLoadRequest())
        assert current.source_view_id != first.source_view_id
        # 新请求可见新回执；旧游标沿原不可变view边界明确失效，不静默换成新视图。
        fresh = cold.load_history(session_id, thread, TurnHistoryLoadRequest(
            turn_ids=["turn-tool-result"], item_ids=[call.item_id], include=["tool_summary"],
            source_view_id=current.source_view_id, source_view_revision=current.source_view_revision,
        ))
        assert fresh.items[0].activity_stats.item_count == 302
        assert fresh.items[0].response_items[0].tool_outcome == "success"
        assert fresh.items[0].response_items[0].source.result_message_sequence is not None


@pytest.mark.asyncio
async def test_large_final_group_uses_the_same_bounded_item_continuation(
    tmp_path, group_case, complete_final_group, monkeypatch,
):
    session_id, _, messages, _ = group_case
    messages[-1].id = "large-final"
    messages[-1].content = [
        {"type": "thinking", "thinking": "先检查，再回答"},
        *[{"type": "text", "text": f"{index:04d} " + "界" * 400} for index in range(600)],
    ]
    saver, _ = await complete_final_group()
    thread = saver._storage.storage.main_thread_id(session_id)
    expected = {
        item.item_id for item in saver.read_canonical_items(session_id, thread)
        if item.message_group_id == "message-large-final" and item.semantic_kind == "assistant_output"
    }
    assert len(expected) == 600
    with RolloutCheckpointSaver(tmp_path) as cold:
        original_read = cold._storage.checkpoints._read_record_envelopes

        def no_body(session_id, thread_id, checkpoint_ns, rows, *, connection=None):
            rows = list(rows)
            assert not rows, "final与活动续读只消费SQLite投影，不物化整条原消息"
            return original_read(session_id, thread_id, checkpoint_ns, rows, connection=connection)

        monkeypatch.setattr(cold._storage.checkpoints, "_read_record_envelopes", no_body)
        reader = RolloutHistoryReader(RolloutContextReader(cold._storage))
        initial = reader.load(session_id, thread, TurnHistoryLoadRequest(
            turn_ids=["turn-tool-result"], include=["metadata", "final_response"],
        ))
        detail, = initial.items
        assert len(detail.response_items) == 256
        assert detail.detail_truncated
        assert len(detail.final_response) <= 65536
        # 最终输出不增加过程计数；只计原5项和最终消息内的1项思考。
        assert detail.activity_stats.item_count == 6
        assert all(part.final for part in detail.response_items)
        assert any(part.truncated for part in detail.response_items)

        seen: set[str] = set()
        cursor = None
        pages = []
        while True:
            page = reader.load(session_id, thread, TurnHistoryLoadRequest(
                direction="before" if cursor else "tail", cursor=cursor,
                turn_ids=["turn-tool-result"], include=["activity_outline", "assistant_text"],
                source_view_id=initial.source_view_id, source_view_revision=initial.source_view_revision,
            ))
            members = page.items[0].response_items
            assert len(members) <= 256
            assert not seen.intersection(part.id for part in members)
            seen.update(part.id for part in members)
            pages.append(page)
            final = [part for part in members if part.final]
            assert all(part.id in expected for part in final)
            assert all(part.source.message_group.size == 601 for part in final)
            assert sum(len(part.text) for part in final) <= 65536
            assert page.items[0].activity_stats.item_count == 6
            assert page.has_more == bool(page.before_cursor)
            if not page.before_cursor:
                break
            cursor = page.before_cursor
            assert len(pages) < 4, "有界续页必须推进真实Item成员"
        assert len(pages) == 3
        assert {part.id for page in pages for part in page.items[0].response_items if part.final} == expected
        assert len(seen) == 606
        # 首段不在尾页，定点读取仍可直接到达，不追读上述续页。
        target = next(part for part in pages[-1].items[0].response_items if part.final)
        point = reader.load(session_id, thread, TurnHistoryLoadRequest(
            turn_ids=["turn-tool-result"], item_ids=[target.id], include=["final_response"],
            source_view_id=initial.source_view_id, source_view_revision=initial.source_view_revision,
        ))
        actual, = point.items[0].response_items
        assert actual.id == target.id and actual.final
        assert actual.text == target.text
