from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import pytest

from tests.e2e.system.gateway import gateway_target
from tests.e2e.system.gateway.gateway_target import (
    GatewaySshTarget,
    start_remote_gateway_via_ssh,
    stop_remote_gateway,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX 进程命令必须在 Linux 或 macOS 上运行",
)


@pytest.fixture
def posix_gateway_platform(request: pytest.FixtureRequest) -> Literal["linux", "macos"]:
    return cast(Literal["linux", "macos"], request.param)


@pytest.fixture
def posix_gateway_case(
    monkeypatch: pytest.MonkeyPatch,
    posix_gateway_platform: Literal["linux", "macos"],
    request: pytest.FixtureRequest,
) -> PosixGatewayCase:
    context = TestRunContext.from_test_file(Path(__file__).resolve()).prepare()
    runtime_root = context.runtime_root_for_node(request.node.nodeid)
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    runtime_root.mkdir(parents=True)
    run_root = context.workspace_root / runtime_root.parent.name / runtime_root.name
    repository_root = run_root / "O'Neil repository"
    remote_workspace = run_root / "O'Neil workspace"
    boxteam_home = runtime_root / "O'Neil boxteam home"
    repository_root.mkdir(parents=True, exist_ok=True)

    for relative_path in (
        Path("configs/gateway_inline.jsonc"),
        Path("configs/gateway_schema.jsonc"),
        Path("configs/workspace_inline.jsonc"),
        Path("configs/workspace_schema.jsonc"),
        Path("package.json"),
    ):
        destination = repository_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path.cwd() / relative_path, destination)

    package = json.loads((repository_root / "package.json").read_text(encoding="utf-8"))
    capture_path = runtime_root / f"gateway-{posix_gateway_platform}.json"
    uvicorn = repository_root / "uvicorn"
    uvicorn.mkdir()
    (uvicorn / "__init__.py").write_text("", encoding="utf-8")
    (uvicorn / "__main__.py").write_text(
        """\
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

port = int(sys.argv[sys.argv.index('--port') + 1])
capture = Path(os.environ['BOXTEAM_TEST_GATEWAY_CAPTURE'])
names = (
    'BOXTEAM_HOME', 'BOXTEAM_GATEWAY_ROOT', 'BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT',
    'BOXTEAM_RUNTIME_MANIFEST', 'BOXTEAM_PYTHON_BIN', 'BOXTEAM_NODE_BIN',
    'PYTHONUNBUFFERED',
)
capture.write_text(json.dumps({
    'argv': sys.argv,
    'environment': {name: os.environ.get(name) for name in names},
}), encoding='utf-8')

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == '/api/gateway/health' else 404)
        self.end_headers()

    def log_message(self, *_args):
        pass

ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
""",
        encoding="utf-8",
    )

    python_environment = repository_root / ".venv"
    python = python_environment / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(sys.executable).resolve())

    fake_bin = run_root / "O'Neil bin"
    fake_bin.mkdir()
    node = fake_bin / "node"
    node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    node.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
    )
    monkeypatch.setenv("BOXTEAM_TEST_GATEWAY_CAPTURE", str(capture_path))

    target = GatewaySshTarget(
        target_id=f"local-{posix_gateway_platform}",
        platform=posix_gateway_platform,
        host="127.0.0.1",
        ssh_port=22,
        username="integration-test",
        private_key=runtime_root / "unused-key",
        known_hosts_path=runtime_root / "unused-known-hosts",
        repository_path=str(repository_root),
        python_environment=str(python_environment),
    )
    started: dict[str, str] = {}

    def run_local_command(
        _target: GatewaySshTarget,
        command: str,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if "nohup" in command and result.returncode == 0:
            started["process_id"] = result.stdout.strip()
        if result.returncode != 0:
            raise RuntimeError(
                f"POSIX 本地远端命令失败: stdout={result.stdout}, stderr={result.stderr}"
            )
        return result

    monkeypatch.setattr(gateway_target, "clear_remote_listeners", lambda *_args: None)
    monkeypatch.setattr(gateway_target, "run_ssh_command", run_local_command)
    monkeypatch.setattr(
        gateway_target,
        "ssh_command",
        lambda _target, command: ["sh", "-c", command],
    )
    monkeypatch.setattr(gateway_target, "TARGET_READY_TIMEOUT_SECONDS", 20)
    port_block = integration_port_block_for_file(Path(__file__).resolve())
    port_offset = 0 if posix_gateway_platform == "linux" else 1
    return PosixGatewayCase(
        target=target,
        repository_root=repository_root,
        workspace_root=remote_workspace,
        boxteam_home=boxteam_home,
        capture_path=capture_path,
        gateway_port=port_block.port(port_offset),
        package_version=str(package["version"]),
        started_process=started,
    )


@dataclass(frozen=True, slots=True)
class PosixGatewayCase:
    target: GatewaySshTarget
    repository_root: Path
    workspace_root: Path
    boxteam_home: Path
    capture_path: Path
    gateway_port: int
    package_version: str
    started_process: dict[str, str]


@pytest.fixture
def sleeping_gateway_process(
    posix_gateway_case: PosixGatewayCase,
) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=posix_gateway_case.repository_root,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


@pytest.mark.parametrize("posix_gateway_platform", ["linux"], indirect=True)
def test_posix_gateway_cleanup_terminates_owned_process(
    posix_gateway_case: PosixGatewayCase,
    sleeping_gateway_process: subprocess.Popen[bytes],
) -> None:
    stop_remote_gateway(
        posix_gateway_case.target,
        str(sleeping_gateway_process.pid),
    )

    assert sleeping_gateway_process.wait(timeout=1) is not None


@pytest.mark.parametrize(
    "posix_gateway_platform",
    ["linux", "macos"],
    indirect=True,
)
def test_posix_gateway_start_health_and_cleanup(
    posix_gateway_case: PosixGatewayCase,
) -> None:
    case = posix_gateway_case
    process_id = ""
    try:
        process_id = start_remote_gateway_via_ssh(
            target=case.target,
            remote_workspace_path=str(case.workspace_root),
            remote_gateway_port=case.gateway_port,
            remote_boxteam_home=str(case.boxteam_home),
        )
        assert case.started_process["process_id"] == process_id

        manifest_path = case.boxteam_home / "runtime" / "runtime-manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        assert not manifest_bytes.startswith(b"\xef\xbb\xbf")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        assert manifest["distribution"] == "source-development"
        assert manifest["version"] == case.package_version
        assert manifest["application_root"] == str(case.repository_root)

        config_root = case.boxteam_home / "config"
        for source_name, installed_name in (
            ("gateway_inline.jsonc", "gateway.jsonc"),
            ("gateway_schema.jsonc", "gateway_schema.jsonc"),
            ("workspace_inline.jsonc", "workspace.jsonc"),
            ("workspace_schema.jsonc", "workspace_schema.jsonc"),
        ):
            assert (config_root / installed_name).read_bytes() == (
                case.repository_root / "configs" / source_name
            ).read_bytes()

        runtime_environment = json.loads(case.capture_path.read_text(encoding="utf-8"))[
            "environment"
        ]
        assert runtime_environment["BOXTEAM_HOME"] == str(case.boxteam_home)
        assert runtime_environment["BOXTEAM_GATEWAY_ROOT"] == str(
            case.boxteam_home / "state" / "gateway"
        )
        assert runtime_environment["BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT"] == str(
            case.workspace_root
        )
        assert runtime_environment["BOXTEAM_RUNTIME_MANIFEST"] == str(manifest_path)
        assert runtime_environment["BOXTEAM_PYTHON_BIN"] == str(
            case.repository_root / ".venv" / "bin" / "python"
        )
        if case.target.platform == "linux":
            assert runtime_environment["BOXTEAM_NODE_BIN"] == "/usr/local/bin/node"
        else:
            assert runtime_environment["BOXTEAM_NODE_BIN"].endswith("O'Neil bin/node")
        assert runtime_environment["PYTHONUNBUFFERED"] == "1"
    finally:
        process_id = process_id or case.started_process.get("process_id", "")
        if process_id.isdecimal():
            stop_remote_gateway(case.target, process_id)
