"""真实 Gateway/Workspace HTTP 与受控 Provider 的通信核心集成验收。"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

import commentjson
import httpx
import pytest

from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.stubs.http_stubs import HTTPStubState, openai_chat_stub
from tests.support.api_helpers import create_session
from tests.support.communication_federation import (
    CommunicationNode,
    open_communication_triad,
)
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import (
    BackendProcess,
    close_backend_process,
    start_backend_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@dataclass
class CommunicationCase:
    client: httpx.AsyncClient
    model: HTTPStubState
    workspace: Path
    artifacts: Path
    backend: BackendProcess
    gateway: GatewayProcess
    gateway_id: str
    barrier_stage: str | None = None

    def record(self, name: str, value: object) -> None:
        (self.artifacts / (name + ".json")).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        )

    async def session_node(self, session_id: str) -> Path:
        response = await self.client.get("/api/v1/session-catalog/export")
        assert response.status_code == 200, response.text
        node = next(node for node in response.json()["data"]["items"] if node["node_id"] == session_id)
        return self.workspace / ".boxteam/sessions" / node["storage_relative_path"]

    async def hard_restart(self) -> None:
        previous = self.backend
        previous.process.kill()
        assert await asyncio.to_thread(previous.process.wait, timeout=10) < 0
        await asyncio.to_thread(close_backend_process, previous)
        self.backend = await asyncio.to_thread(
            start_backend_process, workspace_root=str(self.workspace), port=previous.port,
            log_name="communication-restarted", env_overrides={"BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{self.gateway.port}"},
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
    config = commentjson.loads(config_path.read_text())
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
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
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
    with openai_chat_stub(ports.port(10)) as model:
        backend = await asyncio.to_thread(
            start_backend_process, workspace_root=str(workspace), port=ports.port(0),
            log_name="communication-backend", env_overrides=environment,
            asgi_app="tests.support.communication_fault_backend:app" if barrier or trace else "app.main:app",
        )
        gateway = None
        case = None
        try:
            gateway = await asyncio.to_thread(
                start_gateway_process, workspace_root=workspace, port=ports.port(1),
                default_backend_url=f"http://127.0.0.1:{backend.port}",
            )
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=90,
            ) as client:
                await acquire_gateway_guest(client)
                health = await client.get("/api/gateway/health")
                assert health.status_code == 200, health.text
                case = CommunicationCase(client, model, workspace, artifacts, backend, gateway, health.json()["data"]["gateway_id"], environment.get("BOXTEAM_TEST_COMMUNICATION_BARRIER_STAGE"))
                try:
                    yield case
                finally:
                    case.record("provider-wire", model.requests)
        finally:
            if gateway is not None:
                await asyncio.to_thread(close_gateway_process, gateway)
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
    results: dict[str, object] = {}

    def reply(payload: dict[str, object]) -> dict[str, object]:
        messages = payload["messages"]
        human = next((str(m["content"]) for m in reversed(messages) if m["role"] == "user" and m["content"]), "")
        if "<session_message" in human and "c1-private-message" in human:
            return {"content": "c1-target-result"}
        if "c1-source-trigger" not in human:
            return {"content": "通信验证会话"}
        tools = {m.get("name"): json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        if "send_message_to_session" not in tools:
            return _tool("send_message_to_session", {
                "target_session_id": target_session, "content": "c1-private-message", "kind": "result",
            }, "call_c1_send")
        results["receipt"] = tools["send_message_to_session"]
        if "wait_for_session" not in tools:
            return _tool("wait_for_session", {
                "target_session_id": target_session, "communication_id": tools["send_message_to_session"]["communication_id"],
                "timeout_seconds": 30,
            }, "call_c1_wait")
        results["wait"] = tools["wait_for_session"]
        if "invoke_extension_tool" not in tools:
            return _tool("invoke_extension_tool", {"tool_name": "read_context", "arguments": {
                "resource_identity": str(session_resource_identity(case.backend.workspace_id, target_session)),
                "uri": session_resource_uri(case.backend.workspace_id, target_session), "scope": "workspace", "view": "messages",
            }}, "call_c1_read")
        results["read"] = tools["invoke_extension_tool"]
        return {"content": "c1-source-completed"}

    case.model.response_callback = reply
    response = await case.client.post(f"/api/v1/sessions/{source_session}/messages", json={
        "message": {"content": "c1-source-trigger，发送后等待并冷读目标。"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    source_job = await _completed(case.client, response.json()["data"]["job_id"])
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
    database = target_node / "rollout/index.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT execution_origin,turn_id FROM executions").fetchall() == [("internal", None)]
        assert connection.execute("SELECT COUNT(*) FROM turn_records").fetchone()[0] == 0
        assert connection.execute("SELECT source_id,job_id FROM runtime_execution_wakeups").fetchall() == [(receipt["communication_id"], inbox.job_id)]


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
    database = node / "rollout/index.sqlite"
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
    results: dict[str, object] = {}

    def reply(payload: dict[str, object]) -> dict[str, object]:
        messages = payload["messages"]
        human = next((str(m["content"]) for m in reversed(messages) if m["role"] == "user" and m["content"]), "")
        if "<session_message" in human and "c2-private-message" in human:
            return {"content": "c2-target-recovered"}
        tools = {m.get("name"): json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        if "c2-source-trigger" in human:
            if "send_message_to_session" not in tools:
                return _tool("send_message_to_session", {"target_session_id": target_session, "content": "c2-private-message"}, "call_c2_send")
            results["receipt"] = tools["send_message_to_session"]
            return {"content": "c2-source-accepted"}
        if "c2-resume-trigger" in human:
            if "wait_for_session" not in tools:
                return _tool("wait_for_session", {"target_session_id": target_session, "communication_id": results["receipt"]["communication_id"], "timeout_seconds": 20}, "call_c2_wait")
            results["wait"] = tools["wait_for_session"]
            return {"content": "c2-observation-completed"}
        return {"content": "C2 验证会话"}

    case.model.response_callback = reply
    response = await case.client.post(f"/api/v1/sessions/{source_session}/messages", json={
        "message": {"content": "c2-source-trigger"}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    await _completed(case.client, response.json()["data"]["job_id"])
    async with asyncio.timeout(20):
        while not (case.artifacts / "before-source.json").exists():
            await asyncio.sleep(0.02)
    barrier = json.loads((case.artifacts / "before-source.json").read_text())
    assert barrier["state"] == "target_accepted" and barrier["job_id"] is None and barrier["execution_id"] is None
    assert barrier["communication_id"] == results["receipt"]["communication_id"]
    target_node = await case.session_node(target_session)
    with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
        claimed = store.get_communication_inbox(barrier["communication_id"])
        assert claimed.state == "target_accepted" and claimed.admission_claim_generation is not None
    before = target_node / "rollout/index.sqlite"
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
    resumed = await case.client.post(f"/api/v1/sessions/{source_session}/messages", json={
        "message": {"content": "c2-resume-trigger"}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert resumed.status_code == 200, resumed.text
    await _completed(case.client, resumed.json()["data"]["job_id"])
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
    tools: dict[str, object] = {}

    def reply(payload: dict[str, object]) -> dict[str, object]:
        messages = payload["messages"]
        human = next((str(m["content"]) for m in reversed(messages) if m["role"] == "user" and m["content"]), "")
        if "<session_message" in human and "matrix-private-message" in human:
            return {"content": "matrix-target-completed"}
        if "matrix-source-trigger" not in human:
            return {"content": "通信崩溃矩阵"}
        results = {m.get("name"): json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        if "send_message_to_session" not in results:
            return _tool("send_message_to_session", {"target_session_id": target_session, "content": "matrix-private-message"}, "call_matrix_send")
        tools["receipt"] = results["send_message_to_session"]
        return {"content": "matrix-source-completed"}

    case.model.response_callback = reply
    response = await case.client.post(f"/api/v1/sessions/{source_session}/messages", json={
        "message": {"content": "matrix-source-trigger"}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    marker_path = case.artifacts / ("barrier-" + case.barrier_stage + ".json")
    async with asyncio.timeout(40):
        while not marker_path.exists():
            await asyncio.sleep(0.02)
    marker = json.loads(marker_path.read_text())
    communication_id = marker["communication_id"]
    if case.barrier_stage == "after_receipt":
        target_marker_path = case.artifacts / "before-source.json"
        async with asyncio.timeout(40):
            while not target_marker_path.exists():
                await asyncio.sleep(0.02)
        target_marker = json.loads(target_marker_path.read_text())
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
            # 全新 target 的 acceptance 不初始化运行时；数据库应由恢复 admission 创建。
            assert not (target_node / "rollout/index.sqlite").exists()
        else:
            assert accepted.state == "target_accepted" and accepted.job_id is None
            await _completed(case.client, response.json()["data"]["job_id"])
            database = target_node / "rollout/index.sqlite"
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
    database = target_node / "rollout/index.sqlite"
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
    with sqlite3.connect((target_node / "rollout/index.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT execution_id,execution_origin,turn_id FROM executions").fetchall() == [(recovered_inbox.execution_id, "internal", None)]
        assert connection.execute("SELECT COUNT(*) FROM turn_records").fetchone()[0] == 0
        assert connection.execute("SELECT source_id,job_id FROM runtime_execution_wakeups").fetchall() == [(communication_id, recovered_inbox.job_id)]
    case.record("crash-window", {"stage": case.barrier_stage, "marker": marker, "source_before": asdict(outbox), "source_after": asdict(recovered_outbox), "target_after": asdict(recovered_inbox), "target_execution_outcome": outcome})


@pytest.fixture
async def communication_triad(request: pytest.FixtureRequest) -> AsyncIterator[tuple[dict[str, CommunicationNode], HTTPStubState, Path]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir / request.node.name
    artifacts.mkdir(parents=True, exist_ok=True)
    ports = integration_port_block_for_file(Path(request.node.path))
    with openai_chat_stub(ports.port(10)) as model:
        async with open_communication_triad(
            context, _prepare_workspace,
            resource_service_nodes=getattr(request, "param", {}).get("resource_service_nodes", ()),
            shared_session_ids=getattr(request, "param", {}).get("shared_session_ids", ()),
        ) as nodes:
            try:
                yield nodes, model, artifacts
            finally:
                (artifacts / "provider-wire.json").write_text(json.dumps(model.requests, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
async def test_c3_real_reverse_channel_question_and_reply(communication_triad) -> None:
    nodes, model, artifacts = communication_triad
    source, target = nodes["b"], nodes["c"]
    source_session = await create_session(source.client, "C3 B source")
    target_session = await create_session(target.client, "C3 C target")
    results: dict[str, object] = {}

    def reply(payload: dict[str, object]) -> dict[str, object]:
        messages = payload["messages"]
        human = next((str(m["content"]) for m in reversed(messages) if m["role"] == "user" and m["content"]), "")
        tools = {m.get("name"): json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        # reply 可能在 wait 后注入同一次请求，先保存已经完成的工具结果。
        if "send_message_to_session" in tools:
            receipt = tools["send_message_to_session"]
            results[receipt["kind"]] = receipt
        if "wait_for_session" in tools:
            results["wait"] = tools["wait_for_session"]
        if "<session_message" in human and "c3-question" in human:
            if "send_message_to_session" not in tools:
                correlation = re.search(r'"communication_id"\s*:\s*"([^"]+)"', human)
                assert correlation is not None, human
                return _tool("send_message_to_session", {
                    "target_session_id": session_resource_uri(source.workspace_id, source_session, gateway_authority=source.gateway_id), "kind": "reply",
                    "reply_to_communication_id": correlation[1], "content": "c3-reply-from-C",
                }, "call_c3_reply")
            return {"content": "c3-C-completed"}
        if "<session_message" in human and "c3-reply-from-C" in human:
            return {"content": "c3-B-reply-seen"}
        if "c3-source-trigger" in human:
            if "send_message_to_session" not in tools:
                return _tool("send_message_to_session", {
                    "target_session_id": session_resource_uri(target.workspace_id, target_session, gateway_authority=target.gateway_id), "kind": "question", "content": "c3-question",
                }, "call_c3_question")
            if "wait_for_session" not in tools:
                return _tool("wait_for_session", {"target_session_id": target_session, "communication_id": results["question"]["communication_id"], "timeout_seconds": 30}, "call_c3_wait")
            return {"content": "c3-B-source-completed"}
        return {"content": "C3 通信验证会话"}

    model.response_callback = reply
    accepted = await source.client.post(f"/api/v1/sessions/{source_session}/messages", json={
        "message": {"content": "c3-source-trigger"}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert accepted.status_code == 200, accepted.text
    source_job = await _completed(source.client, accepted.json()["data"]["job_id"])
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
        }, ensure_ascii=False, indent=2) + "\n")
    hub_catalog = await nodes["a"].client.get("/api/v1/session-catalog/export")
    assert hub_catalog.status_code == 200 and hub_catalog.json()["data"]["items"] == []
