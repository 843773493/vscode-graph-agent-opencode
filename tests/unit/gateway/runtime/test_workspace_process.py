import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.gateway.runtime.workspace_process import (
    resolve_backend_workspace_id,
    resolve_python_executable,
    start_local_backend_process,
    start_local_node_service_process,
)


def test_resolve_python_executable_preserves_venv_symlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_PYTHON_BIN", "/workspace/.venv/bin/python")

    assert resolve_python_executable() == Path(
        "/workspace/.venv/bin/python"
    )


def test_resolve_python_executable_rejects_relative_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_PYTHON_BIN", ".venv/bin/python")

    with pytest.raises(ValueError, match="必须是绝对路径"):
        resolve_python_executable()


def test_resolve_python_executable_rejects_windows_rooted_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("BOXTEAM_PYTHON_BIN", "/workspace/python.exe")

    with pytest.raises(ValueError, match="必须是绝对路径"):
        resolve_python_executable()


def test_resolve_python_executable_accepts_windows_drive_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = r"C:\workspace\.venv\Scripts\python.exe"
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("BOXTEAM_PYTHON_BIN", configured)

    assert str(resolve_python_executable()) == configured


def test_resolve_backend_workspace_id_consumes_canonical_single_line_cli_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_id = "00000000-0000-4000-8000-000000000001"
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=f"{expected_id}\n", stderr="")

    monkeypatch.setattr(
        "app.gateway.runtime.workspace_process.resolve_python_executable",
        lambda: Path("/project/.venv/bin/python"),
    )
    monkeypatch.setattr("app.gateway.runtime.workspace_process.subprocess.run", fake_run)

    assert resolve_backend_workspace_id(tmp_path, tmp_path) == expected_id
    assert calls[0][0] == [
        "/project/.venv/bin/python",
        "-m",
        "app.core.workspace_identity",
        "--workspace-root",
        str(tmp_path.resolve()),
    ]
    assert calls[0][1]["cwd"] == tmp_path.resolve()


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "message"),
    [
        (2, "", "invalid workspace\n", "执行失败"),
        (
            0,
            "00000000-0000-4000-8000-000000000001\n",
            "warning\n",
            "stderr 必须为空",
        ),
        (0, "00000000-0000-4000-8000-000000000001", "", "只输出一行"),
        (
            0,
            "00000000-0000-4000-8000-000000000001\nextra\n",
            "",
            "只输出一行",
        ),
        (
            0,
            "00000000-0000-4000-8000-000000000001\n",
            "unexpected output",
            "stderr 必须为空",
        ),
        (0, "00000000-0000-4000-8000-00000000000A\n", "", "非标准 UUID 文本"),
    ],
)
def test_resolve_backend_workspace_id_rejects_invalid_cli_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    stderr: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        "app.gateway.runtime.workspace_process.resolve_python_executable",
        lambda: Path("/project/.venv/bin/python"),
    )
    monkeypatch.setattr(
        "app.gateway.runtime.workspace_process.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )

    with pytest.raises((RuntimeError, TypeError, ValueError), match=message):
        resolve_backend_workspace_id(tmp_path, tmp_path)


def test_workspace_backend_has_bounded_connection_drain_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed_process_stub_factory,
) -> None:
    calls: list[list[str]] = []
    process = managed_process_stub_factory(timeout_once=False)

    def fake_spawn(command: list[str], **_: object) -> object:
        calls.append(command)
        return process

    monkeypatch.setenv("BOXTEAM_PYTHON_BIN", "/workspace/.venv/bin/python")
    monkeypatch.setattr(
        "app.gateway.runtime.workspace_process._spawn_logged_process",
        fake_spawn,
    )
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    managed = start_local_backend_process(
        project_root=tmp_path,
        mounts=(MountedWorkspace("00000000-0000-4000-8000-000000000001", workspace_root),),
        backend_generation="test-backend-generation",
        port=41000,
        log_dir=tmp_path / "logs",
    )
    managed.detach()

    assert calls[0][-4:] == [
        "--log-level",
        "warning",
        "--timeout-graceful-shutdown",
        "2",
    ]


