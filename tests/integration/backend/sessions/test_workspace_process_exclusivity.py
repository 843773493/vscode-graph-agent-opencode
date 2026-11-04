"""真实 Backend 的工作区独占跟随物理根，不跟随 Gateway home。"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS
from tests.support.processes.workspace import (
    BackendProcess,
    close_backend_process,
    start_backend_process,
)


@pytest.fixture
def backend_lifecycle(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> Callable[[str, int], AbstractContextManager[BackendProcess]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))

    @contextmanager
    def start(name: str, offset: int) -> Iterator[BackendProcess]:
        backend = start_backend_process(
            workspace_root=integration_workspace_root_path, port=ports.port(offset),
            log_name=name, boxteam_home=context.output_root / f"home-{name}",
            artifacts_dir=context.artifacts_dir,
        )
        try:
            yield backend
        finally:
            close_backend_process(backend)

    return start


def test_another_home_cannot_bypass_workspace_lock_and_can_take_over_after_exit(
    backend_lifecycle: Callable[[str, int], AbstractContextManager[BackendProcess]],
    request: pytest.FixtureRequest,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path))
    with backend_lifecycle("original", 0) as original:
        headers = {**LOCAL_TOKEN_HEADERS, "X-BoxTeam-Workspace-Id": original.workspace_id}
        with httpx.Client(base_url=f"http://127.0.0.1:{original.port}", headers=headers, timeout=20) as client:
            created = client.post("/api/v1/sessions", json={"title": "跨 home 保留的会话"})
            assert created.status_code == 200, created.text
            session = created.json()["data"]
            session_id = session["session_id"]
            main_thread = client.get(f"/api/v1/sessions/{session_id}/main-thread")
            assert main_thread.status_code == 200, main_thread.text
            thread = main_thread.json()["data"]
            identity_path = Path(original.workspace_root) / ".boxteam/workspace-identity.json"
            identity_bytes = identity_path.read_bytes()
            assert json.loads(identity_bytes)["workspace_id"] == original.workspace_id

            with (
                pytest.raises(RuntimeError, match=r"后端进程提前退出:.*returncode=[1-9]"),
                backend_lifecycle("contender", 1),
            ):
                pytest.fail("不同 home 的第二个 Backend 不应进入可服务状态")
            rejected_log = (context.artifacts_dir / "contender.stderr.log").read_text(encoding="utf-8")
            assert "SQLite 状态库已被另一个进程占用" in rejected_log
            assert ".workspace.sqlite.lock" in rejected_log
            assert original.process.poll() is None
            health = client.get("/api/v1/health")
            assert health.status_code == 200, health.text
            assert health.json()["workspace_id"] == original.workspace_id
            assert identity_path.read_bytes() == identity_bytes

    with backend_lifecycle("successor", 2) as successor:
        assert successor.workspace_id == original.workspace_id
        assert identity_path.read_bytes() == identity_bytes
        with httpx.Client(base_url=f"http://127.0.0.1:{successor.port}", headers=headers, timeout=20) as client:
            restored = client.get(f"/api/v1/sessions/{session_id}")
            assert restored.status_code == 200, restored.text
            assert restored.json()["data"] == session
            restored_thread = client.get(f"/api/v1/sessions/{session_id}/main-thread")
            assert restored_thread.status_code == 200, restored_thread.text
            assert restored_thread.json()["data"] == thread
            assert restored.headers["X-Request-ID"] == restored.json()["request_id"]
        (context.artifacts_dir / "ownership.json").write_text(json.dumps({
            "workspace_id": successor.workspace_id, "session": session, "main_thread": thread,
            "original_pid": original.process.pid, "successor_pid": successor.process.pid,
            "original_exit_code": original.process.returncode,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
