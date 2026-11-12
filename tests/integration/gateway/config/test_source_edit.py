"""经真实 Gateway 进程保存来源文件，观察原 active、事件与 restart 状态。"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)

SOURCE = "gateway_mutable_override"
LOCAL = "gateway_local_mutable_override"


@dataclass(frozen=True)
class SourceGateway:
    client: httpx.Client
    config_root: Path
    artifacts: Path

    def document(self, source: str) -> dict:
        response = self.client.get("/api/gateway/config/sources/" + source)
        response.raise_for_status()
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        return response.json()["data"]

    def edit(self, source: str, text: str, key: str) -> tuple[dict, dict]:
        before = self.document(source)
        request = {
            "gateway_id": before["gateway_id"], "source_key": source, "jsonc": text,
            "base_file_digest": before["file_digest"], "base_layer_revision": before["layer_revision"],
            "base_layer_digest": before["layer_digest"], "expected_active_revision": before["active_revision"],
            "expected_active_digest": before["active_digest"], "idempotency_key": key,
        }
        endpoint = "/api/gateway/config/sources/" + source
        preflight = self.client.post(endpoint + "/preflight", json=request)
        preflight.raise_for_status()
        assert self.document(source) == before
        response = self.client.put(endpoint, json=request)
        response.raise_for_status()
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        saved = response.json()["data"]
        (self.artifacts / (key + ".json")).write_text(json.dumps({
            "request": request, "preflight": preflight.json(), "saved": response.json(),
        }, ensure_ascii=False, indent=2) + "\n")
        return saved, request


@pytest.fixture
def source_gateway(request: pytest.FixtureRequest, integration_workspace_root_path: str) -> Iterator[SourceGateway]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = Path(integration_workspace_root_path)
    ports = integration_port_block_for_file(Path(request.node.path))
    reset_gateway_persistent_state(workspace_root=workspace)
    with ExitStack() as resources:
        backend = start_backend_process(workspace_root=str(workspace), port=ports.port(0), log_name="source-edit-backend")
        resources.callback(close_backend_process, backend)
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=f"http://127.0.0.1:{backend.port}",
            port=ports.port(1), artifacts_dir=context.artifacts_dir,
        )
        resources.callback(close_gateway_process, gateway)
        client = resources.enter_context(httpx.Client(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=10, trust_env=False,
        ))
        yield SourceGateway(client, workspace.parent / "boxteam-home/config", context.artifacts_dir)


def test_source_writer_and_watcher_share_active_events_and_native_files(source_gateway: SourceGateway) -> None:
    case = source_gateway
    before = case.document(SOURCE)
    assert case.document("inline:0")["writable"] is False
    local, _ = case.edit(LOCAL, '{"logging":{"runtime":{"level":"warning"}}}', "local-green")
    # 原修订序列由 pending 与 active 共用，接纳和 promotion 各分配一次。
    assert local["edit"]["state"] == "active" and local["active_revision"] == before["active_revision"] + 2
    source_text = '{ // 用户来源被 local 遮蔽\n "logging":{"runtime":{"level":"debug"}},\n}\n'
    shadowed, original = case.edit(SOURCE, source_text, "shadowed-user")
    assert shadowed["edit"]["state"] == "active"
    assert shadowed["active_revision"] == local["active_revision"]
    assert shadowed["edit"]["source_saved"] and shadowed["edit"]["layer_imported"]
    assert (case.config_root / "gateway.jsonc").read_text() == source_text
    replay = case.client.put("/api/gateway/config/sources/" + SOURCE, json=original).raise_for_status().json()["data"]
    assert replay["edit"] == shadowed["edit"]
    changed = {**original, "jsonc": "{}"}
    assert case.client.put("/api/gateway/config/sources/" + SOURCE, json=changed).status_code == 409
    # 原生删除由既有 watcher 导入，与 source writer 使用同一来源链。
    (case.config_root / "gateway_local.jsonc").unlink()
    deadline = time.monotonic() + 10
    observations = []
    while time.monotonic() < deadline:
        document = case.document(LOCAL)
        observations.append(document)
        if document["file_presence"] == "absent" and document["layer_digest"] is None and document["effective"]["logging"]["runtime"]["level"] == "debug":
            break
        time.sleep(0.05)
    else:
        pytest.fail("原生删除 local 来源后，watcher 未恢复用户来源的有效值")
    (case.artifacts / "native-delete-observations.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2) + "\n")
    # 遮蔽提交占一条 pending 修订；随后原生修改再分配 pending 和 active。
    assert document["active_revision"] == local["active_revision"] + 3
    events = case.client.get("/api/gateway/config/events").raise_for_status().json()["data"]["events"]
    assert [event["result"] for event in events] == ["applied", "applied"]
    assert events[0]["candidate_id"] == local["edit"]["candidate_id"]
    assert all(event["candidate_id"] != shadowed["edit"]["candidate_id"] for event in events)
    for endpoint in ("/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"):
        response = case.client.get(endpoint).raise_for_status()
        assert response.headers["X-Request-ID"] == response.json()["request_id"]


def test_restart_source_replay_keeps_original_candidate_and_active(source_gateway: SourceGateway) -> None:
    case = source_gateway
    before = case.document(SOURCE)
    saved, request = case.edit(SOURCE, '{"runtime":{"gateway":{"process":{"health":{"request_timeout_seconds":3}}}}}', "restart-source")
    assert saved["edit"]["state"] == "pending_restart"
    assert saved["status"]["candidate_id"] == saved["edit"]["candidate_id"]
    assert saved["status"]["restart_required"] is True
    assert saved["active_revision"] == before["active_revision"] and saved["effective"] == before["effective"]
    events = case.client.get("/api/gateway/config/events").raise_for_status().json()["data"]["events"]
    assert len(events) == 1 and events[0]["result"] == "restart_required"
    for _ in range(2):
        response = case.client.put("/api/gateway/config/sources/" + SOURCE, json=request).raise_for_status()
        assert response.json()["data"]["edit"] == saved["edit"]
    assert case.client.get("/api/gateway/config/events").raise_for_status().json()["data"]["events"] == events
    assert case.config_root.joinpath("gateway.jsonc").read_text() == request["jsonc"]
