from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

import tests.support.processes.gateway as gateway_processes
import tests.support.processes.workspace as workspace_processes


@dataclass
class ManualClock:
    now: float = 0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    value = ManualClock()
    monkeypatch.setattr(workspace_processes, "time", value)
    return value


@pytest.fixture
def process() -> Mock:
    value = Mock(spec=subprocess.Popen)
    value.pid = 4312
    value.returncode = None
    value.poll.return_value = None
    value.communicate.return_value = ("启动标准输出", "启动错误详情")
    return value


@contextmanager
def health_response(status: int = 200):
    yield SimpleNamespace(status=status)


def test_ready_service_is_not_delayed_by_one_second_polling(
    clock: ManualClock,
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def connect(_url: str, *, timeout: float):
        if clock.now < 0.15:
            raise URLError(ConnectionRefusedError("监听尚未建立"))
        return health_response()

    monkeypatch.setattr(workspace_processes, "urlopen", connect)
    workspace_processes.wait_for_http_ok("http://fixture/health", process)

    assert 0.15 <= clock.now <= 0.25


def test_http_rejection_fails_immediately_with_target_and_status(
    clock: ManualClock,
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = BytesIO(b"wrong owner")
    error = HTTPError("http://fixture/health", 403, "Forbidden", {}, body)
    monkeypatch.setattr(workspace_processes, "urlopen", Mock(side_effect=error))

    with pytest.raises(RuntimeError, match=r"status=403.*http://fixture/health") as failure:
        workspace_processes.wait_for_http_ok("http://fixture/health", process)

    assert failure.value.__cause__ is error
    assert clock.now == 0
    assert body.closed


def test_connect_timeout_consumes_only_remaining_readiness_budget(
    clock: ManualClock,
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts = []

    def connect(_url: str, *, timeout: float):
        observed_timeouts.append(timeout)
        clock.sleep(timeout)
        raise TimeoutError("连接未完成")

    monkeypatch.setattr(workspace_processes, "urlopen", connect)
    with pytest.raises(TimeoutError, match="http://fixture/health") as failure:
        workspace_processes.wait_for_http_ok("http://fixture/health", process, timeout_seconds=0.12)

    assert observed_timeouts == pytest.approx([0.12])
    assert clock.now == pytest.approx(0.12)
    assert isinstance(failure.value.__cause__, TimeoutError)


def test_refused_connection_reports_last_error_without_oversleep(
    clock: ManualClock,
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = URLError(ConnectionRefusedError("连接被拒绝"))
    monkeypatch.setattr(workspace_processes, "urlopen", Mock(side_effect=error))

    with pytest.raises(TimeoutError, match="连接被拒绝") as failure:
        workspace_processes.wait_for_http_ok("http://fixture/health", process, timeout_seconds=0.12)

    assert clock.now == pytest.approx(0.12)
    assert failure.value.__cause__ is error


def test_early_exit_keeps_return_code_and_captured_process_output(
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process.returncode = 7
    process.poll.return_value = 7
    connect = Mock()
    monkeypatch.setattr(workspace_processes, "urlopen", connect)

    with pytest.raises(RuntimeError, match="returncode=7") as failure:
        workspace_processes.wait_for_http_ok("http://fixture/health", process)

    assert "启动标准输出" in str(failure.value)
    assert "启动错误详情" in str(failure.value)
    connect.assert_not_called()


@pytest.mark.parametrize("captured_output", ["string", "bytes", "empty"])
def test_early_exit_remains_primary_when_output_capture_times_out(
    captured_output: str,
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout: str | bytes | None = "已捕获标准输出"
    stderr: str | bytes | None = "已捕获错误详情"
    if captured_output == "bytes":
        stdout, stderr = stdout.encode(), stderr.encode()
    elif captured_output == "empty":
        stdout = stderr = None
    output_error = subprocess.TimeoutExpired(
        ["fixture-service"], 0.12, output=stdout, stderr=stderr,
    )
    process.poll.return_value = 7
    process.communicate.side_effect = output_error
    connect = Mock()
    monkeypatch.setattr(workspace_processes, "urlopen", connect)

    with pytest.raises(RuntimeError, match=r"returncode=7.*http://fixture/health") as failure:
        workspace_processes.wait_for_http_ok("http://fixture/health", process, timeout_seconds=0.12)

    assert failure.value.__cause__ is output_error
    assert any("输出采集" in note for note in failure.value.__notes__)
    if captured_output != "empty":
        assert "已捕获标准输出" in str(failure.value)
        assert "已捕获错误详情" in str(failure.value)
    connect.assert_not_called()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()


def test_successful_http_status_other_than_200_is_an_explicit_error(
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_processes, "urlopen", lambda *_args, **_kwargs: health_response(202))
    with pytest.raises(RuntimeError, match="status=202"):
        workspace_processes.wait_for_http_ok("http://fixture/health", process)


def test_readiness_cancellation_is_not_translated_to_connection_failure(
    clock: ManualClock,
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = KeyboardInterrupt("停止等待")
    monkeypatch.setattr(workspace_processes, "urlopen", Mock(side_effect=error))
    with pytest.raises(KeyboardInterrupt) as failure:
        workspace_processes.wait_for_http_ok("http://fixture/health", process)
    assert failure.value is error
    assert not clock.sleeps


def test_gateway_retries_promptly_and_keeps_authentication_header(
    clock: ManualClock,
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Request] = []

    def connect(request: Request, *, timeout: float):
        requests.append(request)
        if clock.now < 0.15:
            raise URLError(ConnectionRefusedError("监听尚未建立"))
        return health_response()

    monkeypatch.setattr(workspace_processes, "urlopen", connect)
    gateway_processes.wait_for_gateway_ready(4312, process)

    assert 0.15 <= clock.now <= 0.25
    assert len(requests) > 1
    assert all(
        request.full_url == "http://127.0.0.1:4312/api/gateway/health"
        and dict(request.header_items()) == {"X-local-token": "local-dev-token"}
        for request in requests
    )


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_gateway_http_rejection_is_not_retried_as_startup(
    status: int,
    clock: ManualClock,
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "http://127.0.0.1:4312/api/gateway/health"
    body = BytesIO(b"gateway refusal")
    error = HTTPError(url, status, "refused", {}, body)
    connect = Mock(side_effect=error)
    monkeypatch.setattr(workspace_processes, "urlopen", connect)

    with pytest.raises(RuntimeError, match=rf"status={status}.*{url}") as failure:
        gateway_processes.wait_for_gateway_ready(4312, process)

    assert failure.value.__cause__ is error
    assert connect.call_count == 1
    assert clock.now == 0
    assert body.closed


@contextmanager
def workspace_health_response(payload: object, status: int = 200):
    with BytesIO(json.dumps(payload).encode()) as body:
        yield SimpleNamespace(status=status, read=body.read)


def test_workspace_ready_keeps_identity_header_without_one_second_polling(
    clock: ManualClock,
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Request] = []

    def connect(request: Request, *, timeout: float):
        requests.append(request)
        if clock.now < 0.15:
            raise URLError(ConnectionRefusedError("监听尚未建立"))
        return workspace_health_response({"workspace_id": "workspace-expected"})

    monkeypatch.setattr(workspace_processes, "urlopen", connect)
    workspace_processes.wait_for_backend_ready(4312, process, "workspace-expected")

    assert 0.15 <= clock.now <= 0.25
    assert all(
        request.full_url == "http://127.0.0.1:4312/api/v1/health"
        and request.get_header("X-boxteam-workspace-id") == "workspace-expected"
        for request in requests
    )


@pytest.mark.parametrize("connection_timeout", [False, True])
def test_workspace_readiness_obeys_remaining_budget(
    connection_timeout: bool,
    clock: ManualClock,
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_processes, "TEST_READY_TIMEOUT_SECONDS", 0.12)
    timeouts: list[float] = []
    error = TimeoutError("连接未完成")

    def connect(_request: Request, *, timeout: float):
        timeouts.append(timeout)
        if connection_timeout:
            clock.sleep(timeout)
        raise error

    monkeypatch.setattr(workspace_processes, "urlopen", connect)
    with pytest.raises(TimeoutError, match="后端在 0.12 秒内未就绪") as failure:
        workspace_processes.wait_for_backend_ready(4312, process, "workspace-expected")

    assert clock.now == pytest.approx(0.12)
    assert all(0 < timeout <= 0.12 for timeout in timeouts)
    assert failure.value.__cause__ is error


def test_workspace_http_error_closes_response_and_reads_only_bounded_diagnostics(
    clock: ManualClock,
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = BytesIO(b"wrong workspace " * 100_000)
    read = Mock(wraps=body.read)
    monkeypatch.setattr(body, "read", read)
    error = HTTPError("http://fixture/health", 403, "Forbidden", {}, body)
    monkeypatch.setattr(workspace_processes, "urlopen", Mock(side_effect=error))

    with pytest.raises(RuntimeError, match="status=403.*workspace-expected.*wrong workspace") as failure:
        workspace_processes.wait_for_backend_ready(4312, process, "workspace-expected")

    assert failure.value.__cause__ is error
    assert clock.now == 0
    assert body.closed
    read.assert_called_once()
    assert read.call_args.args and 0 < read.call_args.args[0] <= 1200


def test_workspace_http_status_other_than_200_is_not_retried(
    clock: ManualClock,
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def connect(_request: Request, *, timeout: float):
        nonlocal calls
        calls += 1
        clock.now += 1
        return workspace_health_response({"workspace_id": "workspace-expected"}, status=202)

    monkeypatch.setattr(workspace_processes, "urlopen", connect)
    with pytest.raises(RuntimeError, match="status=202"):
        workspace_processes.wait_for_backend_ready(4312, process, "workspace-expected")

    assert calls == 1


@pytest.mark.parametrize("payload", [{"workspace_id": "another-workspace"}, {}])
def test_workspace_success_status_still_rejects_another_or_missing_owner(
    payload: dict[str, str],
    clock: ManualClock,
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_processes, "urlopen", lambda *_args, **_kwargs: workspace_health_response(payload))
    with pytest.raises(RuntimeError, match="UUID 不匹配.*expected=workspace-expected"):
        workspace_processes.wait_for_backend_ready(4312, process, "workspace-expected")
    assert not clock.sleeps


@pytest.mark.parametrize("payload", [None, [], "healthy"])
def test_workspace_success_requires_a_json_object(
    payload: object,
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_processes, "urlopen", lambda *_args, **_kwargs: workspace_health_response(payload))
    with pytest.raises(TypeError, match="必须是 JSON 对象"):
        workspace_processes.wait_for_backend_ready(4312, process, "workspace-expected")


def test_workspace_early_exit_keeps_existing_error_contract(
    process: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process.poll.return_value = 7
    process.returncode = 7
    connect = Mock()
    monkeypatch.setattr(workspace_processes, "urlopen", connect)
    with pytest.raises(RuntimeError, match="后端进程提前退出.*returncode=7"):
        workspace_processes.wait_for_backend_ready(4312, process, "workspace-expected")
    connect.assert_not_called()
