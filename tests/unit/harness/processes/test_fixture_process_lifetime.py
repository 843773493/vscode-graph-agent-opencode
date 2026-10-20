from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call

import pytest

from tests.e2e.system.workspace_services.terminal import (
    test_persistent_terminal_resources as persistent_case,
)
from tests.e2e.system.workspace_services.terminal import (
    test_terminal_powershell as windows_case,
)
from tests.e2e.system.workspace_services.terminal import (
    test_terminal_workspace_addressing as addressing_case,
)
from tests.harness.python import terminal_backend
from tests.integration.workspace_services.browser import (
    test_browser_custom_tools as browser_case,
)
from tests.integration.workspace_services.browser import (
    test_browser_resource_performance as performance_case,
)


@dataclass
class DualTerminalStartup:
    fixture: Iterator[list[tuple[str, int, Path]]]
    processes: list[Mock]
    backend_spawn: Mock
    frontend_spawn: Mock
    readiness: Mock
    terminate: Mock
    frontend_results: list[tuple[Mock, tuple[Path, Path]]]


@pytest.fixture
def dual_terminal_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> DualTerminalStartup:
    processes = [Mock(name=name) for name in ("backend-a", "frontend-a", "backend-b", "frontend-b")]
    context = Mock()
    context.workspace_root = tmp_path / "workspace"
    context.artifacts_dir = tmp_path / "artifacts"
    context.prepare.return_value = context
    monkeypatch.setattr(addressing_case, "TestRunContext", SimpleNamespace(
        from_test_file=Mock(return_value=context),
    ))
    monkeypatch.setattr(addressing_case, "prepare_default_test_workspace", Mock())
    monkeypatch.setattr(addressing_case, "load_or_create_workspace_id", lambda root: f"workspace-{root.name}")
    monkeypatch.setattr(addressing_case.shutil, "which", lambda _name: "node")
    backend_spawn = Mock(side_effect=[processes[0], processes[2]])
    frontend_results = [
        (process, (tmp_path / f"{name}.stdout.log", tmp_path / f"{name}.stderr.log"))
        for process, name in [(processes[1], "a"), (processes[3], "b")]
    ]
    frontend_spawn = Mock(side_effect=frontend_results)
    readiness = Mock()
    terminate = Mock()
    monkeypatch.setattr(addressing_case, "start_terminal_backend", backend_spawn)
    monkeypatch.setattr(addressing_case, "spawn_logged_process", frontend_spawn)
    monkeypatch.setattr(addressing_case, "wait_for_http_ok", readiness)
    monkeypatch.setattr(addressing_case, "terminate_process", terminate)
    request = Mock()
    request.node.path = Path(__file__)
    fixture = addressing_case.terminal_mounts.__wrapped__(request, 18000)
    return DualTerminalStartup(
        fixture, processes, backend_spawn, frontend_spawn, readiness, terminate, frontend_results,
    )


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("stage", ["complete", "backend-spawn", "frontend-spawn", "readiness"])
def test_dual_terminal_fixture_attempts_all_owned_cleanup_after_error(
    dual_terminal_startup: DualTerminalStartup, stage: str, interrupted: bool,
) -> None:
    startup = dual_terminal_startup
    startup_error = RuntimeError("第二组 Terminal 创建失败")
    cleanup_error = KeyboardInterrupt("清理中断") if interrupted else RuntimeError("清理失败")
    owned = startup.processes
    if stage == "backend-spawn":
        startup.backend_spawn.side_effect = [startup.processes[0], startup_error]
        owned = startup.processes[:2]
    elif stage == "frontend-spawn":
        startup.frontend_spawn.side_effect = [startup.frontend_results[0], startup_error]
        owned = startup.processes[:3]
    elif stage == "readiness":
        startup.readiness.side_effect = [None, startup_error]

    def terminate(process: Mock) -> None:
        if process is owned[-1]:
            raise cleanup_error

    startup.terminate.side_effect = terminate
    with pytest.raises(type(cleanup_error)) as failure:
        if stage == "complete":
            assert len(next(startup.fixture)) == 2
            startup.fixture.close()
        else:
            next(startup.fixture)

    assert failure.value is cleanup_error
    assert startup.terminate.call_args_list == [call(process) for process in reversed(owned)]
    if stage != "complete":
        assert failure.value.__context__ is startup_error


@pytest.fixture
def subprocess_startup(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock]:
    process = Mock(spec=subprocess.Popen)
    spawn = Mock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(terminal_backend.shutil, "which", lambda _name: "node")
    return process, spawn


