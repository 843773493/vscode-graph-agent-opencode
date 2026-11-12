"""独立 Gateway 与下游 HTTP 替身验证资源查询错误及同入口恢复。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app.schemas.internal_v2.communication import ContextQueryRequest
from app.schemas.search_federation import GatewayContextSearchRequest
from app.services.business.session_context_resource import (
    gateway_session_inventory_identity,
    gateway_session_inventory_uri,
    session_resource_identity,
    session_resource_uri,
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.diagnostic_sink import install_failing_diagnostic_sink
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    ensure_local_port_available,
    terminate_process,
    wait_for_http_ok,
    workspace_backend_id_for_root,
)
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A

resource_stub_app = FastAPI()


def create_catalog_diagnostic_failure_app() -> FastAPI:
    """仅组合可开关的日志故障，聚合、鉴权和路由仍使用真实 Gateway。"""
    from app.gateway.main import app

    install_failing_diagnostic_sink(
        logger_names=("app.gateway.control.resource_catalog",),
        control=Path(os.environ["BOXTEAM_TEST_CATALOG_SINK_CONTROL"]),
        destination=Path(os.environ["BOXTEAM_TEST_CATALOG_SINK_DESTINATION"]),
        audit=Path(os.environ["BOXTEAM_TEST_CATALOG_SINK_AUDIT"]),
    )
    return app


@resource_stub_app.post("/api/v1/context/read")
def stub_context_read(request: Request, query: dict[str, object]) -> JSONResponse:
    control = json.loads(Path(os.environ["BOXTEAM_TEST_CATALOG_CONTROL"]).read_text())
    request_id = request.headers["X-Request-ID"]
    with Path(control["requests_path"]).open("a") as output:
        output.write(json.dumps({"workspace_id": request.headers["X-BoxTeam-Workspace-Id"], "request_id": request_id}) + "\n")
    reference = {
        "display_uri": control.get("display_uri"), "resource_kind": "mcp_catalog", "owner_scope": "workspace",
        "facet": "catalog", "revision": "sha256:jcs:v1:" + "a" * 64, "availability": "available",
        "provenance_ref": {"kind": "sealed_resource", "token": "sha256:jcs:v1:" + "b" * 64},
    }
    item = {"kind": "record", "uri": control.get("item_uri"), "text": "/user/document.txt",
            "resource_refs": [reference]}
    result = {**{key: query[key] for key in ("resource_identity", "uri", "scope", "view")},
              "revision": "read-fixture-1", "items": [item]}
    if control["mode"] == "read_uri":
        result["uri"] = control["native_path"]
    elif control["mode"] == "item_uri":
        item["uri"] = control["native_path"]
    elif control["mode"] == "display_uri":
        reference["display_uri"] = control["native_path"]
    elif control["mode"] == "missing_display_uri":
        del reference["display_uri"]
    return JSONResponse({"request_id": request_id, "data": result}, headers={"X-Request-ID": request_id})


@resource_stub_app.get("/api/v1/health")
async def stub_health(workspace_id: str | None = Header(default=None, alias="X-BoxTeam-Workspace-Id")) -> object:
    return {"status": "ok", "workspace_id": workspace_id or os.environ["BOXTEAM_TEST_CATALOG_OWNER"]}


@resource_stub_app.get("/api/v1/config/reload-status")
async def stub_config() -> object:
    return {"data": {"healthy": True, "restart_required": False, "changed_sections": []}}


@resource_stub_app.get("/api/v1/session-catalog/export")
async def stub_catalog() -> object:
    return {"data": {"revision": "resource-query-empty", "items": []}}


@resource_stub_app.post("/api/v1/search")
def stub_context_search(request: Request, query: dict[str, object]) -> JSONResponse:
    control = json.loads(Path(os.environ["BOXTEAM_TEST_CATALOG_CONTROL"]).read_text())
    owner = request.headers["X-BoxTeam-Workspace-Id"]
    request_id = request.headers["X-Request-ID"]
    with Path(control["requests_path"]).open("a") as output:
        output.write(json.dumps({"workspace_id": owner, "request_id": request_id}) + "\n")
    result = {
        "resource_identity": str(workspace_session_inventory_identity(owner)),
        "uri": workspace_session_inventory_uri(owner, gateway_authority=request.headers["X-BoxTeam-Gateway-Id"]),
        "scope": "workspace", "query": query["query"], "match_mode": query["match_mode"],
        "revision": "query-fixture-1", "total_matches": 1,
        "matches": [{
            "resource_identity": str(session_resource_identity(owner, SESSION_A)),
            "uri": session_resource_uri(owner, SESSION_A), "scope": "workspace",
            "preview": "context-query-marker", "source": "effective_context", "revision": "history-fixture-1",
            "record_index": 0, "match_start": 0, "match_end": 20,
        }],
    }
    if owner == os.environ["BOXTEAM_TEST_CATALOG_OWNER"]:
        if control["mode"] == "validation":
            result["total_matches"] = control["native_path"]
        elif control["mode"] == "search_uri":
            result["uri"] = control["native_path"]
        elif control["mode"] == "match_uri":
            result["matches"][0]["uri"] = control["native_path"]
    return JSONResponse({"request_id": request_id, "data": {
        "provider_id": "session-context", "workspace_id": owner, "category": "context", "operation": "content",
        "contract_version": 1, "state": "complete", "context": result,
    }}, headers={"X-Request-ID": request_id})


@resource_stub_app.get("/api/v1/resources")
def stub_resources(request: Request) -> JSONResponse:
    control = json.loads(Path(os.environ["BOXTEAM_TEST_CATALOG_CONTROL"]).read_text())
    owner = request.headers["X-BoxTeam-Workspace-Id"]
    with Path(control["requests_path"]).open("a") as output:
        output.write(json.dumps({"workspace_id": owner, "request_id": request.headers["X-Request-ID"]}) + "\n")
    resource = {
        "resource_id": f"browser-{owner}", "workspace_id": owner, "kind": "browser", "retention_scope": "workspace",
        "source_thread": {"session_id": SESSION_A, "thread_id": THREAD_A}, "thread_associations": [],
        "owner_revision": 3, "status": "running", "reconciliation_status": "verified",
        "unattached_since": None, "reclaim_at": None, "available_actions": ["stop", "delete"],
        "metadata": {"title": "查询用浏览器"}, "created_at": "2026-10-05T00:00:00Z",
        "updated_at": "2026-10-05T00:00:00Z", "error": None,
    }
    if owner == os.environ["BOXTEAM_TEST_CATALOG_OWNER"] and control["mode"] != "ready":
        if control["mode"] == "validation":
            return JSONResponse({"data": {"workspace_id": owner, "items": control["native_path"]}})
        if control["mode"] == "owner-envelope":
            return JSONResponse({"data": {"workspace_id": control["native_path"], "items": [resource]}})
        if control["mode"] == "owner-item":
            return JSONResponse({"data": {"workspace_id": owner, "items": [resource, {
                **resource, "resource_id": "browser-foreign", "workspace_id": control["native_path"],
            }]}})
        return JSONResponse({"message": control["native_path"]}, status_code=503 if control["mode"] == "http-status" else 200)
    return JSONResponse({"data": {"workspace_id": owner, "items": [resource]}})


@pytest.fixture
def catalog_upstream(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> Iterator[tuple[TestRunContext, Path, str, int, int]]:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = Path(integration_workspace_root_path)
    owner = workspace_backend_id_for_root(workspace)
    runtime = context.runtime_root_for_node(request.node.nodeid)
    runtime.mkdir(parents=True, exist_ok=True)
    control = runtime / "resource-response.json"
    control.write_text(json.dumps({"mode": "ready", "requests_path": str(context.artifacts_dir / "upstream-requests.jsonl")}))
    ports = integration_port_block_for_file(Path(request.node.fspath))
    ensure_local_port_available(ports.port(0), service_name="资源目录 HTTP 替身")
    with ExitStack() as lifetime:
        log = lifetime.enter_context((context.artifacts_dir / "upstream.log").open("a"))
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", f"{__name__}:resource_stub_app", "--host", "127.0.0.1",
             "--port", str(ports.port(0)), "--log-level", "warning"],
            env={**os.environ, "BOXTEAM_TEST_CATALOG_OWNER": owner, "BOXTEAM_TEST_CATALOG_CONTROL": str(control)},
            stdout=log, stderr=log, text=True,
        )
        lifetime.callback(terminate_process, process)
        wait_for_http_ok(f"http://127.0.0.1:{ports.port(0)}/api/v1/health", process)
        yield context, control, owner, ports.port(0), ports.port(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("sink_fails", [False, True], ids=["log-ok", "log-failed"])
async def test_resource_catalog_errors_keep_healthy_scope_and_recover(
    catalog_upstream: tuple[TestRunContext, Path, str, int, int], sink_fails: bool,
) -> None:
    context, control, failed_owner, upstream_port, gateway_port = catalog_upstream
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    sibling = context.workspace_root / f"catalog-sibling-{'log-failed' if sink_fails else 'log-ok'}"
    sibling.mkdir()
    sibling_owner = workspace_backend_id_for_root(sibling)
    sink_control = control.with_name("diagnostic-enabled")
    sink_blocker = control.with_name("diagnostic-log-secret")
    sink_blocker.write_text("普通文件不能作为日志目录", encoding="utf-8")
    audit = context.artifacts_dir / "resource-catalog-diagnostics.jsonl"
    evidence = []
    with ExitStack() as lifetime:
        gateway = start_gateway_process(
            workspace_root=context.workspace_root, default_backend_url=f"http://127.0.0.1:{upstream_port}",
            port=gateway_port, artifacts_dir=context.artifacts_dir,
            asgi_app=f"{__name__}:create_catalog_diagnostic_failure_app",
            extra_env={"BOXTEAM_TEST_CATALOG_SINK_CONTROL": str(sink_control),
                       "BOXTEAM_TEST_CATALOG_SINK_DESTINATION": str(sink_blocker / "service.log"),
                       "BOXTEAM_TEST_CATALOG_SINK_AUDIT": str(audit)},
        )
        lifetime.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway_port}", headers=LOCAL_TOKEN_HEADERS, timeout=30) as client:
            added = await client.post("/api/gateway/workspaces/local", json={
                "root_path": str(sibling), "name": "健康工作区", "backend_url": f"http://127.0.0.1:{upstream_port}",
            })
            assert added.status_code == 200, added.text
            targets = {item["workspace_id"]: item for item in added.json()["data"]["items"]}
            assert set(targets) == {failed_owner, sibling_owner}
            failed_scope = f"{targets[failed_owner]['gateway_id']}/{failed_owner}"
            initial = await client.get("/api/gateway/resources")
            assert initial.status_code == 200 and initial.json()["data"]["errors"] == [], initial.text
            assert {(item["gateway_id"], item["workspace_id"]) for item in initial.json()["data"]["items"]} == {
                (target["gateway_id"], target["workspace_id"]) for target in targets.values()
            }
            assert all(item["gateway_connection_id"] is None for item in initial.json()["data"]["items"])
            healthy_resource, = [item for item in initial.json()["data"]["items"]
                                 if item["resource"]["workspace_id"] == sibling_owner]
            if sink_fails:
                sink_control.touch()
            state = json.loads(control.read_text())
            for mode in ("envelope", "validation", "http-status", "owner-envelope", "owner-item"):
                for index, native_path in enumerate(("/private/catalog-marker/state.sqlite", r"C:\private\catalog-marker\state.sqlite", r"\\server\catalog-marker\state.sqlite", sibling_owner)):
                    state.update(mode=mode, native_path=native_path)
                    control.write_text(json.dumps(state))
                    request_id = f"catalog-{mode}-{index}"
                    failed = await client.get("/api/gateway/resources", headers={"X-Request-ID": request_id})
                    assert failed.status_code == 200, failed.text
                    body = failed.json()
                    assert body["request_id"] == failed.headers["X-Request-ID"] == request_id
                    assert "catalog-marker" not in failed.text
                    assert str(sink_blocker) not in failed.text
                    item, = body["data"]["items"]
                    assert item == healthy_resource
                    assert item["workspace_id"] == targets[sibling_owner]["workspace_id"]
                    assert item["resource"]["workspace_id"] == sibling_owner
                    error, = body["data"]["errors"]
                    assert error["scope_key"] == failed_scope
                    assert error["label"] == f"本机 Gateway · {targets[failed_owner]['name']}"
                    error_type = {"envelope": "ValueError", "validation": "ValidationError", "http-status": "HTTPStatusError", "owner-envelope": "ValueError", "owner-item": "ValueError"}[mode]
                    expected = f"工作区资源目录读取失败: {error_type}" + (", status=503" if mode == "http-status" else "")
                    if sink_fails:
                        expected += ", diagnostic_sink_error=NotADirectoryError"
                    assert error["message"] == expected
                    observed = [json.loads(line) for line in Path(state["requests_path"]).read_text().splitlines()]
                    assert {row["workspace_id"] for row in observed if row["request_id"] == request_id} == {failed_owner, sibling_owner}
                    evidence.append(body)
            state["mode"] = "ready"
            control.write_text(json.dumps(state))
            recovered = await client.get("/api/gateway/resources")
            assert recovered.status_code == 200, recovered.text
            assert recovered.json()["data"]["errors"] == []
            assert {item["resource"]["workspace_id"] for item in recovered.json()["data"]["items"]} == {failed_owner, sibling_owner}
            evidence.append(recovered.json())
            assert next(item for item in recovered.json()["data"]["items"]
                        if item["resource"]["workspace_id"] == sibling_owner) == healthy_resource
            if sink_fails:
                records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
                assert len(records) == 20
                assert [record["request_id"] for record in records] == [body["request_id"] for body in evidence[:-1]]
                assert all(record["workspace_id"] == failed_scope for record in records)
                assert all(record["error_type"] in {"ValueError", "ValidationError", "HTTPStatusError"} for record in records)
                assert "catalog-marker" in audit.read_text(encoding="utf-8")
                # 下游恢复后即使日志故障尚未解除，也不应再产生失败记录或影响资源查询。
                sink_control.unlink()
                log_recovered = await client.get("/api/gateway/resources")
                assert log_recovered.status_code == 200 and log_recovered.json()["data"] == recovered.json()["data"]
                assert len(audit.read_text(encoding="utf-8").splitlines()) == 20
        logs = (audit if sink_fails else context.artifacts_dir / "gateway.stderr.log").read_text(encoding="utf-8")
        assert "catalog-marker" in logs and "Traceback" in logs
        assert "catalog-validation-1" in logs
        (context.artifacts_dir / "catalog-errors.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
async def test_context_search_validation_errors_keep_healthy_results_and_recover(
    catalog_upstream: tuple[TestRunContext, Path, str, int, int],
) -> None:
    context, control, failed_owner, upstream_port, gateway_port = catalog_upstream
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    sibling = context.workspace_root / "search-sibling"
    sibling.mkdir()
    sibling_owner = workspace_backend_id_for_root(sibling)
    evidence = []
    with ExitStack() as lifetime:
        gateway = start_gateway_process(
            workspace_root=context.workspace_root, default_backend_url=f"http://127.0.0.1:{upstream_port}",
            port=gateway_port, artifacts_dir=context.artifacts_dir,
        )
        lifetime.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway_port}", headers=LOCAL_TOKEN_HEADERS, timeout=30) as client:
            health = await client.get("/api/gateway/health")
            health.raise_for_status()
            gateway_id = health.json()["data"]["gateway_id"]
            added = await client.post("/api/gateway/workspaces/local", json={
                "root_path": str(sibling), "name": "健康搜索工作区", "backend_url": f"http://127.0.0.1:{upstream_port}",
            })
            assert added.status_code == 200, added.text
            assert {item["workspace_id"] for item in added.json()["data"]["items"]} == {failed_owner, sibling_owner}
            query = GatewayContextSearchRequest.model_validate({
                "source_gateway_id": gateway_id,
                "resource_target": {"gateway_id": gateway_id,
                    "resource_identity": str(gateway_session_inventory_identity(gateway_id)),
                    "uri": gateway_session_inventory_uri(gateway_id), "scope": "gateway"},
                "resource_search": {"scope": {"mode": "all"}, "queries": [{
                    "category": "context", "operation": "content", "match_mode": "literal", "query": "context-query-marker",
                }]},
            })
            payload = {"operation": "read", "resource_target": query.resource_target.to_payload(), "body": query.model_dump(mode="json")}
            state = json.loads(control.read_text())
            faults = [(mode, path) for mode in ("validation", "search_uri", "match_uri") for path in (
                "/private/context-marker/state.sqlite", r"C:\private\context-marker\state.sqlite", r"\\server\context-marker\state.sqlite",
            )]
            for index, (mode, native_path) in enumerate(faults):
                state.update(mode=mode, native_path=native_path)
                control.write_text(json.dumps(state))
                request_id = f"context-validation-{index}"
                failed = await client.post("/api/gateway/federation/relay", json=payload, headers={"X-Request-ID": request_id})
                assert failed.status_code == 200, failed.text
                body = failed.json()
                assert body["request_id"] == failed.headers["X-Request-ID"] == request_id
                assert "context-marker" not in failed.text
                result = body["data"]
                events = result["events"]
                assert events[0]["type"] == "scope" and events[-1]["type"] == "end"
                assert events[-1]["summary"]["returned_results"] == 1
                page, = [event["page"]["context"] for event in events if event["type"] == "page"]
                match, = page["matches"]
                assert match["resource_identity"] == str(session_resource_identity(sibling_owner, SESSION_A))
                assert match["uri"] == session_resource_uri(sibling_owner, SESSION_A)
                error, = [branch for branch in events[-1]["summary"]["branches"] if branch["state"] == "failed"]
                assert error["code"] == "invalid_response"
                assert error["branch"]["owner"]["workspace_id"] == failed_owner
                observed = [json.loads(line) for line in Path(state["requests_path"]).read_text().splitlines()]
                assert {row["workspace_id"] for row in observed if row["request_id"] == request_id} == {failed_owner, sibling_owner}
                evidence.append(body)
            state["mode"] = "ready"
            control.write_text(json.dumps(state))
            recovered = await client.post("/api/gateway/federation/relay", json=payload)
            assert recovered.status_code == 200, recovered.text
            restored = recovered.json()["data"]
            assert restored["events"][-1]["summary"]["state"] == "complete"
            assert restored["events"][-1]["summary"]["returned_results"] == 2
            assert {match["uri"] for event in restored["events"] if event["type"] == "page"
                    for match in event["page"]["context"]["matches"]} == {
                session_resource_uri(owner, SESSION_A) for owner in (failed_owner, sibling_owner)
            }
            # 一个分支恢复不改变健康 owner 的版本，不再为错误集合伪造全局 revision。
            previous_page, = [event["page"]["context"] for event in evidence[-1]["data"]["events"] if event["type"] == "page"]
            healthy_page, = [event["page"]["context"] for event in restored["events"] if event["type"] == "page"
                             and event["branch"]["branch"]["owner"]["workspace_id"] == sibling_owner]
            assert healthy_page["revision"] == previous_page["revision"] == "query-fixture-1"
            evidence.append(recovered.json())
        logs = (context.artifacts_dir / "gateway.stderr.log").read_text()
        assert "context-marker" in logs and "ValidationError" in logs and "Traceback" in logs
        assert all(f"context-validation-{index}" in logs for index in range(len(faults)))
        (context.artifacts_dir / "context-search-errors.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
async def test_context_read_rejects_invalid_addresses_and_preserves_null_and_dangling_uri(
    catalog_upstream: tuple[TestRunContext, Path, str, int, int],
) -> None:
    context, control, owner, upstream_port, gateway_port = catalog_upstream
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    evidence = []
    with ExitStack() as lifetime:
        gateway = start_gateway_process(
            workspace_root=context.workspace_root, default_backend_url=f"http://127.0.0.1:{upstream_port}",
            port=gateway_port, artifacts_dir=context.artifacts_dir,
        )
        lifetime.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway_port}", headers=LOCAL_TOKEN_HEADERS, timeout=30) as client:
            health = await client.get("/api/gateway/health")
            health.raise_for_status()
            gateway_id = health.json()["data"]["gateway_id"]
            read = {
                "resource_identity": str(session_resource_identity(owner, SESSION_A)),
                "uri": session_resource_uri(owner, SESSION_A), "scope": "workspace", "view": "overview",
            }
            query = ContextQueryRequest.model_validate({
                "source_gateway_id": gateway_id,
                "resource_target": {"gateway_id": gateway_id, **{key: read[key] for key in ("resource_identity", "uri", "scope")}},
                "read": read,
            })
            payload = {"operation": "read", "resource_target": query.resource_target.to_payload(), "body": query.model_dump(mode="json")}
            state = json.loads(control.read_text())
            faults = [(field, path) for field in ("read_uri", "item_uri", "display_uri") for path in (
                "/private/context-marker/source.json", r"C:\private\context-marker\source.json",
                r"\\server\context-marker\source.json", "", "file:///private/context-marker/source.json",
            )] + [("missing_display_uri", "unused")]
            for index, (field, path) in enumerate(faults):
                state.update(mode=field, native_path=path)
                control.write_text(json.dumps(state))
                request_id = f"context-address-{index}"
                failed = await client.post("/api/gateway/federation/relay", json=payload, headers={"X-Request-ID": request_id})
                assert failed.status_code == 409, failed.text
                body = failed.json()
                assert body["request_id"] == failed.headers["X-Request-ID"] == request_id
                assert body["detail"]["code"] == "federation-malformed-frame"
                assert "context-marker" not in failed.text and "input_value" not in failed.text
                evidence.append(body)
            # 地址只核 grammar，不解析当前文件或补造内部聚合地址；合法历史字节原样保留。
            dangling = "boxteam://gateway_remote/workspace/ws_missing/resources/files/root/%7eabsent%2520file"
            for uri in (None, dangling):
                state.update(mode="ready", display_uri=uri, item_uri=uri)
                control.write_text(json.dumps(state))
                recovered = await client.post("/api/gateway/federation/relay", json=payload)
                assert recovered.status_code == 200, recovered.text
                result = recovered.json()["data"]
                assert all(result[key] == read[key] for key in read)
                item, = result["items"]
                reference, = item["resource_refs"]
                assert item["uri"] == reference["display_uri"] == uri
                assert item["text"] == "/user/document.txt"
                assert reference["revision"] == "sha256:jcs:v1:" + "a" * 64
                assert reference["provenance_ref"] == {"kind": "sealed_resource", "token": "sha256:jcs:v1:" + "b" * 64}
                evidence.append(recovered.json())
            observed = [json.loads(line) for line in Path(state["requests_path"]).read_text().splitlines()]
            assert {row["request_id"] for row in observed}.issuperset(f"context-address-{index}" for index in range(len(faults)))
        logs = (context.artifacts_dir / "gateway.stderr.log").read_text()
        assert "context-marker" in logs and "ValidationError" in logs
        (context.artifacts_dir / "context-addresses.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
