"""同次 checkpoint/history 批读复用边界，逐 item 校验和下次损坏检测不省略。"""

import sqlite3
from collections import Counter

import pytest

from app.core.identifier import create_uuid_hex
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.catalog import item_reader
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader
from tests.integration.backend.sessions.test_checkpoint_message_groups import (
    _save,
)
from tests.integration.backend.sessions.test_checkpoint_message_groups import (
    group_case as group_case,  # noqa: PLC0414 - 复用真实消息组，在当前文件隔离工作区
)
from tests.integration.backend.sessions.test_session_history_loading import (
    _seed_rollout,
)


@pytest.fixture(params=["checkpoint", "indexed_history"])
def checkpoint_batch(tmp_path, group_case, request):
    session, _, messages, _ = group_case
    # 一条真实消息展开为许多 canonical item，必须覆盖同组内及跨组的提交复用。
    messages[1].tool_calls.extend(
        {"id": f"pending-call-{index}", "name": "read", "args": {"path": f"{index}.txt"}, "type": "tool_call"}
        for index in range(32)
    )
    saver, config = _save(tmp_path, group_case)
    thread = config["configurable"]["thread_id"]
    storage = saver._storage.storage
    reader = RolloutContextReader(saver._storage)

    def read():
        if request.param == "checkpoint":
            return saver.get_tuple(config).checkpoint
        with reader.open_snapshot(session, thread) as snapshot:
            return reader.read_projection_records_batch(
                snapshot, chain=reader.resolve_chain(snapshot), turn_ids=("turn-tool-result",),
            )

    path = storage.index_path(session, thread_id=thread)
    with sqlite3.connect(path) as connection:
        commits = {row[0] for row in connection.execute("SELECT DISTINCT commit_id FROM item_catalog")}
        count = connection.execute("SELECT count(*) FROM item_catalog").fetchone()[0]
        member = connection.execute(
            "SELECT item_id FROM item_catalog WHERE semantic_kind='tool_call' "
            "AND json_extract(projection_manifest_json,'$.projection_group.ordinal') "
            "< json_extract(projection_manifest_json,'$.projection_group.size')-1 "
            "ORDER BY item_sequence LIMIT 1"
        ).fetchone()[0]
    assert count > 32 and len(commits) == 2
    expected = read()
    jsonl = storage.jsonl_path(session, thread_id=thread)
    return read, expected, path, commits, member, jsonl


@pytest.mark.parametrize("damage", ["commit_metadata", "item_locator", "item_projection"])
def test_message_batches_reuse_only_commit_and_scope_checks(checkpoint_batch, monkeypatch, damage):
    read, expected, path, commits, member, jsonl = checkpoint_batch
    before = jsonl.read_bytes()
    checked_commits, checked_scopes = [], []
    validate_commit = item_reader.validate_stored_commit
    validate_scope = item_reader.require_historical_scope

    def commit(row, **kwargs):
        checked_commits.append(row[0])
        return validate_commit(row, **kwargs)

    def scope(connection, session, thread, **kwargs):
        checked_scopes.append((session, thread))
        return validate_scope(connection, session, thread, **kwargs)

    monkeypatch.setattr(item_reader, "validate_stored_commit", commit)
    monkeypatch.setattr(item_reader, "require_historical_scope", scope)
    for _ in range(2):
        checked_commits.clear()
        checked_scopes.clear()
        assert read() == expected
        assert Counter(checked_commits) == Counter({value: 1 for value in commits})
        assert len(checked_scopes) == 1

    with sqlite3.connect(path) as connection:
        if damage == "commit_metadata":
            # 损坏旧提交；当前末尾提交仍合法，下一次读取必须重新发现它。
            connection.execute("UPDATE storage_commits SET metadata_json=metadata_json||' ' WHERE commit_id=?", (min(commits),))
            error = "metadata_json 不是 RFC 8785 JCS object"
        elif damage == "item_locator":
            connection.execute("UPDATE item_catalog SET jsonl_length=jsonl_length+1 WHERE item_id=?", (member,))
            error = "JSONL|offset"
        else:
            connection.execute("UPDATE item_catalog SET tool_call_id='forged' WHERE item_id=?", (member,))
            error = "tool_call_id 不一致"
    with pytest.raises(RuntimeError, match=error):
        read()
    assert jsonl.read_bytes() == before


@pytest.fixture
def selected_history(tmp_path, session_bundle_factory, frozen_provider_prepare):
    session = "ses_" + create_uuid_hex()
    sessions = tmp_path / ".boxteam" / "sessions"
    session_bundle_factory(sessions, session)
    _seed_rollout(frozen_provider_prepare, tmp_path, session, count=3)
    with RolloutCheckpointSaver(sessions) as saver:
        reader = RolloutHistoryReader(RolloutContextReader(saver._storage))
        thread = saver._storage.storage.main_thread_id(session)
        yield reader, saver, session, thread


