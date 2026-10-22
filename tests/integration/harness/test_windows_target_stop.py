"""在原生 PowerShell 中核验已枚举进程退出及停止权限错误。"""

from __future__ import annotations

import base64
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="必须由原生 PowerShell 执行")


@pytest.fixture
def sleeping_processes() -> Iterator[tuple[subprocess.Popen, subprocess.Popen]]:
    def close(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    with ExitStack() as lifetime:
        processes = []
        for _ in range(2):
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            lifetime.callback(close, process)
            processes.append(process)
        yield processes[0], processes[1]


@pytest.fixture
def stop_probe() -> Callable[[str, str], subprocess.CompletedProcess]:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    source = Path.cwd() / "tools/cross-platform-development-targets/windows/Manage-Target.ps1"
    source_literal = source.as_posix().replace("'", "''")

    def run(name: str, body: str) -> subprocess.CompletedProcess:
        script = f"""
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseInput(
    [IO.File]::ReadAllText('{source_literal}',[Text.Encoding]::UTF8),[ref]$tokens,[ref]$errors)
if ($errors.Count) {{ throw 'management script parse failed' }}
$functions=$ast.FindAll({{param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -in @('Fail-Target','Stop-DevelopmentPorts')
}},$false)
foreach ($function in $functions) {{ Invoke-Expression $function.Extent.Text }}
{body}
"""
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                   base64.b64encode(script.encode("utf-16-le")).decode("ascii")]
        result = subprocess.run(command, capture_output=True, timeout=20, check=False)
        (context.artifacts_dir / f"{name}.stdout.raw").write_bytes(result.stdout)
        (context.artifacts_dir / f"{name}.stderr.raw").write_bytes(result.stderr)
        return result

    return run


def test_stop_continues_when_an_enumerated_process_has_exited(sleeping_processes, stop_probe):
    exited, running = sleeping_processes
    exited.kill()
    exited.wait(timeout=10)
    result = stop_probe("exited", f"""
function Get-NetTCPConnection {{
    [PSCustomObject]@{{LocalPort=8010;OwningProcess={exited.pid}}}
    [PSCustomObject]@{{LocalPort=8014;OwningProcess={running.pid}}}
}}
Stop-DevelopmentPorts 'development'
Write-Output 'stop-completed'
""")
    assert result.returncode == 0, result.stderr
    assert b"stop-completed" in result.stdout
    running.wait(timeout=10)


def test_stop_preserves_other_process_errors(sleeping_processes, stop_probe):
    _, running = sleeping_processes
    result = stop_probe("denied", f"""
function Get-NetTCPConnection {{ [PSCustomObject]@{{LocalPort=8014;OwningProcess={running.pid}}} }}
function Stop-Process {{ Write-Error -Message 'probe-permission-denied' -ErrorId 'ProbeAccessDenied' -ErrorAction Stop }}
Stop-DevelopmentPorts 'development'
""")
    assert result.returncode != 0
    assert b"probe-permission-denied" in result.stderr
    assert running.poll() is None
