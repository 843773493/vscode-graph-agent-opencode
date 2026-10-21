"""真实 Gateway→受管 Workspace→Provider→skill_load；模型由本地协议桩驱动。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import commentjson
import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.services.orchestration.session_subagent_service import _derive_delegation_id
from tests.support.e2e_control.harness import E2ETestControlHarness
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import integration_port_block_for_file

SKILL_NAME = "gateway-frozen-proof"
OLD_BODY = "Gateway 冻结正文 Ω 001  \r\n"
NEW_BODY = "Gateway 新版正文 Ω 002  \r\n"
FRONTMATTER = f"---\r\nname: {SKILL_NAME}\r\ndescription: Gateway raw snapshot proof\r\n---\r\n"


@dataclass
class ModelState:
    requests: list[dict] = field(default_factory=list)
    before_first_reply: Callable[[], None] | None = None
    respond: Callable[[dict], tuple[dict, str]] | None = None


@pytest.fixture(scope="module")
def model_server():
    state = ModelState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def do_POST(self) -> None:
            assert self.path == "/v1/chat/completions"
            assert self.headers["Authorization"] == "Bearer gateway-skill-test-key"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append(payload)
            if state.respond is not None:
                delta, reason = state.respond(payload)
            elif len(state.requests) == 1:
                assert state.before_first_reply is not None
                state.before_first_reply()
                delta = {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": "call_gateway_skill", "type": "function",
                    "function": {"name": "skill_load", "arguments": json.dumps({"name": SKILL_NAME, "mode": "tracked"})},
                }]}
                reason = "tool_calls"
            else:
                delta, reason = {"role": "assistant", "content": "GATEWAY_SKILL_OK"}, "stop"
            chunks = [
                {"id": "chatcmpl-gateway-skill", "object": "chat.completion.chunk", "created": int(time.time()),
                 "model": "gateway-skill-test-model", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "chatcmpl-gateway-skill", "object": "chat.completion.chunk", "created": int(time.time()),
                 "model": "gateway-skill-test-model", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
            ]
            body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def scripted_model(model_server):
    state, endpoint = model_server
    state.requests.clear()
    state.before_first_reply = None
    state.respond = None
    return state, endpoint


async def _job(client: httpx.AsyncClient, session_id: str) -> dict:
    response = await client.post(f"/api/v1/sessions/{session_id}/messages", json={
        "message": {"content": "继续验证已登记的 Skill 来源"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    job_id = response.json()["data"]["job_id"]
    return await _wait_job(client, job_id)


async def _wait_job(client: httpx.AsyncClient, job_id: str) -> dict:
    async with asyncio.timeout(30):
        while True:
            response = await client.get(f"/api/v1/jobs/{job_id}")
            assert response.status_code == 200, response.text
            job = response.json()["data"]
            if job["status"] in {"completed", "succeeded", "failed", "cancelled", "timed_out"}:
                return job
            await asyncio.sleep(0.05)


def _configure_model(config_path: Path, endpoint: str) -> None:
    config = commentjson.loads(config_path.read_text())
    config["llm"]["providers"] = [{
        "id": "gateway-skill-test", "endpoint": endpoint, "model": "gateway-skill-test-model",
        "api_key": "gateway-skill-test-key", "custom_llm_provider": "openai",
        "api_mode": {"protocol": "chat_completions", "model_info": {"supports_function_calling": True, "supports_reasoning": False}, "supports_reasoning": {"reasoning_content": False}},
    }]
    for agent in config["agents"].values():
        if "model" in agent:
            agent["model"] = {"primary_provider": "gateway-skill-test", "fallback_providers": []}
        # 只保留内置工具，移除与来源闭包无关的外部 Provider 引用。
        agent["tools"]["custom"] = []
    config.setdefault("runtime", {}).setdefault("gateway", {})["connection"] = {"timeout_seconds": 2}
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")


async def _assert_skill_tool_schema(client: httpx.AsyncClient) -> dict:
    response = await client.get("/api/v1/tools")
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    tools = response.json()["data"]
    skill = next(item for item in tools if item["tool_id"] == "skill_load")
    assert skill["name"] == "skill_load" and skill["origin"] == "builtin"
    schema = skill["parameters"]
    assert schema["type"] == "object"
    assert schema["required"] == ["name"]
    assert set(schema["properties"]) == {"name", "mode"}
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["name"]["minLength"] == 1
    assert schema["properties"]["mode"]["enum"] == ["snapshot", "tracked", "untrack"]
    assert schema["properties"]["mode"]["default"] == "snapshot"
    return response.json()


async def test_gateway_authenticated_raw_sources_drive_frozen_skill_and_restart(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    integration_workspace_config_path: str, scripted_model,
):
    state, endpoint = scripted_model
    workspace = Path(integration_workspace_root_path)
    output = workspace.parent
    artifacts = output / "artifacts"
    artifacts.mkdir(exist_ok=True)
    home = output / "boxteam-home"
    port = integration_port_block_for_file(Path(request.node.fspath)).port(11)
    config_path = Path(integration_workspace_config_path)
    _configure_model(config_path, endpoint)
    skill_file = home / "skills" / SKILL_NAME / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_bytes((FRONTMATTER + OLD_BODY).encode())
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=None, port=port,
        boxteam_home=home, artifacts_dir=artifacts / "gateway-first",
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=60, trust_env=False) as client:
            await acquire_gateway_guest(client)
            health = await client.get("/api/gateway/health")
            gateway_id = health.json()["data"]["gateway_id"]
            workspace_info = await client.get("/api/v1/workspace")
            assert workspace_info.status_code == 200, workspace_info.text
            tool_catalog = await _assert_skill_tool_schema(client)
            (artifacts / "tools-available.json").write_text(json.dumps(tool_catalog, ensure_ascii=False, indent=2) + "\n")
            workspace_id = json.loads((workspace / ".boxteam" / "workspace-identity.json").read_text())["workspace_id"]
            raw_path = f"/api/gateway/skill-sources/{gateway_id}/workspaces/{workspace_id}"
            for headers in ({}, {"X-Local-Token": "wrong"}, {"X-BoxTeam-Federation-Token": "unrelated"}):
                async with httpx.AsyncClient(base_url=base_url, headers=headers, trust_env=False) as denied:
                    assert (await denied.get(raw_path)).status_code == 401
            assert (await client.get(raw_path.replace(gateway_id, "gateway_wrong"))).status_code == 409
            assert (await client.get(raw_path.replace(workspace_id, str(uuid4())))).status_code == 403
            raw = await client.get(raw_path)
            assert raw.status_code == 200, raw.text
            assert raw.headers["X-Request-ID"] == raw.json()["request_id"]
            original = raw.json()["data"]
            assert original["gateway_id"] == gateway_id and original["workspace_id"] == workspace_id
            assert original["entries"][0]["content"] == FRONTMATTER + OLD_BODY
            assert str(home) not in raw.text and "local-dev-token" not in raw.text
            assert (await client.get(raw_path, headers={"If-None-Match": raw.headers["ETag"]})).status_code == 304

            def change_after_model_capture() -> None:
                skill_file.write_bytes((FRONTMATTER + NEW_BODY).encode())
                with httpx.Client(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=2, trust_env=False) as probe:
                    deadline = time.monotonic() + 6
                    while time.monotonic() < deadline:
                        publication = probe.get(raw_path).json()["data"]
                        if publication["revision"] != original["revision"]:
                            assert publication["entries"][0]["content"] == FRONTMATTER + NEW_BODY
                            # Workspace 共享观察器在下一次真实工具调用前接受这个 live revision。
                            time.sleep(1.2)
                            return
                        time.sleep(0.05)
                raise AssertionError("Gateway watcher 未接受新 raw revision")

            state.before_first_reply = change_after_model_capture
            created = await client.post("/api/v1/sessions", json={"title": "Gateway frozen Skill"})
            assert created.status_code == 200, created.text
            session_id = created.json()["data"]["session_id"]
            thread_reply = await client.get(f"/api/v1/sessions/{session_id}/main-thread")
            thread_id = thread_reply.json()["data"]["thread_id"]
            job = await _job(client, session_id)
            assert job["status"] in {"completed", "succeeded"}, job
            assert len(state.requests) == 2
            first_text = json.dumps(state.requests[0], ensure_ascii=False)
            second_text = json.dumps(state.requests[1], ensure_ascii=False)
            assert "Gateway raw snapshot proof" in first_text
            assert OLD_BODY.strip() not in first_text and NEW_BODY.strip() not in first_text
            assert OLD_BODY.strip() in second_text and NEW_BODY.strip() not in second_text
            tool_messages = [item for item in state.requests[1]["messages"] if item["role"] == "tool"]
            assert tool_messages and "loaded" in json.dumps(tool_messages)
            job = await _job(client, session_id)
            assert job["status"] in {"completed", "succeeded"}, job
            assert NEW_BODY.strip() in json.dumps(state.requests[-1], ensure_ascii=False)
            refreshed = (await client.get(raw_path)).json()["data"]
            assert refreshed["revision"] != original["revision"]
            assert refreshed["entries"][0]["entry_identity"] == original["entries"][0]["entry_identity"]

            skill_file.write_bytes(b"\xff")
            async with asyncio.timeout(6):
                while True:
                    unavailable = (await client.get(raw_path)).json()["data"]
                    if not unavailable["available"]:
                        break
                    await asyncio.sleep(0.05)
            assert unavailable["error_code"] == "source-unavailable"
            assert str(home) not in json.dumps(unavailable)
            await asyncio.sleep(1.2)
            unavailable_tools = await _assert_skill_tool_schema(client)
            assert unavailable_tools["data"] == tool_catalog["data"]
            (artifacts / "tools-source-unavailable.json").write_text(json.dumps(unavailable_tools, ensure_ascii=False, indent=2) + "\n")
            before_failure = len(state.requests)
            failed = await _job(client, session_id)
            assert failed["status"] == "failed" and "unavailable" in failed["error_message"], failed
            assert len(state.requests) == before_failure
            skill_file.write_bytes((FRONTMATTER + NEW_BODY).encode())

        close_gateway_process(gateway)
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=port, boxteam_home=home,
            artifacts_dir=artifacts / "gateway-restarted",
        )
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=60, trust_env=False) as client:
            await acquire_gateway_guest(client)
            restarted_health = await client.get("/api/gateway/health")
            assert restarted_health.json()["data"]["gateway_id"] == gateway_id
            rebuilt = (await client.get(raw_path)).json()["data"]
            assert rebuilt["revision"] == refreshed["revision"]
            assert rebuilt["entries"] == refreshed["entries"]
            restored = await _job(client, session_id)
            assert restored["status"] in {"completed", "succeeded"}, restored
            assert NEW_BODY.strip() in json.dumps(state.requests[-1], ensure_ascii=False)
            receipt = await client.post(f"/api/v1/sessions/{session_id}/threads/{thread_id}/skills/untrack", json={"name": SKILL_NAME})
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()["data"]["tracked"] is False
        (artifacts / "model-requests.json").write_text(json.dumps(state.requests, ensure_ascii=False, indent=2) + "\n")
    finally:
        close_gateway_process(gateway)


def _rows(database: Path, query: str, parameters: tuple[str, ...] = ()) -> list[dict]:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, parameters)]


async def test_cold_child_dispatch_and_result_keep_creation_gateway_source(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    integration_workspace_config_path: str, scripted_model,
):
    """真实 task 发布后硬杀；冷恢复 child 与结果 reply 保留原 HTTP 请求身份及 Skill 来源。"""
    state, endpoint = scripted_model
    workspace = Path(integration_workspace_root_path)
    output = workspace.parent
    artifacts = output / "artifacts" / "cold-child"
    artifacts.mkdir(parents=True, exist_ok=True)
    home = output / "boxteam-home"
    port = integration_port_block_for_file(Path(request.node.fspath)).port(12)
    _configure_model(Path(integration_workspace_config_path), endpoint)
    skill_file = home / "skills" / SKILL_NAME / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_bytes((FRONTMATTER + OLD_BODY).encode())
    release_task_call = threading.Event()

    def respond(payload: dict) -> tuple[dict, str]:
        contents = "\n".join(str(item.get("content", "")) for item in payload["messages"])
        skill_results = [item for item in payload["messages"] if item["role"] == "tool" and item.get("name") == "skill_load"]
        if "<delegated_task " in contents or "<generated_session_result " in contents:
            child = "<delegated_task " in contents
            if skill_results:
                assert "loaded" in json.dumps(skill_results)
                assert OLD_BODY.strip() in contents
                return {"role": "assistant", "content": "COLD_CHILD_DONE" if child else "COLD_REPLY_DONE"}, "stop"
            assert "Gateway raw snapshot proof" in contents
            return {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_child_skill" if child else "call_reply_skill", "type": "function",
                "function": {"name": "skill_load", "arguments": json.dumps({"name": SKILL_NAME})},
            }]}, "tool_calls"
        if any(item["role"] == "tool" and item.get("name") == "task" for item in payload["messages"]):
            return {"role": "assistant", "content": "PARENT_WAITING_FOR_CHILD"}, "stop"
        assert release_task_call.wait(20), "pytest 未为真实 task 发布屏障绑定 operation identity"
        return {"role": "assistant", "tool_calls": [{
            "index": 0, "id": "call_cold_child", "type": "function",
            "function": {"name": "task", "arguments": json.dumps({"description": "COLD_CHILD_SKILL_TASK：显式加载 Gateway Skill 并返回结果。"})},
        }]}, "tool_calls"

    state.respond = respond
    control = E2ETestControlHarness(artifacts / "control")
    control.start()
    gateway = None
    phase = "thread.creation.published"
    delegation_id = None
    try:
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=port, boxteam_home=home,
            artifacts_dir=artifacts / "gateway-first", asgi_app="tests.support.e2e_control.gateway_app:app",
            extra_env=control.environment,
        )
        base_url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=60, trust_env=False) as client:
            await acquire_gateway_guest(client)
            gateway_id = (await client.get("/api/gateway/health")).json()["data"]["gateway_id"]
            assert (await client.get("/api/v1/workspace")).status_code == 200
            await _assert_skill_tool_schema(client)
            created = await client.post("/api/v1/sessions", json={"title": "Cold Gateway child"})
            assert created.status_code == 200, created.text
            session_id = created.json()["data"]["session_id"]
            main_thread_id = (await client.get(f"/api/v1/sessions/{session_id}/main-thread")).json()["data"]["thread_id"]
            accepted = await client.post(f"/api/v1/sessions/{session_id}/messages", json={
                "message": {"content": "创建独立子线程并验证 Gateway Skill 来源"},
                "run": {"mode": "single_agent", "agent_id": "default"},
            }, headers={"X-Request-ID": "request-cold-child-original"})
            assert accepted.status_code == 200, accepted.text
            request_id = accepted.headers["X-Request-ID"]
            assert request_id == accepted.json()["request_id"] == "request-cold-child-original"
            parent_job_id = accepted.json()["data"]["job_id"]
            delegation_id = _derive_delegation_id(
                parent_session_id=session_id, parent_job_id=parent_job_id,
                parent_tool_call_id="call_cold_child", subagent_type="general-purpose",
            )
            control.arm(delegation_id, phase)
            release_task_call.set()
            await asyncio.to_thread(control.acknowledged, delegation_id, phase)
            published_event = next(event for event in control.events if event["operation_id"] == delegation_id and event["phase"] == phase)
            crashed_pid = published_event["pid"]
            locator = _rows(
                workspace / ".boxteam" / "navigation" / "session-catalog.sqlite",
                "SELECT storage_relative_locator FROM nodes WHERE node_id = ?", (session_id,),
            )[0]["storage_relative_locator"]
            session_node = workspace / ".boxteam" / locator
            creation_database = session_node / "session-control.sqlite"
            record = _rows(creation_database, "SELECT * FROM thread_creation_records WHERE thread_creation_idempotency_key = ?", (delegation_id,))[0]
            intent = _rows(creation_database, "SELECT * FROM thread_execution_intents WHERE admission_idempotency_key = ?", (delegation_id,))[0]
            assert record["state"] == "published" and intent["state"] == "pending"
            assert json.loads(record["task_seed"])["gateway_id"] == gateway_id
            assert json.loads(record["task_seed"])["request_id"] == request_id
            assert len(state.requests) == 1, "初次 child 只能在冷启动后进入真实 Runner"
            (artifacts / "published-before-crash.json").write_text(json.dumps({"record": record, "intent": intent, "phase": published_event}, ensure_ascii=False, indent=2) + "\n")
            os.kill(crashed_pid, signal.SIGKILL)
        close_gateway_process(gateway)
        gateway = None
        control.release(delegation_id, phase)

        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=port, boxteam_home=home,
            artifacts_dir=artifacts / "gateway-restarted", asgi_app="tests.support.e2e_control.gateway_app:app",
            extra_env=control.environment,
        )
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=60, trust_env=False) as client:
            await acquire_gateway_guest(client)
            assert (await client.get("/api/v1/workspace")).status_code == 200
            child_job = await _wait_job(client, intent["job_id"])
            assert child_job["status"] in {"completed", "succeeded"}, child_job
            assert child_job["thread_id"] == record["child_thread_id"]
            async with asyncio.timeout(30):
                while True:
                    resolver = get_session_path_resolver(workspace / ".boxteam/sessions")
                    thread_node = resolver.resolve_thread_node(session_id, main_thread_id)
                    wakeups = _rows(thread_node / "rollout" / "index.sqlite", "SELECT * FROM runtime_execution_wakeups WHERE source_id = ?", (f"subagent:{delegation_id}:result",))
                    if wakeups:
                        break
                    await asyncio.sleep(0.05)
            assert len(wakeups) == 1 and wakeups[0]["gateway_id"] == gateway_id
            assert wakeups[0]["request_id"] == request_id
            reply = await _wait_job(client, wakeups[0]["job_id"])
            assert reply["status"] in {"completed", "succeeded"}, reply
            assert reply["thread_id"] == main_thread_id
            compiled_child = [event for event in control.events if event["phase"] == "agent.graph.compiled" and event.get("details", {}).get("thread_id") == record["child_thread_id"]]
            assert compiled_child and all(event["pid"] != crashed_pid for event in compiled_child)
            traces = [json.loads(line) for line in (session_node / "logs" / "traces" / "messages.jsonl").read_text().splitlines()]
            jobs = {event["job_id"]: event for event in traces if event["type"] == "job_created"}
            assert jobs[parent_job_id]["payload"]["request_id"] == request_id
            assert jobs[intent["job_id"]]["payload"]["gateway_id"] == gateway_id
            assert jobs[intent["job_id"]]["payload"]["request_id"] == request_id
            assert jobs[wakeups[0]["job_id"]]["payload"]["gateway_id"] == gateway_id
            assert jobs[wakeups[0]["job_id"]]["payload"]["request_id"] == request_id
            requests = [json.dumps(payload["messages"], ensure_ascii=False) for payload in state.requests]
            child_requests = [payload for payload in requests if "<delegated_task " in payload]
            reply_requests = [payload for payload in requests if "<generated_session_result " in payload]
            assert len(child_requests) == 2 and OLD_BODY.strip() in child_requests[1]
            assert len(reply_requests) == 2 and "COLD_CHILD_DONE" in reply_requests[0]
            assert OLD_BODY.strip() in reply_requests[1]
            assert all(request_id not in payload for payload in requests), "请求身份不得进入 Provider 正文"
            (artifacts / "restored-dispatch-and-reply.json").write_text(json.dumps({"child_job": child_job, "reply_job": reply, "wakeup": wakeups[0], "job_created": jobs, "compiled_child": compiled_child}, ensure_ascii=False, indent=2) + "\n")
    finally:
        release_task_call.set()
        if delegation_id is not None:
            control.release(delegation_id, phase)
        if gateway is not None:
            close_gateway_process(gateway)
        control.close()
        (artifacts / "model-requests.json").write_text(json.dumps(state.requests, ensure_ascii=False, indent=2) + "\n")
