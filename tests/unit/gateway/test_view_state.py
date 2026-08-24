from __future__ import annotations

import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.user_access import UserAccessService
from app.gateway.control.view_state import UserViewStateStore


def test_view_state_is_scoped_by_user_workspace_and_session(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    access = UserAccessService(state=state)
    views = UserViewStateStore(state=state)
    try:
        first_user = access.create_user(display_name="用户 A", user_id="user-a")
        second_user = access.create_user(display_name="用户 B", user_id="user-b")
        first = access.acquire_user(user_id=first_user.user_id, client_label="A")
        second = access.acquire_user(user_id=second_user.user_id, client_label="B")

        saved = views.put(
            context=first,
            workspace_id="workspace-a",
            session_id="session-a",
            turn_anchor="turn-8",
            scroll_offset=128.5,
            follow_latest=False,
            projection_version=2,
            tool_details_expanded=True,
        )
        assert saved.turn_anchor == "turn-8"
        assert views.get(
            context=first,
            workspace_id="workspace-a",
            session_id="session-a",
        ) == saved
        assert views.get(
            context=second,
            workspace_id="workspace-a",
            session_id="session-a",
        ) is None
    finally:
        state.close()


def test_guest_view_state_is_not_persisted(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    access = UserAccessService(state=state)
    views = UserViewStateStore(state=state)
    try:
        guest = access.acquire_guest()
        with pytest.raises(PermissionError, match="guest_view_state_not_persistent"):
            views.put(
                context=guest,
                workspace_id="workspace-a",
                session_id="session-a",
                turn_anchor="turn-1",
                scroll_offset=0,
                follow_latest=True,
                projection_version=1,
                tool_details_expanded=False,
            )
    finally:
        state.close()


def test_taken_over_user_cannot_read_or_overwrite_view_state(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    access = UserAccessService(state=state)
    views = UserViewStateStore(state=state)
    try:
        user = access.create_user(display_name="接管用户", user_id="taken-over")
        first = access.acquire_user(user_id=user.user_id, client_label="电脑 A")
        views.put(
            context=first,
            workspace_id="workspace-a",
            session_id="session-a",
            turn_anchor="turn-old",
            scroll_offset=1,
            follow_latest=False,
            projection_version=1,
            tool_details_expanded=False,
        )
        second = access.acquire_user(
            user_id=user.user_id,
            client_label="电脑 B",
            takeover=True,
        )

        with pytest.raises(PermissionError, match="user_session_taken_over"):
            views.put(
                context=first,
                workspace_id="workspace-a",
                session_id="session-a",
                turn_anchor="turn-stale",
                scroll_offset=2,
                follow_latest=False,
                projection_version=1,
                tool_details_expanded=False,
            )

        saved = views.put(
            context=second,
            workspace_id="workspace-a",
            session_id="session-a",
            turn_anchor="turn-new",
            scroll_offset=3,
            follow_latest=False,
            projection_version=1,
            tool_details_expanded=False,
        )
        assert saved.turn_anchor == "turn-new"
    finally:
        state.close()
