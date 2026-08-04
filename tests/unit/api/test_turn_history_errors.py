from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.abstractions.turn_history import (
    TurnProjectionMutation,
    TurnProjectionOperation,
)
from app.api.session_turns import get_session_turn_bootstrap, list_session_turns
from app.schemas.public_v2.common import JobStatus
from app.schemas.public_v2.turn import TurnDetailDTO
from app.services.infrastructure.turn_history import TurnHistoryStore


class _CorruptedTurnService:
    def __init__(self, store: TurnHistoryStore) -> None:
        self._store = store

    async def bootstrap(self, session_id: str):
        self._store.list_summaries(session_id, limit=1)
        raise AssertionError("损坏分页必须先抛错")

    async def list_turns(
        self,
        session_id: str,
        *,
        limit: int,
        cursor: str | None,
    ):
        self._store.list_summaries(session_id, limit=limit, cursor=cursor)
        raise AssertionError("损坏分页必须先抛错")


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["bootstrap", "list"])
async def test_missing_timeline_turn_file_is_projection_corruption(
    route: str,
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    store = TurnHistoryStore(sessions_dir)
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    turn = TurnDetailDTO(
        turn_id="job_1",
        job_id="job_1",
        session_id="session_1",
        ordinal=1,
        revision=1,
        status=JobStatus.completed,
        source_message_ids=["message_1"],
        final_response="ok",
        created_at=created_at,
        updated_at=created_at,
        completed_at=created_at,
    )
    store.apply_operation(
        "session_1",
        TurnProjectionOperation(
            event_id="event_1",
            mutations=[
                TurnProjectionMutation(
                    turn_id=turn.turn_id,
                    base_revision=0,
                    create=turn,
                )
            ],
        ),
    )
    store._files.turn_record_path("session_1", "job_1").unlink()
    service = _CorruptedTurnService(store)

    with pytest.raises(HTTPException) as raised:
        if route == "bootstrap":
            await get_session_turn_bootstrap(
                "session_1",
                BackgroundTasks(),
                _="",
                request_id="request_1",
                turn_history_service=service,  # type: ignore[arg-type]
            )
        else:
            await list_session_turns(
                "session_1",
                BackgroundTasks(),
                limit=20,
                cursor=None,
                _="",
                request_id="request_1",
                turn_history_service=service,  # type: ignore[arg-type]
            )

    assert raised.value.status_code == 500
    assert raised.value.detail["code"] == "turn_projection_corrupted"
    assert raised.value.detail["session_id"] == "session_1"
