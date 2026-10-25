"""真实 Gateway 与共享 Backend；模型使用共享精确回放。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import commentjson
import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.core.mounted_workspaces import (
    MountedWorkspace,
    mounted_workspace_manifest_digest,
)
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import get_gateway_local_token
from app.gateway.credentials import (
    FederationCredential,
    FederationCredentialStore,
    load_or_create_gateway_id,
)
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.main import gateway_http_exception_handler
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes.workspaces_managed import router as managed_workspaces_router
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.workspace_ids import build_managed_local_workspace_id
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@dataclass
class SharedBackendCase:
    client: httpx.AsyncClient
    context: TestRunContext
    roots: tuple[Path, Path]
    boxteam_home: Path
    replay: ModelStreamReplay
    gateway: GatewayProcess
    planned_calls: dict[str, dict[str, str]] = field(default_factory=dict)

    async def data(self, method: str, path: str, **kwargs):
        response = await self.client.request(method, path, **kwargs)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert response.headers["X-Request-ID"] == payload["request_id"]
        return payload["data"]

    async def plan(self, workspace_id: str, session_id: str, text: str) -> str:
        main = await self.data("GET", f"/api/v1/sessions/{session_id}/main-thread", headers={"X-BoxTeam-Workspace-Id": workspace_id})
        thread_id = main["thread_id"]
        port = integration_port_block_for_file(self.context.test_file).port(80)
        interaction = build_cassette(
            asset_id="shared-managed-backend", model="e2e-stub-model",
            url=f"http://127.0.0.1:{port}/v1/chat/completions",
            frames=chat_completion_frames(
                response_id="chatcmpl-shared-owner", model="e2e-stub-model",
                deltas=({"role": "assistant", "content": text},), finish_reason="stop",
            ),
        ).raw["interactions"][0]
        call, = self.replay.control.plan_model_calls(session_id, thread_id, [interaction])
        self.planned_calls[call] = {"session_id": session_id, "thread_id": thread_id, "model_call_id": call}
        return call

    def request(self, call: str) -> dict[str, object]:
        artifact = json.loads((self.replay.artifacts / (call + ".json")).read_text(encoding="utf-8"))
        assert artifact["identity"] == self.planned_calls[call]
        assert artifact["request"]["stream"] is True
        return artifact["request"]


@pytest.fixture
async def shared_backend_case(request: pytest.FixtureRequest) -> AsyncIterator[SharedBackendCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    roots = (context.workspace_root / "primary", context.workspace_root / "secondary")
    for root in roots:
        prepare_default_test_workspace(
            workspace_root=root, template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        )
        (root / ".boxteam/workspace-identity.json").unlink(missing_ok=True)
        config_path = install_test_workspace_config(
            workspace_root=root, config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
            schema_path=Path.cwd() / "configs/workspace_schema.jsonc",
        )
        config = commentjson.loads(config_path.read_text())
        for provider in config["llm"]["providers"]:
            provider.update({
                "endpoint": f"http://127.0.0.1:{ports.port(80)}/v1", "model": "e2e-stub-model",
                "api_key": "e2e-local-model-key", "custom_llm_provider": "openai",
                "api_mode": {
                    "protocol": "chat_completions", "model_info": {"supports_function_calling": True, "supports_reasoning": False},
                    "supports_reasoning": {"reasoning_content": False},
                },
            })
            provider.pop("auth", None)
            provider.pop("request_options", None)
        for agent in config["agents"].values():
            agent["model"]["fallback_providers"] = []
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    boxteam_home = context.boxteam_home_for_node(request.node.nodeid)
    with model_stream_replay(
        context.artifacts_dir / request.node.name, runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="shared-backend-mount-isolation-and-controlled-exit",
    ) as replay:
        gateway = await asyncio.to_thread(
            start_gateway_process, workspace_root=roots[0], default_backend_url=None, port=ports.port(20),
            boxteam_home=boxteam_home, artifacts_dir=context.artifacts_dir / "gateway",
            asgi_app="tests.support.e2e_control.gateway_app:app",
            extra_env={**replay.environment, "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
        )
        case = None
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{ports.port(20)}", headers=LOCAL_TOKEN_HEADERS, timeout=180,
            ) as client:
                case = SharedBackendCase(client, context, roots, boxteam_home, replay, gateway)
                await acquire_gateway_guest(client)
                yield case
                assert {path.stem for path in replay.artifacts.glob("*.json")} == set(case.planned_calls)
        finally:
            await asyncio.to_thread(close_gateway_process, case.gateway if case is not None else gateway)


async def _join_group(case: SharedBackendCase):
    initial = await case.data("GET", "/api/gateway/workspaces")
    primary = next(item for item in initial["items"] if item["system_default"])
    added = await case.data("POST", "/api/gateway/workspaces/local", json={
        "root_path": str(case.roots[1]), "name": "Secondary",
    })
    secondary = next(item for item in added["items"] if item["workspace_id"] == build_managed_local_workspace_id(str(case.roots[1])))
    assert secondary["status"] == "ready", "普通新增必须自动启动独立 singleton"
    assert secondary["backend_generation"] != primary["backend_generation"]
    await case.data("POST", f"/api/gateway/workspaces/{secondary['workspace_id']}/runtime/stop")
    request = {
        "expected_backend_generation": primary["backend_generation"],
        "expected_workspace_ids": primary["affected_workspace_ids"],
        "desired_workspace_ids": [primary["workspace_id"], secondary["workspace_id"]],
    }
    joined = await case.data(
        "POST", f"/api/gateway/workspaces/{primary['workspace_id']}/runtime/restart-safe", json=request,
    )
    assert joined["status"] == "restarted"
    members = joined["workspaces"]["items"]
    assert all(item["status"] == "ready" for item in members)
    assert {item["backend_generation"] for item in members} == {joined["backend_generation"]}
    assert all(set(item["affected_workspace_ids"]) == set(request["desired_workspace_ids"]) for item in members)
    return primary["workspace_id"], secondary["workspace_id"], joined


@pytest.fixture
def root_binding_peer_credential(shared_backend_case: SharedBackendCase) -> FederationCredential:
    return FederationCredentialStore(
        storage_path=shared_backend_case.boxteam_home / "state/gateway/credentials/federation.json",
    ).issue(connection_id="root-binding-peer", peer_gateway_id="gateway_0123456789abcdef0123456789abcdef")


@pytest.fixture
async def root_binding_relay_client(
    shared_backend_case: SharedBackendCase, root_binding_peer_credential: FederationCredential,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    case = shared_backend_case
    source_home = case.boxteam_home.with_name(case.boxteam_home.name + "-relay")
    source_root = source_home / "state/gateway"
    monkeypatch.setenv("BOXTEAM_HOME", str(source_home))
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(source_root))
    registry = GatewayWorkspaceRegistry(storage_path=source_root / "workspaces.json")
    registry.upsert_remote_gateway(RemoteGatewayConnection(
        connection_id="root-binding-peer", name="Target", host="127.0.0.1", port=22,
        username="integration", private_key_path=None, ssh_config_host=None,
        remote_gateway_port=case.client.base_url.port,
        remote_gateway_id=load_or_create_gateway_id(case.boxteam_home / "state/gateway/identity.json"),
        protocol_version=1, source_owner="manual",
    ), runtime=WorkspaceRuntime(service_urls={"workspace_api": str(case.client.base_url)}))
    FederationCredentialStore(storage_path=source_root / "credentials/federation.json").put(root_binding_peer_credential)
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.add_exception_handler(HTTPException, gateway_http_exception_handler)
    app.state.registry = registry
    app.state.port_forward_manager = SshPortForwardManager(
        registry=registry, storage_path=source_root / "port-forwards.json", log_dir=source_root / "logs",
    )
    app.state.workspace_runtime_controller = GatewayWorkspaceRuntimeController(
        registry=registry, project_root=Path.cwd(), log_dir=source_root / "logs",
    )
    app.state.federation_policy_store = FederationPolicyStore(initial={"rules": [{
        "capability": "managed_workspace_admin", "effect": "allow",
        "principal_ref": principal_ref_for_gateway(root_binding_peer_credential.peer_gateway_id),
    }]})
    app.include_router(managed_workspaces_router)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://source-gateway",
            headers={"X-Local-Token": get_gateway_local_token()},
        ) as client:
            yield client
    finally:
        registry.close()


@pytest.mark.asyncio
async def test_workspace_startup_log_keeps_owner_without_native_roots_in_diagnostics(
    shared_backend_case: SharedBackendCase, root_binding_peer_credential: FederationCredential,
) -> None:
    case = shared_backend_case
    targets = await case.data("GET", "/api/gateway/workspaces")
    target = next(item for item in targets["items"] if item["system_default"])
    workspace_id, backend_workspace_id = target["workspace_id"], target["backend_workspace_id"]
    health = await case.client.get("/api/v1/health", headers={"X-BoxTeam-Workspace-Id": workspace_id})
    assert health.status_code == 200, health.text
    assert health.json()["workspace_id"] == backend_workspace_id
    log_id = f"workspace:{workspace_id}:workspace_api"
    receipts = []
    for route, owner_field, headers in (
        ("/api/gateway/diagnostics", "workspace_id", LOCAL_TOKEN_HEADERS),
        ("/api/gateway/federation/diagnostics", "remote_workspace_id", {
            "X-BoxTeam-Federation-Token": root_binding_peer_credential.token,
        }),
    ):
        response = await case.client.get(route, params={
            owner_field: workspace_id, "log_id": log_id, "tail_lines": 1000,
        }, headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert response.headers["X-Request-ID"] == payload["request_id"]
        data = payload["data"]
        assert data["selected_log_id"] == log_id
        log = next(item for item in data["logs"] if item["log_id"] == log_id)
        assert log["status"] == "available"
        assert log["workspace_id"] == workspace_id
        assert f"Workspace 挂载 ready: workspace_id={backend_workspace_id}" in log["tail"]
        assert f"Workspace 启动阶段 end: workspace_id={backend_workspace_id}" in log["tail"]
        # 核实真实启动 producer 与两个诊断 consumer，不用伪造日志代替产品启动。
        for native_root in (*case.roots, case.boxteam_home, Path.cwd()):
            assert str(native_root) not in response.text
        receipts.append({"route": route, "request_id": payload["request_id"], "log": log})
    assert not case.planned_calls
    (case.context.artifacts_dir / "startup-log-uri-boundary.json").write_text(
        json.dumps(receipts, ensure_ascii=False, indent=2) + "\n",
    )


@pytest.mark.asyncio
async def test_real_gateway_denies_ungranted_peer_before_directory_registry_or_process_changes(
    shared_backend_case: SharedBackendCase, root_binding_peer_credential: FederationCredential,
    root_binding_relay_client: httpx.AsyncClient,
) -> None:
    case = shared_backend_case
    gateway_root = case.boxteam_home / "state/gateway"
    credential = root_binding_peer_credential
    initial = await case.data("GET", "/api/gateway/workspaces")
    target = initial["items"][0]
    backend_before = (await case.client.get("/api/v1/health")).json()
    state_path = gateway_root / "gateway.sqlite"

    def registry_rows():
        with sqlite3.connect(f"file:{state_path}?mode=ro", uri=True) as connection:
            return {
                "targets": connection.execute("SELECT * FROM gateway_workspace_registry ORDER BY workspace_id").fetchall(),
                "metadata": connection.execute("SELECT * FROM gateway_config WHERE config_key='workspace_registry_meta'").fetchall(),
                "revision": connection.execute("SELECT * FROM registry_meta WHERE registry_key='workspace'").fetchall(),
                "journal": connection.execute("SELECT * FROM registry_apply_journal ORDER BY rowid").fetchall(),
            }

    before = registry_rows()
    denied_root = case.context.workspace_root / "denied-peer-created-root"
    assert not denied_root.exists()
    peer_headers = {"X-BoxTeam-Federation-Token": credential.token}
    receipts = []
    async with httpx.AsyncClient(base_url=str(case.client.base_url), headers=peer_headers, timeout=30) as peer:
        for route, key in (
            ("/api/gateway/federation/workspaces", "items"),
            ("/api/gateway/federation/managed-workspaces", "items"),
            ("/api/gateway/federation/diagnostics", "workspaces"),
        ):
            response = await peer.get(route)
            assert response.status_code == 200, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
            items = response.json()["data"][key]
            assert items
            assert all("root_path" not in item and "workspace_root" not in item for item in items)
        for method, path, payload in (
            ("POST", "/api/gateway/federation/managed-workspaces", {"root_path": str(denied_root), "create_directory": True}),
            ("DELETE", f"/api/gateway/federation/managed-workspaces/{target['workspace_id']}", None),
        ):
            request_id = f"root-binding-denied-{method.lower()}"
            response = await peer.request(method, path, json=payload, headers={"X-Request-ID": request_id})
            assert response.status_code == 403, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
            receipts.append({"method": method, "status": response.status_code, "request_id": request_id})
            assert registry_rows() == before
            assert not denied_root.exists()

    # 源端 ASGI 走生产 relay 路由及真实 HTTP；源策略 allow 不能替代目标独立进程的授权。
    relay_statuses = []
    for method, path, payload in (
        ("POST", "/api/gateway/managed-workspaces", {
            "gateway_connection_id": credential.connection_id, "root_path": str(denied_root), "create_directory": True,
        }),
        ("DELETE", f"/api/gateway/managed-workspaces/{target['workspace_id']}?gateway_connection_id={credential.connection_id}", None),
    ):
        request_id = f"root-binding-relay-denied-{method.lower()}"
        response = await root_binding_relay_client.request(method, path, json=payload, headers={"X-Request-ID": request_id})
        assert response.status_code == 403, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        relay_statuses.append(response.status_code)
        assert registry_rows() == before
        assert not denied_root.exists()
    for route, key in (
        ("/api/gateway/workspaces", "items"), ("/api/gateway/managed-workspaces", "items"),
        ("/api/gateway/inbound-access", "items"), ("/api/gateway/diagnostics", "workspaces"),
    ):
        data = await case.data("GET", route)
        assert data[key]
        assert all("root_path" not in item and "workspace_root" not in item for item in data[key])
    backend_after = (await case.client.get("/api/v1/health")).json()
    for key in ("process_id", "workspace_id", "backend_generation", "mount_manifest_digest"):
        assert backend_after[key] == backend_before[key]
    assert not list(case.replay.artifacts.glob("*.json"))
    (case.context.artifacts_dir / "root-binding-peer-denied.json").write_text(json.dumps({
        "receipts": receipts, "relay_statuses": relay_statuses,
        "registry_unchanged": True, "backend_before": backend_before, "backend_after": backend_after,
    }, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
async def test_real_peer_admin_root_binding_survives_restart_and_revocation(
    shared_backend_case: SharedBackendCase, root_binding_peer_credential: FederationCredential,
    root_binding_relay_client: httpx.AsyncClient,
) -> None:
    case = shared_backend_case
    config_path = case.boxteam_home / "config/gateway.jsonc"
    config = commentjson.loads(config_path.read_text())
    rules = [{
        "capability": "managed_workspace_admin", "effect": "allow",
        "principal_ref": principal_ref_for_gateway(root_binding_peer_credential.peer_gateway_id),
    }]

    async def publish_policy(value: list[dict[str, str]]) -> None:
        before = await case.data("GET", "/api/gateway/config/reload-status")
        config.setdefault("permissions", {})["federation"] = {"rules": value}
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        async with asyncio.timeout(30):
            while True:
                status = await case.data("GET", "/api/gateway/config/reload-status")
                assert status["last_error"] is None, status
                if status["active_revision"] != before["active_revision"]:
                    assert not status["restart_required"], status
                    assert any(path == "/permissions" or path.startswith("/permissions/federation")
                               for path in status["applied_paths"]), status
                    return
                await asyncio.sleep(0.1)

    await publish_policy(rules)
    probe = case.roots[1] / "root-binding-probe.txt"
    probe.write_text("原生 owner 的固定根\n")
    registered = await root_binding_relay_client.post("/api/gateway/managed-workspaces", json={
        "gateway_connection_id": root_binding_peer_credential.connection_id,
        "root_path": str(case.roots[1]), "name": "Peer managed root",
    })
    assert registered.status_code == 200, registered.text
    target_id = build_managed_local_workspace_id(str(case.roots[1]))
    native = await case.data("GET", "/api/gateway/workspaces")
    target = next(item for item in native["items"] if item["workspace_id"] == target_id)
    backend_id = target["backend_workspace_id"]
    assert target["status"] == "ready"
    for forbidden in (str(case.roots[1]), root_binding_peer_credential.token):
        assert forbidden not in registered.text

    async def read_bound_root() -> dict:
        response = await case.client.post(f"/api/v1/workspaces/{backend_id}/resources/navigation", json={
            "uri": f"boxteam://workspace/{backend_id}/resources/files/root/{probe.name}", "operation": "read",
        })
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        assert response.json()["data"]["content"] == "原生 owner 的固定根\n"
        assert str(case.roots[1]) not in response.text
        return response.json()

    await read_bound_root()
    gateway_pid = case.gateway.process.pid
    port = case.gateway.port
    await asyncio.to_thread(close_gateway_process, case.gateway)
    case.gateway = await asyncio.to_thread(
        start_gateway_process, workspace_root=case.roots[0], default_backend_url=None, port=port,
        boxteam_home=case.boxteam_home, artifacts_dir=case.context.artifacts_dir / "gateway-restarted",
        asgi_app="tests.support.e2e_control.gateway_app:app",
        extra_env={**case.replay.environment, "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
    )
    assert case.gateway.process.pid != gateway_pid
    await acquire_gateway_guest(case.client)
    restored = await case.data("GET", "/api/gateway/workspaces")
    restored_target = next(item for item in restored["items"] if item["workspace_id"] == target_id)
    assert restored_target["backend_workspace_id"] == backend_id
    await read_bound_root()

    # 撤权在实际调用前生效；目标注册和物理根不得被源端 allow 覆盖。
    await publish_policy([])
    denied = await root_binding_relay_client.delete(
        f"/api/gateway/managed-workspaces/{target_id}",
        params={"gateway_connection_id": root_binding_peer_credential.connection_id},
    )
    assert denied.status_code == 403, denied.text
    assert any(item["workspace_id"] == target_id for item in (await case.data("GET", "/api/gateway/workspaces"))["items"])
    await read_bound_root()
    await publish_policy(rules)
    removed = await root_binding_relay_client.delete(
        f"/api/gateway/managed-workspaces/{target_id}",
        params={"gateway_connection_id": root_binding_peer_credential.connection_id},
    )
    assert removed.status_code == 200, removed.text
    assert all(item["workspace_id"] != target_id for item in (await case.data("GET", "/api/gateway/workspaces"))["items"])
    assert probe.read_text() == "原生 owner 的固定根\n"
    assert not list(case.replay.artifacts.glob("*.json"))
    (case.context.artifacts_dir / "root-binding-peer-granted.json").write_text(json.dumps({
        "target_id": target_id, "backend_id": backend_id, "gateway_restarted": True,
        "denied_status": denied.status_code, "removed_status": removed.status_code,
    }, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
async def test_real_gateway_one_backend_two_mounts_and_isolated_session_jobs(shared_backend_case: SharedBackendCase) -> None:
    case = shared_backend_case
    primary, secondary, joined = await _join_group(case)
    ids = (primary, secondary)
    healths = [await case.data("GET", "/api/v1/workspace", headers={"X-BoxTeam-Workspace-Id": identity}) for identity in ids]
    assert len({item["workspace_id"] for item in healths}) == 2
    await case.data("POST", f"/api/gateway/workspaces/{secondary}/activate")
    for index, (root, health) in enumerate(zip(case.roots, healths, strict=True)):
        probe = root / "uri-mount-probe.txt"
        probe.write_text(f"固定挂载 {index}\n")
        uri = f"boxteam://workspace/{health['workspace_id']}/resources/files/root/{probe.name}"
        response = await case.client.post("/api/v1/resources/navigation", json={"uri": uri, "operation": "read"}, headers={"X-BoxTeam-Workspace-Id": ids[1 - index]})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["content"] == f"固定挂载 {index}\n"
        assert response.json()["data"]["node"]["uri"] == uri
        assert str(root) not in response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
    assert (await case.data("GET", "/api/gateway/workspaces"))["active_workspace_id"] == secondary
    backend_healths = []
    sessions = []
    for index, identity in enumerate(ids):
        headers = {"X-BoxTeam-Workspace-Id": identity}
        response = await case.client.get("/api/v1/health", headers=headers)
        response.raise_for_status()
        backend_healths.append(response.json())
        session = await case.data("POST", "/api/v1/sessions", headers=headers, json={"title": f"mount-{index}"})
        sessions.append(session["session_id"])
        call = await case.plan(identity, sessions[-1], f"mount-{index} 已完成")
        accepted = await case.data("POST", f"/api/v1/sessions/{sessions[-1]}/messages", headers=headers, json={
            "message": {"content": f"回复 mount-{index}"}, "run": {"mode": "single_agent", "agent_id": "default"},
        })
        async with asyncio.timeout(60):
            while True:
                job = await case.data("GET", f"/api/v1/jobs/{accepted['job_id']}", headers=headers)
                if job["status"] in {"completed", "failed", "interrupted", "cancelled"}:
                    assert job["status"] == "completed", job
                    assert job["session_id"] == sessions[-1]
                    break
                await asyncio.sleep(0.1)
        wire = case.request(call)
        assert any(message["role"] == "user" and message["content"] == f"回复 mount-{index}" for message in wire["messages"])
        history = await case.data("POST", f"/api/v1/sessions/{sessions[-1]}/history", headers=headers, json={"direction": "tail"})
        assert history["items"][-1]["final_response"] == f"mount-{index} 已完成"
    assert len({item["process_id"] for item in backend_healths}) == 1
    assert {item["backend_generation"] for item in backend_healths} == {joined["backend_generation"]}
    assert len({item["mount_manifest_digest"] for item in backend_healths}) == 1
    for root, session_id, other in zip(case.roots, sessions, reversed(sessions), strict=True):
        path = root / ".boxteam/navigation/session-catalog.sqlite"
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            assert connection.execute("SELECT count(*) FROM nodes WHERE node_id=?", (session_id,)).fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM nodes WHERE node_id=?", (other,)).fetchone()[0] == 0
    wrong = await case.client.get(f"/api/v1/sessions/{sessions[0]}", headers={"X-BoxTeam-Workspace-Id": secondary})
    assert wrong.status_code == 404
    state_path = case.boxteam_home / "state/gateway/gateway.sqlite"
    with sqlite3.connect(f"file:{state_path}?mode=ro", uri=True) as connection:
        metadata = json.loads(connection.execute(
            "SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'",
        ).fetchone()[0])
    assert len(metadata["managed_backends"]) == 1
    assert len(metadata["managed_backends"][0]["members"]) == 2
    (case.context.artifacts_dir / "shared-owner-core.json").write_text(json.dumps({
        "members": ids, "health": backend_healths, "sessions": sessions,
        "managed_backends": metadata["managed_backends"],
    }, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
async def test_shared_member_requests_block_and_stale_contract_preserves_whole_group(shared_backend_case: SharedBackendCase) -> None:
    case = shared_backend_case
    primary, secondary, joined = await _join_group(case)
    for action in ("start", "stop", "restart-safe", "restart-force"):
        blocked = await case.data("POST", f"/api/gateway/workspaces/{secondary}/runtime/{action}")
        assert blocked["status"] == "blocked"
        assert set(blocked["affected_workspace_ids"]) == {primary, secondary}
        assert blocked["blockers"][0]["workspace_id"] == secondary
        assert blocked["blockers"][0]["kind"] == "backend_group"
    response = await case.client.post(f"/api/gateway/workspaces/{primary}/runtime/restart-safe", json={
        "expected_backend_generation": joined["backend_generation"],
        "expected_workspace_ids": [primary],
    })
    assert response.status_code == 409
    after = await case.data("GET", "/api/gateway/workspaces")
    assert {item["backend_generation"] for item in after["items"]} == {joined["backend_generation"]}
    assert all(item["status"] == "ready" for item in after["items"])


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="候选退出注入需要 POSIX signal")
async def test_candidate_process_failure_recovers_complete_previous_group(shared_backend_case: SharedBackendCase) -> None:
    case = shared_backend_case
    primary, secondary, joined = await _join_group(case)
    gateway_log = case.context.artifacts_dir / "gateway/gateway.stderr.log"
    previous_pids = set(re.findall(r"启动工作区后端: pid=(\d+)", gateway_log.read_text()))

    async def interrupt_only_candidate() -> int:
        async with asyncio.timeout(45):
            while True:
                matches = re.findall(r"启动工作区后端: pid=(\d+) port=(\d+)", gateway_log.read_text())
                candidate = next(((int(pid), int(port)) for pid, port in matches if pid not in previous_pids), None)
                if candidate is not None:
                    pid, port = candidate
                    command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ")
                    assert b"tests.support.e2e_control.backend_app:app" in command and str(port).encode() in command
                    os.kill(pid, signal.SIGKILL)
                    return pid
                await asyncio.sleep(0.05)

    monitor = asyncio.create_task(interrupt_only_candidate())
    try:
        response = await case.client.post(f"/api/gateway/workspaces/{primary}/runtime/restart-safe", json={
            "expected_backend_generation": joined["backend_generation"],
            "expected_workspace_ids": [primary, secondary],
        })
        killed_pid = await monitor
        assert response.status_code == 502, response.text
        failed_status = response.status_code
    finally:
        if not monitor.done():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
    recovered = await case.data("GET", "/api/gateway/workspaces")
    assert all(item["status"] == "ready" and item["connection_error"] is None for item in recovered["items"])
    assert {item["backend_generation"] for item in recovered["items"]} == {joined["backend_generation"]}
    healths = []
    for identity in (primary, secondary):
        response = await case.client.get("/api/v1/health", headers={"X-BoxTeam-Workspace-Id": identity})
        response.raise_for_status()
        healths.append(response.json())
    assert len({item["process_id"] for item in healths}) == 1
    assert all(item["process_id"] != killed_pid for item in healths)
    assert {item["backend_generation"] for item in healths} == {joined["backend_generation"]}
    assert len({item["mount_manifest_digest"] for item in healths}) == 1
    (case.context.artifacts_dir / "candidate-recovery.json").write_text(json.dumps({
        "candidate_pid": killed_pid, "response": failed_status, "recovered_health": healths,
    }, ensure_ascii=False, indent=2) + "\n")


async def _add_nondefault_singleton(case: SharedBackendCase):
    added = await case.data("POST", "/api/gateway/workspaces/local", json={
        "root_path": str(case.roots[1]), "name": "Exit Secondary",
    })
    return next(item for item in added["items"] if item["workspace_id"] == build_managed_local_workspace_id(str(case.roots[1])))


def _managed_owner_snapshot(case: SharedBackendCase):
    path = case.boxteam_home / "state/gateway/gateway.sqlite"
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return json.loads(connection.execute(
            "SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'",
        ).fetchone()[0])["managed_backends"]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="真实退出进程证明需要 POSIX pid")
async def test_real_delete_last_member_blocks_job_then_force_commits_durable_interrupted(shared_backend_case: SharedBackendCase) -> None:
    case = shared_backend_case
    secondary = await _add_nondefault_singleton(case)
    identity = secondary["workspace_id"]
    headers = {"X-BoxTeam-Workspace-Id": identity}
    health = (await case.client.get("/api/v1/health", headers=headers)).json()
    owner_before = _managed_owner_snapshot(case)
    log_path = case.context.artifacts_dir / "gateway/gateway.stderr.log"
    starts_before = re.findall(r"启动工作区后端: pid=(\d+)", log_path.read_text())
    assert starts_before[-1] == str(health["process_id"])
    request = {
        "expected_backend_generation": secondary["backend_generation"],
        "expected_workspace_ids": secondary["affected_workspace_ids"],
        "desired_workspace_ids": [],
    }
    session = await case.data("POST", "/api/v1/sessions", headers=headers, json={"title": "last-member-job"})
    call = await case.plan(identity, session["session_id"], "退出探针完成")
    case.replay.control.arm(call, "model.response_ready")
    try:
        accepted = await case.data("POST", f"/api/v1/sessions/{session['session_id']}/messages", headers=headers, json={
            "message": {"content": "保持运行直到受控退出"}, "run": {"mode": "single_agent", "agent_id": "default"},
        })
        await asyncio.to_thread(case.replay.control.acknowledged, call, "model.response_ready", 20)
        assert any(message["role"] == "user" and message["content"] == "保持运行直到受控退出" for message in case.request(call)["messages"])
        blocked = await case.client.request("DELETE", f"/api/gateway/workspaces/{identity}", json=request)
        assert blocked.status_code == 409, blocked.text
        detail = blocked.json()["detail"]
        assert detail["status"] == "blocked" and detail["backend_generation"] == secondary["backend_generation"]
        assert any(item["workspace_id"] == identity and item["kind"] == "job" and item["resource_id"] == accepted["job_id"] for item in detail["blockers"])
        after = await case.data("GET", "/api/gateway/workspaces")
        preserved = next(item for item in after["items"] if item["workspace_id"] == identity)
        assert preserved["status"] == "ready"
        assert preserved["backend_generation"] == secondary["backend_generation"]
        assert {name: service["local_url"] for name, service in preserved["services"].items()} == {name: service["local_url"] for name, service in secondary["services"].items()}
        assert _managed_owner_snapshot(case) == owner_before
        with sqlite3.connect(f"file:{case.boxteam_home / 'state/gateway/gateway.sqlite'}?mode=ro", uri=True) as connection:
            persisted = json.loads(connection.execute("SELECT payload_json FROM gateway_workspace_registry WHERE workspace_id=?", (identity,)).fetchone()[0])
        assert persisted["desired_running"]
        runtime = await case.data("GET", "/api/v1/runtime/status", headers=headers)
        assert runtime["lifecycle_state"] == "ready" and runtime["accepting_jobs"]
        forced = await case.data("DELETE", f"/api/gateway/workspaces/{identity}?force=true", json=request)
        assert all(item["workspace_id"] != identity for item in forced["items"])
        assert len(_managed_owner_snapshot(case)) == 1
        assert len(_managed_owner_snapshot(case)[0]["members"]) == 1
        interrupted = [
            event for event in TraceEventStore(case.roots[1] / ".boxteam/sessions").read_events(session["session_id"])
            if event.job_id == accepted["job_id"] and event.type == "session_interrupted"
        ]
        assert len(interrupted) == 1 and interrupted[0].payload.session_id == session["session_id"]
        async with asyncio.timeout(10):
            while True:
                try:
                    os.kill(health["process_id"], 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.05)
        starts = re.findall(r"启动工作区后端: pid=(\d+)", log_path.read_text())
        assert starts == starts_before, "最后成员退出不得启动空 Backend"
        (case.context.artifacts_dir / "controlled-last-member-delete.json").write_text(json.dumps({
            "safe_blocked": detail, "forced_workspaces": forced,
            "durable_interrupted": interrupted[0].model_dump(mode="json"),
            "old_process_id": health["process_id"], "backend_starts": starts,
            "remaining_owners": _managed_owner_snapshot(case),
        }, ensure_ascii=False, indent=2) + "\n")
    finally:
        case.replay.control.release(call, "model.response_ready")


@pytest.mark.asyncio
async def test_real_initiator_exit_then_explicit_empty_target_releases_owner(shared_backend_case: SharedBackendCase) -> None:
    case = shared_backend_case
    primary, secondary, joined = await _join_group(case)
    request = {
        "expected_backend_generation": joined["backend_generation"],
        "expected_workspace_ids": joined["affected_workspace_ids"],
        "desired_workspace_ids": [primary],
    }
    exited = await case.data("POST", f"/api/gateway/workspaces/{secondary}/runtime/restart-safe", json=request)
    assert exited["status"] == "restarted"
    assert set(exited["affected_workspace_ids"]) == {primary, secondary}
    offline = next(item for item in exited["workspaces"]["items"] if item["workspace_id"] == secondary)
    assert offline["status"] == "offline" and offline["backend_generation"] is None
    started = await case.data("POST", f"/api/gateway/workspaces/{secondary}/runtime/start")
    stopped = await case.data("POST", f"/api/gateway/workspaces/{secondary}/runtime/restart-safe", json={
        "expected_backend_generation": started["backend_generation"],
        "expected_workspace_ids": started["affected_workspace_ids"],
        "desired_workspace_ids": [],
    })
    assert stopped["status"] == "stopped" and stopped["affected_workspace_ids"] == [secondary]
    owners = _managed_owner_snapshot(case)
    assert len(owners) == 1 and [member["workspace_id"] for member in owners[0]["members"]] == [primary]
    final = await case.data("GET", "/api/gateway/workspaces")
    target = next(item for item in final["items"] if item["workspace_id"] == secondary)
    assert target["status"] == "offline"
    assert target["backend_generation"] is None and target["affected_workspace_ids"] == [secondary]
    (case.context.artifacts_dir / "controlled-last-member-empty-target.json").write_text(json.dumps({
        "initiator_exit": exited, "explicit_empty_exit": stopped, "remaining_owners": owners,
    }, ensure_ascii=False, indent=2) + "\n")


@pytest.fixture
def nondefault_third_root(shared_backend_case: SharedBackendCase) -> Path:
    case = shared_backend_case
    root = case.context.workspace_root / "stopped-member-exit-third"
    prepare_default_test_workspace(
        workspace_root=root, template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    (root / ".boxteam/workspace-identity.json").unlink(missing_ok=True)
    (root / ".boxteam/workspace.jsonc").write_bytes((case.roots[1] / ".boxteam/workspace.jsonc").read_bytes())
    return root


@pytest.mark.asyncio
async def test_real_stopped_shared_delete_persists_remaining_contract_then_starts_one_mount(
    shared_backend_case: SharedBackendCase, nondefault_third_root: Path,
) -> None:
    case = shared_backend_case
    secondary = await _add_nondefault_singleton(case)
    added = await case.data("POST", "/api/gateway/workspaces/local", json={
        "root_path": str(nondefault_third_root), "name": "Stopped Exit Third",
    })
    third = next(item for item in added["items"] if item["workspace_id"] == build_managed_local_workspace_id(str(nondefault_third_root)))
    first_id, remaining_id = secondary["workspace_id"], third["workspace_id"]
    await case.data("POST", f"/api/gateway/workspaces/{remaining_id}/runtime/stop")
    joined = await case.data("POST", f"/api/gateway/workspaces/{first_id}/runtime/restart-safe", json={
        "expected_backend_generation": secondary["backend_generation"],
        "expected_workspace_ids": [first_id], "desired_workspace_ids": [first_id, remaining_id],
    })
    assert joined["status"] == "restarted" and set(joined["affected_workspace_ids"]) == {first_id, remaining_id}
    old_owner = next(owner for owner in _managed_owner_snapshot(case) if {member["workspace_id"] for member in owner["members"]} == {first_id, remaining_id})
    old_member = next(member for member in old_owner["members"] if member["workspace_id"] == remaining_id)
    previous_healths = []
    for identity in (first_id, remaining_id):
        response = await case.client.get("/api/v1/health", headers={"X-BoxTeam-Workspace-Id": identity})
        response.raise_for_status()
        previous_healths.append(response.json())
    assert len({health["process_id"] for health in previous_healths}) == 1
    log_path = case.context.artifacts_dir / "gateway/gateway.stderr.log"
    starts_before = re.findall(r"启动工作区后端: pid=(\d+)", log_path.read_text())
    previous_request = {"expected_backend_generation": joined["backend_generation"], "expected_workspace_ids": [first_id, remaining_id]}
    stopped = await case.data("POST", f"/api/gateway/workspaces/{first_id}/runtime/stop", json=previous_request)
    assert stopped["status"] == "stopped" and stopped["backend_generation"] == joined["backend_generation"]
    deleted = await case.data("DELETE", f"/api/gateway/workspaces/{first_id}", json={**previous_request, "desired_workspace_ids": [remaining_id]})
    assert first_id not in {item["workspace_id"] for item in deleted["items"]}
    remaining = next(item for item in deleted["items"] if item["workspace_id"] == remaining_id)
    assert remaining["status"] == "offline" and remaining["affected_workspace_ids"] == [remaining_id]
    assert all(service["local_url"] is None for service in remaining["services"].values())
    new_owner = next(owner for owner in _managed_owner_snapshot(case) if any(member["workspace_id"] == remaining_id for member in owner["members"]))
    assert new_owner["owner_id"] == old_owner["owner_id"] and new_owner["generation"] != old_owner["generation"]
    assert new_owner["members"] == [old_member] and remaining["backend_generation"] == new_owner["generation"]
    path = case.boxteam_home / "state/gateway/gateway.sqlite"
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT count(*) FROM gateway_workspace_registry WHERE workspace_id=?", (first_id,)).fetchone()[0] == 0
        persisted = json.loads(connection.execute("SELECT payload_json FROM gateway_workspace_registry WHERE workspace_id=?", (remaining_id,)).fetchone()[0])
    assert not persisted["desired_running"] and persisted["runtime_lease_id"] is None
    starts_after_exit = re.findall(r"启动工作区后端: pid=(\d+)", log_path.read_text())
    assert starts_after_exit == starts_before, "停机成员删除不能启动旧组或剩余 Backend"
    stale = await case.client.post(f"/api/gateway/workspaces/{remaining_id}/runtime/start", json=previous_request)
    assert stale.status_code == 409 and "陈旧" in stale.json()["detail"]
    started = await case.data("POST", f"/api/gateway/workspaces/{remaining_id}/runtime/start", json={
        "expected_backend_generation": new_owner["generation"], "expected_workspace_ids": [remaining_id],
    })
    assert started["status"] == "started" and started["affected_workspace_ids"] == [remaining_id]
    response = await case.client.get("/api/v1/health", headers={"X-BoxTeam-Workspace-Id": remaining_id})
    response.raise_for_status()
    health = response.json()
    published_owner = next(owner for owner in _managed_owner_snapshot(case) if any(member["workspace_id"] == remaining_id for member in owner["members"]))
    assert [member["workspace_id"] for member in published_owner["members"]] == [remaining_id]
    assert published_owner["members"] == [{
        "workspace_id": remaining_id, "backend_workspace_id": third["backend_workspace_id"],
    }]
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        binding = json.loads(connection.execute(
            "SELECT payload_json FROM gateway_workspace_registry WHERE workspace_id=?", (remaining_id,),
        ).fetchone()[0])
    assert binding["backend_workspace_id"] == third["backend_workspace_id"]
    assert Path(binding["root_path"]) == nondefault_third_root.resolve()
    assert str(nondefault_third_root.resolve()) not in json.dumps(published_owner)
    mount = MountedWorkspace(
        binding["backend_workspace_id"], Path(binding["root_path"]),
        binding["local_service_urls"]["terminal_manager"], binding["local_service_urls"]["browser_manager"],
    )
    assert health["workspace_id"] == third["backend_workspace_id"] == mount.workspace_id
    assert mount.workspace_root == nondefault_third_root.resolve()
    assert health["backend_generation"] == published_owner["generation"] == started["backend_generation"]
    assert health["mount_manifest_digest"] == mounted_workspace_manifest_digest((mount,))
    assert health["process_id"] != previous_healths[0]["process_id"]
    starts_final = re.findall(r"启动工作区后端: pid=(\d+)", log_path.read_text())
    assert starts_final[:-1] == starts_before and starts_final[-1] == str(health["process_id"])
    (case.context.artifacts_dir / "stopped-shared-member-delete.json").write_text(json.dumps({
        "old_owner": old_owner, "stopped": stopped, "deleted": deleted, "remaining_owner": new_owner,
        "remaining_target": persisted, "stale_status": stale.status_code, "started": started,
        "health": health, "published_owner": published_owner,
        "starts_before": starts_before, "starts_after_exit": starts_after_exit, "starts_final": starts_final,
    }, ensure_ascii=False, indent=2) + "\n")
