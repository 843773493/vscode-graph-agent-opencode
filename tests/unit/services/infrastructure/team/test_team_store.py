from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.schemas.internal_v2.team import TeamBoardDTO, TeamEventDTO
from app.services.infrastructure.team.store import TeamStore


def _board() -> TeamBoardDTO:
    now = datetime.now(UTC)
    return TeamBoardDTO(
        team_id="team_0123456789abcdef0123456789abcdef",
        name="测试团队",
        session_id="ses_parent", coordinator_thread_id="thr_main",
        version=1,
        created_at=now,
        updated_at=now,
    )


def test_team_store_round_trip_and_recent_events(tmp_path):
    store = TeamStore(path_resolver=SimpleNamespace(resolve_session_node=lambda session_id: tmp_path / session_id))
    board = _board()
    store.create(board)
    event = TeamEventDTO(
        event_id="tevt_0123456789abcdef0123456789abcdef",
        team_id=board.team_id,
        type="team.created",
        session_id="ses_parent", actor_thread_id="thr_main",
        created_at=board.created_at,
        payload={"name": board.name},
    )
    store.append_event(event)

    assert store.get(board.session_id, board.team_id) == board
    assert store.list(board.session_id) == [board]
    assert store.recent_events(board.session_id, board.team_id) == [event]


def test_team_store_rejects_path_like_team_id(tmp_path):
    store = TeamStore(path_resolver=SimpleNamespace(resolve_session_node=lambda session_id: tmp_path / session_id))

    with pytest.raises(ValueError, match="非法 team_id"):
        store.get("ses_parent", "../../outside")


def test_team_store_exposes_corrupt_board(tmp_path):
    store = TeamStore(path_resolver=SimpleNamespace(resolve_session_node=lambda session_id: tmp_path / session_id))
    board = _board()
    store.create(board)
    board_path = tmp_path / board.session_id / "teams" / board.team_id / "team.json"
    board_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        store.get(board.session_id, board.team_id)
