"""真实 Gateway／双 Backend／Agent／HTTP MCP 的工作区私有 header 纵切。"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import commentjson
import httpx
import pytest
from starlette.datastructures import Headers

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.agents.test_debug_prompt_flow import _tool_call
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done
from tests.support.mcp.http_endpoint import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 沿当前文件正式输出路径复用原 HTTP MCP
)
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
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

SOURCE = "workspace_root_mutable_override"
HEADER = "X-Private-Token"
MODEL = "private-header-replay-model"
MODEL_ENDPOINT = "https://opencode.ai/zen/v1"


@dataclass(frozen=True)
class PrivateHeaderCase:
    client: httpx.AsyncClient
    context: TestRunContext
    workspaces: dict[str, Path]
    replay: ModelStreamReplay
    endpoint: str
    changed_endpoint: str
    private_values: dict[str, str] = field(repr=False)
    evidence: list[dict[str, object]] = field(default_factory=list)
    requests: list[dict[str, object]] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)
    changed_requests: list[dict[str, object]] = field(default_factory=list)

    def require_public(self, data: bytes) -> None:
        if any(value.encode("ascii") in data for value in self.private_values.values()):
            raise AssertionError("私有输入原值进入了公开响应、配置、模型请求或普通日志")


@pytest.fixture
async def private_header_case(request: pytest.FixtureRequest, http_schema_endpoint_factory) -> AsyncIterator[PrivateHeaderCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspaces: dict[str, Path] = {}
    inline_agents = commentjson.loads((Path.cwd() / "configs/workspace_inline.jsonc").read_text(encoding="utf-8"))["agents"]
    for label in ("A1", "A2"):
        workspace = prepare_test_workspace(workspace_root=context.workspace_root / label,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", template_items=())
        config_path = install_test_workspace_config(workspace_root=workspace,
            config_path=Path.cwd() / "configs/tests/workspace/default.jsonc", schema_path=Path.cwd() / "configs/workspace_schema.jsonc")
        config = commentjson.loads(config_path.read_text(encoding="utf-8"))
        config["llm"]["providers"] = [{"id": "primary", "endpoint": MODEL_ENDPOINT, "model": MODEL,
            "api_key": "private-header-replay-key", "custom_llm_provider": "openai",
            "api_mode": config["llm"]["providers"][0]["api_mode"]}]
        for agent_id in set(inline_agents) | set(config["agents"]):
            config["agents"].setdefault(agent_id, {})["model"] = {"primary_provider": "primary", "fallback_providers": []}
        config["agents"]["default"]["tools"]["custom"] = []
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        workspaces[label] = workspace
    runtime = context.runtime_root_for_node(request.node.nodeid)
    private_values = {label: secrets.token_urlsafe(32) for label in workspaces}
    requests: list[dict[str, object]] = []
    calls: list[dict[str, object]] = []
    changed_requests: list[dict[str, object]] = []

    def matches(headers: Headers) -> list[str]:
        return [label for label, value in private_values.items() if headers.get(HEADER) == value]

    def observe_request(headers: Headers) -> None:
        requests.append({"credential_labels": matches(headers)})

    def observe_call(name: str, arguments: dict[str, object], headers: Headers) -> None:
        calls.append({"name": name, "arguments": arguments, "credential_labels": matches(headers)})

    def observe_new_endpoint(headers: Headers) -> None:
        changed_requests.append({"credential_labels": matches(headers)})

    async with (
        http_schema_endpoint_factory("private", request_observer=observe_request, call_observer=observe_call) as endpoint,
        http_schema_endpoint_factory("changed-endpoint", request_observer=observe_new_endpoint) as changed,
    ):
        with model_stream_replay(context.output_root, runtime_root=runtime, business_assertion=request.node.nodeid) as replay:
            gateway = await asyncio.to_thread(start_gateway_process, workspace_root=workspaces["A1"], default_backend_url=None,
                port=integration_port_block_for_file(Path(request.node.path)).port(0),
                boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir,
                asgi_app="tests.support.e2e_control.gateway_app:app", extra_env=replay.environment)
            case = None
            try:
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}",
                    headers=LOCAL_TOKEN_HEADERS, timeout=60, trust_env=False) as client:
                    case = PrivateHeaderCase(client, context, workspaces, replay, endpoint.url, changed.url,
                        private_values, requests=requests, calls=calls, changed_requests=changed_requests)
                    yield case
            finally:
                # 关闭 Gateway 时仍让原 HTTP Server 处理 SDK 的连接释放。
                await asyncio.to_thread(close_gateway_process, gateway)
                if case is not None:
                    (context.artifacts_dir / "public-exchanges.json").write_text(
                        json.dumps(case.evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    (context.artifacts_dir / "remote-observations.json").write_text(json.dumps({
                        "http_requests": requests, "tool_calls": calls, "new_endpoint_requests": changed_requests,
                    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    private_directories = [workspace / ".boxteam/credentials" / name
                        for workspace in workspaces.values() for name in ("mcp-headers", "mcp-inputs")]
                    for root in (context.workspace_root, context.run_root, runtime, replay.artifacts):
                        for path in root.rglob("*"):
                            if path.is_file() and not any(path.is_relative_to(directory) for directory in private_directories):
                                case.require_public(path.read_bytes())


async def test_private_headers_survive_backend_restart_and_reject_cross_workspace_or_endpoint(
    private_header_case: PrivateHeaderCase,
) -> None:
    case = private_header_case

    async def exchange(method: str, path: str, body: dict | None = None, *,
        status: int = 200, headers: dict[str, str] | None = None, envelope: bool = True) -> dict:
        request_id = f"private-headers-{len(case.evidence)}"
        response = await case.client.request(method, path, json=body, headers={**(headers or {}), "X-Request-ID": request_id})
        case.require_public(response.content)
        payload = response.json()
        assert response.headers["X-Request-ID"] == request_id
        if envelope:
            assert payload["request_id"] == request_id
        case.evidence.append({"method": method, "path": path, "status": response.status_code,
            "request_id": request_id, "body": payload})
        assert response.status_code == status, response.text
        return payload

    async def source_edit(base: str, reference: dict, endpoint: str, key: str) -> tuple[str, dict]:
        path = base + "/config/sources/" + SOURCE
        document = (await exchange("GET", path))["data"]
        config = commentjson.loads(document["jsonc"])
        config["mcp"] = {"servers": {"secure": {"enabled": True, "transport": "streamable_http",
            "url": endpoint, "headers": {HEADER: reference}}}}
        edit = {"gateway_id": document["gateway_id"], "workspace_id": document["workspace_id"], "source_key": SOURCE,
            "jsonc": json.dumps(config, ensure_ascii=False, indent=2) + "\n", "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": key}
        await exchange("POST", path + "/preflight", edit)
        return path, edit

    async def invoke(label: str, workspace_id: str, gateway_id: str, session: str, thread: str, phase: str) -> None:
        proof = label.lower() + "-" + phase
        search = {**_tool_call(call_index=0, name="search_tools", arguments={"tool_ids": ["mcp__secure__probe"]}),
            "id": "private-header-search-" + proof}
        call = {**_tool_call(call_index=1, name="invoke_extension_tool",
            arguments={"tool_name": "mcp__secure__probe", "arguments": {"proof": proof}}), "id": "private-header-invoke-" + proof}
        interactions = [build_cassette(asset_id=f"private-header-{proof}-{index}", model=MODEL,
            url=MODEL_ENDPOINT + "/chat/completions", frames=chat_completion_frames(
                response_id=f"chatcmpl-private-header-{proof}-{index}", model=MODEL,
                deltas=({"role": "assistant", **delta},), finish_reason="tool_calls" if "tool_calls" in delta else "stop",
            )).raw["interactions"][0] for index, delta in enumerate([
                {"tool_calls": [search]}, {"tool_calls": [call]}, {"content": "PRIVATE_HEADER_OK " + proof},
            ])]
        call_ids = case.replay.control.plan_model_calls(session, thread, interactions)
        async with httpx.AsyncClient(base_url=case.client.base_url, cookies=case.client.cookies,
            headers={**LOCAL_TOKEN_HEADERS, "X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Target-Gateway-Id": gateway_id},
            timeout=60, trust_env=False) as selected:
            accepted = await selected.post(f"/api/v1/sessions/{session}/messages", json={
                "message": {"content": "发现并调用 MCP probe，参数 proof=" + proof + "，读取回执后结束。"},
                "run": {"mode": "single_agent", "agent_id": "default"},
            })
            case.require_public(accepted.content)
            assert accepted.status_code == 200, accepted.text
            await wait_for_job_done(selected, accepted.json()["data"]["job_id"], max_attempts=90)
        records = [json.loads((case.replay.artifacts / f"{call_id}.json").read_text(encoding="utf-8")) for call_id in call_ids]
        case.require_public(json.dumps(records).encode("utf-8"))
        result, = [message for message in records[-1]["request"]["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == call["id"]]
        content, = result["content"]
        assert content["type"] == "text" and json.loads(content["text"]) == {"name": "probe", "arguments": {"proof": proof}}
        assert [row for row in case.calls if row["arguments"] == {"proof": proof}] == [
            {"name": "probe", "arguments": {"proof": proof}, "credential_labels": [label]},
        ]
        case.evidence.append({"phase": proof, "session_id": session, "thread_id": thread, "model_call_ids": call_ids})

    listing = (await exchange("GET", "/api/gateway/workspaces"))["data"]
    first, = listing["items"]
    first_id = first["workspace_id"]
    await exchange("GET", f"/api/v1/workspaces/{first_id}/workspace")
    listing = (await exchange("POST", "/api/gateway/managed-workspaces",
        {"root_path": str(case.workspaces["A2"]), "name": "私有 header A2"}))["data"]
    second_id, = [item["workspace_id"] for item in listing["items"] if item["workspace_id"] != first_id]
    identities = {"A1": first_id, "A2": second_id}
    references: dict[str, dict] = {}
    sessions: dict[str, tuple[str, str]] = {}
    healths: dict[str, dict] = {}
    targets: dict[str, dict] = {}
    for label, workspace_id in identities.items():
        base = f"/api/v1/workspaces/{workspace_id}"
        await exchange("GET", base + "/workspace")
        document = (await exchange("GET", base + "/config/sources/" + SOURCE))["data"]
        target = {"kind": "workspace", "gateway_id": document["gateway_id"], "workspace_id": workspace_id}
        targets[label] = target
        selection = {"X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Target-Gateway-Id": target["gateway_id"]}
        # 同一新入口的原生与显式工作区 mount 都必须到达精确 owner。
        private_path = "/api/v1/mcp/private-headers" if label == "A1" else base + "/mcp/private-headers"
        response = await exchange("POST", private_path, {"target": target, "server_id": "secure",
            "header_name": HEADER, "endpoint": case.endpoint, "value": case.private_values[label]}, headers=selection)
        assert response["target"] == target and response["protocol_version"] == 1
        reference = response["data"]
        assert {key: reference[key] for key in ("workspace_id", "server_id", "header_name", "endpoint")} == {
            "workspace_id": workspace_id, "server_id": "secure", "header_name": HEADER, "endpoint": case.endpoint}
        references[label] = reference
        path, edit = await source_edit(base, reference, case.endpoint, label + "-configure")
        saved = (await exchange("PUT", path, edit))["data"]
        assert saved["edit"]["state"] == "active" and saved["edit"]["source_saved"] and saved["edit"]["layer_imported"]
        assert json.loads(saved["jsonc"])["mcp"]["servers"]["secure"]["headers"][HEADER] == reference
        server, = (await exchange("GET", base + "/mcp/servers"))["data"]
        assert server["server_id"] == "secure" and server["status"] == "ready"
        healths[label] = await exchange("GET", base + "/health", envelope=False)
        async with httpx.AsyncClient(base_url=case.client.base_url, cookies=case.client.cookies,
            headers={**LOCAL_TOKEN_HEADERS, **selection}, timeout=60, trust_env=False) as selected:
            session = await create_session(selected, "MCP 私有 header " + label)
            thread_response = await selected.get(f"/api/v1/sessions/{session}/main-thread")
            thread_response.raise_for_status()
            sessions[label] = session, thread_response.json()["data"]["thread_id"]
        await invoke(label, workspace_id, target["gateway_id"], *sessions[label], "before")
    assert healths["A1"]["process_id"] != healths["A2"]["process_id"]
    assert references["A1"]["version"] != references["A2"]["version"]

    for label, workspace_id in identities.items():
        listing = (await exchange("GET", "/api/gateway/workspaces"))["data"]
        item, = [item for item in listing["items"] if item["workspace_id"] == workspace_id]
        assert item["affected_workspace_ids"] == [workspace_id]
        restarted = (await exchange("POST", f"/api/gateway/workspaces/{workspace_id}/runtime/restart-safe", {
            "expected_backend_generation": item["backend_generation"], "expected_workspace_ids": item["affected_workspace_ids"],
        }))["data"]
        assert restarted["status"] == "restarted" and restarted["affected_workspace_ids"] == [workspace_id]
        health = await exchange("GET", f"/api/v1/workspaces/{workspace_id}/health", envelope=False)
        assert health["status"] == "ok" and health["process_id"] != healths[label]["process_id"]
        assert health["backend_id"] == healths[label]["backend_id"]
    for label, workspace_id in identities.items():
        base = f"/api/v1/workspaces/{workspace_id}"
        restored = (await exchange("GET", base + "/config/sources/" + SOURCE))["data"]
        assert json.loads(restored["jsonc"])["mcp"]["servers"]["secure"]["headers"][HEADER] == references[label]
        await invoke(label, workspace_id, targets[label]["gateway_id"], *sessions[label], "after")
    assert len(case.calls) == 4 and case.requests and all(len(row["credential_labels"]) == 1 for row in case.requests)

    # 先完成冷恢复，再分别破坏两工作区候选，避免失败源文件互相干扰。
    for label, reference, url in (("A1", references["A1"], case.changed_endpoint), ("A2", references["A1"], case.endpoint)):
        path, edit = await source_edit(f"/api/v1/workspaces/{identities[label]}", reference, url, label + "-reject")
        failed = await exchange("PUT", path, edit, status=422)
        assert "不属于当前工作区、Server、header 或精确 endpoint" in json.dumps(failed, ensure_ascii=False)
    assert case.changed_requests == []
    assert len(case.calls) == 4
