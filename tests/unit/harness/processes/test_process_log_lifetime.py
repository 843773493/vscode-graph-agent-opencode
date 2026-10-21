from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TextIO
from unittest.mock import Mock

import pytest

from tests.support.processes import (
    gateway as gateway_processes,
    workspace as workspace_processes,
)


@dataclass
class ProcessStartup:
    module: ModuleType
    start: Callable[
        [],
        workspace_processes.BackendProcess | gateway_processes.GatewayProcess,
    ]
    close: Callable[..., None]
    spawn: Mock
    ready: Mock
    terminate: Mock


@pytest.fixture(params=["workspace", "gateway"])
def startup(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ProcessStartup:
    workspace = tmp_path / "workspace"
    config = workspace / ".boxteam" / "workspace.jsonc"
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")
    module = workspace_processes if request.param == "workspace" else gateway_processes
    process = Mock(spec=subprocess.Popen)
    process.pid = 4312
    spawn = Mock(return_value=process)
    ready = Mock()
    terminate = Mock()
    monkeypatch.setattr(module, "subprocess", SimpleNamespace(Popen=spawn))
    monkeypatch.setattr(module, "ensure_local_port_available", Mock())
    monkeypatch.setattr(module, "resolve_workspace_python_executable", lambda _root: tmp_path / "python")
    monkeypatch.setattr(module, "terminate_process", terminate)
    monkeypatch.setattr(module, "register_test_process", Mock())
    monkeypatch.setattr(module, "unregister_test_process", Mock())
    if request.param == "workspace":
        monkeypatch.setattr(module, "install_user_configuration", Mock())
        monkeypatch.setattr(module, "resolve_backend_workspace_id", lambda *_args: "00000000-0000-4000-8000-000000000001")
        monkeypatch.setattr(module, "wait_for_backend_ready", ready)
        start = partial(workspace_processes.start_backend_process,
            workspace_root=str(workspace), port=18040, log_name="startup",
        )
        close = workspace_processes.close_backend_process
    else:
        monkeypatch.setattr(module, "_install_current_user_configuration", Mock())
        monkeypatch.setattr(module.shutil, "which", lambda _name: "node")
        monkeypatch.setattr(module, "wait_for_gateway_ready", ready)
        start = partial(gateway_processes.start_gateway_process,
            workspace_root=workspace, default_backend_url=None, port=18040,
        )
        close = gateway_processes.close_gateway_process
    return ProcessStartup(module, start, close, spawn, ready, terminate)


def test_spawn_failure_closes_both_logs_and_keeps_original_error(startup: ProcessStartup) -> None:
    error = OSError("进程创建失败")
    startup.spawn.side_effect = error

    with pytest.raises(OSError) as failure:
        startup.start()

    assert failure.value is error
    assert startup.spawn.call_args.kwargs["stdout"].closed
    assert startup.spawn.call_args.kwargs["stderr"].closed
    startup.ready.assert_not_called()
    startup.terminate.assert_not_called()


def test_stderr_open_failure_closes_stdout_before_any_spawn(
    startup: ProcessStartup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = OSError("错误日志文件无法打开")
    opened: list[TextIO] = []
    real_open = open

    def open_log(file: Path, mode: str, *, encoding: str) -> TextIO:
        if file.name.endswith(".stderr.log"):
            raise error
        stream = real_open(file, mode, encoding=encoding)
        opened.append(stream)
        return stream

    monkeypatch.setattr(startup.module, "open", open_log, raising=False)
    with pytest.raises(OSError) as failure:
        startup.start()

    assert failure.value is error
    assert len(opened) == 1
    assert opened[0].closed
    startup.spawn.assert_not_called()
    startup.terminate.assert_not_called()


def test_ready_handle_owns_logs_until_close(startup: ProcessStartup) -> None:
    handle = startup.start()
    try:
        assert handle.stdout_file is startup.spawn.call_args.kwargs["stdout"]
        assert handle.stderr_file is startup.spawn.call_args.kwargs["stderr"]
        assert not handle.stdout_file.closed
        assert not handle.stderr_file.closed
        startup.terminate.assert_not_called()
    finally:
        startup.close(handle)

    assert handle.stdout_file.closed
    assert handle.stderr_file.closed
    startup.terminate.assert_called_once_with(handle.process)


def test_readiness_failure_releases_process_and_logs(startup: ProcessStartup) -> None:
    error = KeyboardInterrupt("就绪等待被中断")
    startup.ready.side_effect = error

    with pytest.raises(KeyboardInterrupt) as failure:
        startup.start()

    assert failure.value is error
    assert startup.spawn.call_args.kwargs["stdout"].closed
    assert startup.spawn.call_args.kwargs["stderr"].closed
    startup.terminate.assert_called_once_with(startup.spawn.return_value)


@pytest.mark.parametrize("close_error_type", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("stderr_also_fails", [False, True])
@pytest.mark.parametrize("termination_fails", [False, True])
def test_log_close_failure_still_closes_other_log_and_preserves_all_errors(
    startup: ProcessStartup,
    close_error_type: type[BaseException],
    stderr_also_fails: bool,
    termination_fails: bool,
) -> None:
    handle = startup.start()
    stdout_error = close_error_type("stdout 关闭失败")
    stderr_error = OSError("stderr 关闭失败") if stderr_also_fails else None
    termination_error = RuntimeError("进程终止失败") if termination_fails else None
    startup.terminate.side_effect = termination_error
    order: list[str] = []

    def close_log(stream: TextIO, label: str, error: BaseException | None) -> None:
        order.append(label)
        stream.close()
        if error is not None:
            raise error

    stdout = Mock(wraps=handle.stdout_file)
    stderr = Mock(wraps=handle.stderr_file)
    stdout.close.side_effect = partial(close_log, handle.stdout_file, "stdout", stdout_error)
    stderr.close.side_effect = partial(close_log, handle.stderr_file, "stderr", stderr_error)
    faulting_handle = replace(handle, stdout_file=stdout, stderr_file=stderr)
    try:
        # 捕获注入的中断，避免错误实现把整轮 pytest 一并中止。
        with pytest.raises(BaseExceptionGroup) as failure:
            startup.close(faulting_handle)

        assert order == ["stdout", "stderr"]
        assert handle.stdout_file.closed
        assert handle.stderr_file.closed
        assert failure.value.exceptions == tuple(
            error for error in (termination_error, stdout_error, stderr_error)
            if error is not None
        )
        startup.terminate.assert_called_once_with(handle.process)
        startup.module.unregister_test_process.assert_not_called()
    finally:
        handle.stdout_file.close()
        handle.stderr_file.close()
