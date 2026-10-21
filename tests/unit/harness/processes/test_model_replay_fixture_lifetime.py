from __future__ import annotations

import json
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.integration.harness import test_model_http_assets as replay_case


@dataclass
class ReplayLifetime:
    fixture: Generator[tuple[str, list[dict[str, object]]], None, None]
    server: Mock
    thread: Mock
    cleanups: list[str]
    requests_path: Path


@pytest.fixture(params=["responses", "chat_completions"])
def replay_lifetime(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> ReplayLifetime:
    server = Mock(server_port=18000)
    thread = Mock()
    thread.is_alive.return_value = False
    cleanups: list[str] = []
    server.shutdown.side_effect = lambda: cleanups.append("shutdown")
    thread.join.side_effect = lambda **_kwargs: cleanups.append("join")
    server.server_close.side_effect = lambda: cleanups.append("socket-close")
    monkeypatch.setattr(replay_case, "ThreadingHTTPServer", Mock(return_value=server))
    monkeypatch.setattr(replay_case.threading, "Thread", Mock(return_value=thread))
    fixture = replay_case.model_replay.__wrapped__(str(tmp_path / "workspace"), request.param)
    return ReplayLifetime(fixture, server, thread, cleanups, tmp_path / "artifacts/model-replay-requests.json")


@pytest.mark.parametrize("interrupted", [False, True])
def test_replay_start_failure_closes_socket_and_preserves_diagnostics(
    replay_lifetime: ReplayLifetime, interrupted: bool,
) -> None:
    state = replay_lifetime
    error = KeyboardInterrupt("回放线程启动中断") if interrupted else OSError("回放线程启动失败")
    state.thread.start.side_effect = error

    with pytest.raises(type(error)) as failure:
        next(state.fixture)

    assert failure.value is error
    assert state.cleanups == ["socket-close"]
    state.server.shutdown.assert_not_called()
    state.thread.join.assert_not_called()
    assert json.loads(state.requests_path.read_text()) == []


@pytest.mark.parametrize("thread_alive", [False, True])
def test_replay_close_checks_thread_exit_and_preserves_requests(
    replay_lifetime: ReplayLifetime, thread_alive: bool,
) -> None:
    state = replay_lifetime
    state.thread.is_alive.return_value = thread_alive
    endpoint, requests = next(state.fixture)
    assert endpoint == "http://127.0.0.1:18000/v1"
    requests.append({"input": "真实收到的请求"})

    if thread_alive:
        with pytest.raises(AssertionError):
            state.fixture.close()
    else:
        state.fixture.close()

    assert state.cleanups == ["shutdown", "join", "socket-close"]
    state.thread.join.assert_called_once_with(timeout=5)
    state.thread.is_alive.assert_called_once_with()
    assert json.loads(state.requests_path.read_text()) == requests


@pytest.mark.parametrize("stage", ["shutdown", "join", "socket-close"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_replay_teardown_failure_releases_remaining_resources_and_saves_requests(
    replay_lifetime: ReplayLifetime, stage: str, interrupted: bool,
) -> None:
    state = replay_lifetime
    body_error = LookupError("回放使用失败")
    cleanup_error = KeyboardInterrupt("回放清理中断") if interrupted else OSError("回放清理失败")
    action = {"shutdown": state.server.shutdown, "join": state.thread.join, "socket-close": state.server.server_close}[stage]

    def fail_cleanup(**_kwargs: object) -> None:
        state.cleanups.append(stage)
        raise cleanup_error

    action.side_effect = fail_cleanup
    _, requests = next(state.fixture)
    requests.append({"input": "失败前收到的请求"})
    with pytest.raises(type(cleanup_error)) as failure:
        state.fixture.throw(body_error)

    assert failure.value is cleanup_error
    assert cleanup_error.__context__ is body_error
    assert state.cleanups == ["shutdown", "join", "socket-close"]
    assert json.loads(state.requests_path.read_text()) == requests
