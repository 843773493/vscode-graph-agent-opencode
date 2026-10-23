from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.integration.harness import test_gateway_readiness as gateway_case
from tests.integration.harness import test_workspace_readiness as workspace_case


@dataclass
class EndpointLifetime:
    fixture: Generator[tuple[int, list[tuple[str, str | None]]], None, None]
    server: Mock
    thread: Mock
    cleanups: list[str]


@pytest.fixture(params=[gateway_case, workspace_case], ids=["gateway", "workspace"])
def endpoint_lifetime(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> EndpointLifetime:
    module = request.param
    server = Mock(server_port=18000)
    thread = Mock()
    thread.is_alive.return_value = False
    cleanups: list[str] = []
    server.shutdown.side_effect = lambda: cleanups.append("shutdown")
    thread.join.side_effect = lambda **_kwargs: cleanups.append("join")
    server.server_close.side_effect = lambda: cleanups.append("socket-close")
    monkeypatch.setattr(module, "HTTPServer", Mock(return_value=server))
    monkeypatch.setattr(module, "Thread", Mock(return_value=thread))
    parameter = 200 if module is gateway_case else (200, {"workspace_id": workspace_case.EXPECTED_WORKSPACE_ID})
    fixture = module.health_endpoint.__wrapped__(SimpleNamespace(param=parameter))
    return EndpointLifetime(fixture, server, thread, cleanups)


@pytest.mark.parametrize("interrupted", [False, True])
def test_endpoint_start_failure_closes_socket_without_stopping_unstarted_thread(
    endpoint_lifetime: EndpointLifetime, interrupted: bool,
) -> None:
    state = endpoint_lifetime
    error = KeyboardInterrupt("HTTP线程启动中断") if interrupted else OSError("HTTP线程启动失败")
    state.thread.start.side_effect = error

    with pytest.raises(type(error)) as failure:
        next(state.fixture)

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
    assert next(state.fixture) == (18000, [])

    if thread_alive:
        with pytest.raises(AssertionError, match="HTTP 服务线程未停止"):
            state.fixture.close()
    else:
        state.fixture.close()

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
    next(state.fixture)
    with pytest.raises(type(cleanup_error)) as failure:
        state.fixture.throw(body_error)

    assert failure.value is cleanup_error
    assert cleanup_error.__context__ is body_error
    assert state.cleanups == ["shutdown", "join", "socket-close"]
