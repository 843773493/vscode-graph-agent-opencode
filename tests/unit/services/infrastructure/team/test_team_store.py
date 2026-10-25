from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.session_control_store import SessionControlStore
from app.schemas.internal_v2.team import TeamBoardDTO, TeamEventDTO
from app.services.infrastructure.team.store import TeamStore
from tests.support.canonical_id_at import session_id_at, thread_id_at
from tests.support.graph_binding import TEST_THREAD_GRAPH_BINDING

CREATED = datetime(2026, 6, 1, 12, tzinfo=UTC)
SESSION_ID, THREAD_ID = session_id_at(CREATED), thread_id_at(CREATED)


@pytest.fixture
def team_store_case(tmp_path):
    control = SessionControlStore(tmp_path / "session-control.sqlite")
    control.initialize_main_thread(THREAD_ID, CREATED, TEST_THREAD_GRAPH_BINDING)
    control.initialize_fence()
    admissions = []

    @asynccontextmanager
    async def write_admission(session_id):
        assert session_id == SESSION_ID
        admissions.append(session_id)
        yield

    @contextmanager
    def owner(session_id):
        assert session_id == SESSION_ID
        yield SimpleNamespace(control_store=control)

    board = TeamBoardDTO(team_id="team_0123456789abcdef0123456789abcdef", name="测试团队",
                         session_id=SESSION_ID, coordinator_thread_id=THREAD_ID, version=1,
                         created_at=CREATED, updated_at=CREATED)
    event = TeamEventDTO(event_id="tevt_0123456789abcdef0123456789abcdef", team_id=board.team_id,
                         session_id=SESSION_ID, actor_thread_id=THREAD_ID, type="team.created",
                         created_at=CREATED, payload={"name": board.name})
    store = TeamStore(factory=SimpleNamespace(for_owner_session=owner), write_admission=write_admission)
    yield SimpleNamespace(store=store, control=control, board=board, event=event, admissions=admissions)
    control.close()


@pytest.mark.asyncio
async def test_team_store_round_trip_and_recent_events(team_store_case):
    case = team_store_case
    revision = await case.store.commit(case.board, case.event)
    assert revision == 1
    assert await case.store.commit(case.board, case.event) == revision
    assert case.store.get(SESSION_ID, case.board.team_id) == case.board
    assert case.store.list(SESSION_ID) == [case.board]
    assert case.store.recent_events(SESSION_ID, case.board.team_id) == [case.event]
    record = case.control.get_collaboration_fanout(revision)
    assert [item.thread_id for item in record.recipients] == [THREAD_ID]
    assert case.control.get_lease(record.lease_id).operation_kind == "collaboration_fanout"
    assert case.admissions == [SESSION_ID, SESSION_ID]
    assert not (case.control.database_path.parent / "teams").exists()


@pytest.mark.asyncio
async def test_latest_task_event_is_exact_and_survives_long_history_and_reopen(team_store_case):
    case = team_store_case
    await case.store.commit(case.board, case.event)
    original = None
    for index in range(28):
        board = case.store.get(SESSION_ID, case.board.team_id)
        event = case.event.model_copy(update={"event_id": "tevt_" + f"{index + 1:032x}",
            "type": "task.dispatch_failed" if index == 0 else "task.updated",
            "payload": {"task_id": "original" if index == 0 else f"other-{index}"}})
        await case.store.commit(board.model_copy(update={"version": board.version + 1}), event)
        if index == 0:
            original = event
    assert original not in case.store.recent_events(SESSION_ID, case.board.team_id)
    assert case.store.latest_task_event(SESSION_ID, case.board.team_id, "original") == original
    assert case.store.latest_task_event(SESSION_ID, case.board.team_id, "missing") is None
    database = case.control.database_path
    case.control.close()
    case.control = SessionControlStore(database)
    assert json.loads(case.control.collaboration_latest_task_event(case.board.team_id, "original")) == original.model_dump(mode="json")
    case.control.close()


def test_team_store_rejects_path_like_team_id(team_store_case):
    with pytest.raises(ValueError, match="非法 team_id"):
        team_store_case.store.get(SESSION_ID, "../../outside")


@pytest.mark.asyncio
async def test_team_store_exposes_corrupt_board(team_store_case):
    case = team_store_case
    await case.store.commit(case.board, case.event)
    case.control.connection.execute("UPDATE collaboration_boards SET board_json='{not-json'")
    with pytest.raises(json.JSONDecodeError):
        case.store.get(SESSION_ID, case.board.team_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("session_id", "ses_019c3df7f8527d568e834acf3c0f6612"),
    ("team_id", "team_ffffffffffffffffffffffffffffffff"),
])
async def test_recent_events_reject_other_owner_without_rewriting_ledger(team_store_case, field, value):
    case = team_store_case
    revision = await case.store.commit(case.board, case.event)
    damaged = case.event.model_dump(mode="json") | {field: value}
    encoded = json.dumps(damaged)
    case.control.connection.execute(
        "UPDATE collaboration_events SET event_json=? WHERE event_id=?", (encoded, case.event.event_id),
    )
    fanout = case.control.get_collaboration_fanout(revision)
    leases = case.control.list_non_terminal_leases()
    with pytest.raises(RuntimeError, match="团队事件.*owner/ID"):
        case.store.recent_events(SESSION_ID, case.board.team_id)
    assert case.control.collaboration_recent_events(case.board.team_id, 20) == (encoded,)
    assert case.control.get_collaboration_ledger_revision() == revision
    assert case.control.get_collaboration_fanout(revision) == fanout
    assert case.control.list_non_terminal_leases() == leases
    assert case.store.get(SESSION_ID, case.board.team_id) == case.board


@pytest.mark.asyncio
async def test_team_board_event_fanout_and_lease_rollback_together(team_store_case):
    case = team_store_case
    case.control.connection.execute("CREATE TRIGGER reject_event BEFORE INSERT ON collaboration_events "
                                    "BEGIN SELECT RAISE(ABORT,'event write failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="event write failed"):
        await case.store.commit(case.board, case.event)
    assert case.control.collaboration_boards() == ()
    assert case.control.collaboration_recent_events(case.board.team_id, 20) == ()
    assert case.control.list_collaboration_fanouts() == ()
    assert case.control.list_non_terminal_leases() == ()
    assert case.control.get_collaboration_ledger_revision() == 0


@pytest.mark.asyncio
async def test_team_store_rejects_board_version_and_event_preimage_drift(team_store_case):
    case = team_store_case
    await case.store.commit(case.board, case.event)
    with pytest.raises(RuntimeError, match="幂等 preimage"):
        await case.store.commit(case.board.model_copy(update={"name": "漂移"}), case.event)
    with pytest.raises(RuntimeError, match="version CAS"):
        await case.store.commit(case.board, case.event.model_copy(update={"event_id": "tevt_" + "f" * 32}))
    assert len(case.control.list_collaboration_fanouts()) == 1


def test_team_store_rejects_old_json_without_reading_it(team_store_case):
    case = team_store_case
    legacy = case.control.database_path.parent / "teams"
    legacy.mkdir()
    (legacy / "team.json").write_text("旧开发数据", encoding="utf-8")
    with pytest.raises(RuntimeError, match="旧团队 JSON 存储不受支持"):
        case.store.list(SESSION_ID)
