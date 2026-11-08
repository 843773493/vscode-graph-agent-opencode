"""真实 localhost SSH 联邦下的目标安装、私有 env 与重启恢复；模型使用统一回放。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import commentjson
import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.gateway.federation.ports import principal_ref_for_gateway
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.agents.test_debug_prompt_flow import _tool_call
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    _plan_communication_calls,
    _prepare_workspace,
)
from tests.integration.gateway.test_mcp_artifact_installation import ArtifactIndex
from tests.integration.gateway.test_mcp_private_env import (
    RECEIPT_KEY,
    TOKEN_KEY,
)
from tests.integration.gateway.test_mcp_private_env import (
    artifact_download_gate as artifact_download_gate,  # noqa: PLC0414 - 复用标准来源装具
)
from tests.integration.gateway.test_mcp_private_env import (
    artifact_index as artifact_index,  # noqa: PLC0414 - 复用同一 wheel／registry HTTP 来源
)
from tests.integration.gateway.test_mcp_private_env import (
    private_env_catalog as private_env_catalog,  # noqa: PLC0414 - 不另建假 MCP 实现
)
from tests.integration.gateway.test_mcp_private_headers import SOURCE
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done
from tests.support.communication.federation import (
    CommunicationNode,
    open_communication_triad,
)
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.sealed_request_audit import audit_sealed_requests


@dataclass
class RemoteEnvCase:
    context: TestRunContext
    nodes: dict[str, CommunicationNode]
    replay: ModelStreamReplay
    secret: str = field(repr=False)
    evidence: list[dict] = field(default_factory=list)

    def require_public(self, content: bytes) -> None:
        assert self.secret.encode() not in content, "私有 env 原值进入普通响应、日志或模型请求"


@pytest.fixture
async def remote_env_case(request: pytest.FixtureRequest) -> AsyncIterator[RemoteEnvCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()

    def prepare(node_context: TestRunContext, model_port: int, gateway_port: int) -> Path:
        workspace = _prepare_workspace(node_context, model_port, gateway_port)
        config_path = workspace / ".boxteam/workspace.jsonc"
        config = commentjson.loads(config_path.read_text(encoding="utf-8"))
        config["agents"]["default"]["tools"]["custom"] = []
        config["mcp"] = {"servers": {}}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return workspace

    with model_stream_replay(context.artifacts_dir / "model-stream",
        runtime_root=context.runtime_root_for_node(request.node.nodeid), business_assertion=request.node.nodeid) as replay:
        case = None
        try:
            async with open_communication_triad(context, prepare,
                backend_asgi_app="tests.support.e2e_control.backend_app:app",
                backend_environment={**replay.environment, "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
            ) as nodes:
                case = RemoteEnvCase(context, nodes, replay, secrets.token_urlsafe(32))
                yield case
        finally:
            if case is not None:
                (context.artifacts_dir / "remote-private-env.json").write_text(
                    json.dumps(case.evidence, ensure_ascii=False, indent=2), encoding="utf-8")
                credentials = case.nodes["b"].workspace / ".boxteam/credentials/mcp-inputs"
                for root in (context.workspace_root, context.run_root, replay.artifacts):
                    for path in root.rglob("*"):
                        if path.is_file() and not path.is_relative_to(credentials):
                            case.require_public(path.read_bytes())


async def test_remote_installation_private_env_and_original_thread_recover_on_native_owner(
    remote_env_case: RemoteEnvCase, private_env_catalog: ArtifactIndex,
) -> None:
    case, index = remote_env_case, private_env_catalog
    hub, native = case.nodes["a"], case.nodes["b"]
    assert native.gateway_home is not None
    selection = {"X-BoxTeam-Target-Gateway-Id": native.gateway_id, "X-BoxTeam-Workspace-Id": native.workspace_id}

    async def exchange(method: str, path: str, body: dict | None = None, *,
        client: httpx.AsyncClient = hub.client, workspace: bool = False, status: int = 200) -> dict:
        request_id = f"remote-private-env-{len(case.evidence)}"
        response = await client.request(method, path, json=body,
            headers={"X-Request-ID": request_id, **(selection if workspace else {})})
        case.require_public(response.content)
        payload = response.json()
        case.evidence.append({"method": method, "path": path, "status": response.status_code, "response": payload})
        assert response.headers["X-Request-ID"] == payload["request_id"] == request_id
        assert response.status_code == status, response.text
        return payload

    # 由目标的正式配置 watcher 发布精确主体授权，不替换策略或伪造配对。
    before = (await exchange("GET", "/api/gateway/config/reload-status", client=native.client))["data"]
    config_path = native.gateway_home / "config/gateway.jsonc"
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("permissions", {})["federation"] = {"default_effect": "allow", "rules": [
        {"capability": capability, "effect": "allow", "principal_ref": principal_ref_for_gateway(hub.gateway_id)}
        for capability in ("mcp_installation_manage", "configuration_read", "configuration_write")
    ]}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    async with asyncio.timeout(15):
        while True:
            current = (await exchange("GET", "/api/gateway/config/reload-status", client=native.client))["data"]
            if current["revision"] != before["revision"] and current["healthy"]:
                assert current["state"] == "active" and not current["restart_required"]
                break
            await asyncio.sleep(0.1)

    listing = (await exchange("GET", "/api/gateway/workspaces"))["data"]["items"]
    remote, = [item for item in listing if item["gateway_id"] == native.gateway_id]
    assert remote["workspace_id"] == native.workspace_id and remote["connection_kind"] == "remote_gateway"
    target, = (await exchange("POST", "/api/gateway/mcp-installation-targets", {"gateway_id": native.gateway_id}))["data"]
    assert target["gateway_id"] == native.gateway_id
    assert (await exchange("POST", "/api/gateway/mcp-installation-targets", {"gateway_id": native.gateway_id}, client=native.client))["data"] == [target]
    installation = f"/api/gateway/mcp/{native.gateway_id}/{target['backend_id']}"
    manifest = (await exchange("POST", installation + "/candidate-manifests", {"target": target, "source": index.source()}))["data"]
    variant, = manifest["variants"]
    candidate = {"target": target, "source": index.source(), "manifest_digest": manifest["manifest_digest"], "variant": variant["ref"]}
    plan = (await exchange("POST", installation + "/candidate-plans", candidate))["data"]
    operation = (await exchange("POST", installation + "/installations", {"candidate": candidate,
        "plan_digest": plan["plan_digest"], "operation_id": "remote-private-env-install", "entrypoint": "private-env-server"}))["data"]
    async with asyncio.timeout(60):
        while operation["status"] == "running":
            await asyncio.sleep(0.1)
            operation = (await exchange("GET", installation + "/installation-operations/remote-private-env-install"))["data"]
    assert operation["status"] == "succeeded", operation
    artifact = operation["artifact"]
    assert Path(artifact["command"]).is_relative_to(native.gateway_home / "artifacts/mcp" / target["backend_id"])
    assert (await exchange("GET", installation + "/installation-operations/remote-private-env-install", client=native.client))["data"] == operation

    base = f"/api/v1/workspaces/{native.workspace_id}"
    workspace_target = {"kind": "workspace", "gateway_id": native.gateway_id, "workspace_id": native.workspace_id}
    receipts = native.workspace / "private-env-observations.jsonl"
    candidate.update({"server_id": "secure", "inputs": {RECEIPT_KEY: str(receipts)}})
    reference = (await exchange("POST", base + "/mcp/candidate-plans/private-env", {"target": workspace_target,
        "server_id": "secure", "input_key": TOKEN_KEY, "candidate": candidate, "value": case.secret}, workspace=True))["data"]
    assert reference["workspace_id"] == native.workspace_id and reference["server_id"] == "secure"
    candidate["inputs"][TOKEN_KEY] = reference
    configured_plan = (await exchange("POST", base + "/mcp/candidate-configurations/plans", candidate, workspace=True))["data"]
    source_path = base + "/config/sources/" + SOURCE
    document = (await exchange("GET", source_path, workspace=True))["data"]
    edit = {"gateway_id": native.gateway_id, "workspace_id": native.workspace_id, "source_key": SOURCE,
        "jsonc": document["jsonc"], "base_file_digest": document["file_digest"],
        "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
        "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
        "idempotency_key": "remote-private-env-configure"}
    configuration = {"target": workspace_target, "server_id": "secure",
        "plan": {"candidate": candidate, "plan_digest": configured_plan["plan_digest"]}, "managed_launch": artifact["binding"]}
    draft = (await exchange("POST", base + "/mcp/candidate-configurations/draft",
        {**configuration, "source_edit": edit, "enabled": True}, workspace=True))["data"]
    saved = (await exchange("PUT", base + "/mcp/candidate-configurations",
        {**configuration, "source_edit": draft["source_edit"]}, workspace=True))["data"]
    assert saved["edit"]["state"] == "active" and saved["edit"]["source_saved"]
    server, = (await exchange("GET", base + "/mcp/servers", workspace=True))["data"]
    assert server["server_id"] == "secure" and server["status"] == "ready"
    original_connection = server["connection_ref"]
    source_bytes = (native.workspace / ".boxteam/workspace.jsonc").read_bytes()
    digest = hashlib.sha256(case.secret.encode()).hexdigest()
    captures: dict[str, dict] = {}
    async with httpx.AsyncClient(base_url=hub.client.base_url, cookies=hub.client.cookies,
        headers={**hub.client.headers, **selection}, timeout=60, trust_env=False) as selected:
        session = await create_session(selected, "远端受管 MCP 私有 env 恢复")
        thread = (await exchange("GET", base + f"/sessions/{session}/main-thread", workspace=True))["data"]["thread_id"]
        for phase in ("before", "after"):
            if phase == "after":
                # 真实进程重启；不预填 checkpoint、凭据或安装事实。
                restarted = await asyncio.to_thread(native.restart_backend)
                assert restarted["before_pid"] != restarted["after_pid"]
                assert (await exchange("POST", "/api/gateway/mcp-installation-targets",
                    {"gateway_id": native.gateway_id}))["data"] == [target]
                restored = (await exchange("GET", base + "/mcp/servers", workspace=True))["data"]
                restored_server, = restored
                assert restored_server["status"] == "ready" and restored_server["connection_ref"] != original_connection
                assert (native.workspace / ".boxteam/workspace.jsonc").read_bytes() == source_bytes
            proof = "remote-private-env-" + phase
            call = _tool_call(call_index=0, name="invoke_extension_tool", arguments={
                "tool_name": "mcp__secure__private_env_probe", "arguments": {"proof": proof}})
            call["id"] = "remote-env-invoke-" + phase
            search = _tool_call(call_index=0, name="search_tools", arguments={"tool_ids": ["mcp__secure__private_env_probe"]})
            search["id"] = "remote-env-search-" + phase
            calls, _ = await _plan_communication_calls(case.replay, selected, native.workspace, session, [
                {"tool_calls": [search]},
                {"tool_calls": [call]}, {"content": "REMOTE_PRIVATE_ENV_OK " + phase},
            ], thread_id=thread)
            accepted = await selected.post(f"/api/v1/sessions/{session}/messages", json={
                "message": {"content": "发现并调用远端私有 env 工具，proof=" + proof},
                "run": {"mode": "single_agent", "agent_id": "default"}})
            case.require_public(accepted.content)
            assert accepted.status_code == 200, accepted.text
            job = await wait_for_job_done(selected, accepted.json()["data"]["job_id"], max_attempts=90)
            case.require_public(json.dumps(job).encode())
            captures.update({identity: json.loads((case.replay.artifacts / f"{identity}.json").read_text()) for identity in calls})
            result, = [message for message in captures[calls[-1]]["request"]["messages"]
                if message.get("role") == "tool" and message.get("tool_call_id") == call["id"]]
            text, = result["content"]
            assert text["type"] == "text" and json.loads(text["text"]) == {
                "proof": proof, "secret_digest": digest, "prefix": str(Path(artifact["command"]).parent.parent)}
    called = [row for line in receipts.read_text().splitlines() if (row := json.loads(line))["kind"] == "called"]
    assert called == [{"kind": "called", "proof": "remote-private-env-" + phase, "secret_digest": digest} for phase in ("before", "after")]
    resolver = get_session_path_resolver(native.workspace / ".boxteam/sessions")
    session_node = resolver.resolve_session_node(session)
    audit = audit_sealed_requests(resolver.resolve_thread_node(session, thread) / "rollout/index.sqlite",
        (session_node / "logs/llm_requests").glob("*.json"), captures)
    case.evidence.append({"owner": workspace_target, "backend": target, "session_id": session,
        "thread_id": thread, "audit": audit, "actual_calls": called})
    for name in ("a", "c"):
        node = case.nodes[name]
        assert node.gateway_home is not None
        assert (await exchange("GET", "/api/v1/mcp/servers", client=node.client))["data"] == []
        assert not list((node.workspace / ".boxteam/credentials/mcp-inputs").glob("*.json"))
        assert not (node.gateway_home / "artifacts/mcp" / target["backend_id"]).exists()
    assert len(list((native.workspace / ".boxteam/credentials/mcp-inputs").glob("*.json"))) == 1
