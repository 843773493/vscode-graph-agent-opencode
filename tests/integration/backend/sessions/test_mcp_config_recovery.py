"""真实 OS 退出窗口中的 MCP 配置提交、连接重建与 HTTP 事件续读。"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.core.workspace_identity import load_or_create_workspace_id
from configs.installer import install_user_configuration
from configs.runtime import read_jsonc_object
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.mcp.test_invocation_lifecycle import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 复用唯一 HTTP/MCP 夹具
)
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS
from tests.support.processes.scope import register_test_process, unregister_test_process
from tests.support.processes.workspace import terminate_process
from tests.support.workspaces import prepare_default_test_workspace

_PROCESS = r'''
import asyncio, json, os, sys
from pathlib import Path
import uvicorn
from app import main

async def run():
    mode, identity, port, report_dir = sys.argv[1:]
    report_dir = Path(report_dir)
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=int(port), log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("Backend 尚未就绪即退出")
            await asyncio.sleep(0.02)
        container = main.app.state.workspace_container_registry.get(identity)
        service, owner = container.config_service, container.mcp_catalog_owner

        def report(name):
            status = service.get_reload_status()
            configuration = service.get_snapshot().to_dict()["mcp"]["servers"]["schema"]
            temporary = report_dir / (name + ".tmp")
            temporary.write_text(json.dumps({
                "pid": os.getpid(), "url": configuration.get("url"), "server": configuration,
                "connection_ref": owner._connection_ref(owner._runtimes["schema"]),
                "catalog_status": owner.publication_snapshot().status,
                "state": status.state, "healthy": status.healthy, "active_revision": status.active_revision,
            }), encoding="utf-8")
            temporary.replace(report_dir / name)

        if mode == "cleanup_failure":
            from app.services.infrastructure.events.channel_events import resource_state_channel_name

            def fail_release():
                raise RuntimeError("/private/transport/path token=must-never-leak")

            owner._runtimes["schema"].scope.register(fail_release, label="injected-transport-cleanup")
            channel = container.job_event_bus.event_channel_service.channel(resource_state_channel_name("mcp"))
            subscription = channel.subscribe(label="cleanup product evidence")

            async def record_cleanup_event():
                try:
                    delivery = await subscription.next()
                    event = delivery.event
                    (report_dir / "cleanup-event.json").write_text(json.dumps({
                        "owner_domain": event.owner_domain, "resource_id": event.resource_id,
                        "state": event.state,
                    }))
                finally:
                    channel.unsubscribe(subscription)

            asyncio.create_task(record_cleanup_event())
        elif mode == "after_stdio_stop":
            close = owner._runtimes["schema"].scope.close
            async def exit_after_stdio_stop():
                await close()
                report("fault.json")
                os._exit(83)
            owner._runtimes["schema"].scope.close = exit_after_stdio_stop
        elif mode != "recover":
            persist = service._persist_active_snapshot
            def exit_at_promotion(*args, **kwargs):
                if kwargs.get("apply_id") is None:
                    return persist(*args, **kwargs)
                if mode == "after_promotion":
                    persist(*args, **kwargs)
                report("fault.json")
                os._exit(81 if mode == "before_promotion" else 82)
            service._persist_active_snapshot = exit_at_promotion
        report("ready.json")
        await task
    finally:
        server.should_exit = True
        await task

asyncio.run(run())
'''


@dataclass(frozen=True)
class RecoveryCase:
    context: TestRunContext
    mount: MountedWorkspace
    environment: dict[str, str] = field(repr=False)

    @asynccontextmanager
    async def process(self, mode: str) -> AsyncIterator[tuple[subprocess.Popen[str], Path, str]]:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        reports = self.context.artifacts_dir / f"{mode}-{uuid4().hex}"
        reports.mkdir()
        with (reports / "process.raw").open("wb") as output:
            process = await asyncio.to_thread(subprocess.Popen,
                [sys.executable, "-c", _PROCESS, mode, self.mount.workspace_id, str(port), str(reports)],
                cwd=Path.cwd(), env=self.environment, stdout=output, stderr=subprocess.STDOUT, text=True,
            )
            register_test_process(process, lambda: terminate_process(process))
            try:
                yield process, reports, f"http://127.0.0.1:{port}"
            finally:
                await asyncio.to_thread(terminate_process, process)
                unregister_test_process(process)

    def facts(self) -> dict[str, list[dict[str, object]]]:
        path = self.mount.workspace_root / ".boxteam/state/workspace.sqlite"
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            return {
                table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in ("config_active_snapshot", "config_pending_candidate", "config_apply_journal", "config_apply_claim", "config_events")
            }


@pytest.fixture
def recovery_case(request: pytest.FixtureRequest) -> RecoveryCase:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = prepare_default_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    (root / ".boxteam/workspace-identity.json").unlink(missing_ok=True)
    home = context.boxteam_home_for_node(request.node.nodeid)
    install_user_configuration(config_root=home / "config", profile="default", project_root=Path.cwd(), force=True)
    mount = MountedWorkspace(load_or_create_workspace_id(root), root)
    environment = {**os.environ, "BOXTEAM_HOME": str(home), "PYTHONUNBUFFERED": "1",
                   "BOXTEAM_MOUNTED_WORKSPACES": json.dumps([mount.to_payload()]),
                   "BOXTEAM_BACKEND_GENERATION": f"test_backend_{uuid4().hex}"}
    environment.pop("WORKSPACE_ROOT", None)
    return RecoveryCase(context, mount, environment)


async def ready(process: subprocess.Popen[str], reports: Path) -> dict[str, object]:
    async with asyncio.timeout(45):
        while not (reports / "ready.json").exists():
            assert process.poll() is None, (reports / "process.raw").read_text()
            await asyncio.sleep(0.05)
    return json.loads((reports / "ready.json").read_text())


async def test_real_retirement_failure_keeps_committed_config_and_reports_cleanup(
    recovery_case: RecoveryCase, http_schema_endpoint_factory,
) -> None:
    case = recovery_case
    async with http_schema_endpoint_factory("old") as old, http_schema_endpoint_factory("new") as new:
        new.definitions.write_text(json.dumps({"probe": {"type": "object"}, "new_connection": {"type": "object"}}))
        path = case.mount.workspace_root / ".boxteam/workspace.jsonc"
        payload = read_jsonc_object(path) if path.exists() else {}
        payload["mcp"] = {"servers": {"schema": {"enabled": True, "transport": "streamable_http", "url": old.url}}}
        path.write_text(json.dumps(payload), encoding="utf-8")
        async with case.process("cleanup_failure") as (process, reports, base_url):
            original = await ready(process, reports)
            payload["mcp"]["servers"]["schema"]["url"] = new.url
            path.write_text(json.dumps(payload), encoding="utf-8")
            prefix = f"/api/v1/workspaces/{case.mount.workspace_id}"
            async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=10) as client:
                async with asyncio.timeout(25):
                    while True:
                        response = await client.get(prefix + "/config/reload-status", headers={"X-Request-ID": "mcp-cleanup-proof"})
                        assert response.status_code == 200, response.text
                        status = response.json()["data"]
                        if status["mcp_cleanup"] and status["mcp_cleanup"][0]["phase"] == "cleanup_failed":
                            break
                        await asyncio.sleep(0.05)
                assert response.headers["x-request-id"] == response.json()["request_id"] == "mcp-cleanup-proof"
                assert status["healthy"] and status["state"] == "active"
                assert status["applied_paths"] == ["/mcp/servers/schema/url"]
                assert not status["restart_required"] and status["deferred_paths"] == []
                cleanup, = status["mcp_cleanup"]
                assert cleanup == {"server_id": "schema", "connection_ref": original["connection_ref"],
                                   "phase": "cleanup_failed", "release_verified": False, "error_type": "ExceptionGroup"}
                assert "private" not in response.text and "must-never-leak" not in response.text
                servers = await client.get(prefix + "/mcp/servers")
                assert servers.status_code == 200, servers.text
                assert {tool["tool_id"] for server in servers.json()["data"] for tool in server["tools"]} == {
                    "mcp__schema__probe", "mcp__schema__new_connection",
                }
                event = json.loads((reports / "cleanup-event.json").read_text())
                assert event == {"owner_domain": "mcp", "resource_id": original["connection_ref"], "state": "release_failed"}
                published = case.facts()
                assert published["config_apply_journal"][-1]["state"] == "committed"
                assert published["config_events"][-1]["result"] == "applied"
                assert len(published["config_events"]) == 1
                # 失败引用不清除；冲突的新候选不能假装继续替换成功。
                payload["mcp"]["servers"]["schema"]["url"] = old.url
                path.write_text(json.dumps(payload), encoding="utf-8")
                async with asyncio.timeout(20):
                    while True:
                        response = await client.get(prefix + "/config/reload-status")
                        assert response.status_code == 200, response.text
                        rejected = response.json()["data"]
                        if rejected["state"] == "rejected":
                            break
                        await asyncio.sleep(0.05)
                assert rejected["applied_paths"] == [] and rejected["mcp_cleanup"] == [cleanup]
                assert case.facts()["config_active_snapshot"] == published["config_active_snapshot"]
                (case.context.artifacts_dir / "cleanup-proof.json").write_text(json.dumps({
                    "committed": status, "rejected": rejected, "event": event,
                }, indent=2))


@pytest.mark.parametrize(("window", "wait_for_expiry"), [
    ("before_promotion", False), ("before_promotion", True), ("after_promotion", False),
])
async def test_real_process_exit_recovers_commit_boundary_and_replays_http_events(
    recovery_case: RecoveryCase, http_schema_endpoint_factory, window: str, wait_for_expiry: bool,
) -> None:
    case = recovery_case
    async with http_schema_endpoint_factory("old") as old, http_schema_endpoint_factory("new") as new:
        new.definitions.write_text(json.dumps({"probe": {"type": "object"}, "new_connection": {"type": "object"}}))
        path = case.mount.workspace_root / ".boxteam/workspace.jsonc"
        payload = read_jsonc_object(path) if path.exists() else {}
        payload["mcp"] = {"servers": {"schema": {"enabled": True, "transport": "streamable_http", "url": old.url}}}
        path.write_text(json.dumps(payload), encoding="utf-8")
        async with case.process(window) as (process, reports, _base_url):
            original = await ready(process, reports)
            payload["mcp"]["servers"]["schema"]["url"] = new.url
            path.write_text(json.dumps(payload), encoding="utf-8")
            code = await asyncio.wait_for(asyncio.to_thread(process.wait), 20)
            assert code == (81 if window == "before_promotion" else 82), (reports / "process.raw").read_text()
            fault = json.loads((reports / "fault.json").read_text())
            assert fault["url"] == old.url and fault["connection_ref"] == original["connection_ref"]
        crashed = case.facts()
        case.context.artifacts_dir.joinpath("crashed.json").write_text(json.dumps(crashed, indent=2))
        active, = crashed["config_active_snapshot"]
        journal, = crashed["config_apply_journal"]
        assert json.loads(str(journal["side_effects_json"]))
        pending, = crashed["config_pending_candidate"]
        if window == "before_promotion":
            assert json.loads(str(active["payload_json"]))["mcp"]["servers"]["schema"]["url"] == old.url
            assert journal["state"] == pending["state"] == "applying"
            assert not any(event["result"] == "applied" for event in crashed["config_events"])
        else:
            assert json.loads(str(active["payload_json"]))["mcp"]["servers"]["schema"]["url"] == new.url
            assert journal["state"] == "committed" and pending["state"] == "active"
            assert [event["result"] for event in crashed["config_events"]].count("applied") == 1
        claim, = crashed["config_apply_claim"]
        expiry = datetime.fromisoformat(str(claim["lease_expires_at"]))
        if wait_for_expiry:
            await asyncio.sleep(max(0, (expiry - datetime.now(UTC)).total_seconds()) + 0.05)
        async with case.process("recover") as (process, reports, base_url):
            restored = await ready(process, reports)
            if window == "before_promotion" and not wait_for_expiry:
                assert datetime.now(UTC) < expiry, "未覆盖 lease 仍有效的立即重启窗口"
                assert restored["url"] == old.url and restored["state"] == "applying" and not restored["healthy"]
            else:
                assert restored["url"] == new.url and restored["state"] == "active" and restored["healthy"]
            async with httpx.AsyncClient(base_url=base_url, headers={**LOCAL_TOKEN_HEADERS, "X-BoxTeam-Workspace-Id": case.mount.workspace_id}) as client:
                async with asyncio.timeout(40):
                    while True:
                        response = await client.get("/api/v1/config/reload-status")
                        assert response.status_code == 200, response.text
                        assert response.json()["request_id"] == response.headers["X-Request-ID"]
                        status = response.json()["data"]
                        if window == "before_promotion" and datetime.now(UTC) < expiry:
                            assert status["state"] == "applying" and status["active_revision"] == original["active_revision"]
                            live_claim, = case.facts()["config_apply_claim"]
                            assert live_claim["fencing_token"] == claim["fencing_token"]
                        if status["state"] == "active" and status["healthy"] and not case.facts()["config_apply_claim"]:
                            break
                        assert process.poll() is None, (reports / "process.raw").read_text()
                        await asyncio.sleep(0.25)
                servers = await client.get("/api/v1/mcp/servers")
                assert servers.status_code == 200, servers.text
                assert servers.json()["request_id"] == servers.headers["X-Request-ID"]
                server, = servers.json()["data"]
                assert server["status"] == "ready"
                assert {tool["tool_id"] for tool in server["tools"]} == {"mcp__schema__probe", "mcp__schema__new_connection"}
                events = (await client.get("/api/v1/config/events")).json()["data"]["events"]
                applied, = [event for event in events if event["result"] == "applied"]
                assert applied["applied_paths"] == ["/mcp/servers/schema/url"]
                assert applied["deferred_paths"] == [] and applied["active_revision"] == status["active_revision"]
                # 真 HTTP SSE 从原游标续读；断开后再读仍得到同一提交事件。
                for _ in range(2):
                    async with client.stream("GET", "/api/v1/config/events/stream", headers={"Last-Event-ID": str(applied["event_seq"] - 1)}) as stream:
                        assert stream.status_code == 200
                        async with asyncio.timeout(5):
                            async for line in stream.aiter_lines():
                                if line.startswith("data: "):
                                    assert json.loads(line[6:]) == applied
                                    break
                            else:
                                pytest.fail("SSE 没有返回已提交配置事件")
            assert restored["pid"] != original["pid"]
            assert restored["connection_ref"] != original["connection_ref"]
        recovered = case.facts()
        case.context.artifacts_dir.joinpath("recovered.json").write_text(json.dumps(recovered, indent=2))
        assert json.loads(str(recovered["config_active_snapshot"][0]["payload_json"]))["mcp"]["servers"]["schema"]["url"] == new.url
        assert not recovered["config_apply_claim"]
        old_journal = next(row for row in recovered["config_apply_journal"] if row["apply_id"] == journal["apply_id"])
        assert old_journal["state"] == ("recovery_required" if window == "before_promotion" else "committed")
        assert not old.receipts.exists() and not new.receipts.exists()
