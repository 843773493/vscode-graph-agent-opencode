from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.user_access import UserAccessService
from app.gateway.control.view_state import UserViewStateStore


class _InjectedPrimaryError(RuntimeError):
    pass


class _InjectedRollbackError(RuntimeError):
    pass


class _InjectedCloseError(RuntimeError):
    pass


@dataclass
class _CleanupFaults:
    primary_operation: Literal["insert", "commit"] | None = None
    primary_error: _InjectedPrimaryError = field(
        default_factory=lambda: _InjectedPrimaryError("primary write failure")
    )
    rollback_error: _InjectedRollbackError | None = None
    close_error: _InjectedCloseError | None = None


class _SQLiteConnectionFaultBoundary:
    def __init__(self, connection: sqlite3.Connection, faults: _CleanupFaults) -> None:
        self.connection = connection
        self.faults = faults
        self.rollback_calls = 0
        self.close_calls = 0
        self.commit_calls = 0
        self.closed = False

    def execute(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Cursor:
        normalized_sql = sql.strip().upper()
        if (
            self.faults.primary_operation == "insert"
            and normalized_sql.startswith("INSERT INTO USER_VIEW_STATE")
        ):
            raise self.faults.primary_error
        if self.faults.primary_operation == "commit" and normalized_sql == "COMMIT":
            raise self.faults.primary_error
        cursor = self.connection.execute(sql, parameters)
        if normalized_sql == "COMMIT":
            self.commit_calls += 1
        return cursor

    def rollback(self) -> None:
        self.rollback_calls += 1
        if self.faults.rollback_error is not None:
            raise self.faults.rollback_error
        self.connection.rollback()

    def close(self) -> None:
        self.close_calls += 1
        self.connection.close()
        self.closed = True
        if self.faults.close_error is not None:
            raise self.faults.close_error


class _GatewayStateConnectionFaultBoundary:
    def __init__(self, state: GatewayStateStore, faults: _CleanupFaults) -> None:
        self.state = state
        self.faults = faults
        self.connections: list[_SQLiteConnectionFaultBoundary] = []

    def connection(self) -> _SQLiteConnectionFaultBoundary:
        connection = _SQLiteConnectionFaultBoundary(self.state.connection(), self.faults)
        self.connections.append(connection)
        return connection


@dataclass
class _ViewStateFaultHarness:
    views: UserViewStateStore
    boundary: _GatewayStateConnectionFaultBoundary
    faults: _CleanupFaults


@pytest.fixture
def gateway_state(tmp_path) -> Iterator[GatewayStateStore]:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        yield state
    finally:
        state.close()


@pytest.fixture
def user_access_context(gateway_state: GatewayStateStore):
    access = UserAccessService(state=gateway_state)
    user = access.create_user(display_name="故障注入用户", user_id="cleanup-fault-user")
    return access.acquire_user(user_id=user.user_id, client_label="测试客户端")


@pytest.fixture
def cleanup_faults() -> _CleanupFaults:
    return _CleanupFaults()


@pytest.fixture
def view_state_fault_harness(
    gateway_state: GatewayStateStore,
    cleanup_faults: _CleanupFaults,
) -> _ViewStateFaultHarness:
    boundary = _GatewayStateConnectionFaultBoundary(gateway_state, cleanup_faults)
    views = UserViewStateStore(state=cast(GatewayStateStore, boundary))
    return _ViewStateFaultHarness(views, boundary, cleanup_faults)


def test_view_state_cleanup_removes_only_stale_rows(tmp_path):
    """视图状态按保留窗口清理：只删过期行，返回删除条数。"""

    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    access = UserAccessService(state=state)
    views = UserViewStateStore(state=state, retention_days=30)
    try:
        user = access.create_user(display_name="清理用户", user_id="cleanup-user")
        context = access.acquire_user(user_id=user.user_id, client_label="A")
        views.put(
            context=context,
            workspace_id="workspace-a",
            session_id="session-stale",
            turn_anchor="turn-old",
            scroll_offset=1,
            follow_latest=False,
            projection_version=1,
            tool_details_expanded=False,
        )
        views.put(
            context=context,
            workspace_id="workspace-a",
            session_id="session-fresh",
            turn_anchor="turn-new",
            scroll_offset=2,
            follow_latest=True,
            projection_version=1,
            tool_details_expanded=False,
        )
        connection = state.connection()
        try:
            connection.execute(
                "UPDATE user_view_state SET updated_at = ? WHERE session_id = ?",
                (
                    (datetime.now(UTC) - timedelta(days=90)).isoformat(),
                    "session-stale",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        assert views.cleanup_expired() == 1
        # 新鲜行仍在，过期行已删除；再次清理为 0 行
        assert views.get(
            context=context, workspace_id="workspace-a", session_id="session-fresh"
        ) is not None
        assert views.get(
            context=context, workspace_id="workspace-a", session_id="session-stale"
        ) is None
        assert views.cleanup_expired() == 0
    finally:
        state.close()


def test_view_state_retention_days_must_be_positive(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        with pytest.raises(ValueError, match="保留天数必须大于 0 天"):
            UserViewStateStore(state=state, retention_days=0)
    finally:
        state.close()


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


def _persisted_rows(state: GatewayStateStore) -> int:
    connection = state.connection()
    try:
        return int(
            connection.execute("SELECT COUNT(*) FROM user_view_state").fetchone()[0]
        )
    finally:
        connection.close()


def _put_view_state(views: UserViewStateStore, context) -> object:
    return views.put(
        context=context,
        workspace_id="workspace-fault",
        session_id="session-fault",
        turn_anchor="turn-fault",
        scroll_offset=4.5,
        follow_latest=True,
        projection_version=2,
        tool_details_expanded=False,
    )


@pytest.mark.parametrize("primary_operation", ["insert", "commit"])
@pytest.mark.parametrize(
    ("rollback_fails", "close_fails"),
    [(True, False), (False, True), (True, True)],
)
def test_view_state_put_preserves_primary_error_when_cleanup_fails(
    gateway_state: GatewayStateStore,
    user_access_context,
    view_state_fault_harness: _ViewStateFaultHarness,
    primary_operation: Literal["insert", "commit"],
    rollback_fails: bool,
    close_fails: bool,
) -> None:
    harness = view_state_fault_harness
    harness.faults.primary_operation = primary_operation
    if rollback_fails:
        harness.faults.rollback_error = _InjectedRollbackError("rollback failure")
    if close_fails:
        harness.faults.close_error = _InjectedCloseError("close failure")

    with pytest.raises(_InjectedPrimaryError) as raised:
        _put_view_state(harness.views, user_access_context)

    connection = harness.boundary.connections[-1]
    assert raised.value is harness.faults.primary_error
    assert connection.rollback_calls == 1
    assert connection.close_calls == 1
    assert connection.closed
    notes = "\n".join(raised.value.__notes__)
    assert ("回滚失败" in notes) is rollback_fails
    assert ("InjectedRollbackError: rollback failure" in notes) is rollback_fails
    assert ("连接关闭失败" in notes) is close_fails
    assert ("InjectedCloseError: close failure" in notes) is close_fails
    assert _persisted_rows(gateway_state) == 0


def test_view_state_put_closes_connection_and_commits_on_success(
    gateway_state: GatewayStateStore,
    user_access_context,
    view_state_fault_harness: _ViewStateFaultHarness,
) -> None:
    saved = _put_view_state(view_state_fault_harness.views, user_access_context)

    connection = view_state_fault_harness.boundary.connections[-1]
    assert saved.workspace_id == "workspace-fault"
    assert saved.session_id == "session-fault"
    assert connection.commit_calls == 1
    assert connection.rollback_calls == 0
    assert connection.close_calls == 1
    assert connection.closed
    assert _persisted_rows(gateway_state) == 1


def test_view_state_put_reports_close_failure_after_commit_without_rollback_claim(
    gateway_state: GatewayStateStore,
    user_access_context,
    view_state_fault_harness: _ViewStateFaultHarness,
) -> None:
    close_error = _InjectedCloseError("close failure after committed write")
    view_state_fault_harness.faults.close_error = close_error

    with pytest.raises(_InjectedCloseError) as raised:
        _put_view_state(view_state_fault_harness.views, user_access_context)

    connection = view_state_fault_harness.boundary.connections[-1]
    assert raised.value is close_error
    assert connection.commit_calls == 1
    assert connection.rollback_calls == 0
    assert connection.close_calls == 1
    assert connection.closed
    assert _persisted_rows(gateway_state) == 1
