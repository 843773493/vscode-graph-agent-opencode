"""冻结「会话不存在」在 API 适配层统一映射为 404。

业务服务用 ``NotFoundError``（Session 详情）或目录解析器的 ``KeyError``
（``会话目录节点不存在``）表达会话缺失。``NotFoundError`` 继承 ``HTTPException``
但基类默认 ``status_code=500``；适配层漏接就会把「找不到」报成服务端故障。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import messages as messages_api
from app.api import sessions as sessions_api
from app.core.exceptions import NotFoundError

MISSING = "ses_0d3e5c937a5f4c12becd97dd1390a51e"
CATALOG_KEY_ERROR = KeyError(f"会话目录节点不存在: {MISSING}")


def _raise(error: Exception):
    async def _call(*args, **kwargs):
        raise error

    return _call


class _FakeJobService:
    """pending-request 入口在会话缺失时由存储层抛 KeyError。"""

    async def list_pending(self, session_id: str):
        raise CATALOG_KEY_ERROR

    async def clear_pending(self, session_id: str):
        raise CATALOG_KEY_ERROR


class _FakeMessageService:
    async def list(self, *, session_id: str, limit: int, cursor: str | None):
        raise CATALOG_KEY_ERROR

    async def get(self, *, session_id: str, message_id: str):
        raise CATALOG_KEY_ERROR

    async def get_agent_state_messages(self, *, session_id: str):
        raise CATALOG_KEY_ERROR


class _FakeGoalService:
    async def get(self, session_id: str):
        raise NotFoundError(f"Session {session_id} not found")


class _FakeInformationService:
    async def get_information(self, session_id: str):
        raise NotFoundError(f"Session {session_id} not found")


class _FakeChangesService:
    async def list_changesets(self, session_id: str):
        raise NotFoundError(f"Session {session_id} not found")


class _FakeTraceSessionService:
    async def list_trace_events(self, session_id: str, *, cursor, limit):
        raise NotFoundError(f"Session {session_id} not found")


class _FakeLlmLogService:
    def list_session_logs(self, session_id: str):
        raise CATALOG_KEY_ERROR


class _FakeCompactionService:
    async def compact(self, *, session_id: str):
        raise NotFoundError(f"Session {session_id} not found")


@pytest.mark.asyncio
async def test_list_pending_requests_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await messages_api.list_pending_requests(
            MISSING,
            _="local",
            request_id="req",
            job_service=_FakeJobService(),
        )

    assert captured.value.status_code == 404


@pytest.mark.asyncio
async def test_clear_pending_requests_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await messages_api.clear_pending_requests(
            MISSING,
            _="local",
            request_id="req",
            job_service=_FakeJobService(),
        )

    assert captured.value.status_code == 404


@pytest.mark.asyncio
async def test_list_messages_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await messages_api.list_messages(
            MISSING,
            limit=50,
            cursor=None,
            _="local",
            request_id="req",
            message_service=_FakeMessageService(),
        )

    assert captured.value.status_code == 404


@pytest.mark.asyncio
async def test_get_message_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await messages_api.get_message(
            MISSING,
            "msg_x",
            _="local",
            request_id="req",
            message_service=_FakeMessageService(),
        )

    assert captured.value.status_code == 404


@pytest.mark.asyncio
async def test_agent_state_messages_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await messages_api.get_agent_state_messages(
            MISSING,
            _="local",
            request_id="req",
            message_service=_FakeMessageService(),
        )

    assert captured.value.status_code == 404


@pytest.mark.asyncio
async def test_get_session_goal_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await sessions_api.get_session_goal(
            MISSING,
            _="local",
            request_id="req",
            goal_service=_FakeGoalService(),
        )

    assert captured.value.status_code == 404


@pytest.mark.asyncio
async def test_get_session_information_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await sessions_api.get_session_information(
            MISSING,
            _="local",
            request_id="req",
            information_service=_FakeInformationService(),
        )

    assert captured.value.status_code == 404


@pytest.mark.asyncio
async def test_list_session_changesets_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await sessions_api.list_session_changesets(
            MISSING,
            _="local",
            request_id="req",
            session_changes_service=_FakeChangesService(),
        )

    assert captured.value.status_code == 404


@pytest.mark.asyncio
async def test_list_session_traces_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await sessions_api.list_session_traces(
            MISSING,
            cursor=None,
            limit=100,
            _="local",
            request_id="req",
            session_service=_FakeTraceSessionService(),
        )

    assert captured.value.status_code == 404


@pytest.mark.asyncio
async def test_llm_request_logs_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await sessions_api.list_session_llm_request_logs(
            MISSING,
            _="local",
            request_id="req",
            llm_request_log_service=_FakeLlmLogService(),
        )

    assert captured.value.status_code == 404


@pytest.mark.asyncio
async def test_compact_session_context_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await sessions_api.compact_session_context(
            MISSING,
            _="local",
            request_id="req",
            context_compaction_service=_FakeCompactionService(),
        )

    assert captured.value.status_code == 404
