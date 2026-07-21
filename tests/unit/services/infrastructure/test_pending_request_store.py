from datetime import datetime, timezone

import pytest

from app.schemas.public_v2.pending_request import PendingRequestDTO
from app.services.infrastructure.pending_request_store import PendingRequestStore


@pytest.mark.asyncio
async def test_pending_request_store_round_trip(tmp_path):
    store = PendingRequestStore(sessions_dir=tmp_path / "sessions")
    request = PendingRequestDTO(
        job_id="job_restore",
        message_id="msg_restore",
        session_id="ses_restore",
        content="重启后继续保留",
        kind="steering",
        position=0,
        agent_id="default",
        message_created_at="2026-07-17T00:00:00+00:00",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    await store.save("ses_restore", [request])
    restored = await store.load("ses_restore")

    assert restored == [request]
    assert (
        tmp_path / "sessions" / "ses_restore" / "pending_requests.json"
    ).is_file()
