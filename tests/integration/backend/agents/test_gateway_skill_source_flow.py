"""真实 Gateway→受管 Workspace→Provider→skill_load；模型由共享 model_stream 精确回放驱动。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import commentjson
import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.services.orchestration.session_subagent_service import _derive_delegation_id
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.support.e2e_control.harness import E2ETestControlHarness
from tests.support.model_stream.config import model_stream_replay
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)

SKILL_NAME = "gateway-frozen-proof"
OLD_BODY = "Gateway 冻结正文 Ω 001  \r\n"
NEW_BODY = "Gateway 新版正文 Ω 002  \r\n"
FRONTMATTER = f"---\r\nname: {SKILL_NAME}\r\ndescription: Gateway raw snapshot proof\r\n---\r\n"


MODEL = "gateway-skill-test-model"
ENDPOINT = "https://opencode.ai/zen/v1"


@dataclass
class ModelState:
    control: E2ETestControlHarness
    config_path: Path
    artifacts: Path
    call_ids: list[str] = field(default_factory=list)

    @property
    def requests(self) -> list[dict]:
        return [json.loads(path.read_text())["request"] for call_id in self.call_ids
                if (path := self.artifacts / f"{call_id}.json").is_file()]

    @property
    def environment(self) -> dict[str, str]:
        return {**self.control.environment, "BOXTEAM_TEST_MODEL_STREAM_CONFIG": str(self.config_path)}

    def plan(self, session_id: str, thread_id: str, *replies: tuple[dict, str]) -> list[str]:
        interactions = [build_cassette(
            asset_id="gateway-skill-response", model=MODEL,
            url=f"{ENDPOINT}/chat/completions",
            frames=chat_completion_frames(response_id="chatcmpl-gateway-skill", model=MODEL,
                                          deltas=(delta,), finish_reason=reason),
        ).raw["interactions"][0] for delta, reason in replies]
        calls = self.control.plan_model_calls(session_id, thread_id, interactions)
        self.call_ids.extend(calls)
        return calls


def _skill_reply(call_id: str, *, tracked: bool = False) -> tuple[dict, str]:
    arguments = {"name": SKILL_NAME, **({"mode": "tracked"} if tracked else {})}
    return {"role": "assistant", "tool_calls": [{
        "index": 0, "id": call_id, "type": "function",
        "function": {"name": "skill_load", "arguments": json.dumps(arguments)},
    }]}, "tool_calls"


def _text_reply(text: str) -> tuple[dict, str]:
    return {"role": "assistant", "content": text}, "stop"


@pytest.fixture
def scripted_model(request: pytest.FixtureRequest, integration_workspace_root_path: str) -> Iterator[ModelState]:
    output = Path(integration_workspace_root_path).parent
    with model_stream_replay(
        output, runtime_root=output / "runtime" / request.node.name,
        business_assertion="gateway-skill-source-flow",
    ) as replay:
        yield ModelState(replay.control, replay.config_path, replay.artifacts)


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
    state = scripted_model
    workspace = Path(integration_workspace_root_path)
    output = workspace.parent
    artifacts = output / "artifacts"
    artifacts.mkdir(exist_ok=True)
    home = output / "boxteam-home"
    port = integration_port_block_for_file(Path(request.node.fspath)).port(11)
    config_path = Path(integration_workspace_config_path)
    _configure_model(config_path, ENDPOINT)
    skill_file = home / "skills" / SKILL_NAME / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_bytes((FRONTMATTER + OLD_BODY).encode())
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=None, port=port,
        boxteam_home=home, artifacts_dir=artifacts / "gateway-first", asgi_app="tests.support.e2e_control.gateway_app:app",
        extra_env=state.environment,
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
            publication_path = raw_path.replace("/skill-sources/", "/skill-source-connections/") + "/publications/wait"
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
            first_publication = await client.post(publication_path, json={})
            assert first_publication.status_code == 200, first_publication.text
            assert first_publication.headers["X-Request-ID"] == first_publication.json()["request_id"]
            first_notice = first_publication.json()["data"]
            assert first_notice["revision"] == original["revision"] and first_notice["gap"]

            def change_after_model_capture() -> None:
                skill_file.write_bytes((FRONTMATTER + NEW_BODY).encode())
                with httpx.Client(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=6, trust_env=False) as probe:
                    notified = probe.post(publication_path, json={"after_cursor": first_notice["cursor"]})
                    assert notified.status_code == 200, notified.text
                    notice = notified.json()["data"]
                    assert notice["changed"] and notice["revision"] != original["revision"]
                    publication = probe.get(raw_path).json()["data"]
                    assert publication["revision"] == notice["revision"]
                    assert publication["entries"][0]["content"] == FRONTMATTER + NEW_BODY
                    # 通知后的后台获取不改变本轮已冻结的 Skill；下一 Turn 使用 accepted 新版。
                    time.sleep(1.2)

            created = await client.post("/api/v1/sessions", json={"title": "Gateway frozen Skill"})
            assert created.status_code == 200, created.text
            session_id = created.json()["data"]["session_id"]
            thread_reply = await client.get(f"/api/v1/sessions/{session_id}/main-thread")
            thread_id = thread_reply.json()["data"]["thread_id"]
            calls = state.plan(session_id, thread_id, _skill_reply("call_gateway_skill", tracked=True),
                               _text_reply("GATEWAY_SKILL_OK"))
            state.control.arm(calls[0], "model.response_ready")
            accepted = await client.post(f"/api/v1/sessions/{session_id}/messages", json={
                "message": {"content": "继续验证已登记的 Skill 来源"},
                "run": {"mode": "single_agent", "agent_id": "default"},
            })
            assert accepted.status_code == 200, accepted.text
            try:
                await asyncio.to_thread(state.control.acknowledged, calls[0], "model.response_ready")
                await asyncio.to_thread(change_after_model_capture)
            finally:
                state.control.release(calls[0], "model.response_ready")
            job = await _wait_job(client, accepted.json()["data"]["job_id"])
            assert job["status"] in {"completed", "succeeded"}, job
            assert len(state.requests) == 2
            first_text = json.dumps(state.requests[0], ensure_ascii=False)
            second_text = json.dumps(state.requests[1], ensure_ascii=False)
            assert "Gateway raw snapshot proof" in first_text
            assert OLD_BODY.strip() not in first_text and NEW_BODY.strip() not in first_text
            assert OLD_BODY.strip() in second_text and NEW_BODY.strip() not in second_text
            tool_messages = [item for item in state.requests[1]["messages"] if item["role"] == "tool"]
            assert tool_messages and "loaded" in json.dumps(tool_messages)
            state.plan(session_id, thread_id, _text_reply("GATEWAY_SKILL_OK"))
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
            artifacts_dir=artifacts / "gateway-restarted", asgi_app="tests.support.e2e_control.gateway_app:app",
            extra_env=state.environment,
        )
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=60, trust_env=False) as client:
            await acquire_gateway_guest(client)
            restarted_health = await client.get("/api/gateway/health")
            assert restarted_health.json()["data"]["gateway_id"] == gateway_id
            rebuilt = (await client.get(raw_path)).json()["data"]
            assert rebuilt["revision"] == refreshed["revision"]
            assert rebuilt["entries"] == refreshed["entries"]
            state.plan(session_id, thread_id, _text_reply("GATEWAY_SKILL_OK"))
            # TODO: 冷启动 runtime 曾耗时约25秒并触发原30秒 Job期限；保留期限，后续定位性能。
            restored = await _job(client, session_id)
            assert restored["status"] in {"completed", "succeeded"}, restored
            assert NEW_BODY.strip() in json.dumps(state.requests[-1], ensure_ascii=False)
            assert len(state.requests) == len(state.call_ids) == 4
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
    state = scripted_model
    workspace = Path(integration_workspace_root_path)
    output = workspace.parent
    artifacts = output / "artifacts" / "cold-child"
    artifacts.mkdir(parents=True, exist_ok=True)
    home = output / "boxteam-home"
    port = integration_port_block_for_file(Path(request.node.fspath)).port(12)
    _configure_model(Path(integration_workspace_config_path), ENDPOINT)
    skill_file = home / "skills" / SKILL_NAME / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_bytes((FRONTMATTER + OLD_BODY).encode())
    control = state.control
    gateway = None
    phase = "thread.creation.published"
    delegation_id = None
    try:
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=port, boxteam_home=home,
            artifacts_dir=artifacts / "gateway-first", asgi_app="tests.support.e2e_control.gateway_app:app",
            extra_env=state.environment,
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
            first_call = state.plan(session_id, main_thread_id, ({"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_cold_child", "type": "function",
                "function": {"name": "task", "arguments": json.dumps({"description": "COLD_CHILD_SKILL_TASK：显式加载 Gateway Skill 并返回结果。"})},
            }]}, "tool_calls"))[0]
            control.arm(first_call, "model.response_ready")
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
            control.release(first_call, "model.response_ready")
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
            # 关键 child/intent 由真实 task 发布；只在观察到 owner 后计划后续模型响应。
            state.plan(session_id, record["child_thread_id"], _skill_reply("call_child_skill"),
                       _text_reply("COLD_CHILD_DONE"))
            state.plan(session_id, main_thread_id, _skill_reply("call_reply_skill"),
                       _text_reply("COLD_REPLY_DONE"))
            os.kill(crashed_pid, signal.SIGKILL)
        close_gateway_process(gateway)
        gateway = None
        control.release(delegation_id, phase)

        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=port, boxteam_home=home,
            artifacts_dir=artifacts / "gateway-restarted", asgi_app="tests.support.e2e_control.gateway_app:app",
            extra_env=state.environment,
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
                        assert len(wakeups) == 1
                        assert wakeups[0]["last_error"] is None, wakeups[0]
                        # 持久唤醒先于 Job 创建；仅派发收据提交后才查询原 Job。
                        if wakeups[0]["state"] == "dispatched":
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
            assert len(state.requests) == len(state.call_ids) == 5
            assert all(request_id not in payload for payload in requests), "请求身份不得进入 Provider 正文"
            (artifacts / "restored-dispatch-and-reply.json").write_text(json.dumps({"child_job": child_job, "reply_job": reply, "wakeup": wakeups[0], "job_created": jobs, "compiled_child": compiled_child}, ensure_ascii=False, indent=2) + "\n")
    finally:
        if delegation_id is not None:
            control.release(delegation_id, phase)
        if gateway is not None:
            close_gateway_process(gateway)
        (artifacts / "model-requests.json").write_text(json.dumps(state.requests, ensure_ascii=False, indent=2) + "\n")
