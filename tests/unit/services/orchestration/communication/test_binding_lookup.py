"""SessionControlStoreWaitBindingLookup 测试。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.session_control_store import SessionControlStore
from app.services.orchestration.communication.binding_lookup import (
    SessionControlStoreWaitBindingLookup,
)


class _FakePathResolver:
    def __init__(self, root: Path) -> None:
        self._root = root

    def resolve_session_node(self, session_id: str) -> Path:
        return self._root / session_id


@pytest.mark.asyncio
async def test_resolves_bound_communication_from_target_store(tmp_path: Path) -> None:
    session_id = f"ses_{uuid.uuid4().hex}"
    main_thread_id = f"thr_{uuid.uuid4().hex}"
    communication_id = f"comm_{uuid.uuid4().hex}"
    job_id = f"job_{uuid.uuid4().hex}"
    turn_id = f"turn_{uuid.uuid4().hex}"
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    store = SessionControlStore(session_dir / "session-control.sqlite")
    try:
        store.initialize_main_thread(main_thread_id, datetime.now(UTC))
        store.initialize_fence("active", 1)
        store.create_or_get_communication_inbox(
            session_id=session_id,
            communication_id=communication_id,
            source_gateway_id="gw_a",
            source_workspace_id="ws_a",
            source_session_id=f"ses_{uuid.uuid4().hex}",
            source_thread_id=f"thr_{uuid.uuid4().hex}",
            target_thread_id=main_thread_id,
            kind="question",
            reply_to_communication_id=None,
            payload_hash="a" * 64,
        )
        store.claim_communication_inbox_admission(
            communication_id,
            claim_owner="worker-a",
            claim_generation=1,
        )
        store.mark_communication_inbox_execution_bound(
            communication_id,
            job_id=job_id,
            turn_id=turn_id,
            claim_owner="worker-a",
            claim_generation=1,
        )
    finally:
        store.close()

    lookup = SessionControlStoreWaitBindingLookup(
        path_resolver=_FakePathResolver(tmp_path)
    )
    binding = await lookup.resolve(
        target_session_id=session_id,
        communication_id=communication_id,
    )
    assert binding is not None
    assert binding.target_session_id == session_id
    assert binding.target_main_thread_id == main_thread_id
    assert binding.job_id == job_id
    assert binding.turn_id == turn_id


@pytest.mark.asyncio
async def test_missing_communication_returns_none(tmp_path: Path) -> None:
    lookup = SessionControlStoreWaitBindingLookup(
        path_resolver=_FakePathResolver(tmp_path)
    )
    assert await lookup.resolve(
        target_session_id=f"ses_{uuid.uuid4().hex}",
        communication_id=f"comm_{uuid.uuid4().hex}",
    ) is None
