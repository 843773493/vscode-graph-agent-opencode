from __future__ import annotations

import subprocess
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from tests.e2e.system.workspace_services.terminal import (
    test_terminal_workspace_addressing as addressing_case,
)
from tests.integration.workspace_services.browser import (
    test_browser_resource_performance as performance_case,
)


@dataclass
class DualTerminalStartup:
    fixture: Generator[list[tuple[str, object, Path]], None, None]
    services: list[Mock]
    start: Mock
    stop: Mock


@pytest.fixture
def dual_terminal_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> DualTerminalStartup:
    services = [Mock(name=name) for name in ("plugin-a", "plugin-b")]
    context = Mock()
    context.workspace_root = tmp_path / "workspace"
    context.artifacts_dir = tmp_path / "artifacts"
    context.prepare.return_value = context
    monkeypatch.setattr(addressing_case, "TestRunContext", SimpleNamespace(
        from_test_file=Mock(return_value=context),
    ))
    monkeypatch.setattr(addressing_case, "prepare_test_workspace", Mock())
    monkeypatch.setattr(addressing_case, "load_or_create_workspace_id", lambda root: f"workspace-{root.name}")
    start = Mock(side_effect=services)
    stop = Mock()
    monkeypatch.setattr(addressing_case, "start_terminal_plugin_service", start)
    monkeypatch.setattr(addressing_case, "stop_terminal_plugin_service", stop)
    request = Mock()
    request.node.path = Path(__file__)
    fixture = addressing_case.terminal_mounts._get_wrapped_function()(request=request, e2e_backend_port=18000)
    return DualTerminalStartup(
        fixture, services, start, stop,
    )


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("stage", ["complete", "second-service-start"])
def test_dual_terminal_fixture_attempts_all_owned_cleanup_after_error(
    dual_terminal_startup: DualTerminalStartup, stage: str, interrupted: bool,
) -> None:
    startup = dual_terminal_startup
    startup_error = RuntimeError("第二组 Terminal 创建失败")
    cleanup_error = KeyboardInterrupt("清理中断") if interrupted else RuntimeError("清理失败")
    owned = startup.services
    if stage == "second-service-start":
        startup.start.side_effect = [startup.services[0], startup_error]
        owned = startup.services[:1]

    def stop(service: Mock) -> None:
        if service is owned[-1]:
            raise cleanup_error

    startup.stop.side_effect = stop
    with pytest.raises(type(cleanup_error)) as failure:
        if stage == "complete":
            assert len(next(startup.fixture)) == 2
            startup.fixture.close()
        else:
            next(startup.fixture)

    assert failure.value is cleanup_error
    assert startup.stop.call_args_list == [call(service) for service in reversed(owned)]
    if stage != "complete":
        assert failure.value.__context__ is startup_error


@pytest.fixture
def subprocess_startup(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock]:
    process = Mock(spec=subprocess.Popen)
    spawn = Mock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    return process, spawn


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_backend_readiness_failure_cleans_owned_process_and_preserves_errors(
    interrupted: bool,
    cleanup_fails: bool,
    subprocess_startup: tuple[Mock, Mock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, _spawn = subprocess_startup
    startup_error = KeyboardInterrupt("启动中断") if interrupted else RuntimeError("就绪失败")
    cleanup_error = RuntimeError("清理失败")
    terminate = Mock(side_effect=cleanup_error if cleanup_fails else None)
    monkeypatch.setattr(performance_case, "ensure_local_port_available", Mock())
    monkeypatch.setattr(performance_case, "load_or_create_workspace_id", lambda _root: "workspace-fixture")
    monkeypatch.setattr(performance_case, "wait_for_http_ok", Mock(side_effect=startup_error))
    monkeypatch.setattr(performance_case, "terminate_process", terminate)

    expected_error = BaseExceptionGroup if cleanup_fails else type(startup_error)
    with pytest.raises(expected_error) as failure:
        performance_case._browser_backend_process(
            workspace_root=tmp_path, backend_port=18040, log_path=tmp_path / "browser.log",
        )

    if cleanup_fails:
        assert isinstance(failure.value, BaseExceptionGroup)
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
