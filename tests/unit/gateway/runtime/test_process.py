import json
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.gateway.runtime.local_workspace import validate_workspace_root_access
from app.gateway.runtime.process import (
    AdoptedManagedProcess,
    ManagedProcess,
    SshLocalForwardSpec,
    allocate_local_port_in_range,
    is_local_port_available,
    resolve_backend_workspace_id,
    resolve_python_executable,
    ssh_tunnel_port_range_from_env,
    start_local_backend_process,
    start_local_node_service_process,
    start_workspace_ssh_port_forward_process,
)


class _LogFile:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Process:
    pid = 4321

    def __init__(self, *, timeout_once: bool) -> None:
        self.returncode = None
        self._timeout_once = timeout_once
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.wait_calls += 1
        if self._timeout_once and self.wait_calls == 1:
            raise subprocess.TimeoutExpired("test", timeout)
        self.returncode = 0
        return 0


def test_resolve_python_executable_preserves_venv_symlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_PYTHON_BIN", "/workspace/.venv/bin/python")

    assert resolve_python_executable(Path("/workspace")) == Path(
        "/workspace/.venv/bin/python"
    )


def test_resolve_python_executable_rejects_relative_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_PYTHON_BIN", ".venv/bin/python")

    with pytest.raises(ValueError, match="必须是绝对路径"):
        resolve_python_executable(Path("/workspace"))


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
        "app.gateway.runtime.process.resolve_python_executable",
        lambda _: Path("/project/.venv/bin/python"),
    )
    monkeypatch.setattr("app.gateway.runtime.process.subprocess.run", fake_run)

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
        (0, "00000000-0000-4000-8000-000000000001\n", "warning\n", "stderr 必须为空"),
        (0, "00000000-0000-4000-8000-000000000001", "", "只输出一行"),
        (0, "00000000-0000-4000-8000-000000000001\nextra\n", "", "只输出一行"),
        (0, "00000000-0000-4000-8000-000000000001\n", "unexpected output", "stderr 必须为空"),
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
        "app.gateway.runtime.process.resolve_python_executable",
        lambda _: Path("/project/.venv/bin/python"),
    )
    monkeypatch.setattr(
        "app.gateway.runtime.process.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )

    with pytest.raises((RuntimeError, TypeError, ValueError), match=message):
        resolve_backend_workspace_id(tmp_path, tmp_path)


def test_validate_workspace_root_access_rejects_unwritable_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.os.access",
        lambda *_: False,
    )

    with pytest.raises(PermissionError, match=r"无法创建 \.boxteam"):
        validate_workspace_root_access(workspace_root)


def test_validate_workspace_root_access_rejects_unwritable_metadata_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    metadata_root = workspace_root / ".boxteam"
    metadata_root.mkdir(parents=True)
    calls: list[Path] = []

    def fake_access(path: Path, _: int) -> bool:
        calls.append(path)
        return path != metadata_root

    monkeypatch.setattr("app.gateway.runtime.local_workspace.os.access", fake_access)

    with pytest.raises(PermissionError, match="保存会话和服务状态"):
        validate_workspace_root_access(workspace_root)
    assert calls == [workspace_root, metadata_root]


def test_ssh_tunnel_port_range_reads_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MIN", "43000")
    monkeypatch.setenv("BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MAX", "43010")

    assert ssh_tunnel_port_range_from_env() == (43000, 43010)


