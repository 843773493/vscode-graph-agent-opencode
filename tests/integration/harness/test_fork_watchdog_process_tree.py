"""经真实 matrix 验证 fork watchdog 的进程归属与退出，不启动产品。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import psutil
import pytest

from app.core.atomic_fs import native_io_path
from tests.harness.python.run_context import TestRunContext

PROBE = r"""
import json, os, subprocess, sys, time
from pathlib import Path
from app.core.atomic_fs import native_io_path
from tests.support.fork_deadline import ForkDeadlineWatchdog
root, artifacts, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
os.chdir(root)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(45)'])
watchdog = ForkDeadlineWatchdog(artifacts)
native_io_path(artifacts / 'owned.json').write_text(json.dumps({
    'protected_pid': os.getpid(), 'child_pid': child.pid,
    'watchdog_pid': watchdog._process.pid,
}), encoding='utf-8')
if mode == 'expired':
    started = time.monotonic()
    watchdog.start('probe-token', 'probe-target', 'probe-operation', started, started + 0.3)
    while True:
        time.sleep(0.01)
elif mode == 'completed':
    started = time.monotonic()
    watchdog.start('probe-token', 'probe-target', 'probe-operation', started, started + 30)
    watchdog.complete('probe-token')
    watchdog.close()
    child.terminate()
    child.wait(timeout=3)
    raise SystemExit(0)
else:
    watchdog._send({'action': 'invalid-probe-action'})
watchdog.wait_for_termination()
raise RuntimeError('watchdog returned without terminating the protected process')
"""


def _is_executing(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _stop_owned_process(process: psutil.Process) -> None:
    if not process.is_running():
        return
    owned = [*reversed(process.children(recursive=True)), process]
    for child in owned:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    _, remaining = psutil.wait_procs(owned, timeout=3)
    assert not [child.pid for child in remaining if _is_executing(child.pid)]


@pytest.fixture
def watchdog_case(request: pytest.FixtureRequest) -> Iterator[tuple[Path, Path, Path, str]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = Path.cwd()
    mode = str(request.param)
    workspace = context.workspace_root / context.run_root.name / mode
    artifacts = context.artifacts_dir
    workspace.mkdir(parents=True)
    (workspace / 'tests/runner').mkdir(parents=True)
    if sys.platform == 'win32':
        guard = workspace / 'app/runtime/windows_job.py'
        guard.parent.mkdir(parents=True)
        shutil.copyfile(root / 'app/runtime/windows_job.py', guard)
    matrix = {'schemaVersion': 1, 'suites': [{
        'id': 'fork-watchdog-probe', 'enabled': True,
        'timeoutMs': 10_000, 'dataLimitKb': 4194304,
        'command': ['uv', 'run', '--no-sync', 'python', '-c', PROBE, str(root), str(artifacts), mode],
    }]}
    (workspace / 'tests/runner/matrix.jsonc').write_text(json.dumps(matrix), encoding='utf-8')
    yield root, workspace, artifacts, mode


@pytest.mark.parametrize('watchdog_case', ['expired', 'invalid-control', 'completed'], indirect=True)
def test_watchdog_exit_preserves_unrelated_process_and_reports_real_failure(
    watchdog_case: tuple[Path, Path, Path, str],
) -> None:
    root, workspace, artifacts, mode = watchdog_case
    with ExitStack() as cleanup:
        peer = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(45)'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cleanup.callback(peer.wait, timeout=3)
        cleanup.callback(_stop_owned_process, psutil.Process(peer.pid))
        stdout = cleanup.enter_context((artifacts / 'stdout.raw').open('wb'))
        stderr = cleanup.enter_context((artifacts / 'stderr.raw').open('wb'))
        command = ['bun', str(root / 'tests/runner/run-tests.mjs'), '--suite=fork-watchdog-probe']
        runner = subprocess.Popen(
            command, cwd=workspace,
            env=os.environ | {'BOXTEAM_PROJECT_ROOT': str(workspace), 'PYTHONPATH': str(root)},
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            start_new_session=sys.platform != 'win32',
        )
        cleanup.callback(runner.wait, timeout=3)
        cleanup.callback(_stop_owned_process, psutil.Process(runner.pid))
        started = time.monotonic()
        code = runner.wait(timeout=20)
        stdout.flush()
        stderr.flush()
        output = (artifacts / 'stdout.raw').read_text(encoding='utf-8', errors='replace')
        errors = (artifacts / 'stderr.raw').read_text(encoding='utf-8', errors='replace')
        result = {'command': command, 'exit_code': code, 'elapsed_seconds': time.monotonic() - started,
                  'peer_alive': peer.poll() is None, 'mode': mode}
        native_io_path(artifacts / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        assert (code == 0) == (mode == 'completed'), (output, errors)
        assert '"status":"TIMEOUT"' not in output, (output, errors)
        assert peer.poll() is None
        assert '_enter_buffered_busy' not in errors, errors
        registration = native_io_path(artifacts / 'owned.json')
        assert registration.is_file(), (output, errors)
        facts = json.loads(registration.read_text(encoding='utf-8'))
        # POSIX sidecar 在独立 session 中；runner 先退出时仍须观察其真实终态。
        deadline = time.monotonic() + 3
        while any(_is_executing(pid) for pid in facts.values()) and time.monotonic() < deadline:
            time.sleep(0.01)
        alive = [pid for pid in facts.values() if _is_executing(pid)]
        result.update({'owned': facts, 'live_after_exit': alive, 'settled_seconds': time.monotonic() - started})
        native_io_path(artifacts / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        assert not alive, facts
        if mode == 'expired':
            records = list(native_io_path(artifacts).glob('fork-deadline-*.json'))
            assert len(records) == 1, errors
            receipt = json.loads(records[0].read_text(encoding='utf-8'))
            assert (receipt['target'], receipt['operation_key'], receipt['backend_state']) == (
                'probe-target', 'probe-operation', 'unknown',
            )
            assert receipt['elapsed_seconds'] >= 0.3
            assert 'watchdog returned without terminating' not in errors
        elif mode == 'invalid-control':
            assert 'invalid-probe-action' in errors
        else:
            assert not list(native_io_path(artifacts).glob('fork-deadline-*.json'))
            assert not errors
