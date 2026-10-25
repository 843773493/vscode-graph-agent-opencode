"""Child creation owner 对 coordinator 身份的校验。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.session_service import SessionService
from tests.unit.services.business.session_children.helpers import (
    _control_for,
    _create_child_thread,
)


async def test_child_creation_rejects_foreign_coordinator_identity(
    session_service: SessionService,
) -> None:
    parent = await session_service.create(SessionCreateRequest(title="父会话"))
    control = _control_for(session_service, parent.session_id)
    try:
        parent_thread_id = await session_service.resolve_main_thread(parent.session_id)
        own_thread = _create_child_thread(
            control,
            owner_session_id=parent.session_id,
            coordinator_thread_id=parent_thread_id,
            key="own-coordinator",
            created_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            delegation_id="del_" + "b" * 32,
            title="属主父会话的委派",
        )
    finally:
        control.close()

    with pytest.raises(RuntimeError, match="owner/task reference"):
        invalid_control = _control_for(session_service, parent.session_id)
        try:
            _create_child_thread(
                invalid_control,
                owner_session_id=parent.session_id,
                coordinator_thread_id=parent_thread_id,
                key="foreign-coordinator",
                created_at=datetime(2026, 6, 1, 12, 1, tzinfo=UTC),
                delegation_id="del_" + "d" * 32,
                title="漂移 coordinator",
                coordinator_session_id=create_prefixed_id("ses"),
            )
        finally:
            invalid_control.close()

    verification = _control_for(session_service, parent.session_id)
    try:
        assert [
            member.delegation_id for member in verification.list_collaboration_members()
        ] == ["del_" + "b" * 32]
        assert [row.thread_id for row in verification.list_child_thread_rows()] == [
            own_thread
        ]
        with pytest.raises(KeyError):
            verification.get_thread_creation_record("foreign-coordinator")
        with pytest.raises(KeyError):
            verification.get_collaboration_member("del_" + "d" * 32)
        with pytest.raises(KeyError):
            verification.get_initial_execution_intent("foreign-coordinator")
    finally:
        verification.close()

    async with session_service.session_read_admission(parent.session_id):
        result = await session_service.list_child_threads(parent.session_id)

    # owner 不接受外部 coordinator 身份，失败前不产生 child/member。
    assert result.total == 1
    own = [item for item in result.items if item.delegation_id == "del_" + "b" * 32]
    assert own[0].thread_id == own_thread
    assert own[0].collaboration_state == "published"