def test_ssh_tunnel_port_range_rejects_invalid_order(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MIN", "43010")
    monkeypatch.setenv("BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MAX", "43000")

    with pytest.raises(ValueError, match="不能大于"):
        ssh_tunnel_port_range_from_env()


def test_allocate_local_port_in_range_skips_occupied_port():
    first_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        first_socket.bind(("127.0.0.1", 0))
        occupied_port = int(first_socket.getsockname()[1])
        free_port = None
        for candidate in (occupied_port - 1, occupied_port + 1):
            if candidate < 1 or candidate > 65535:
                continue
            probe_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe_socket.bind(("127.0.0.1", candidate))
                free_port = candidate
                break
            except OSError:
                continue
            finally:
                probe_socket.close()
        if free_port is None:
            pytest.skip("没有找到相邻可用端口用于范围分配测试")

        assert (
            allocate_local_port_in_range(
                min(occupied_port, free_port),
                max(occupied_port, free_port),
            )
            == free_port
        )
    finally:
        first_socket.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 端口复用语义")
def test_port_availability_probe_allows_reuse_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int]] = []

    class _ProbeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def setsockopt(self, level: int, option: int, value: int) -> None:
            calls.append((level, option, value))

        def bind(self, address: tuple[str, int]) -> None:
            assert address == ("127.0.0.1", 8010)

    monkeypatch.setattr(socket, "socket", lambda *_: _ProbeSocket())

    assert is_local_port_available(8010) is True
    assert calls == [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组信号仅在 POSIX 上可用")
def test_managed_process_closes_posix_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(timeout_once=False)
    log_file = _LogFile()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "app.gateway.runtime.process._posix_group_is_alive",
        lambda _pid, _starttime: process.poll() is None,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.process.os.killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    ManagedProcess(process=process, log_file=log_file).close()

    assert signals == [(4321, 15)]
    assert log_file.closed is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组信号仅在 POSIX 上可用")
def test_managed_process_kills_group_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(timeout_once=True)
    log_file = _LogFile()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "app.gateway.runtime.process._posix_group_is_alive",
        lambda _pid, _starttime: process.poll() is None,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.process.os.killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    managed = ManagedProcess(process=process, log_file=log_file)
    with pytest.raises(TimeoutError, match="未完成优雅关闭") as raised:
        managed.close(timeout_seconds=0.01)

    assert signals == [(4321, 15), (4321, 9)]
    assert process.wait_calls == 2
    assert managed.close_failure is raised.value
    assert isinstance(raised.value.__cause__, subprocess.TimeoutExpired)
    assert log_file.closed is False
    with pytest.raises(TimeoutError) as retried:
        managed.close(timeout_seconds=0.01)
    assert retried.value is raised.value
    assert process.wait_calls == 2


def test_managed_process_windows_stops_complete_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(timeout_once=False)
    log_file = _LogFile()
    calls: list[list[str]] = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        "app.gateway.runtime.process.subprocess.run",
        lambda arguments, **_: (
            calls.append(arguments) or SimpleNamespace(returncode=0, stderr="")
        ),
    )

    ManagedProcess(process=process, log_file=log_file).close()

    assert calls == [["taskkill", "/T", "/PID", "4321"]]
    assert log_file.closed is True


def test_managed_process_windows_force_kills_complete_process_tree_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(timeout_once=True)
    log_file = _LogFile()
    calls: list[list[str]] = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        "app.gateway.runtime.process.subprocess.run",
        lambda arguments, **_: (
            calls.append(arguments) or SimpleNamespace(returncode=0, stderr="")
        ),
    )

    managed = ManagedProcess(process=process, log_file=log_file)
    with pytest.raises(TimeoutError, match="未完成优雅关闭") as raised:
        managed.close(timeout_seconds=0.01)

    assert calls == [
        ["taskkill", "/T", "/PID", "4321"],
        ["taskkill", "/T", "/F", "/PID", "4321"],
    ]
    assert process.wait_calls == 2
    assert managed.close_failure is raised.value
    assert isinstance(raised.value.__cause__, subprocess.TimeoutExpired)
    assert log_file.closed is False
    with pytest.raises(TimeoutError) as retried:
        managed.close(timeout_seconds=0.01)
    assert retried.value is raised.value
    assert process.wait_calls == 2


def test_adopted_process_windows_checks_liveness_without_spawning_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_pids: list[int] = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        psutil,
        "pid_exists",
        lambda pid: checked_pids.append(pid) or True,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.process.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("进程探活不得启动外部命令"),
    )

    assert AdoptedManagedProcess(pid=4321)._is_alive() is True
    assert checked_pids == [4321]


def test_adopted_process_windows_reports_missing_pid_as_not_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(psutil, "pid_exists", lambda _pid: False)

    assert AdoptedManagedProcess(pid=4321)._is_alive() is False


