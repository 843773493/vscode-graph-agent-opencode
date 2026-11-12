from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from http.server import HTTPServer
from unittest.mock import Mock

import pytest

from tests.support import http_server


@dataclass
class EndpointLifetime:
    context: AbstractContextManager[HTTPServer]
    server: Mock
    thread: Mock
    cleanups: list[str]
    thread_factory: Mock


@pytest.fixture
def endpoint_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> EndpointLifetime:
    server = Mock(server_port=18000)
    thread = Mock()
    thread.is_alive.return_value = False
    cleanups: list[str] = []
    server.shutdown.side_effect = lambda: cleanups.append("shutdown")
    thread.join.side_effect = lambda **_kwargs: cleanups.append("join")
    server.server_close.side_effect = lambda: cleanups.append("socket-close")
    thread_factory = Mock(return_value=thread)
    monkeypatch.setattr(http_server, "Thread", thread_factory)
    return EndpointLifetime(http_server.running_http_server(server), server, thread, cleanups, thread_factory)


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("stage", ["construct", "start"])
def test_endpoint_start_failure_closes_socket_without_stopping_unstarted_thread(
    endpoint_lifetime: EndpointLifetime, interrupted: bool, stage: str,
) -> None:
    state = endpoint_lifetime
    error = KeyboardInterrupt("HTTP线程启动中断") if interrupted else OSError("HTTP线程启动失败")
    action = state.thread_factory if stage == "construct" else state.thread.start
    action.side_effect = error

    with pytest.raises(type(error)) as failure, state.context:
        pytest.fail("启动失败不得进入上下文")

    assert failure.value is error
    assert state.cleanups == ["socket-close"]
    state.server.shutdown.assert_not_called()
    state.thread.join.assert_not_called()


@pytest.mark.parametrize("thread_alive", [False, True])
def test_endpoint_close_checks_thread_exit_and_releases_socket(
    endpoint_lifetime: EndpointLifetime, thread_alive: bool,
) -> None:
    state = endpoint_lifetime
    state.thread.is_alive.return_value = thread_alive
    if thread_alive:
        with pytest.raises(AssertionError, match="HTTP 服务线程未停止"), state.context:
            pass
    else:
        with state.context as server:
            assert server is state.server

    assert state.cleanups == ["shutdown", "join", "socket-close"]
    state.thread.join.assert_called_once_with(timeout=5)
    state.thread.is_alive.assert_called_once_with()


@pytest.mark.parametrize("stage", ["shutdown", "join", "socket-close"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_endpoint_teardown_failure_still_attempts_remaining_resources(
    endpoint_lifetime: EndpointLifetime, stage: str, interrupted: bool,
) -> None:
    state = endpoint_lifetime
    body_error = LookupError("HTTP探针使用失败")
    cleanup_error = KeyboardInterrupt("HTTP清理中断") if interrupted else OSError("HTTP清理失败")
    action = {"shutdown": state.server.shutdown, "join": state.thread.join, "socket-close": state.server.server_close}[stage]

    def fail_cleanup(**_kwargs: object) -> None:
        state.cleanups.append(stage)
        raise cleanup_error

    action.side_effect = fail_cleanup
    with pytest.raises(type(cleanup_error)) as failure, state.context:
        raise body_error

    assert failure.value is cleanup_error
    assert cleanup_error.__context__ is body_error
    assert state.cleanups == ["shutdown", "join", "socket-close"]
