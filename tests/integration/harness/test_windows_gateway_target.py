from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.e2e.system.gateway import gateway_target
from tests.e2e.system.gateway.gateway_target import (
    GatewaySshTarget,
    _windows_gateway_start_command,
    stop_remote_gateway,
    wait_for_remote_gateway_ready,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="必须在 Windows 上执行原生 PowerShell 与进程树验证",
)


@dataclass(frozen=True, slots=True)
class WindowsGatewayCase:
    target: GatewaySshTarget
    workspace_root: Path
    boxteam_home: Path
    capture_path: Path
    gateway_port: int
    package_version: str


@pytest.fixture
def windows_gateway_case(monkeypatch: pytest.MonkeyPatch) -> WindowsGatewayCase:
    context = TestRunContext.from_test_file(Path(__file__).resolve()).prepare()
    project_root = Path.cwd().resolve()
    repository_root = context.workspace_root / "O'Neil repository"
    remote_workspace = context.workspace_root / "O'Neil workspace"
    boxteam_home = context.output_root / "O'Neil boxteam home"
    artifacts = context.artifacts_dir
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
        shutil.copyfile(project_root / relative_path, destination)

    package = json.loads((repository_root / "package.json").read_text(encoding="utf-8"))
    capture_path = artifacts / "gateway-process-environment.json"
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

    target = GatewaySshTarget(
        target_id="windows-local-integration",
        platform="windows",
        host="127.0.0.1",
        ssh_port=22,
        username="integration-test",
        private_key=artifacts / "unused-key",
        known_hosts_path=artifacts / "unused-known-hosts",
        repository_path=str(repository_root),
        python_environment=str(Path(sys.executable).resolve().parent.parent),
    )
    monkeypatch.setenv("BOXTEAM_TEST_GATEWAY_CAPTURE", str(capture_path))
    monkeypatch.setattr(
        gateway_target,
        "ssh_command",
        lambda _target, remote_command: remote_command.split(),
    )
    port_block = integration_port_block_for_file(Path(__file__).resolve())
    return WindowsGatewayCase(
        target=target,
        workspace_root=remote_workspace,
        boxteam_home=boxteam_home,
        capture_path=capture_path,
        gateway_port=port_block.port(0),
        package_version=str(package["version"]),
    )


def test_windows_gateway_bootstrap_health_and_process_cleanup(
    windows_gateway_case: WindowsGatewayCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = windows_gateway_case
    command = _windows_gateway_start_command(
        case.target,
        remote_workspace_path=str(case.workspace_root),
        remote_gateway_port=case.gateway_port,
        remote_boxteam_home=str(case.boxteam_home),
    )
    result = subprocess.run(
        command.split(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    process_id = result.stdout.strip()
    assert process_id.isdecimal(), f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    try:
        assert result.returncode == 0, result.stderr
        monkeypatch.setattr(gateway_target, "TARGET_READY_TIMEOUT_SECONDS", 20)
        wait_for_remote_gateway_ready(
            target=case.target,
            remote_gateway_port=case.gateway_port,
            process_id=process_id,
            remote_boxteam_home=str(case.boxteam_home),
        )

        manifest_path = case.boxteam_home / "runtime" / "runtime-manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        assert not manifest_bytes.startswith(b"\xef\xbb\xbf")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        assert manifest["distribution"] == "source-development"
        assert manifest["version"] == case.package_version
        assert manifest["application_root"] == case.target.repository_path
        assert manifest["python_executable"] == (
            Path(case.target.python_environment) / "Scripts" / "python.exe"
        ).as_posix().replace("/", "\\")

        config_root = case.boxteam_home / "config"
        for source_name, installed_name in (
            ("gateway_inline.jsonc", "gateway.jsonc"),
            ("gateway_schema.jsonc", "gateway_schema.jsonc"),
            ("workspace_inline.jsonc", "workspace.jsonc"),
            ("workspace_schema.jsonc", "workspace_schema.jsonc"),
        ):
            assert (config_root / installed_name).read_bytes() == (
                Path(case.target.repository_path) / "configs" / source_name
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
        assert (
            runtime_environment["BOXTEAM_PYTHON_BIN"] == manifest["python_executable"]
        )
        assert Path(runtime_environment["BOXTEAM_NODE_BIN"]).name.lower() == "node.exe"
        assert runtime_environment["PYTHONUNBUFFERED"] == "1"
    finally:
        stop_remote_gateway(case.target, process_id)

    import psutil

    assert not psutil.pid_exists(int(process_id))
