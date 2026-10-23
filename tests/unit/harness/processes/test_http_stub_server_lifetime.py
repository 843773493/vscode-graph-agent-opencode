from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from unittest.mock import Mock

import pytest

from tests.integration.stubs import http_stubs


@dataclass
class HTTPStubLifetime:
    context: AbstractContextManager[http_stubs.HTTPStubState]
    server: Mock
    thread: Mock
    thread_factory: Mock
    cleanups: list[str]


@pytest.fixture(params=["generation", "chat", "tool-loop"])
def stub_lifetime(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> HTTPStubLifetime:
    server, thread = Mock(), Mock()
    thread.is_alive.return_value = False
    cleanups: list[str] = []
    server.shutdown.side_effect = lambda: cleanups.append("shutdown")
    thread.join.side_effect = lambda **_kwargs: cleanups.append("join")
    server.server_close.side_effect = lambda: cleanups.append("socket-close")
    thread_factory = Mock(return_value=thread)
    monkeypatch.setattr(http_stubs, "ThreadingHTTPServer", Mock(return_value=server))
    monkeypatch.setattr(http_stubs.threading, "Thread", thread_factory)
    if request.param == "generation":
        context = http_stubs.generation_target_stub(
            18000, output_workspace_id="output-workspace", backend_workspace_id="backend-workspace",
        )
    elif request.param == "chat":
        context = http_stubs.openai_chat_stub(18000)
    else:
        context = http_stubs.openai_chat_tool_loop_stub(18000)
    return HTTPStubLifetime(context, server, thread, thread_factory, cleanups)


@pytest.mark.parametrize("stage", ["construct-thread", "start-thread"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_stub_start_failure_closes_socket_without_stopping_unstarted_thread(
    stub_lifetime: HTTPStubLifetime, stage: str, interrupted: bool,
) -> None:
    state = stub_lifetime
    error = KeyboardInterrupt("替身线程启动中断") if interrupted else OSError("替身线程启动失败")
    action = state.thread_factory if stage == "construct-thread" else state.thread.start
    action.side_effect = error

    with pytest.raises(type(error)) as failure, state.context:
        pytest.fail("启动失败不应进入替身上下文")

    assert failure.value is error
    assert state.cleanups == ["socket-close"]
    state.server.shutdown.assert_not_called()
    state.thread.join.assert_not_called()


@pytest.mark.parametrize("thread_alive", [False, True])
def test_stub_close_checks_thread_exit_and_releases_socket(
    stub_lifetime: HTTPStubLifetime, thread_alive: bool,
) -> None:
    state = stub_lifetime
    state.thread.is_alive.return_value = thread_alive
    if thread_alive:
        with pytest.raises(AssertionError, match="HTTP替身服务线程未停止"), state.context:
            pass
    else:
        with state.context as stub:
            assert isinstance(stub, http_stubs.HTTPStubState)
            assert stub.requests == []

    assert sorted(state.cleanups) == ["join", "shutdown", "socket-close"]
    state.thread.join.assert_called_once_with(timeout=5)


@pytest.mark.parametrize("stage", ["shutdown", "join", "socket-close"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_stub_teardown_failure_attempts_remaining_cleanups_and_preserves_error_chain(
    stub_lifetime: HTTPStubLifetime, stage: str, interrupted: bool,
) -> None:
    state = stub_lifetime
    body_error = LookupError("替身使用失败")
    cleanup_error = KeyboardInterrupt("替身清理中断") if interrupted else OSError("替身清理失败")
    action = {"shutdown": state.server.shutdown, "join": state.thread.join, "socket-close": state.server.server_close}[stage]

    def fail_cleanup(**_kwargs: object) -> None:
        state.cleanups.append(stage)
        raise cleanup_error

    action.side_effect = fail_cleanup
    with pytest.raises(type(cleanup_error)) as failure, state.context:
        raise body_error

    assert failure.value is cleanup_error
    assert cleanup_error.__context__ is body_error
    assert sorted(state.cleanups) == ["join", "shutdown", "socket-close"]
