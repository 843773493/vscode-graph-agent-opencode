import errno
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.gateway.runtime.workspace_process import (
    resolve_backend_workspace_id,
    resolve_python_executable,
    start_local_backend_process,
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

    assert resolve_backend_workspace_id(tmp_path, tmp_path, initialize=False) == expected_id
    assert calls[1][0] == [*calls[0][0], "--read-only"]


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "message", "error_type"),
    [
        (2, "/private/stdout-marker", "/private/stderr-marker\n", "执行失败", RuntimeError),
        (
            0,
            "00000000-0000-4000-8000-000000000001\n",
            "/private/stderr-marker\n",
            "stderr 必须为空",
            RuntimeError,
        ),
        (0, "00000000-0000-4000-8000-000000000001", "", "只输出一行", TypeError),
        (
            0,
            "00000000-0000-4000-8000-000000000001\nextra\n",
            "",
            "只输出一行",
            TypeError,
        ),
        (
            0,
            "00000000-0000-4000-8000-000000000001\n",
            "unexpected output",
            "stderr 必须为空",
            RuntimeError,
        ),
        (0, "00000000-0000-4000-8000-00000000000A\n", "", "必须使用标准 UUID 文本", ValueError),
        (0, "/private/identity-marker\n", "", "必须是标准 UUID", ValueError),
    ],
)
def test_resolve_backend_workspace_id_rejects_invalid_cli_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    stderr: str,
    message: str,
    error_type: type[Exception],
    caplog: pytest.LogCaptureFixture,
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

    with pytest.raises(error_type, match=message) as caught:
        resolve_backend_workspace_id(tmp_path, tmp_path)
    assert str(tmp_path) not in str(caught.value)
    assert "/private/" not in str(caught.value)
    assert str(tmp_path) in caplog.text
    assert repr(stdout) in caplog.text
    assert repr(stderr) in caplog.text
    if returncode:
        assert f"exit={returncode}" in str(caught.value)


@pytest.mark.parametrize("scope", ["project", "workspace", "not-directory"])
def test_identity_cli_directory_failure_retains_diagnostics_without_public_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, scope: str,
) -> None:
    invalid = tmp_path / "private-root"
    if scope == "not-directory":
        invalid.write_text("file", encoding="utf-8")
    project = invalid if scope == "project" else tmp_path
    workspace = tmp_path if scope == "project" else invalid
    expected = NotADirectoryError if scope == "not-directory" else FileNotFoundError
    with pytest.raises(expected) as caught:
        resolve_backend_workspace_id(project, workspace)
    assert str(tmp_path) not in str(caught.value)
    assert "private-root" not in str(caught.value)
    assert str(invalid) in caplog.text
    if scope != "not-directory":
        assert caught.value.errno == errno.ENOENT
        assert isinstance(caught.value.__cause__, FileNotFoundError)


@pytest.mark.parametrize("failure", ["spawn", "timeout"])
def test_identity_cli_process_failure_retains_type_and_private_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, failure: str,
) -> None:
    private_command = [str(tmp_path / "private-python"), str(tmp_path / "private-root")]
    original = (
        OSError(errno.ENOENT, "missing executable", private_command[0])
        if failure == "spawn" else
        subprocess.TimeoutExpired(private_command, 30, output="private-output", stderr="private-error")
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise original

    monkeypatch.setattr("app.gateway.runtime.workspace_process.subprocess.run", fail)
    with pytest.raises(type(original)) as caught:
        resolve_backend_workspace_id(tmp_path, tmp_path)
    assert caught.value.__cause__ is original
    assert str(tmp_path) not in str(caught.value)
    assert str(tmp_path) in caplog.text
    if isinstance(original, subprocess.TimeoutExpired):
        assert caught.value.timeout == original.timeout
        assert caught.value.output is None
        assert caught.value.stderr is None
        assert "private-output" in caplog.text
        assert "private-error" in caplog.text
    else:
        assert caught.value.errno == original.errno


@pytest.mark.parametrize(
    ("timeout_seconds", "expected_cli_seconds"),
    [(0.25, "1"), (2.0, "2"), (2.01, "3")],
)
def test_workspace_backend_rounds_connection_drain_timeout_for_uvicorn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed_process_stub_factory,
    timeout_seconds: float,
    expected_cli_seconds: str,
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
        mounts=(
            MountedWorkspace("00000000-0000-4000-8000-000000000001", workspace_root),
        ),
        backend_generation="test-backend-generation",
        port=41000,
        log_dir=tmp_path / "logs",
        connection_drain_timeout_seconds=timeout_seconds,
    )
    managed.detach()

    assert calls[0][calls[0].index("-m") + 1] == "app.runtime.http_server"
    assert calls[0][-4:] == [
        "--log-level",
        "warning",
        "--timeout-graceful-shutdown",
        expected_cli_seconds,
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
    mounts = tuple(
        MountedWorkspace(f"00000000-0000-4000-8000-{index + 1:012d}", tmp_path / f"workspace-{index}")
        for index in range(2)
    )
    for mount in mounts:
        mount.workspace_root.mkdir()
    managed = start_local_backend_process(
        project_root=tmp_path,
        mounts=mounts,
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
    assert json.loads(environment["BOXTEAM_MOUNTED_WORKSPACES"]) == [mount.to_payload() for mount in mounts]
    assert environment["BOXTEAM_BACKEND_GENERATION"] == "test-backend-generation"
    for mount in mounts:
        assert json.loads((mount.workspace_root / ".boxteam/workspace-identity.json").read_text()) == {"workspace_id": mount.workspace_id}
