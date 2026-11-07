"""以独立进程和真实 HTTP 验证 Gateway listener 与原管理 owner 的关闭顺序。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.workspace import (
    ensure_local_port_available,
    wait_for_http_ok,
)

_APP_SOURCE = '''\
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request

artifacts = Path(ARTIFACTS_PATH)
mode = FAILURE_MODE

def record(event):
    with (artifacts / "events.log").open("a", encoding="utf-8") as stream:
        stream.write(event + "\\n")

class Owner:
    async def quiesce(self):
        record("quiesce-enter")
        if mode == "quiesce_error":
            raise RuntimeError("original-quiesce-failure")
        async with asyncio.timeout(5):
            while not (artifacts / "release").exists():
                await asyncio.sleep(0.01)
        record("quiesce-finish")

owner = Owner()

@asynccontextmanager
async def lifespan(app):
    if mode == "startup_error":
        raise RuntimeError("original-startup-failure")
    app.state.extension_management = owner
    record("startup")
    try:
        yield {"extension_management": owner}
    finally:
        record("lifespan-exit")

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {"ready": True}

@app.post("/callback")
async def callback(request: Request):
    record("callback")
    return {"original_owner": request.state.extension_management is owner}
'''


@dataclass(frozen=True)
class RunnerProcess:
    process: subprocess.Popen[str]
    artifacts: Path
    url: str

    def events(self) -> list[str]:
        path = self.artifacts / "events.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def wait_for_event(self, event: str) -> None:
        deadline = time.monotonic() + 3
        while event not in self.events():
            assert self.process.poll() is None, self.stderr()
            assert time.monotonic() < deadline, f"未观察到 {event}: {self.events()}"
            time.sleep(0.01)

    def stderr(self) -> str:
        return (self.artifacts / "stderr.log").read_text(encoding="utf-8")


@pytest.fixture
def runner_process(request: pytest.FixtureRequest) -> Iterator[RunnerProcess]:
    context = TestRunContext.from_test_file(Path(__file__).resolve()).prepare()
    artifacts = context.artifacts_dir
    workspace = context.workspace_root / context.run_root.name / context.runtime_root_for_node(request.node.nodeid).name
    workspace.mkdir(parents=True)
    mode = getattr(request, "param", "normal")
    source = _APP_SOURCE.replace("ARTIFACTS_PATH", repr(str(artifacts))).replace("FAILURE_MODE", repr(mode))
    (workspace / "runner_fixture.py").write_text(source, encoding="utf-8")
    port = integration_port_block_for_file(Path(__file__).resolve()).port(1)
    ensure_local_port_available(port, service_name="Gateway runner隔离进程")
    command = [
        sys.executable, "-m", "app.gateway.server.runner", "runner_fixture:app",
        "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        "--timeout-graceful-shutdown", "1",
    ]
    environment = {**os.environ, "PYTHONPATH": str(Path.cwd().resolve())}
    (artifacts / "command.json").write_text(json.dumps({"argv": command, "cwd": str(workspace)}, indent=2) + "\n")
    with (artifacts / "stdout.log").open("w", encoding="utf-8") as stdout, (
        artifacts / "stderr.log"
    ).open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, cwd=workspace, env=environment, stdout=stdout, stderr=stderr, text=True)
        try:
            yield RunnerProcess(process, artifacts, f"http://127.0.0.1:{port}")
        finally:
            # 兜底仅回收本 fixture 持有的进程；产品关闭结论在此之前断言。
            (artifacts / "release").touch()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            (artifacts / "exit.json").write_text(json.dumps({"returncode": process.poll()}) + "\n")


@pytest.mark.skipif(sys.platform == "win32", reason="本用例发送真实POSIX SIGTERM；不能代替Windows原生信号验收")
def test_runner_keeps_listener_until_original_owner_quiesces(runner_process: RunnerProcess) -> None:
    runner = runner_process
    wait_for_http_ok(runner.url + "/health", runner.process, timeout_seconds=5)
    runner.process.send_signal(signal.SIGTERM)
    runner.wait_for_event("quiesce-enter")

    # 使用新 client 建立新连接，不能靠停机前已有keep-alive连接蒙混通过。
    with httpx.Client(trust_env=False, timeout=2) as client:
        response = client.post(runner.url + "/callback")
        assert response.status_code == 200
        assert response.json() == {"original_owner": True}
    assert runner.events() == ["startup", "quiesce-enter", "callback"]
    assert runner.process.poll() is None

    (runner.artifacts / "release").touch()
    assert runner.process.wait(timeout=5) == -signal.SIGTERM
    assert runner.events() == ["startup", "quiesce-enter", "callback", "quiesce-finish", "lifespan-exit"]
    with httpx.Client(trust_env=False, timeout=1) as client, pytest.raises(httpx.ConnectError):
        client.post(runner.url + "/callback")


@pytest.mark.skipif(sys.platform == "win32", reason="本用例发送真实POSIX SIGTERM；不能代替Windows原生信号验收")
@pytest.mark.parametrize("runner_process", ["quiesce_error"], indirect=True)
def test_runner_reports_original_quiesce_failure(runner_process: RunnerProcess) -> None:
    runner = runner_process
    wait_for_http_ok(runner.url + "/health", runner.process, timeout_seconds=5)
    runner.process.send_signal(signal.SIGTERM)

    assert runner.process.wait(timeout=5) == 1
    assert "quiesce-enter" in runner.events()
    assert "quiesce-finish" not in runner.events()
    assert "RuntimeError: original-quiesce-failure" in runner.stderr()


@pytest.mark.parametrize("runner_process", ["startup_error"], indirect=True)
def test_runner_exits_nonzero_without_quiescing_failed_startup(runner_process: RunnerProcess) -> None:
    runner = runner_process
    assert runner.process.wait(timeout=5) == 3
    assert runner.events() == []
    assert "RuntimeError: original-startup-failure" in runner.stderr()
