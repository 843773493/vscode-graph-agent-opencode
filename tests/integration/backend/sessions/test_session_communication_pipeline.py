"""真实 Gateway/Workspace HTTP 与受控 Provider 的通信核心集成验收。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path

import commentjson
import httpx
import psutil
import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.support.api_helpers import create_session
from tests.support.communication_federation import (
    CommunicationNode,
    open_communication_triad,
)
from tests.support.e2e_control.harness import control_request
from tests.support.model_stream_config import ModelStreamReplay, model_stream_replay
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    BackendProcess,
    close_backend_process,
    start_backend_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


async def _plan_communication_calls(
    replay: ModelStreamReplay, client: httpx.AsyncClient, workspace: Path,
    session_id: str, responses: list[dict[str, object]],
    *, thread_id: str | None = None,
) -> tuple[list[str], str]:
    if thread_id is None:
        main = await client.get(f"/api/v1/sessions/{session_id}/main-thread")
        assert main.status_code == 200, main.text
        thread_id = main.json()["data"]["thread_id"]
    config = commentjson.loads((workspace / ".boxteam/workspace.jsonc").read_text(encoding="utf-8"))
    endpoint = next(provider["endpoint"] for provider in config["llm"]["providers"] if provider["id"] == "primary")
    interactions = [build_cassette(
        asset_id="session-communication", model="e2e-stub-model", url=endpoint + "/chat/completions",
        frames=chat_completion_frames(
            response_id=f"chatcmpl-communication-{index}", model="e2e-stub-model",
            deltas=({"role": "assistant", **response},),
            finish_reason="tool_calls" if "tool_calls" in response else "stop",
        ),
    ).raw["interactions"][0] for index, response in enumerate(responses)]
    return replay.control.plan_model_calls(session_id, thread_id, interactions), thread_id


def _communication_request(
    replay: ModelStreamReplay, planned_calls: dict[str, dict[str, str]], call: str,
) -> dict[str, object]:
    artifact = json.loads((replay.artifacts / (call + ".json")).read_text(encoding="utf-8"))
    assert artifact["identity"] == planned_calls[call]
    assert artifact["request"]["stream"] is True
    return artifact["request"]


def _committed_tool_result(replay: ModelStreamReplay, session_id: str, call_id: str) -> dict[str, object]:
    events = [event["details"] for event in replay.control.events
              if event["phase"] == "test.tool_result.committed" and event["operation_id"] == call_id]
    result, = events
    assert result["session_id"] == session_id
    assert result["item"]["payload"]["tool_call_id"] == call_id
    return result["item"]["payload"]


@dataclass
class CommunicationCase:
    client: httpx.AsyncClient
    replay: ModelStreamReplay
    workspace: Path
    artifacts: Path
    backend: BackendProcess
    gateway: GatewayProcess
    gateway_id: str
    barrier_stage: str | None = None
    planned_calls: dict[str, dict[str, str]] = field(default_factory=dict)

    def record(self, name: str, value: object) -> None:
        (self.artifacts / (name + ".json")).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    async def session_node(self, session_id: str) -> Path:
        response = await self.client.get("/api/v1/session-catalog/export")
        assert response.status_code == 200, response.text
        assert any(node["node_id"] == session_id for node in response.json()["data"]["items"])
        return get_session_path_resolver(self.workspace / ".boxteam/sessions").resolve_session_node(session_id)

    def main_thread_node(self, session_id: str) -> Path:
        resolver = get_session_path_resolver(self.workspace / ".boxteam/sessions")
        return resolver.resolve_thread_node(session_id, resolver.main_thread_id(session_id))

    async def plan(
        self, session_id: str, responses: list[dict[str, object]], *, thread_id: str | None = None,
    ) -> list[str]:
        calls, thread_id = await _plan_communication_calls(
            self.replay, self.client, self.workspace, session_id, responses, thread_id=thread_id,
        )
        self.planned_calls.update({call: {"session_id": session_id, "thread_id": thread_id, "model_call_id": call} for call in calls})
        return calls

    def request(self, call: str) -> dict[str, object]:
        return _communication_request(self.replay, self.planned_calls, call)

    def tool_result(self, session_id: str, call_id: str) -> dict[str, object]:
        return _committed_tool_result(self.replay, session_id, call_id)

    async def hard_restart(self) -> None:
        previous = self.backend
        descendants = psutil.Process(previous.process.pid).children(recursive=True) if os.name == "nt" else []
        if os.name == "nt":
            # venv redirector 的真实解释器是子进程；硬退出必须终止测试拥有的整棵树。
            self.record(f"hard-exit-{previous.process.pid}", [
                {"pid": process.pid, "created_at": process.create_time(), "executable": process.exe()}
                for process in descendants
            ])
            await asyncio.to_thread(
                subprocess.run, ["taskkill", "/PID", str(previous.process.pid), "/T", "/F"],
                check=True, capture_output=True, timeout=10,
            )
            _gone, alive = await asyncio.to_thread(psutil.wait_procs, descendants, timeout=10)
            assert not alive, [process.pid for process in alive]
        else:
            previous.process.kill()
        exit_code = await asyncio.to_thread(previous.process.wait, timeout=10)
        # Windows kill 使用 TerminateProcess(1)，POSIX 使用 SIGKILL。
        assert exit_code == (1 if os.name == "nt" else -signal.SIGKILL)
        await asyncio.to_thread(close_backend_process, previous)
        self.backend = await asyncio.to_thread(
            start_backend_process, workspace_root=str(self.workspace), port=previous.port,
            log_name="communication-restarted", asgi_app="tests.support.e2e_control.backend_app:app",
            env_overrides={**self.replay.environment, "LITELLM_LOCAL_MODEL_COST_MAP": "true", "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{self.gateway.port}"},
        )
        assert self.backend.process.pid != previous.process.pid


def _prepare_workspace(context: TestRunContext, model_port: int, gateway_port: int) -> Path:
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    config_path = install_test_workspace_config(
        workspace_root=workspace, config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
        schema_path=Path.cwd() / "configs/workspace_schema.jsonc",
    )
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = f"http://127.0.0.1:{gateway_port}"
    for provider in config["llm"]["providers"]:
        provider.update({
            "endpoint": f"http://127.0.0.1:{model_port}/v1", "model": "e2e-stub-model",
            "api_key": "e2e-local-model-key", "custom_llm_provider": "openai",
            "api_mode": {"protocol": "chat_completions", "model_info": {
                "supports_function_calling": True, "supports_reasoning": True,
            }, "supports_reasoning": {"reasoning_content": True}},
        })
        provider.pop("auth", None)
        provider.pop("request_options", None)
    for agent in config["agents"].values():
        agent["model"]["fallback_providers"] = []
        agent.setdefault("execution", {})["require_delegated_report"] = False
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return workspace


@pytest.fixture
async def communication_case(request: pytest.FixtureRequest) -> AsyncIterator[CommunicationCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    workspace = _prepare_workspace(context, ports.port(10), ports.port(1))
    reset_gateway_persistent_state(workspace_root=workspace)
    artifacts = context.artifacts_dir / request.node.name
    artifacts.mkdir(parents=True, exist_ok=True)
    barrier = getattr(request, "param", {}).get("barrier", False)
    trace = getattr(request, "param", {}).get("trace", False)
    environment = {"BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{ports.port(1)}"}
    if barrier:
        environment["BOXTEAM_TEST_COMMUNICATION_BARRIER_DIR"] = str(artifacts)
        environment["BOXTEAM_TEST_COMMUNICATION_BARRIER_STAGE"] = "before_source" if barrier is True else barrier
        (artifacts / "before-source.json").unlink(missing_ok=True)
        for marker in artifacts.glob("barrier-*.json"):
            marker.unlink()
    if trace:
        environment["BOXTEAM_TEST_COMMUNICATION_TRACE_DIR"] = str(artifacts)
        (artifacts / "carrier-observations.jsonl").unlink(missing_ok=True)
        (artifacts / "carrier-failure.json").unlink(missing_ok=True)
    with model_stream_replay(
        artifacts, runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="session-communication-and-direct-file-tool-consumers",
    ) as replay:
        environment.update({**replay.environment, "LITELLM_LOCAL_MODEL_COST_MAP": "true"})
        backend = await asyncio.to_thread(
            start_backend_process, workspace_root=str(workspace), port=ports.port(0),
            log_name="communication-backend", env_overrides=environment,
            asgi_app="tests.support.e2e_control.communication_backend_app:app",
        )
        gateway = None
        case = None
        try:
            gateway = await asyncio.to_thread(
                start_gateway_process, workspace_root=workspace, port=ports.port(1),
                default_backend_url=f"http://127.0.0.1:{backend.port}",
                extra_env=getattr(request, "param", {}).get("gateway_environment"),
            )
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=90,
            ) as client:
                await acquire_gateway_guest(client)
                health = await client.get("/api/gateway/health")
                assert health.status_code == 200, health.text
                case = CommunicationCase(client, replay, workspace, artifacts, backend, gateway, health.json()["data"]["gateway_id"], environment.get("BOXTEAM_TEST_COMMUNICATION_BARRIER_STAGE"))
                try:
                    yield case
                finally:
                    case.record("provider-wire", [case.request(call) for call in case.planned_calls if (replay.artifacts / (call + ".json")).exists()])
        finally:
            if gateway is not None:
                await asyncio.to_thread(close_gateway_process, case.gateway if case is not None else gateway)
            await asyncio.to_thread(close_backend_process, case.backend if case is not None else backend)


def _tool(name: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
    return {"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
    }}]}


async def _completed(client: httpx.AsyncClient, job_id: str) -> dict[str, object]:
    async with asyncio.timeout(60):
        while True:
            response = await client.get(f"/api/v1/jobs/{job_id}")
            assert response.status_code == 200, response.text
            job = response.json()["data"]
            if job["status"] in {"completed", "failed", "cancelled", "timed_out", "execution_lost"}:
                assert job["status"] == "completed", job.get("error_message") or job
                return job
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("communication_case", [{"trace": True}], indirect=True)
async def test_c1_real_send_worker_wait_read(communication_case: CommunicationCase) -> None:
    case = communication_case
    source_session = await create_session(case.client, "C1 source")
    target_session = await create_session(case.client, "C1 target")
    source_main = (await case.client.get(f"/api/v1/sessions/{source_session}/main-thread")).json()["data"]["thread_id"]
    target_main = (await case.client.get(f"/api/v1/sessions/{target_session}/main-thread")).json()["data"]["thread_id"]
    await case.plan(target_session, [{"content": "c1-target-result"}])
    first_call, = await case.plan(source_session, [_tool("send_message_to_session", {
        "target_session_id": target_session, "content": "c1-private-message", "kind": "result",
    }, "call_c1_send")])
    send_call = first_call + ":tool-call:call_c1_send"
    case.replay.control.arm(send_call, "test.tool_result.committed")
    response = await case.client.post(f"/api/v1/sessions/{source_session}/messages", json={
        "message": {"content": "c1-source-trigger，发送后等待并冷读目标。"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    try:
        await asyncio.to_thread(case.replay.control.acknowledged, send_call, "test.tool_result.committed", 30)
        sent = case.tool_result(source_session, send_call)
        receipt = json.loads(sent["content"])
        following = await case.plan(source_session, [
            _tool("wait_for_session", {"target_session_id": target_session,
                  "communication_id": receipt["communication_id"], "timeout_seconds": 30}, "call_c1_wait"),
            _tool("invoke_extension_tool", {"tool_name": "read_context", "arguments": {
                "resource_identity": str(session_resource_identity(case.backend.workspace_id, target_session)),
                "uri": session_resource_uri(case.backend.workspace_id, target_session), "scope": "workspace", "view": "messages",
            }}, "call_c1_read"), {"content": "c1-source-completed"},
        ])
    finally:
        case.replay.control.release(send_call, "test.tool_result.committed")
    source_job = await _completed(case.client, response.json()["data"]["job_id"])
    tools = {message["name"]: json.loads(message["content"]) for message in case.request(following[-1])["messages"] if message["role"] == "tool"}
    results = {"receipt": tools["send_message_to_session"], "wait": tools["wait_for_session"], "read": tools["invoke_extension_tool"]}
    assert results["receipt"] == receipt
    case.record("c1-tool-results", results)
    assert results["wait"]["status"] == "completed", results
    assert "c1-target-result" in json.dumps(results["read"], ensure_ascii=False), results
    source_node = await case.session_node(source_session)
    target_node = await case.session_node(target_session)
    with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as source_store, closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as target_store:
        receipt = results["receipt"]
        inbox = target_store.get_communication_inbox(receipt["communication_id"])
        assert (inbox.source_gateway_id, inbox.source_workspace_id, inbox.source_session_id, inbox.source_thread_id) == (
            case.gateway_id, case.backend.workspace_id, source_session, source_main,
        )
        assert inbox.target_thread_id == target_main and inbox.state == "execution_bound"
        assert source_store.list_pending_communication_outboxes() == ()
        assert target_store.list_target_accepted_communication_inboxes() == ()
        target_job = await _completed(case.client, inbox.job_id)
        case.record("c1-binding", {"inbox": asdict(inbox), "source_job": source_job, "target_job": target_job})
    database = case.main_thread_node(target_session) / "rollout/index.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT execution_origin,turn_id FROM executions").fetchall() == [("internal", None)]
        assert connection.execute("SELECT COUNT(*) FROM turn_records").fetchone()[0] == 0
        assert connection.execute("SELECT source_id,job_id FROM runtime_execution_wakeups").fetchall() == [(receipt["communication_id"], inbox.job_id)]


@pytest.mark.asyncio
async def test_real_child_execution_sends_and_waits_with_durable_source_owner(
    communication_case: CommunicationCase,
) -> None:
    """从真实 task 创建 child，经原执行工具链验证 outbox 与 wait 的 source owner。"""
    case = communication_case
    source = await create_session(case.client, "child 通信来源")
    target = await create_session(case.client, "child 通信目标")
    source_main = (await case.client.get(f"/api/v1/sessions/{source}/main-thread")).json()["data"]["thread_id"]
    target_main = (await case.client.get(f"/api/v1/sessions/{target}/main-thread")).json()["data"]["thread_id"]
    child = "thr_" + case.replay.control.next_uuid("thr")
    await case.plan(source, [{"content": "child 初始化完成。"}], thread_id=child)
    await case.plan(source, [
        _tool("task", {"description": "初始化后等待直接消息。", "subagent_type": "general-purpose"}, "child-source-create"),
        {"content": "已创建通信 child。"}, {"content": "child 初始化汇报已处理。"},
    ])
    created = await case.client.post(f"/api/v1/sessions/{source}/messages", json={
        "message": {"content": "创建一个同 Session 的持久 child，等待直接指令。"}, "run": {},
    })
    assert created.status_code == 200, created.text
    await _completed(case.client, created.json()["data"]["job_id"])
    async with asyncio.timeout(60):
        while True:
            listing = await case.client.get(f"/api/v1/sessions/{source}/child-threads")
            assert listing.status_code == 200, listing.text
            items = [row for row in listing.json()["data"]["items"] if row["thread_id"] == child]
            if items and items[0]["report_status"] in {"completed", "completed_empty"}:
                break
            await asyncio.sleep(0.05)
    assert child != source_main
    await case.plan(target, [{"content": "目标 main 完成 child 发送的任务。"}])
    send_provider_id = "child-source-send"
    first_call, = await case.plan(source, [_tool("send_message_to_session", {
        "target_session_id": target, "content": "来自真实 child 的唯一通信。", "kind": "result",
    }, send_provider_id)], thread_id=child)
    send_call = first_call + ":tool-call:" + send_provider_id
    case.replay.control.arm(send_call, "test.tool_result.committed")
    response = await case.client.post(f"/api/v1/sessions/{source}/threads/{child}/messages", json={
        "message": {"content": "向目标发送结果，并通过原 communication 等待完成。"}, "run": {},
    })
    assert response.status_code == 200, response.text
    assert response.json()["data"]["thread_id"] == response.json()["data"]["dispatch"]["thread_id"] == child
    try:
        await asyncio.to_thread(case.replay.control.acknowledged, send_call, "test.tool_result.committed", 30)
        sent = case.tool_result(source, send_call)
        receipt = json.loads(sent["content"])
        following = await case.plan(source, [
            _tool("wait_for_session", {"target_session_id": target,
                  "communication_id": receipt["communication_id"], "timeout_seconds": 60}, "child-source-wait"),
            {"content": "child 已确认目标完成。"},
        ], thread_id=child)
    finally:
        case.replay.control.release(send_call, "test.tool_result.committed")
    source_job = await _completed(case.client, response.json()["data"]["job_id"])
    assert (source_job["session_id"], source_job["thread_id"]) == (source, child)
    final_request = case.request(following[-1])
    tools = {row["name"]: json.loads(row["content"]) for row in final_request["messages"] if row["role"] == "tool"}
    assert tools["send_message_to_session"] == receipt
    assert tools["wait_for_session"]["status"] == "completed"

    def operation(kind: str, model_call: str, provider_call: str) -> str:
        preimage = json.dumps([kind, source, child, model_call, provider_call], separators=(",", ":"))
        return "cop_" + hashlib.sha256(preimage.encode()).hexdigest()

    send_operation = operation("send", first_call, send_provider_id)
    wait_operation = operation("wait", following[0], "child-source-wait")
    source_node = await case.session_node(source)
    target_node = await case.session_node(target)
    with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as source_store, closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as target_store:
        outbox = source_store.get_communication_outbox(send_operation)
        inbox = target_store.get_communication_inbox(receipt["communication_id"])
        assert outbox.source_thread_id == inbox.source_thread_id == child
        assert outbox.target_thread_id == inbox.target_thread_id == target_main
        assert outbox.session_id == inbox.source_session_id == source
        assert outbox.communication_id == receipt["communication_id"]
        assert inbox.state == "execution_bound" and inbox.turn_id is None
        assert outbox.target_session_id == inbox.session_id == target
        target_job = await _completed(case.client, inbox.job_id)
        assert target_job["thread_id"] == target_main
        case.record("child-source-ledger", {"outbox": asdict(outbox), "inbox": asdict(inbox), "receipt": receipt})
    with sqlite3.connect((source_node / CONTROL_DATABASE_NAME).as_uri() + "?mode=ro", uri=True) as connection:
        observations = [json.loads(row[0]) for row in connection.execute(
            "SELECT request_json FROM remote_observations WHERE side='source'",
        )]
        wait, = [row for row in observations if row["envelope"]["source_call_id"] == wait_operation]
        assert wait["envelope"]["source"]["thread_id"] == child
        assert wait["envelope"]["source"]["session_id"] == source
        assert wait["envelope"]["target"]["thread_id"] == target_main
        assert connection.execute("SELECT COUNT(*) FROM communication_outbox WHERE source_thread_id=?", (child,)).fetchone() == (1,)
    committed = [event["details"] for event in case.replay.control.events
                 if event["phase"] == "test.tool_result.committed" and event["operation_id"] in {
                     send_call, following[0] + ":tool-call:child-source-wait",
                 }]
    assert len(committed) == 2 and {(row["session_id"], row["thread_id"]) for row in committed} == {(source, child)}
    case.record("child-source-owner", {"source_main": source_main, "child": child, "target_main": target_main,
                                      "send_operation": send_operation, "wait_operation": wait_operation,
                                      "wait": wait, "committed": committed, "source_job": source_job})


@pytest.mark.asyncio
async def test_main_thread_http_preserves_request_identity_and_cold_state(communication_case: CommunicationCase) -> None:
    case = communication_case
    session = await create_session(case.client, "权威 main 冷读取")
    request_id = "req-main-thread-cold-contract"
    response = await case.client.get(f"/api/v1/sessions/{session}/main-thread", headers={"X-Request-ID": request_id})
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
    node = await case.session_node(session)
    with closing(SessionControlStore(node / CONTROL_DATABASE_NAME)) as store:
        assert response.json()["data"] == {"session_id": session, "thread_id": store.get_main_thread()["thread_id"]}
        assert store.list_target_accepted_communication_inboxes() == ()
    database = case.main_thread_node(session) / "rollout/index.sqlite"
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    case.record("main-thread-http", {
        "body": response.json(), "request_id_header": response.headers["X-Request-ID"],
        "rollout_exists": database.exists(), "execution_count": 0,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("communication_case", [{"barrier": True}], indirect=True)
async def test_c2_hard_restart_proactively_binds_accepted_inbox(communication_case: CommunicationCase) -> None:
    case = communication_case
    source_session = await create_session(case.client, "C2 source")
    target_session = await create_session(case.client, "C2 target")
    source_calls = await case.plan(source_session, [
        _tool("send_message_to_session", {"target_session_id": target_session, "content": "c2-private-message"}, "call_c2_send"),
        {"content": "c2-source-accepted"},
    ])
    await case.plan(target_session, [{"content": "c2-target-recovered"}])
    response = await case.client.post(f"/api/v1/sessions/{source_session}/messages", json={
        "message": {"content": "c2-source-trigger"}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    await _completed(case.client, response.json()["data"]["job_id"])
    receipt, = [json.loads(message["content"]) for message in case.request(source_calls[-1])["messages"] if message["role"] == "tool" and message["name"] == "send_message_to_session"]
    results = {"receipt": receipt}
    async with asyncio.timeout(20):
        while not (case.artifacts / "before-source.json").exists():
            await asyncio.sleep(0.02)
    barrier = json.loads((case.artifacts / "before-source.json").read_text(encoding="utf-8"))
    assert barrier["state"] == "target_accepted" and barrier["job_id"] is None and barrier["execution_id"] is None
    assert barrier["communication_id"] == results["receipt"]["communication_id"]
    target_node = await case.session_node(target_session)
    with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
        claimed = store.get_communication_inbox(barrier["communication_id"])
        assert claimed.state == "target_accepted" and claimed.admission_claim_generation is not None
    before = case.main_thread_node(target_session) / "rollout/index.sqlite"
    if before.exists():
        with sqlite3.connect(before.as_uri() + "?mode=ro", uri=True) as connection:
            assert connection.execute("SELECT COUNT(*) FROM runtime_execution_wakeups").fetchone()[0] == 0
    await case.hard_restart()
    # 启动 owner 必须自己绑定；此断言之前没有任何 read/wait 请求。
    with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
        inbox = store.get_communication_inbox(barrier["communication_id"])
        assert inbox.state == "execution_bound" and inbox.admission_id == barrier["admission_id"]
        assert inbox.admission_claim_generation > claimed.admission_claim_generation
    target_job = await _completed(case.client, inbox.job_id)
    resumed_calls = await case.plan(source_session, [
        _tool("wait_for_session", {"target_session_id": target_session, "communication_id": receipt["communication_id"], "timeout_seconds": 20}, "call_c2_wait"),
        {"content": "c2-observation-completed"},
    ])
    resumed = await case.client.post(f"/api/v1/sessions/{source_session}/messages", json={
        "message": {"content": "c2-resume-trigger"}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert resumed.status_code == 200, resumed.text
    await _completed(case.client, resumed.json()["data"]["job_id"])
    results["wait"], = [json.loads(message["content"]) for message in case.request(resumed_calls[-1])["messages"] if message["role"] == "tool" and message["name"] == "wait_for_session"]
    assert results["wait"]["status"] == "completed", results
    with sqlite3.connect(before.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT execution_id,execution_origin,turn_id FROM executions").fetchall() == [(inbox.execution_id, "internal", None)]
        assert connection.execute("SELECT COUNT(*) FROM turn_records").fetchone()[0] == 0
        assert connection.execute("SELECT source_id,job_id FROM runtime_execution_wakeups").fetchall() == [(barrier["communication_id"], inbox.job_id)]
    case.record("c2-recovery", {"before": asdict(claimed), "after": asdict(inbox), "target_job": target_job, "tools": results})


@pytest.mark.asyncio
@pytest.mark.parametrize("communication_case", [
    {"barrier": "after_outbox"}, {"barrier": "after_receipt"},
    {"barrier": "after_wakeup"}, {"barrier": "after_job", "trace": True},
], indirect=True, ids=["source-outbox", "target-acceptance-before-source-receipt", "source-wakeup-before-job", "job-before-inbox-cas"])
async def test_communication_crash_windows_keep_original_identity(communication_case: CommunicationCase) -> None:
    """各 durable owner 之间硬杀进程，正常 startup 按原 communication/source/Job 恢复。"""
    case = communication_case
    assert case.barrier_stage is not None
    source_session = await create_session(case.client, "崩溃矩阵 source")
    target_session = await create_session(case.client, "崩溃矩阵 target")
    target_node = await case.session_node(target_session)
    target_rollout = case.main_thread_node(target_session) / "rollout"

    def target_context_snapshot():
        return target_rollout.exists(), tuple(
            (path.relative_to(target_rollout).as_posix(), path.read_bytes() if path.is_file() else None)
            for path in sorted(target_rollout.rglob("*"))
        )

    target_context_before = target_context_snapshot()
    database = target_rollout / "index.sqlite"
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            for table in ("executions", "turn_records", "runtime_execution_wakeups", "model_calls", "item_catalog"):
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    await case.plan(source_session, [
        _tool("send_message_to_session", {"target_session_id": target_session, "content": "matrix-private-message"}, "call_matrix_send"),
        {"content": "matrix-source-completed"},
    ])
    target_calls = await case.plan(target_session, [{"content": "matrix-target-completed"}])
    response = await case.client.post(f"/api/v1/sessions/{source_session}/messages", json={
        "message": {"content": "matrix-source-trigger"}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    marker_path = case.artifacts / ("barrier-" + case.barrier_stage + ".json")
    async with asyncio.timeout(40):
        while not marker_path.exists():
            await asyncio.sleep(0.02)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    communication_id = marker["communication_id"]
    if case.barrier_stage == "after_receipt":
        target_marker_path = case.artifacts / "before-source.json"
        async with asyncio.timeout(40):
            while not target_marker_path.exists():
                await asyncio.sleep(0.02)
        target_marker = json.loads(target_marker_path.read_text(encoding="utf-8"))
        assert target_marker["communication_id"] == communication_id
        assert target_marker["state"] == "target_accepted"
        assert target_marker["job_id"] is None and target_marker["execution_id"] is None
    source_node = await case.session_node(source_session)
    target_node = await case.session_node(target_session)
    with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
        with sqlite3.connect(source_node / CONTROL_DATABASE_NAME) as connection:
            operation_id = connection.execute("SELECT send_operation_id FROM communication_outbox WHERE communication_id = ?", (communication_id,)).fetchone()[0]
        outbox = store.get_communication_outbox(operation_id)
        assert outbox.communication_id == communication_id
        source_lease = store.find_lease_by_operation("communication-source|" + outbox.send_operation_id)
        assert source_lease is not None
    if case.barrier_stage == "after_outbox":
        assert outbox.state == "accepted"
        with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store, pytest.raises(KeyError):
            store.get_communication_inbox(communication_id)
    else:
        with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
            accepted = store.get_communication_inbox(communication_id)
            assert accepted.communication_id == communication_id
        if case.barrier_stage == "after_receipt":
            assert outbox.state == "routing" and outbox.latest_receipt is None
            assert marker["receipt"]["admission_id"] == accepted.admission_id
            assert accepted.state == "target_accepted"
            assert accepted.job_id is None and accepted.execution_id is None
            # acceptance 不物化 cold owner；已存在的 rollout 目录及原字节也不能改变。
            assert target_context_snapshot() == target_context_before
        else:
            assert accepted.state == "target_accepted" and accepted.job_id is None
            await _completed(case.client, response.json()["data"]["job_id"])
            database = case.main_thread_node(target_session) / "rollout/index.sqlite"
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                assert connection.execute("SELECT COUNT(*) FROM runtime_execution_wakeups").fetchone()[0] == 1
                assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 1
                if case.barrier_stage == "after_wakeup":
                    assert connection.execute("SELECT state,dispatch_generation FROM runtime_execution_wakeups").fetchall() == [("pending", 0)]
                    assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
            if case.barrier_stage == "after_job":
                await _completed(case.client, marker["binding"]["job_id"])
    await case.hard_restart()
    # 恢复 owner 主动收敛；此前和此循环中都没有 read/wait 调用。
    async with asyncio.timeout(40):
        while True:
            with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
                recovered_outbox = store.get_communication_outbox(outbox.send_operation_id)
            with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
                try:
                    recovered_inbox = store.get_communication_inbox(communication_id)
                except KeyError:
                    recovered_inbox = None
            if recovered_outbox.state == "target_accepted" and recovered_inbox is not None and recovered_inbox.state == "execution_bound":
                break
            await asyncio.sleep(0.02)
    assert recovered_outbox.payload_hash == outbox.payload_hash
    assert recovered_inbox.payload_hash == outbox.payload_hash
    receipt = json.loads(recovered_outbox.latest_receipt)
    assert receipt["admission_id"] == recovered_inbox.admission_id
    if case.barrier_stage == "after_job":
        assert recovered_inbox.job_id == marker["binding"]["job_id"]
        assert recovered_inbox.execution_id == marker["binding"]["execution_id"]
    database = case.main_thread_node(target_session) / "rollout/index.sqlite"
    async with asyncio.timeout(40):
        while True:
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                outcome = connection.execute("SELECT outcome FROM executions WHERE execution_id = ?", (recovered_inbox.execution_id,)).fetchone()[0]
            if outcome != "unknown":
                assert outcome == "completed", outcome
                break
            await asyncio.sleep(0.02)
    with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
        recovered_lease = store.find_lease_by_operation("communication-source|" + outbox.send_operation_id)
        assert recovered_lease.lease_id == source_lease.lease_id
        assert recovered_lease.fencing_token == source_lease.fencing_token
        assert recovered_lease.state == "completed"
    for node, table in ((source_node, "communication_outbox"), (target_node, "communication_inbox")):
        with sqlite3.connect(node / CONTROL_DATABASE_NAME) as connection:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    with sqlite3.connect((case.main_thread_node(target_session) / "rollout/index.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT execution_id,execution_origin,turn_id FROM executions").fetchall() == [(recovered_inbox.execution_id, "internal", None)]
        assert connection.execute("SELECT COUNT(*) FROM turn_records").fetchone()[0] == 0
        assert connection.execute("SELECT source_id,job_id FROM runtime_execution_wakeups").fetchall() == [(communication_id, recovered_inbox.job_id)]
    assert "matrix-private-message" in json.dumps(case.request(target_calls[-1])["messages"], ensure_ascii=False)
    case.record("crash-window", {"stage": case.barrier_stage, "marker": marker, "source_before": asdict(outbox), "source_after": asdict(recovered_outbox), "target_after": asdict(recovered_inbox), "target_execution_outcome": outcome})


@dataclass
class FederatedCommunicationCase:
    nodes: dict[str, CommunicationNode]
    replay: ModelStreamReplay
    artifacts: Path
    planned_calls: dict[str, dict[str, str]] = field(default_factory=dict)

    async def plan(self, node: CommunicationNode, session_id: str, responses: list[dict[str, object]]) -> list[str]:
        calls, thread_id = await _plan_communication_calls(self.replay, node.client, node.workspace, session_id, responses)
        self.planned_calls.update({call: {"session_id": session_id, "thread_id": thread_id, "model_call_id": call} for call in calls})
        return calls

    def request(self, call: str) -> dict[str, object]:
        return _communication_request(self.replay, self.planned_calls, call)

    def requests(self) -> list[dict[str, object]]:
        assert {path.stem for path in self.replay.artifacts.glob("*.json")} == set(self.planned_calls)
        return [self.request(call) for call in self.planned_calls]

    def tool_result(self, session_id: str, call_id: str) -> dict[str, object]:
        return _committed_tool_result(self.replay, session_id, call_id)


@pytest.fixture
async def communication_triad(request: pytest.FixtureRequest) -> AsyncIterator[FederatedCommunicationCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime_root = context.runtime_root_for_node(request.node.nodeid)
    artifacts = context.artifacts_dir / runtime_root.parent.name / request.node.name
    artifacts.mkdir(parents=True, exist_ok=True)
    with model_stream_replay(
        artifacts, runtime_root=runtime_root,
        business_assertion="three-gateway-communication-and-resource-owner-contracts",
    ) as replay:
        async with open_communication_triad(
            context, getattr(request, "param", {}).get("prepare_workspace", _prepare_workspace),
            resource_service_nodes=getattr(request, "param", {}).get("resource_service_nodes", ()),
            shared_session_ids=getattr(request, "param", {}).get("shared_session_ids", ()),
            backend_asgi_app="tests.support.e2e_control.communication_backend_app:app",
            backend_environment={**replay.environment, "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
        ) as nodes:
            case = FederatedCommunicationCase(nodes, replay, artifacts)
            try:
                yield case
            finally:
                (artifacts / "provider-wire.json").write_text(json.dumps(case.requests(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_c3_real_reverse_channel_question_and_reply(communication_triad) -> None:
    case = communication_triad
    nodes, artifacts = case.nodes, case.artifacts
    source, target = nodes["b"], nodes["c"]
    source_session = await create_session(source.client, "C3 B source")
    target_session = await create_session(target.client, "C3 C target")
    control = case.replay.control
    control.arm_match("c3-target", "thread.runtime.acquired", {"session_id": target_session})
    first_call, = await case.plan(source, source_session, [_tool("send_message_to_session", {
        "target_session_id": session_resource_uri(target.workspace_id, target_session, gateway_authority=target.gateway_id),
        "kind": "question", "content": "c3-question",
    }, "call_c3_question")])
    question_call = first_call + ":tool-call:call_c3_question"
    control.arm(question_call, "test.tool_result.committed")
    accepted = await source.client.post(f"/api/v1/sessions/{source_session}/messages", json={
        "message": {"content": "c3-source-trigger"}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert accepted.status_code == 200, accepted.text
    try:
        await asyncio.to_thread(control.acknowledged, question_call, "test.tool_result.committed", 30)
        question_receipt = json.loads(case.tool_result(source_session, question_call)["content"])
        target_calls = await case.plan(target, target_session, [
            _tool("send_message_to_session", {
                "target_session_id": session_resource_uri(source.workspace_id, source_session, gateway_authority=source.gateway_id),
                "kind": "reply", "reply_to_communication_id": question_receipt["communication_id"],
                "content": "c3-reply-from-C",
            }, "call_c3_reply"), {"content": "c3-C-completed"},
        ])
        source_calls = await case.plan(source, source_session, [
            _tool("wait_for_session", {"target_session_id": target_session,
                  "communication_id": question_receipt["communication_id"], "timeout_seconds": 30}, "call_c3_wait"),
            {"content": "c3-B-source-completed"}, {"content": "c3-B-reply-seen"},
        ])
    finally:
        control_request(json.loads(control.environment["BOXTEAM_E2E_TEST_CONTROL"]), {"action": "release_match", "barrier_id": "c3-target"})
        control.release(question_call, "test.tool_result.committed")
    source_job = await _completed(source.client, accepted.json()["data"]["job_id"])
    results = {
        "question": question_receipt,
        "reply": json.loads(case.tool_result(target_session, target_calls[0] + ":tool-call:call_c3_reply")["content"]),
        "wait": json.loads(case.tool_result(source_session, source_calls[0] + ":tool-call:call_c3_wait")["content"]),
    }
    target_wire = json.dumps(case.request(target_calls[0])["messages"], ensure_ascii=False)
    assert "c3-question" in target_wire and question_receipt["communication_id"] in target_wire
    assert results["wait"]["status"] == "completed", results
    source_node = await source.session_node(source_session)
    target_node = await target.session_node(target_session)
    with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as source_store, closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as target_store:
        question = target_store.get_communication_inbox(results["question"]["communication_id"])
        answer = source_store.get_communication_inbox(results["reply"]["communication_id"])
        assert (question.source_gateway_id, question.source_workspace_id, question.source_session_id) == (source.gateway_id, source.workspace_id, source_session)
        assert question.target_gateway_id == target.gateway_id and question.target_workspace_id == target.workspace_id
        assert (answer.source_gateway_id, answer.source_workspace_id, answer.source_session_id) == (target.gateway_id, target.workspace_id, target_session)
        assert answer.reply_to_communication_id == question.communication_id
        assert answer.state == question.state == "execution_bound"
        reply_job = await _completed(source.client, answer.job_id)
        target_job = await _completed(target.client, question.job_id)
        channels = {name: node.channels() for name, node in nodes.items()}
        assert source.channels() == [(nodes["a"].gateway_id, "inbound")]
        assert target.channels() == [(nodes["a"].gateway_id, "inbound")]
        (artifacts / "c3-actual.json").write_text(json.dumps({
            "question": asdict(question), "reply": asdict(answer), "tools": results, "channels": channels,
            "source_job": source_job, "target_job": target_job, "reply_job": reply_job,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    hub_catalog = await nodes["a"].client.get("/api/v1/session-catalog/export")
    assert hub_catalog.status_code == 200 and hub_catalog.json()["data"]["items"] == []