def test_adopted_process_windows_surfaces_process_query_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_query(_pid: int) -> bool:
        raise psutil.AccessDenied(pid=4321)

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        psutil,
        "pid_exists",
        fail_query,
    )

    with pytest.raises(RuntimeError, match="Windows 进程状态查询失败") as raised:
        AdoptedManagedProcess(pid=4321)._is_alive()

    assert isinstance(raised.value.__cause__, psutil.AccessDenied)


def test_workspace_backend_has_bounded_connection_drain_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    process = _Process(timeout_once=False)

    def fake_spawn(command: list[str], **_: object) -> _Process:
        calls.append(command)
        return process

    monkeypatch.setenv("BOXTEAM_PYTHON_BIN", "/workspace/.venv/bin/python")
    monkeypatch.setattr(
        "app.gateway.runtime.process._spawn_logged_process",
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
) -> None:
    captured_environments: list[dict[str, str]] = []
    process = _Process(timeout_once=False)

    def fake_spawn(command: list[str], **kwargs: object) -> _Process:
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        captured_environments.append(environment)
        return process

    monkeypatch.setenv("BOXTEAM_PYTHON_BIN", "/workspace/.venv/bin/python")
    monkeypatch.setenv("BOXTEAM_CONFIG_CANDIDATE_REF", "gateway-candidate")
    monkeypatch.setenv("BOXTEAM_CONFIG_GENERATION", "gateway-generation")
    monkeypatch.setenv("BOXTEAM_CONFIG_FENCING_TOKEN", "gateway-fence")
    monkeypatch.setattr(
        "app.gateway.runtime.process._spawn_logged_process",
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
) -> None:
    project_root = tmp_path / "project"
    terminal_backend = project_root / "src/workspace-services/terminal/server/backend.js"
    browser_backend = project_root / "src/workspace-services/browser/server/backend.js"
    terminal_backend.parent.mkdir(parents=True)
    browser_backend.parent.mkdir(parents=True)
    terminal_backend.touch()
    browser_backend.touch()
    commands: list[list[str]] = []

    def fake_spawn(command: list[str], **_: object) -> _Process:
        commands.append(command)
        return _Process(timeout_once=False)

    monkeypatch.setenv("BOXTEAM_NODE_BIN", "/node")
    monkeypatch.setattr(
        "app.gateway.runtime.process._spawn_logged_process",
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


def test_workspace_ssh_port_forward_reuses_alias_and_long_lived_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    process = _Process(timeout_once=False)
    monkeypatch.setattr(
        "app.gateway.runtime.process.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="hostname server.example.com\nproxyjump bastion\n",
            stderr="",
        ),
    )

    def fake_spawn(command: list[str], **_: object) -> _Process:
        commands.append(command)
        return process

    monkeypatch.setattr(
        "app.gateway.runtime.process._spawn_logged_process",
        fake_spawn,
    )

    managed, _ = start_workspace_ssh_port_forward_process(
        host="server.example.com",
        port=22,
        username="developer",
        private_key_path=None,
        ssh_config_host="development-alias",
        forward=SshLocalForwardSpec(
            local_port=41234,
            remote_host="127.0.0.1",
            remote_port=5173,
        ),
        log_dir=tmp_path / "logs",
        forward_id="pf_test",
    )
    managed.detach()

    command = commands[0]
    assert command[-1] == "development-alias"
    assert "-N" in command
    assert "-T" in command
    assert "127.0.0.1:41234:127.0.0.1:5173" in command
    assert "ExitOnForwardFailure=yes" in command
    assert "ServerAliveInterval=15" in command
    assert "ServerAliveCountMax=3" in command


def test_workspace_ssh_port_forward_rejects_configured_local_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.gateway.runtime.process.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "hostname server.example.com\n"
                "proxyjump bastion\n"
                "localforward 3000 [127.0.0.1]:3000\n"
            ),
            stderr="",
        ),
    )

    with pytest.raises(ValueError, match="专用 Host 别名"):
        start_workspace_ssh_port_forward_process(
            host="server.example.com",
            port=22,
            username="developer",
            private_key_path=None,
            ssh_config_host="development-alias",
            forward=SshLocalForwardSpec(
                local_port=41234,
                remote_host="127.0.0.1",
                remote_port=5173,
            ),
            log_dir=tmp_path / "logs",
            forward_id="pf_test",
        )