@pytest.fixture
def selection_queries(selected_history, monkeypatch):
    _reader, saver, _session, _thread = selected_history
    checkpoints = saver._storage.checkpoints
    query = checkpoints._records_for_turns
    calls = []

    def observe(*args, **kwargs):
        rows = query(*args, **kwargs)
        calls.append((set(kwargs.get("sequences") or ()), {row[0] for row in rows}))
        return rows

    monkeypatch.setattr(checkpoints, "_records_for_turns", observe)
    return calls


@pytest.mark.parametrize("tool_filter", [False, True])
def test_history_only_selects_missing_required_final_messages(selected_history, selection_queries, tool_filter):
    reader, saver, session, thread = selected_history
    calls = selection_queries
    request = (TurnHistoryLoadRequest(
        turn_ids=["job-0003"], tool_call_ids=["model-call-0003:tool-call:call-0003"],
        include=["user", "assistant", "tool_call", "tool_result"],
    ) if tool_filter else TurnHistoryLoadRequest(
        direction="tail", turns=3, include=["user", "assistant", "tool_summary"],
    ))
    storage = saver._storage.storage
    jsonl = storage.jsonl_path(session, thread_id=thread)
    before = jsonl.read_bytes()
    with sqlite3.connect(storage.index_path(session, thread_id=thread)) as connection:
        final_sequence = connection.execute(
            "SELECT final_message_sequence FROM turns WHERE turn_id='job-0003'",
        ).fetchone()[0]
    for _ in range(2):
        calls.clear()
        page = reader.load(session, thread, request)
        ordinals = [3] if tool_filter else [1, 2, 3]
        assert [item.ordinal for item in page.items] == ordinals
        assert [item.final_response for item in page.items] == [f"模型最终响应 {value}" for value in ordinals]
        assert len(calls) == (2 if tool_filter else 1)
        if tool_filter:
            assert calls[1] == ({final_sequence}, {final_sequence})
            assert calls[0][1].isdisjoint(calls[1][1])
            assert [part.kind for part in page.items[0].response_parts
                    if part.kind in {"tool_call", "tool_result", "final_text"}] == ["tool_call", "tool_result", "final_text"]
    assert jsonl.read_bytes() == before


def test_required_selection_can_partially_overlap_base_messages(selected_history, selection_queries):
    reader, _saver, session, thread = selected_history
    context_reader = reader._context_reader
    with context_reader.open_snapshot(session, thread) as snapshot:
        first_sequence, final_sequence = snapshot.connection.execute(
            "SELECT user_message_sequence,final_message_sequence FROM turns WHERE turn_id='job-0003'",
        ).fetchone()
        records = context_reader.read_projection_records_batch(
            snapshot, chain=context_reader.resolve_chain(snapshot), turn_ids=("job-0003",),
            message_roles=("user",),
            required_sequences={"job-0003": (first_sequence, final_sequence, first_sequence)},
        )["job-0003"]
    assert [record["sequence"] for record in records] == [first_sequence, final_sequence]
    assert selection_queries == [(set(), {first_sequence}), ({final_sequence}, {final_sequence})]


def test_required_sequence_validation_precedes_selected_message_subtraction(selected_history):
    reader, _saver, session, thread = selected_history
    context_reader = reader._context_reader
    with context_reader.open_snapshot(session, thread) as snapshot:
        assert snapshot.connection.execute("SELECT MIN(message_sequence) FROM messages").fetchone() == (1,)
        # True 与已选序号 1 在 Python 集合中相等；不能先做差集而跳过严格整数校验。
        with pytest.raises(RuntimeError, match="required_sequence 必须是非负整数"):
            context_reader.read_projection_records_batch(
                snapshot, chain=context_reader.resolve_chain(snapshot), turn_ids=("job-0001",),
                required_sequences={"job-0001": (True,)},
            )


def test_history_rechecks_selected_final_after_success(selected_history):
    reader, saver, session, thread = selected_history
    request = TurnHistoryLoadRequest(direction="tail", turns=3, include=["user", "assistant"])
    assert reader.load(session, thread, request).items[-1].final_response == "模型最终响应 3"
    storage = saver._storage.storage
    jsonl = storage.jsonl_path(session, thread_id=thread)
    original_bytes = jsonl.read_bytes()
    path = storage.index_path(session, thread_id=thread)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE item_catalog SET tool_call_id='forged-final' "
            "WHERE item_id=(SELECT final_item_id FROM turn_records WHERE turn_id='job-0003')",
        )
        connection.commit()
        before = tuple(connection.iterdump())
    with pytest.raises(RuntimeError, match="tool_call_id 不一致"):
        reader.load(session, thread, request)
    with sqlite3.connect(path) as connection:
        assert tuple(connection.iterdump()) == before
    assert jsonl.read_bytes() == original_bytes
