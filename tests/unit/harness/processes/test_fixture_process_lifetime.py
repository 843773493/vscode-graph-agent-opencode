from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from tests.e2e.system.workspace_services.terminal import (
    test_terminal_workspace_addressing as addressing_case,
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
