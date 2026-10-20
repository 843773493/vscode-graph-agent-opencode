from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TextIO
from unittest.mock import Mock

import pytest

from tests.integration.gateway import (
    test_navigation_actor_scope_proxy as navigation_case,
)


@dataclass
class NavigationStartup:
    workspace: Path
    spawn: Mock
    ready: Mock
    terminate: Mock
    logs: list[TextIO]

    def start(self) -> navigation_case._NavigationScopeBackendProcess:
        return navigation_case._start_navigation_scope_backend(
            workspace_root=self.workspace, port=18040,
        )


@pytest.fixture
def startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[NavigationStartup]:
    process = Mock(spec=subprocess.Popen)
    spawn, ready, terminate = Mock(return_value=process), Mock(), Mock()
    logs: list[TextIO] = []
    real_open = open

    def open_log(path: Path, mode: str, *, encoding: str) -> TextIO:
        stream = real_open(path, mode, encoding=encoding)
        logs.append(stream)
        return stream

    monkeypatch.setattr(navigation_case, "open", open_log, raising=False)
    monkeypatch.setattr(navigation_case, "subprocess", SimpleNamespace(Popen=spawn))
    monkeypatch.setattr(navigation_case, "ensure_local_port_available", Mock())
    monkeypatch.setattr(navigation_case, "resolve_workspace_python_executable", lambda _root: tmp_path / "python")
    monkeypatch.setattr(navigation_case, "wait_for_http_ok", ready)
    monkeypatch.setattr(navigation_case, "terminate_process", terminate)
    yield NavigationStartup(tmp_path / "workspace", spawn, ready, terminate, logs)
    # 先由测试观察装具是否释放，再兜底关闭故障实现留下的描述符。
    for stream in logs:
        stream.close()


def test_second_log_open_failure_releases_stdout(
    startup: NavigationStartup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = OSError("stderr 打开失败")
    open_log = navigation_case.open

    def fail_stderr(path: Path, mode: str, *, encoding: str) -> TextIO:
        if path.name.endswith(".stderr.log"):
            raise error
        return open_log(path, mode, encoding=encoding)

    monkeypatch.setattr(navigation_case, "open", fail_stderr)
    with pytest.raises(OSError) as failure:
        startup.start()
    assert failure.value is error
    assert len(startup.logs) == 1 and startup.logs[0].closed
    startup.spawn.assert_not_called()


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_spawn_failure_releases_logs(
    startup: NavigationStartup, error_type: type[BaseException],
) -> None:
    error = error_type("创建进程失败")
    startup.spawn.side_effect = error
    with pytest.raises(error_type) as failure:
        startup.start()
    assert failure.value is error
    assert len(startup.logs) == 2 and all(stream.closed for stream in startup.logs)
    startup.ready.assert_not_called()
    startup.terminate.assert_not_called()


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_readiness_failure_releases_resources_without_reading_full_logs(
    startup: NavigationStartup, monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException], cleanup_fails: bool,
) -> None:
    error, cleanup_error = error_type("就绪失败"), OSError("终止失败")
    startup.ready.side_effect = error
    startup.terminate.side_effect = cleanup_error if cleanup_fails else None
    real_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path.suffix == ".log":
            pytest.fail("启动失败诊断应保留日志路径，不应全量读取历史日志")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(BaseException) as failure:
        startup.start()
    if cleanup_fails:
        assert isinstance(failure.value, BaseExceptionGroup)
        assert failure.value.exceptions[0] is error
        assert failure.value.exceptions[1].exceptions == (cleanup_error,)
    else:
        assert failure.value is error
    notes = "\n".join(error.__notes__)
    assert all(stream.name in notes for stream in startup.logs)
    assert len(startup.logs) == 2 and all(stream.closed for stream in startup.logs)
    startup.terminate.assert_called_once_with(startup.spawn.return_value)


def test_successful_handle_owns_logs_until_closed(startup: NavigationStartup) -> None:
    handle = startup.start()
    assert all(not stream.closed for stream in startup.logs)
    assert handle.stdout_file is startup.logs[0]
    assert handle.stderr_file is startup.logs[1]
    navigation_case._close_navigation_scope_backend(handle)
    assert all(stream.closed for stream in startup.logs)
    startup.terminate.assert_called_once_with(handle.process)


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("termination_fails", [False, True])
def test_close_attempts_both_logs_and_keeps_all_errors(
    startup: NavigationStartup, error_type: type[BaseException], termination_fails: bool,
) -> None:
    handle = startup.start()
    terminate_error, stdout_error, stderr_error = OSError("终止失败"), error_type("stdout 关闭失败"), OSError("stderr 关闭失败")
    startup.terminate.side_effect = terminate_error if termination_fails else None

    def fail_close(stream: TextIO, error: BaseException) -> None:
        stream.close()
        raise error

    stdout, stderr = Mock(wraps=handle.stdout_file), Mock(wraps=handle.stderr_file)
    stdout.close.side_effect = lambda: fail_close(handle.stdout_file, stdout_error)
    stderr.close.side_effect = lambda: fail_close(handle.stderr_file, stderr_error)
    with pytest.raises(BaseException) as failure:
        navigation_case._close_navigation_scope_backend(replace(handle, stdout_file=stdout, stderr_file=stderr))
    assert all(stream.closed for stream in startup.logs)
    assert isinstance(failure.value, BaseExceptionGroup)
    assert failure.value.exceptions == (
        *((terminate_error,) if termination_fails else ()), stdout_error, stderr_error,
    )
