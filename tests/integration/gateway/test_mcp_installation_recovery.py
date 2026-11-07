"""经真实 Gateway／Backend／uv 核原安装幂等、进程中断与发布事实。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import psutil
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.gateway.test_mcp_artifact_installation import (
    _wheel,
    installation_route,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


@dataclass(frozen=True)
class RecoveryCase:
    client: httpx.Client
    context: TestRunContext
    workspace: Path
    home: Path
    index: str
    blocked: threading.Event
    release: threading.Event
    downloads: list[str]


@pytest.fixture
def recovery_case(request: pytest.FixtureRequest) -> Iterator[RecoveryCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = prepare_test_workspace(workspace_root=context.workspace_root / request.node.name,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", template_items=())
    install_test_workspace_config(workspace_root=workspace, config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
        schema_path=Path.cwd() / "configs/workspace_schema.jsonc")
    home = context.boxteam_home_for_node(request.node.nodeid)
    index_root = context.artifacts_dir / "index"
    for version in ("1.0.0", "2.0.0"):
        _wheel(index_root, "recovery-server", version, b"def main():\n    return None\n", entrypoint="recovery-server")
    blocked, release = threading.Event(), threading.Event()
    downloads: list[str] = []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, format: str, *arguments: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path.split("?", 1)[0].endswith(".whl"):
                downloads.append(self.path)
                if "2.0.0" in self.path:
                    blocked.set()
                    assert release.wait(90), "测试未释放正在安装的真实 wheel 下载"
            super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(index_root)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    gateway = None
    try:
        gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None,
            port=integration_port_block_for_file(Path(request.node.path)).port(0), boxteam_home=home, artifacts_dir=context.artifacts_dir)
        with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=60, trust_env=False) as client:
            client.post("/api/gateway/users/guest", json={"tracking": {"test": True}}).raise_for_status()
            yield RecoveryCase(client, context, workspace, home, f"http://127.0.0.1:{server.server_port}/simple", blocked, release, downloads)
    finally:
        release.set()
        try:
            if gateway is not None:
                close_gateway_process(gateway)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            assert not worker.is_alive()
            (context.artifacts_dir / "downloads.json").write_text(json.dumps(downloads, indent=2) + "\n", encoding="utf-8")


def test_original_operation_survives_concurrent_retry_and_backend_exit(recovery_case: RecoveryCase) -> None:
    case = recovery_case
    _, installation_base = installation_route(case.client)
    evidence: list[dict[str, object]] = []
    children: list[psutil.Process] = []

    def exchange(method: str, path: str, key: str, body: dict[str, object] | None = None, *, status: int = 200) -> dict:
        response = case.client.request(method, path, json=body, headers={"X-Request-ID": key})
        payload = response.json()
        assert response.headers["X-Request-ID"] == payload["request_id"] == key
        evidence.append({"method": method, "path": path, "request_id": key, "status": response.status_code, "body": payload})
        assert response.status_code == status, response.text
        return payload

    listing = exchange("GET", "/api/gateway/workspaces", "recovery-workspaces")["data"]
    workspace_id = listing["items"][0]["workspace_id"]
    base = f"/api/v1/workspaces/{workspace_id}"
    exchange("GET", base + "/workspace", "recovery-workspace")
    target = exchange("GET", installation_base + "/installation-capabilities", "recovery-capabilities")["target"]
    original_config = (case.workspace / ".boxteam/workspace.jsonc").read_bytes()

    def intent(version: str, operation_id: str) -> dict[str, object]:
        source = {"kind": "manual", "name": "org.example/recovery", "version": version, "definition": {"packages": [
            {"registryType": "pypi", "identifier": "recovery-server", "version": version, "registryBaseUrl": case.index,
             "transport": {"type": "stdio"}}]}}
        inspected = exchange("POST", installation_base + "/candidate-manifests", operation_id + "-inspect", {"target": target, "source": source})["data"]
        candidate = {"target": target, "source": source, "manifest_digest": inspected["manifest_digest"], "variant": inspected["variants"][0]["ref"]}
        plan = exchange("POST", installation_base + "/candidate-plans", operation_id + "-plan", candidate)["data"]
        return {"candidate": candidate, "plan_digest": plan["plan_digest"], "operation_id": operation_id, "entrypoint": "recovery-server"}

    def observe(operation_id: str) -> dict:
        return exchange("GET", installation_base + "/installation-operations/" + operation_id, operation_id + "-observe")["data"]

    try:
        first_intent = intent("1.0.0", "stable-installation")
        with ThreadPoolExecutor(max_workers=4) as executor:
            receipts = list(executor.map(lambda index: exchange("POST", installation_base + "/installations", f"concurrent-{index}", first_intent)["data"], range(4)))
        assert {item["operation_id"] for item in receipts} == {"stable-installation"}
        assert len({item["intent_digest"] for item in receipts}) == 1
        deadline = time.monotonic() + 40
        published = observe("stable-installation")
        while published["status"] == "running":
            assert time.monotonic() < deadline, published
            time.sleep(0.1)
            published = observe("stable-installation")
        assert published["status"] == "succeeded", published
        artifact = published["artifact"]
        assert artifact["version"] == "1.0.0" and Path(artifact["command"]).is_file()
        downloads_after_publish = list(case.downloads)
        assert exchange("POST", installation_base + "/installations", "repeat-published", first_intent)["data"] == published
        exchange("POST", installation_base + "/installations", "conflicting-entrypoint", {**first_intent, "entrypoint": "another-server"}, status=409)
        assert case.downloads == downloads_after_publish

        interrupted_intent = intent("2.0.0", "interrupted-installation")
        exchange("POST", installation_base + "/installations", "start-interrupted", interrupted_intent)
        assert case.blocked.wait(10), "真实 uv 没有请求被阻塞的 wheel"
        before_exit = observe("interrupted-installation")
        assert before_exit["status"] == "running" and before_exit["stage"] in {"lock", "install"}
        database = case.home / "artifacts/mcp" / target["backend_id"] / "installations.sqlite"
        # 只读原 PID／创建时间用于故障注入，不替换业务阶段或结果。
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            pid, started = connection.execute("SELECT pid, started FROM operation_owners WHERE operation_id=?", ("interrupted-installation",)).fetchone()
            assert connection.execute("SELECT COUNT(*) FROM operations").fetchone() == (2,)
        process = psutil.Process(pid)
        assert process.create_time() == started
        children = process.children(recursive=True)
        assert any("uv" in child.name() for child in children)
        process.kill()
        _, alive = psutil.wait_procs([process], timeout=5)
        assert all(item.status() == psutil.STATUS_ZOMBIE for item in alive)
        case.release.set()
        exchange("POST", f"/api/gateway/workspaces/{workspace_id}/runtime/start", "recover-backend")
        after_exit = observe("interrupted-installation")
        assert after_exit["status"] == "interrupted" and after_exit["artifact"] is None
        assert after_exit["stage"] == before_exit["stage"] and after_exit["created_at"] == before_exit["created_at"]
        assert after_exit["intent_digest"] == before_exit["intent_digest"]
        assert "原安装进程已退出" in after_exit["error"]
        after_restart = exchange("GET", installation_base + "/installation-capabilities", "recovered-capabilities")
        assert after_restart["target"] == target
        _, alive_children = psutil.wait_procs(children, timeout=10)
        assert not alive_children, [item.pid for item in alive_children]
        requests_after_exit = list(case.downloads)
        assert exchange("POST", installation_base + "/installations", "repeat-interrupted", interrupted_intent)["data"] == after_exit
        assert observe("interrupted-installation") == after_exit
        assert case.downloads == requests_after_exit
        assert observe("stable-installation") == published
        assert exchange("GET", installation_base + "/installations", "retained-original-artifact")["data"] == [artifact]
        assert exchange("GET", base + "/mcp/servers", "still-unconfigured")["data"] == []
        assert (case.workspace / ".boxteam/workspace.jsonc").read_bytes() == original_config
    finally:
        case.release.set()
        _, alive = psutil.wait_procs(children, timeout=5)
        for child in alive:
            child.kill()
        _, alive = psutil.wait_procs(alive, timeout=5)
        assert not alive, [item.pid for item in alive]
        (case.context.artifacts_dir / "operation-recovery.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