def test_local_child_process_does_not_inherit_gateway_pending_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed_process_stub_factory,
) -> None:
    captured_environments: list[dict[str, str]] = []
    process = managed_process_stub_factory(timeout_once=False)

    def fake_spawn(command: list[str], **kwargs: object) -> object:
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        captured_environments.append(environment)
        return process

    monkeypatch.setenv("BOXTEAM_PYTHON_BIN", "/workspace/.venv/bin/python")
    monkeypatch.setenv("BOXTEAM_CONFIG_CANDIDATE_REF", "gateway-candidate")
    monkeypatch.setenv("BOXTEAM_CONFIG_GENERATION", "gateway-generation")
    monkeypatch.setenv("BOXTEAM_CONFIG_FENCING_TOKEN", "gateway-fence")
    monkeypatch.setattr(
        "app.gateway.runtime.workspace_process._spawn_logged_process",
        fake_spawn,
    )
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    managed = start_local_backend_process(
        project_root=tmp_path,
        mounts=(MountedWorkspace("00000000-0000-4000-8000-000000000001", workspace_root),),
        backend_generation="test-backend-generation",
        port=41001,
        log_dir=tmp_path / "logs",
        extra_env={"WORKSPACE_ONLY_SETTING": "present"},
    )
    managed.detach()

    assert len(captured_environments) == 1
    environment = captured_environments[0]
    assert all(
        key not in environment
        for key in (
            "BOXTEAM_CONFIG_CANDIDATE_REF",
            "BOXTEAM_CONFIG_GENERATION",
            "BOXTEAM_CONFIG_FENCING_TOKEN",
        )
    )
    assert environment["WORKSPACE_ONLY_SETTING"] == "present"
    assert json.loads(environment["BOXTEAM_MOUNTED_WORKSPACES"]) == [
        MountedWorkspace("00000000-0000-4000-8000-000000000001", workspace_root).to_payload()
    ]
    assert environment["BOXTEAM_BACKEND_GENERATION"] == "test-backend-generation"


def test_both_resource_managers_receive_backend_workspace_uuid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed_process_stub_factory,
) -> None:
    project_root = tmp_path / "project"
    terminal_backend = project_root / "src/workspace-services/terminal/server/backend.js"
    browser_backend = project_root / "src/workspace-services/browser/server/backend.js"
    terminal_backend.parent.mkdir(parents=True)
    browser_backend.parent.mkdir(parents=True)
    terminal_backend.touch()
    browser_backend.touch()
    commands: list[list[str]] = []

    def fake_spawn(command: list[str], **_: object) -> object:
        commands.append(command)
        return managed_process_stub_factory(timeout_once=False)

    monkeypatch.setenv("BOXTEAM_NODE_BIN", "/node")
    monkeypatch.setattr(
        "app.gateway.runtime.workspace_process._spawn_logged_process",
        fake_spawn,
    )

    terminal = start_local_node_service_process(
        project_root=project_root,
        workspace_root=tmp_path,
        service="terminal",
        workspace_id="00000000-0000-4000-8000-000000000001",
        port=41002,
        log_dir=tmp_path / "logs",
    )
    browser = start_local_node_service_process(
        project_root=project_root,
        workspace_root=tmp_path,
        service="browser",
        workspace_id="00000000-0000-4000-8000-000000000001",
        port=41003,
        log_dir=tmp_path / "logs",
    )
    terminal.detach()
    browser.detach()

    assert commands[0][commands[0].index("--workspace-id") + 1] == (
        "00000000-0000-4000-8000-000000000001"
    )
    assert commands[1][commands[1].index("--workspace-id") + 1] == (
        "00000000-0000-4000-8000-000000000001"
    )
