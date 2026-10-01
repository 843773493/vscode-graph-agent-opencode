from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.user_access import (
    UserAccessService,
    UserLeaseOccupiedError,
)


def test_user_lease_blocks_second_client_and_takeover_invalidates_old_session(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    service = UserAccessService(state=state)
    try:
        user = service.create_user(display_name="开发者", user_id="dev")
        first = service.acquire_user(user_id=user.user_id, client_label="电脑 A")

        with pytest.raises(UserLeaseOccupiedError) as error_info:
            service.acquire_user(user_id=user.user_id, client_label="电脑 B")
        assert error_info.value.summary.client_label == "电脑 A"

        second = service.acquire_user(
            user_id=user.user_id,
            client_label="电脑 B",
            takeover=True,
        )
        assert second.access_session_id != first.access_session_id
        assert first.invalidated.is_set()
        assert service.resolve_cookie(first.access_session_id) is None
        assert service.resolve_cookie(second.access_session_id) is not None
    finally:
        state.close()


def test_guest_does_not_create_user_or_view_state(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    service = UserAccessService(state=state)
    try:
        guest = service.acquire_guest(tracking={"test": True})
        assert guest.kind == "guest"
        assert service.list_users() == ()
        assert service.resolve_cookie(guest.access_session_id) is not None
        connection = state.connection()
        try:
            assert connection.execute("SELECT COUNT(*) FROM user_account").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM user_view_state").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM guest_tracking").fetchone()[0] == 1
        finally:
            connection.close()
        restarted = UserAccessService(state=state)
        assert restarted.resolve_cookie(guest.access_session_id) is not None
    finally:
        state.close()


def test_guest_tracking_cleanup_removes_only_expired_guest_records(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    service = UserAccessService(state=state)
    try:
        guest = service.acquire_guest(tracking={"source": "playwright"})
        connection = state.connection()
        try:
            connection.execute(
                "UPDATE guest_tracking SET expires_at = ?",
                ((datetime.now(UTC) - timedelta(days=1)).isoformat(),),
            )
        finally:
            connection.close()
        assert service.cleanup_expired() == (0, 1)
        assert service.resolve_cookie(guest.access_session_id) is None
        assert guest.invalidated.is_set()
    finally:
        state.close()


def test_expired_user_lease_can_be_reacquired_and_invalidates_old_session(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    service = UserAccessService(state=state)
    try:
        user = service.create_user(display_name="过期用户", user_id="expired")
        first = service.acquire_user(user_id=user.user_id, client_label="电脑 A")
        connection = state.connection()
        try:
            connection.execute(
                "UPDATE user_access_lease SET expires_at = ? WHERE user_id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), user.user_id),
            )
        finally:
            connection.close()

        second = service.acquire_user(user_id=user.user_id, client_label="电脑 B")
        assert second.lease_generation == first.lease_generation + 1
        assert first.invalidated.is_set()
        assert service.resolve_cookie(first.access_session_id) is None
        assert service.resolve_cookie(second.access_session_id) is not None
    finally:
        state.close()


@pytest.mark.asyncio
async def test_concurrent_user_acquisition_has_one_winner(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    service = UserAccessService(state=state)
    try:
        user = service.create_user(display_name="并发用户", user_id="concurrent")

        async def acquire(label: str):
            try:
                return await asyncio.to_thread(
                    service.acquire_user,
                    user_id=user.user_id,
                    client_label=label,
                )
            except UserLeaseOccupiedError:
                return None

        first, second = await asyncio.gather(acquire("A"), acquire("B"))
        assert (first is None) != (second is None)
    finally:
        state.close()


def test_repeated_takeover_does_not_grow_invalidations_unboundedly(tmp_path) -> None:
    """反复 takeover 不得让失效事件表无界增长。

    被顶替的 access_session_id 在数据库里的租约行会被删除，cleanup_expired 永远
    看不到它；若不在 takeover 时就地回收，每次登录都会永久留下一个 asyncio.Event。
    这里用「新增键数远小于 takeover 次数」作为有界判据（分片/回收两类修法都能满足，
    仅保留旧行为会红）。
    """

    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    service = UserAccessService(state=state)
    try:
        service.create_user(display_name="轮换用户", user_id="rotate")
        service.acquire_user(user_id="rotate", client_label="第 1 次")
        baseline = len(service._invalidations)
        for index in range(2, 202):
            service.acquire_user(
                user_id="rotate",
                client_label=f"第 {index} 次",
                takeover=True,
            )

        # 200 次 takeover 若每次都留下失效事件，键数会增长约 200；有界修法下
        # 每次顶替旧条目都会被弹出，键数相对基线只增不减 1（当前活跃会话）。
        assert len(service._invalidations) - baseline <= 1
    finally:
        state.close()
