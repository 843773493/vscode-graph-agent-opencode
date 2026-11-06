"""真实 Gateway／Backend／uv 的共享安装独立取消与退出结算。"""

# pytest 按名称注入复用 fixture，测试参数须与导入同名。
# ruff: noqa: F811

from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil

from tests.integration.gateway.test_mcp_installation_recovery import (  # noqa: F401 - 显式复用 pytest fixture，避免复制装具。
    recovery_case,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


def test_shared_installation_cancellation_through_native_gateway(recovery_case):
    case = recovery_case
    evidence = []

    def exchange(method, path, key, body=None, *, status=200):
        response = case.client.request(method, path, json=body, headers={"X-Request-ID": key})
        payload = response.json()
        evidence.append({"method": method, "path": path, "status": response.status_code, "body": payload})
        assert response.headers["X-Request-ID"] == payload["request_id"] == key
        assert response.status_code == status, response.text
        return payload

    listing = exchange("GET", "/api/gateway/workspaces", "cancel-workspaces")["data"]
    workspace_id = listing["items"][0]["workspace_id"]
    base = f"/api/v1/workspaces/{workspace_id}/mcp"
    target = exchange("GET", base + "/installation-capabilities", "cancel-capabilities")["target"]
    secondary = prepare_test_workspace(workspace_root=case.context.workspace_root / "second-backend",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", template_items=())
    install_test_workspace_config(workspace_root=secondary, config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
        schema_path=Path.cwd() / "configs/workspace_schema.jsonc")
    registered = exchange("POST", "/api/gateway/managed-workspaces", "cancel-register-secondary",
        {"root_path": str(secondary), "name": "独立进程取消"})["data"]
    second_id = next(item["workspace_id"] for item in registered["items"] if item["workspace_id"] != workspace_id)
    second_workspace = f"/api/v1/workspaces/{second_id}"
    exchange("GET", second_workspace + "/workspace", "cancel-secondary-workspace")
    second_base = second_workspace + "/mcp"
    assert exchange("GET", second_base + "/installation-capabilities", "cancel-secondary-target")["target"] == target
    first_health = case.client.get(f"/api/v1/workspaces/{workspace_id}/health").json()
    second_health = case.client.get(second_workspace + "/health").json()
    assert first_health["process_id"] != second_health["process_id"]
    evidence.append({"backend_healths": [first_health, second_health]})
    original_config = (case.workspace / ".boxteam/workspace.jsonc").read_bytes()
    source = {"kind": "manual", "name": "org.example/recovery", "version": "2.0.0", "definition": {"packages": [
        {"registryType": "pypi", "identifier": "recovery-server", "version": "2.0.0", "registryBaseUrl": case.index,
         "transport": {"type": "stdio"}}]}}
    document = exchange("POST", base + "/candidate-manifests", "cancel-inspect", {"target": target, "source": source})["data"]
    candidate = {"target": target, "source": source, "manifest_digest": document["manifest_digest"], "variant": document["variants"][0]["ref"]}
    plan = exchange("POST", base + "/candidate-plans", "cancel-plan", candidate)["data"]
    request = {"candidate": candidate, "plan_digest": plan["plan_digest"], "operation_id": "producer", "entrypoint": "recovery-server"}

    def observe(operation_id):
        return exchange("GET", base + "/installation-operations/" + operation_id, operation_id + "-observe")["data"]

    try:
        exchange("POST", base + "/installations", "cancel-start", request)
        assert case.blocked.wait(10), "真实 uv 未开始原 wheel 下载"
        exchange("POST", second_base + "/installations", "cancel-follow", {**request, "operation_id": "survivor"})
        database = case.home / "artifacts/mcp" / target["backend_id"] / "installations.sqlite"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            assert connection.execute("SELECT COUNT(*) FROM operation_owners").fetchone() == (1,)
            pid, started = connection.execute("SELECT pid, started FROM operation_owners").fetchone()
            assert connection.execute("SELECT producer_operation_id FROM operations").fetchall() == [("producer",), ("producer",)]
        process = psutil.Process(pid)
        assert process.create_time() == started and pid == first_health["process_id"]
        original_children = process.children(recursive=True)
        assert any("uv" in child.name().lower() for child in original_children)
        exchange("POST", base + "/installation-operations/producer/cancel", "cancel-wrong-backend",
            {**target, "backend_id": "00000000-0000-4000-8000-000000000099"}, status=409)
        exchange("POST", base + "/installation-operations/missing/cancel", "cancel-missing", target, status=404)
        assert observe("producer")["status"] == "running"
        cancelled = exchange("POST", second_base + "/installation-operations/producer/cancel", "cancel-producer", target)["data"]
        assert cancelled["status"] == "cancelled" and cancelled["cancellation_requested"]
        assert cancelled["artifact"] is None and observe("survivor")["status"] == "running"
        assert any(child.is_running() for child in original_children)
        case.release.set()
        deadline = time.monotonic() + 45
        surviving = observe("survivor")
        while surviving["status"] == "running":
            assert time.monotonic() < deadline, surviving
            time.sleep(0.1)
            surviving = observe("survivor")
        assert surviving["status"] == "succeeded" and surviving["artifact"]["version"] == "2.0.0", surviving
        assert observe("producer") == cancelled
        assert exchange("POST", base + "/installation-operations/survivor/cancel", "cancel-late", target)["data"] == surviving
        assert exchange("GET", base + "/installations", "cancel-published")["data"] == [surviving["artifact"]]
        _, alive = psutil.wait_procs(original_children, timeout=5)
        assert not alive
        assert (case.workspace / ".boxteam/workspace.jsonc").read_bytes() == original_config
    finally:
        (case.context.artifacts_dir / "cancellation-responses.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_last_cancellation_waits_for_real_uv_exit(recovery_case):
    case = recovery_case
    listing = case.client.get("/api/gateway/workspaces").json()["data"]
    workspace_id = listing["items"][0]["workspace_id"]
    base = f"/api/v1/workspaces/{workspace_id}/mcp"
    target = case.client.get(base + "/installation-capabilities").json()["target"]
    source = {"kind": "manual", "name": "org.example/recovery", "version": "2.0.0", "definition": {"packages": [
        {"registryType": "pypi", "identifier": "recovery-server", "version": "2.0.0", "registryBaseUrl": case.index,
         "transport": {"type": "stdio"}}]}}
    inspected = case.client.post(base + "/candidate-manifests", json={"target": target, "source": source})
    inspected.raise_for_status()
    document = inspected.json()["data"]
    candidate = {"target": target, "source": source, "manifest_digest": document["manifest_digest"], "variant": document["variants"][0]["ref"]}
    planned = case.client.post(base + "/candidate-plans", json=candidate)
    planned.raise_for_status()
    request = {"candidate": candidate, "plan_digest": planned.json()["data"]["plan_digest"], "operation_id": "last-consumer", "entrypoint": "recovery-server"}
    accepted = case.client.post(base + "/installations", json=request)
    accepted.raise_for_status()
    assert case.blocked.wait(10)
    database = case.home / "artifacts/mcp" / target["backend_id"] / "installations.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        pid, = connection.execute("SELECT pid FROM operation_owners").fetchone()
    children = psutil.Process(pid).children(recursive=True)
    assert any("uv" in child.name().lower() for child in children)
    path = base + "/installation-operations/last-consumer"
    with ThreadPoolExecutor(max_workers=3) as executor:
        receipts = list(executor.map(lambda _: case.client.post(path + "/cancel", json=target), range(3)))
    assert all(response.status_code == 200 for response in receipts)
    assert all(response.json()["data"]["status"] in {"running", "cancelled"} for response in receipts)
    deadline = time.monotonic() + 15
    result = case.client.get(path).json()["data"]
    while result["status"] == "running":
        assert time.monotonic() < deadline, result
        time.sleep(0.1)
        result = case.client.get(path).json()["data"]
    assert result["status"] == "cancelled" and result["cancellation_requested"] and result["artifact"] is None, result
    _, alive = psutil.wait_procs(children, timeout=3)
    assert not alive
    assert case.client.get(base + "/installations").json()["data"] == []
    assert case.client.post(base + "/installations", json=request).json()["data"] == result
    downloads = list(case.downloads)
    assert case.client.post(path + "/cancel", json=target).json()["data"] == result
    assert case.downloads == downloads
    (case.context.artifacts_dir / "last-cancellation.json").write_text(json.dumps({"result": result, "children": [child.pid for child in children]}, indent=2) + "\n")
