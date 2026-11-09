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
from urllib.parse import urlsplit

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.workspace import (
    ensure_local_port_available,
    terminate_process,
    wait_for_http_ok,
)

_APP_SOURCE = '''\
import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from app.api.runtime_control import router
from app.core.trace_middleware import TraceMiddleware

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

    async def after_http_startup():
        if mode == "after_http_startup_error":
            record("after-http-startup-enter")
            raise RuntimeError("original-after-http-startup-failure")
        if mode == "after_http_startup_wait":
            record("after-http-startup-enter")
            async with asyncio.timeout(5):
                while not (artifacts / "release").exists():
                    await asyncio.sleep(0.01)
            record("after-http-startup-finish")

    try:
        yield {
            "extension_management": owner,
            "after_http_startup": after_http_startup,
            "before_http_shutdown": owner.quiesce,
        }
    finally:
        record("lifespan-exit")

app = FastAPI(lifespan=lifespan)
app.add_middleware(TraceMiddleware)
app.include_router(router)

@app.get("/health")
async def health():
    return {"ready": True, "process_id": os.getpid()}

@app.post("/child")
async def child():
    result = await asyncio.to_thread(subprocess.run,
        [sys.executable, "-c", "print('owned-child')"], capture_output=True, text=True, timeout=3, check=True)
    return {"stdout": result.stdout.strip()}

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
    parameter = getattr(request, "param", "normal")
    mode, control = parameter if isinstance(parameter, tuple) else (parameter, "signal")
    source = _APP_SOURCE.replace("ARTIFACTS_PATH", repr(str(artifacts))).replace("FAILURE_MODE", repr(mode))
    (workspace / "runner_fixture.py").write_text(source, encoding="utf-8")
    port = integration_port_block_for_file(Path(__file__).resolve()).port(1)
    ensure_local_port_available(port, service_name="Gateway runner隔离进程")
    command = [
        sys.executable, "-m", "app.runtime.http_server", "runner_fixture:app",
        "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        "--timeout-graceful-shutdown", "1",
    ]
    if control == "eof":
        command.append("--shutdown-on-stdin-eof")
    environment = {**os.environ, "PYTHONPATH": str(Path.cwd().resolve()),
        "BOXTEAM_RUNTIME_CONTROL_TOKEN": "runner-control-token", "BOXTEAM_BACKEND_GENERATION": "runner-generation"}
    (artifacts / "command.json").write_text(json.dumps({"argv": command, "cwd": str(workspace)}, indent=2) + "\n")
    with (artifacts / "stdout.log").open("w", encoding="utf-8") as stdout, (
        artifacts / "stderr.log"
    ).open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, cwd=workspace, env=environment, stdout=stdout, stderr=stderr, stdin=subprocess.PIPE, text=True)
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
            if process.stdin is not None:
                process.stdin.close()
            (artifacts / "exit.json").write_text(json.dumps({"returncode": process.poll()}) + "\n")


@pytest.mark.parametrize("runner_process", [
    pytest.param(("normal", "signal"), marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX 信号")),
    ("normal", "eof"),
], indirect=True)
def test_runner_keeps_listener_until_original_owner_quiesces(runner_process: RunnerProcess) -> None:
    runner = runner_process
    wait_for_http_ok(runner.url + "/health", runner.process, timeout_seconds=5)
    assert isinstance(runner.process.args, list)
    with httpx.Client(trust_env=False, timeout=5) as client:
        assert client.post(runner.url + "/child").raise_for_status().json() == {"stdout": "owned-child"}
    controlled = "--shutdown-on-stdin-eof" in runner.process.args
    if controlled:
        assert runner.process.stdin is not None
        runner.process.stdin.close()
    else:
        runner.process.send_signal(signal.SIGTERM)
    runner.wait_for_event("quiesce-enter")

    # 新建连接，不能靠停机前的 keep-alive 蒙混通过。
    with httpx.Client(trust_env=False, timeout=2) as client:
        response = client.post(runner.url + "/callback")
        assert response.status_code == 200
        assert response.json() == {"original_owner": True}
    assert runner.events() == ["startup", "quiesce-enter", "callback"]
    assert runner.process.poll() is None
    (runner.artifacts / "release").touch()
    assert runner.process.wait(timeout=5) == (0 if controlled else -signal.SIGTERM)
    assert runner.events() == ["startup", "quiesce-enter", "callback", "quiesce-finish", "lifespan-exit"]
    port = urlsplit(runner.url).port
    assert port is not None
    ensure_local_port_available(port, service_name="已关闭的 Gateway runner")
    with httpx.Client(trust_env=False, timeout=1) as client, pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
        client.post(runner.url + "/callback")


@pytest.mark.parametrize("runner_process", [("after_http_startup_wait", "eof")], indirect=True)
def test_listener_serves_requests_before_after_http_startup_finishes(
    runner_process: RunnerProcess,
) -> None:
    runner = runner_process
    wait_for_http_ok(runner.url + "/health", runner.process, timeout_seconds=5)
    runner.wait_for_event("after-http-startup-enter")

    with httpx.Client(trust_env=False, timeout=2) as client:
        response = client.post(runner.url + "/callback")
        assert response.status_code == 200
        assert response.json() == {"original_owner": True}
    assert "after-http-startup-finish" not in runner.events()
    assert runner.process.poll() is None

    (runner.artifacts / "release").touch()
    runner.wait_for_event("after-http-startup-finish")
    with httpx.Client(trust_env=False, timeout=2) as client:
        assert client.get(runner.url + "/health").raise_for_status().json()["ready"] is True
    terminate_process(runner.process, stdin_eof=True, timeout_seconds=5)
    assert runner.process.returncode == 0
    assert runner.events() == [
        "startup", "after-http-startup-enter", "callback", "after-http-startup-finish",
        "quiesce-enter", "quiesce-finish", "lifespan-exit",
    ]


@pytest.mark.parametrize("runner_process", [("after_http_startup_error", "eof")], indirect=True)
def test_after_http_startup_failure_runs_lifespan_cleanup_and_exits_nonzero(
    runner_process: RunnerProcess,
) -> None:
    runner = runner_process
    wait_for_http_ok(runner.url + "/health", runner.process, timeout_seconds=5)
    runner.wait_for_event("after-http-startup-enter")
    runner.wait_for_event("quiesce-enter")
    assert runner.process.poll() is None

    (runner.artifacts / "release").touch()
    assert runner.process.wait(timeout=5) != 0
    assert runner.events() == [
        "startup", "after-http-startup-enter", "quiesce-enter", "quiesce-finish", "lifespan-exit",
    ]
    assert "original-after-http-startup-failure" in runner.stderr()


@pytest.mark.parametrize("runner_process", [
    pytest.param(("quiesce_error", "signal"), marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX 信号")),
    ("quiesce_error", "eof"),
], indirect=True)
def test_runner_reports_original_quiesce_failure(runner_process: RunnerProcess) -> None:
    runner = runner_process
    wait_for_http_ok(runner.url + "/health", runner.process, timeout_seconds=5)
    assert isinstance(runner.process.args, list)
    if "--shutdown-on-stdin-eof" in runner.process.args:
        assert runner.process.stdin is not None
        runner.process.stdin.close()
    else:
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


@pytest.mark.parametrize("runner_process", [("normal", "eof")], indirect=True)
def test_authenticated_shutdown_uses_original_runner_and_keeps_callback_until_quiesced(
    runner_process: RunnerProcess,
) -> None:
    runner = runner_process
    wait_for_http_ok(runner.url + "/health", runner.process, timeout_seconds=5)
    headers = {"X-Local-Token": "runner-control-token"}
    with httpx.Client(trust_env=False, timeout=2) as client:
        service_pid = client.get(runner.url + "/health").raise_for_status().json()["process_id"]
        payload = {"process_id": service_pid, "backend_generation": "runner-generation"}
        for token in (None, "wrong-token"):
            sent_headers = {} if token is None else {"X-Local-Token": token}
            assert client.post(runner.url + "/api/internal/runtime/shutdown", json=payload,
                headers=sent_headers).status_code == 401
        for extra in ({"Origin": "http://localhost"}, {"Sec-Fetch-Site": "same-origin"}):
            assert client.post(runner.url + "/api/internal/runtime/shutdown", json=payload,
                headers=headers | extra).status_code == 403
        for changed in ({"process_id": service_pid + 1}, {"backend_generation": "wrong-generation"}):
            assert client.post(runner.url + "/api/internal/runtime/shutdown", json=payload | changed,
                headers=headers).status_code == 409
        assert runner.process.poll() is None and runner.events() == ["startup"]
        response = client.post(runner.url + "/api/internal/runtime/shutdown", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        assert response.json()["data"] == payload | {"accepted": True}
        runner.wait_for_event("quiesce-enter")
        assert runner.process.poll() is None
        assert client.post(runner.url + "/callback").raise_for_status().json() == {"original_owner": True}
        (runner.artifacts / "release").touch()
    assert runner.process.wait(timeout=5) == 0
    assert runner.events() == ["startup", "quiesce-enter", "callback", "quiesce-finish", "lifespan-exit"]


@pytest.mark.parametrize("runner_process", [("normal", "eof")], indirect=True)
@pytest.mark.parametrize("timeout_before_release", [False, True])
def test_fixture_eof_close_waits_for_original_owner_and_releases_port(
    runner_process: RunnerProcess, timeout_before_release: bool,
) -> None:
    runner = runner_process
    wait_for_http_ok(runner.url + "/health", runner.process, timeout_seconds=5)
    if timeout_before_release:
        with pytest.raises(TimeoutError, match="stdin EOF 正常停机尚未完成"):
            terminate_process(runner.process, stdin_eof=True, timeout_seconds=0.3)
        assert runner.process.poll() is None
        runner.wait_for_event("quiesce-enter")
        with httpx.Client(trust_env=False, timeout=1) as client:
            assert client.post(runner.url + "/callback").raise_for_status().json() == {"original_owner": True}
    (runner.artifacts / "release").touch()
    terminate_process(runner.process, stdin_eof=True, timeout_seconds=5)
    assert runner.process.returncode == 0
    assert runner.process.stdin is not None and runner.process.stdin.closed
    assert runner.events() == [
        "startup", "quiesce-enter", *(["callback"] if timeout_before_release else []),
        "quiesce-finish", "lifespan-exit",
    ]
    port = urlsplit(runner.url).port
    assert port is not None
    ensure_local_port_available(port, service_name="EOF 关闭后的 Gateway runner")


@pytest.mark.parametrize("runner_process", [("quiesce_error", "eof")], indirect=True)
def test_fixture_eof_close_reports_original_runner_failure(runner_process: RunnerProcess) -> None:
    runner = runner_process
    wait_for_http_ok(runner.url + "/health", runner.process, timeout_seconds=5)
    with pytest.raises(RuntimeError, match="stdin EOF 停机失败"):
        terminate_process(runner.process, stdin_eof=True, timeout_seconds=5)
    assert runner.process.returncode == 1
    assert "original-quiesce-failure" in runner.stderr()