@pytest.mark.parametrize("service", ["terminal", "browser-performance"])
@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_backend_readiness_failure_cleans_owned_process_and_preserves_errors(
    service: str,
    interrupted: bool,
    cleanup_fails: bool,
    subprocess_startup: tuple[Mock, Mock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, _spawn = subprocess_startup
    module = terminal_backend if service == "terminal" else performance_case
    startup_error = KeyboardInterrupt("启动中断") if interrupted else RuntimeError("就绪失败")
    cleanup_error = RuntimeError("清理失败")
    terminate = Mock(side_effect=cleanup_error if cleanup_fails else None)
    monkeypatch.setattr(module, "ensure_local_port_available", Mock())
    monkeypatch.setattr(module, "load_or_create_workspace_id", lambda _root: "workspace-fixture")
    monkeypatch.setattr(module, "wait_for_http_ok", Mock(side_effect=startup_error))
    monkeypatch.setattr(module, "terminate_process", terminate)

    expected_error = BaseExceptionGroup if cleanup_fails else type(startup_error)
    with pytest.raises(expected_error) as failure:
        if service == "terminal":
            terminal_backend.start_terminal_backend(backend_port=18040, workspace_root=str(tmp_path), artifacts_dir=tmp_path / "artifacts")
        else:
            performance_case._browser_backend_process(
                workspace_root=tmp_path, backend_port=18040, log_path=tmp_path / "browser.log",
            )

    if cleanup_fails:
        assert failure.value.exceptions == (startup_error, cleanup_error)
    else:
        assert failure.value is startup_error
    terminate.assert_called_once_with(process)


def test_browser_spawn_failure_closes_opened_log(
    subprocess_startup: tuple[Mock, Mock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _process, spawn = subprocess_startup
    spawn.side_effect = RuntimeError("进程创建失败")
    monkeypatch.setattr(performance_case, "ensure_local_port_available", Mock())
    monkeypatch.setattr(performance_case, "load_or_create_workspace_id", lambda _root: "workspace-fixture")

    with pytest.raises(RuntimeError, match="进程创建失败"):
        performance_case._browser_backend_process(
            workspace_root=tmp_path, backend_port=18040, log_path=tmp_path / "browser.log",
        )
    assert spawn.call_args.kwargs["stdout"].closed


def test_terminal_frontend_spawn_failure_closes_started_backend(
    subprocess_startup: tuple[Mock, Mock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, spawn = subprocess_startup
    spawn.side_effect = RuntimeError("前端创建失败")
    terminate = Mock()
    monkeypatch.setattr(persistent_case, "ensure_local_port_available", Mock())
    monkeypatch.setattr(persistent_case, "start_terminal_backend", Mock(return_value=process))
    monkeypatch.setattr(persistent_case, "terminate_process", terminate)
    fixture = persistent_case.terminal_manager_processes.__wrapped__((18040, 18041), str(tmp_path))

    with pytest.raises(RuntimeError, match="前端创建失败"):
        next(fixture)
    terminate.assert_called_once_with(process)


def test_terminal_spawn_failure_closes_started_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = Mock()
    close_browser = Mock()
    monkeypatch.setattr(browser_case, "start_browser_manager_processes", Mock(return_value=browser))
    monkeypatch.setattr(browser_case, "close_browser_manager_processes", close_browser)
    monkeypatch.setattr(browser_case, "start_terminal_backend", Mock(side_effect=RuntimeError("终端创建失败")))
    fixture = browser_case.browser_manager_processes.__wrapped__((18040, 18041), str(tmp_path))

    with pytest.raises(RuntimeError, match="终端创建失败"):
        next(fixture)
    close_browser.assert_called_once_with(browser)


@pytest.mark.parametrize("stage", ["complete", "frontend-spawn-failure", "health-failure"])
def test_windows_fixture_releases_created_processes_and_logs(
    stage: str,
    subprocess_startup: tuple[Mock, Mock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, spawn = subprocess_startup
    frontend = Mock()
    spawn.side_effect = (
        [backend, RuntimeError("前端创建失败")]
        if stage == "frontend-spawn-failure" else [backend, frontend]
    )
    terminate = Mock()
    monkeypatch.setattr(windows_case, "os", SimpleNamespace(name="nt", environ=os.environ.copy()))
    monkeypatch.setattr(windows_case.shutil, "which", lambda name: name)
    monkeypatch.setattr(windows_case, "ensure_local_port_available", Mock())
    monkeypatch.setattr(windows_case, "load_or_create_workspace_id", lambda _root: "workspace-fixture")
    monkeypatch.setattr(windows_case, "terminate_process", terminate)
    monkeypatch.setattr(
        windows_case, "_wait_for_health",
        Mock(side_effect=RuntimeError("健康检查失败") if stage == "health-failure" else None),
    )
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({"workspace_id": "workspace-fixture"}).encode()
    monkeypatch.setattr(windows_case, "urlopen", Mock(return_value=response))
    fixture = windows_case.windows_terminal_backend.__wrapped__(str(tmp_path), 18000)

    if stage == "complete":
        result = next(fixture)
        assert result.frontend_port == result.port + 1
        fixture.close()
    else:
        with pytest.raises(RuntimeError):
            next(fixture)

    expected = [call(backend)] if stage == "frontend-spawn-failure" else [call(frontend), call(backend)]
    assert terminate.call_args_list == expected
    for spawned in spawn.call_args_list:
        assert spawned.kwargs["stdout"].closed
        assert spawned.kwargs["stderr"].closed
