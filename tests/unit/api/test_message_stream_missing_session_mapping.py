"""冻结消息流/轨迹流/Turn 重放/变更审查入口的「会话不存在」404 映射。

这些入口都会先经 catalog 路径解析器定位会话物理目录：会话缺失时解析器抛
``KeyError``，SessionService.get 抛 ``NotFoundError``（基类默认 status_code=500）。
适配层漏接就会把客户端输入错误落成无上下文 500，且 KeyError 的 str() 还会补引号。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import message_stream as message_stream_api
from app.api import messages as messages_api
from app.api import sessions as sessions_api
from app.core.exceptions import NotFoundError
from app.schemas.internal_v2.message import MessageReplayRequest
from app.schemas.internal_v2.session_changes import SessionFileReviewRequest

MISSING = "ses_019c1ef767ab76f28d14b4197977b6c3"
CATALOG_KEY_ERROR = KeyError(f"会话目录节点不存在: {MISSING}")
NOT_FOUND = NotFoundError(f"Session {MISSING} not found")


class _MissingSessionStore:
    """MessageStreamStore 只实现本测试触及的入口，均按会话缺失失败。"""

    async def existing_stream_ids(self, *, session_id: str, turn_ids: list[str]):
        raise CATALOG_KEY_ERROR

    async def open_existing(
        self, *, session_id: str, turn_id: str, turn_stream_id=None
    ):
        raise CATALOG_KEY_ERROR


class _MissingSessionService:
    async def ensure_trace_cursor(self, session_id: str, after_event_id) -> None:
        raise NOT_FOUND

    def stream_trace_events(self, session_id: str, after_event_id=None):
        raise AssertionError("会话缺失时不得进入事件流生成")


class _StubConfigService:
    def get_trace_stream_heartbeat_interval_seconds(self) -> float:
        raise AssertionError("会话缺失时不得读取心跳配置")


class _MissingChangesService:
    async def set_file_reviewed(
        self,
        *,
        session_id: str,
        file_path: str,
        reviewed: bool,
    ):
        raise NOT_FOUND


class _MissingReplayService:
    async def replay(self, session_id: str, target_message_id: str, request):
        raise CATALOG_KEY_ERROR

    async def replay_turn(self, session_id: str, turn_id: str, request):
        raise CATALOG_KEY_ERROR


@pytest.mark.asyncio
async def test_message_stream_availability_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await message_stream_api.get_message_stream_availability(
            MISSING,
            turn_ids=["turn_x"],
            _="local",
            request_id="req",
            store=_MissingSessionStore(),
        )

    assert captured.value.status_code == 404
    assert captured.value.detail == f"会话目录节点不存在: {MISSING}"


@pytest.mark.asyncio
async def test_stream_message_events_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await message_stream_api.stream_message_events(
            MISSING,
            "turn_x",
            _request_stub(),
            turn_stream_id=None,
            after_seq=None,
            last_event_id=None,
            _="local",
            request_id="req",
            store=_MissingSessionStore(),
        )

    assert captured.value.status_code == 404
    assert captured.value.detail == f"会话目录节点不存在: {MISSING}"


@pytest.mark.asyncio
async def test_message_stream_snapshot_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await message_stream_api.get_message_stream_snapshot(
            MISSING,
            "turn_x",
            turn_stream_id=None,
            _="local",
            request_id="req",
            store=_MissingSessionStore(),
        )

    assert captured.value.status_code == 404
    assert captured.value.detail == f"会话目录节点不存在: {MISSING}"


@pytest.mark.asyncio
async def test_list_message_stream_events_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await message_stream_api.list_message_stream_events(
            MISSING,
            "turn_x",
            turn_stream_id=None,
            after_seq=0,
            limit=100,
            _="local",
            request_id="req",
            store=_MissingSessionStore(),
        )

    assert captured.value.status_code == 404
    assert captured.value.detail == f"会话目录节点不存在: {MISSING}"


@pytest.mark.asyncio
async def test_trace_stream_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await sessions_api.stream_session_traces(
            MISSING,
            after_event_id=None,
            last_event_id=None,
            _="local",
            session_service=_MissingSessionService(),
            config_service=_StubConfigService(),
        )

    assert captured.value.status_code == 404
    assert captured.value.detail == f"Session {MISSING} not found"


@pytest.mark.asyncio
async def test_review_changeset_file_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await sessions_api.review_session_changeset_file(
            MISSING,
            "cs_x",
            SessionFileReviewRequest(file_path="/a.txt", reviewed=True),
            _="local",
            request_id="req",
            session_changes_service=_MissingChangesService(),
        )

    assert captured.value.status_code == 404
    assert captured.value.detail == f"Session {MISSING} not found"


@pytest.mark.asyncio
async def test_replay_message_turn_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await messages_api.replay_message_turn(
            MISSING,
            "msg_x",
            _replay_request(),
            _="local",
            request_id="req",
            replay_service=_MissingReplayService(),
        )

    assert captured.value.status_code == 404
    assert captured.value.detail == f"会话目录节点不存在: {MISSING}"


@pytest.mark.asyncio
async def test_replay_turn_maps_missing_session_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await messages_api.replay_turn(
            MISSING,
            "turn_x",
            _replay_request(),
            _="local",
            request_id="req",
            replay_service=_MissingReplayService(),
        )

    assert captured.value.status_code == 404
    assert captured.value.detail == f"会话目录节点不存在: {MISSING}"


def _replay_request() -> MessageReplayRequest:
    return MessageReplayRequest(
        action="retry_failed",
        acknowledge_context_only=True,
    )


def _request_stub():
    """stream_message_events 只在响应体内使用 request.is_disconnected。"""

    class _Request:
        async def is_disconnected(self) -> bool:
            return False

    return _Request()
