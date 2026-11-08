from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal, TextIO
from unittest.mock import Mock

import pytest

from tests.harness.python import (
    browser_manager,
    process_output,
)
from tests.harness.python.browser_manager import (
    BrowserManagerProcesses,
    close_browser_manager_processes,
)


@dataclass
class AuxiliaryLogStartup:
    start: Callable[
        [],
        browser_manager.BrowserManagerProcesses
        | browser_manager.BrowserFrontendProcess,
    ]
    close: Callable[..., None]
    spawn: Mock
    ready: Mock
    terminate: Mock
    process_count: int
    workspace_root: Path
    artifacts_dir: Path


@pytest.fixture(params=["manager", "frontend"])
def auxiliary_log_startup(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AuxiliaryLogStartup:
    workspace_root = tmp_path / "workspaces" / "nested" / "workspace"
    artifacts_dir = tmp_path / "artifacts"
    processes = [Mock(spec=subprocess.Popen) for _ in range(4)]
    spawn = Mock(side_effect=processes)
    ready = Mock()
    terminate = Mock()
    module = browser_manager
    monkeypatch.setattr(module, "ensure_local_port_available", Mock())
    monkeypatch.setattr(module.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(module, "load_or_create_workspace_id", lambda _root: "fixture-id")
    monkeypatch.setattr(process_output.subprocess, "Popen", spawn)
    monkeypatch.setattr(module, "wait_for_http_ok", ready)
    monkeypatch.setattr(module, "terminate_process", terminate)
    if request.param == "manager":
        start = partial(
            browser_manager.start_browser_manager_processes,
            workspace_root=workspace_root, backend_port=18040,
            frontend_port=18041, backend_host="127.0.0.1",
            artifacts_dir=artifacts_dir,
        )
        close = browser_manager.close_browser_manager_processes
        process_count = 2
    elif request.param == "frontend":
        start = partial(
            browser_manager.start_browser_frontend_process,
            workspace_root=workspace_root, frontend_port=18041,
            artifacts_dir=artifacts_dir,
        )
        close = browser_manager.close_browser_frontend_process
        process_count = 1
    return AuxiliaryLogStartup(
        start, close, spawn, ready, terminate, process_count, workspace_root, artifacts_dir,
    )


def test_auxiliary_logs_follow_explicit_artifact_owner_for_nested_workspace(
    auxiliary_log_startup: AuxiliaryLogStartup,
) -> None:
    handle = auxiliary_log_startup.start()
    try:
        assert auxiliary_log_startup.spawn.call_count == auxiliary_log_startup.process_count
        for call in auxiliary_log_startup.spawn.call_args_list:
            for channel in ("stdout", "stderr"):
                assert Path(call.kwargs[channel].name).parent == auxiliary_log_startup.artifacts_dir
        assert not (auxiliary_log_startup.workspace_root.parent / "artifacts").exists()
    finally:
        auxiliary_log_startup.close(handle)


def test_auxiliary_spawn_failure_closes_logs_and_keeps_error(auxiliary_log_startup: AuxiliaryLogStartup) -> None:
    error = OSError("模拟创建失败")
    auxiliary_log_startup.spawn.side_effect = error
    with pytest.raises(OSError) as failure:
        auxiliary_log_startup.start()
    assert failure.value is error
    assert auxiliary_log_startup.spawn.call_args.kwargs["stdout"].closed
    assert auxiliary_log_startup.spawn.call_args.kwargs["stderr"].closed
    assert any(".stdout.log" in note for note in error.__notes__)
    auxiliary_log_startup.ready.assert_not_called()
    auxiliary_log_startup.terminate.assert_not_called()


def test_auxiliary_stderr_open_failure_releases_stdout(
    auxiliary_log_startup: AuxiliaryLogStartup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[TextIO] = []
    error = OSError("模拟错误日志打开失败")
    real_open = open

    def open_log(path: Path, mode: Literal["w"], *, encoding: str) -> TextIO:
        if path.name.endswith(".stderr.log"):
            raise error
        stream = real_open(path, mode, encoding=encoding)
        opened.append(stream)
        return stream

    monkeypatch.setattr(process_output, "open", open_log, raising=False)
    with pytest.raises(OSError) as failure:
        auxiliary_log_startup.start()
    assert failure.value is error
    assert len(opened) == 1 and opened[0].closed
    auxiliary_log_startup.spawn.assert_not_called()
    auxiliary_log_startup.terminate.assert_not_called()


def test_auxiliary_readiness_failure_preserves_logs_and_cleanup(auxiliary_log_startup: AuxiliaryLogStartup) -> None:
    error = KeyboardInterrupt("模拟就绪中断")
    auxiliary_log_startup.ready.side_effect = error
    with pytest.raises(KeyboardInterrupt) as failure:
        auxiliary_log_startup.start()
    assert failure.value is error
    assert len(error.__notes__) == auxiliary_log_startup.process_count * 2
    assert auxiliary_log_startup.terminate.call_count == auxiliary_log_startup.process_count
    for call in auxiliary_log_startup.spawn.call_args_list:
        assert call.kwargs["stdout"].closed and call.kwargs["stderr"].closed
        assert Path(call.kwargs["stdout"].name).exists()
        assert Path(call.kwargs["stderr"].name).exists()


def test_auxiliary_repeated_start_never_overwrites_logs(auxiliary_log_startup: AuxiliaryLogStartup) -> None:
    previous_paths: set[Path] = set()
    for _ in range(2):
        handle = auxiliary_log_startup.start()
        auxiliary_log_startup.close(handle)
        current_paths: set[Path] = set()
        for call in auxiliary_log_startup.spawn.call_args_list[-auxiliary_log_startup.process_count:]:
            for channel in ("stdout", "stderr"):
                assert call.kwargs[channel].closed
                path = Path(call.kwargs[channel].name)
                current_paths.add(path)
                path.write_text("保留原运行日志", encoding="utf-8")
        assert current_paths.isdisjoint(previous_paths)
        assert all(path.read_text(encoding="utf-8") == "保留原运行日志" for path in previous_paths)
        previous_paths.update(current_paths)
    assert len(previous_paths) == auxiliary_log_startup.process_count * 4


@dataclass
class AuxiliaryLogCloseFailure:
    error: BaseException
    streams: list[TextIO]


@pytest.fixture(params=[
    ("stdout", OSError), ("stderr", OSError),
    ("stdout", KeyboardInterrupt), ("stderr", KeyboardInterrupt),
])
def auxiliary_log_close_failure(
    request: pytest.FixtureRequest,
    auxiliary_log_startup: AuxiliaryLogStartup,
    monkeypatch: pytest.MonkeyPatch,
) -> AuxiliaryLogCloseFailure:
    channel, failure_type = request.param
    error = failure_type("模拟父进程关闭日志失败")
    streams: list[TextIO] = []
    real_open = open

    @contextmanager
    def open_log(path: Path, mode: Literal["w"], *, encoding: str) -> Iterator[TextIO]:
        with real_open(path, mode, encoding=encoding) as stream:
            streams.append(stream)
            try:
                yield stream
            finally:
                if path.name.endswith(f".{channel}.log"):
                    stream.close()
                    raise error

    auxiliary_log_startup.spawn.return_value = next(auxiliary_log_startup.spawn.side_effect)
    auxiliary_log_startup.spawn.side_effect = None
    monkeypatch.setattr(process_output, "open", open_log, raising=False)
    monkeypatch.setattr(
        process_output, "terminate_process", auxiliary_log_startup.terminate, raising=False,
    )
    return AuxiliaryLogCloseFailure(error, streams)


def test_log_close_failure_reclaims_process_before_handle_publication(
    auxiliary_log_startup: AuxiliaryLogStartup,
    auxiliary_log_close_failure: AuxiliaryLogCloseFailure,
) -> None:
    with pytest.raises(type(auxiliary_log_close_failure.error)) as failure:
        auxiliary_log_startup.start()

    assert failure.value is auxiliary_log_close_failure.error
    auxiliary_log_startup.spawn.assert_called_once()
    auxiliary_log_startup.terminate.assert_called_once_with(
        auxiliary_log_startup.spawn.return_value,
    )
    auxiliary_log_startup.ready.assert_not_called()
    assert len(auxiliary_log_close_failure.streams) == 2
    assert all(stream.closed for stream in auxiliary_log_close_failure.streams)
    assert any(".stdout.log" in note for note in failure.value.__notes__)
    assert any(".stderr.log" in note for note in failure.value.__notes__)


def test_log_close_and_process_cleanup_failures_keep_original_error_chain(
    auxiliary_log_startup: AuxiliaryLogStartup,
    auxiliary_log_close_failure: AuxiliaryLogCloseFailure,
) -> None:
    cleanup_error = RuntimeError("模拟未移交进程回收失败")
    auxiliary_log_startup.terminate.side_effect = cleanup_error
    with pytest.raises(BaseException) as failure:
        auxiliary_log_startup.start()

    assert failure.value is cleanup_error
    assert failure.value.__context__ is auxiliary_log_close_failure.error
    auxiliary_log_startup.terminate.assert_called_once_with(
        auxiliary_log_startup.spawn.return_value,
    )
    auxiliary_log_startup.ready.assert_not_called()
    assert all(stream.closed for stream in auxiliary_log_close_failure.streams)


def test_browser_manager_close_reports_all_owned_cleanup_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend_process = object()
    backend_process = object()
    attempted: list[object] = []

    def fail_cleanup(process: object) -> None:
        attempted.append(process)
        raise RuntimeError(f"模拟清理失败 {len(attempted)}")

    monkeypatch.setattr(browser_manager, "terminate_process", fail_cleanup)
    with pytest.raises(BaseExceptionGroup) as failure:
        close_browser_manager_processes(
            BrowserManagerProcesses(
                backend_process=backend_process,  # type: ignore[arg-type]
                frontend_process=frontend_process,  # type: ignore[arg-type]
                backend_port=18041,
                frontend_port=18042,
            )
        )

    assert attempted == [frontend_process, backend_process]
    assert [str(error) for error in failure.value.exceptions] == [
        "模拟清理失败 1",
        "模拟清理失败 2",
    ]
