from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.schemas.public_v2.team import TeamBoardDTO, TeamEventDTO
from app.services.infrastructure.team.store import TeamStore


def _board() -> TeamBoardDTO:
    now = datetime.now(timezone.utc)
    return TeamBoardDTO(
        team_id="team_0123456789abcdef0123456789abcdef",
        name="测试团队",
        coordinator_session_id="ses_parent",
        version=1,
        created_at=now,
        updated_at=now,
    )


def test_team_store_round_trip_and_recent_events(tmp_path):
    store = TeamStore(workspace_root=tmp_path)
    board = _board()
    store.create(board)
    event = TeamEventDTO(
        event_id="tevt_0123456789abcdef0123456789abcdef",
        team_id=board.team_id,
        type="team.created",
        actor_session_id="ses_parent",
        created_at=board.created_at,
        payload={"name": board.name},
    )
    store.append_event(event)

    assert store.get(board.team_id) == board
    assert store.list() == [board]
    assert store.recent_events(board.team_id) == [event]


def test_team_store_rejects_path_like_team_id(tmp_path):
    store = TeamStore(workspace_root=tmp_path)

    with pytest.raises(ValueError, match="非法 team_id"):
        store.get("../../outside")


def test_team_store_exposes_corrupt_board(tmp_path):
    store = TeamStore(workspace_root=tmp_path)
    board = _board()
    store.create(board)
    board_path = tmp_path / ".boxteam" / "teams" / board.team_id / "team.json"
    board_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(Exception):
        store.get(board.team_id)
