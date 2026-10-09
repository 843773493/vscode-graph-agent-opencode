"""真实 HTTP、Job、Provider 与 runtime source/child publication 的核心集成验收。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import commentjson
import httpx
import pytest

from app import main
from app.container import AppContainer
from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore, ThreadExecutionIntent
from app.core.session_thread_context import session_thread_owner_scope
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.hashing import canonical_json_bytes, content_hash, sha256_jcs
from app.domain.itemized.records import CanonicalItemRecord, RuntimeNoticePayload
from app.prompting import PromptSection, internal_message_factory
from app.services.business.message_display import project_message_for_display
from app.services.business.system_reminder_checkpoint_service import (
    persist_interrupt_checkpoint,
    submit_checkpoint_reminder,
)
from app.services.infrastructure.mcp.catalog_activation import MCP_GUIDANCE_SOURCE_ID
from app.services.infrastructure.resource_platform.virtual_resources import (
    workspace_agent_spec_display_uri,
)
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_relative_path,
)
from tests.e2e.system.workspace_services.terminal.terminal_process_helpers import (
    start_terminal_backend,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.stubs.http_stubs import HTTPStubState, openai_chat_stub
from tests.support.api_helpers import create_session
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import (
    BackendProcess,
    close_backend_process,
    start_backend_process,
    terminate_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@dataclass
class RuntimeSourceCase:
    client: httpx.AsyncClient
    container: AppContainer
    model: HTTPStubState
    context: TestRunContext
    artifacts_dir: Path

    def write_actual(self, name: str, value: dict[str, object]) -> None:
        (self.artifacts_dir / f"{name}-actual.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        )

    def sql(self, session_id: str, thread_id: str, query: str, params: tuple = ()) -> list[tuple]:
        root = self.container.checkpointer._storage.storage.root(session_id, thread_id=thread_id)
        with sqlite3.connect(root / "index.sqlite") as connection:
            return connection.execute(query, params).fetchall()


def _prepare_source_workspace(
    context: TestRunContext, port: int, *, delegated_report: bool = False,
    workspace_template: str = "default_test_workspace",
) -> Path:
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces" / workspace_template,
    )
    config_path = install_test_workspace_config(
        workspace_root=workspace,
        config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
        schema_path=Path.cwd() / "configs/workspace_schema.jsonc",
    )
    config = commentjson.loads(config_path.read_text())
    for provider in config["llm"]["providers"]:
        provider.update({
            "endpoint": f"http://127.0.0.1:{port}/v1", "model": "e2e-stub-model",
            "api_key": "e2e-local-model-key", "custom_llm_provider": "openai",
            "api_mode": {"protocol": "chat_completions", "model_info": {
                "supports_function_calling": True, "supports_reasoning": True,
            }, "supports_reasoning": {"reasoning_content": True}},
        })
        provider.pop("auth", None)
        provider.pop("request_options", None)
    config.setdefault("runtime", {}).setdefault("agent", {}).setdefault("run", {})["mode"] = "team"
    for agent in config["agents"].values():
        agent["model"]["fallback_providers"] = []
        agent.setdefault("execution", {})["require_delegated_report"] = delegated_report
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    return workspace


@pytest.fixture
async def source_case(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[RuntimeSourceCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    port = integration_port_block_for_file(Path(request.node.path)).port(10)
    options = getattr(request, "param", {})
    workspace = _prepare_source_workspace(
        context, port, delegated_report=options.get("delegated_report", False),
        workspace_template=options.get("workspace_template", "default_test_workspace"),
    )
    workspace_id = load_or_create_workspace_id(workspace)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("BOXTEAM_HOME", str(context.boxteam_home_for_node(request.node.nodeid)))
    mount_payload = {
        "workspace_id": workspace_id, "workspace_root": str(workspace), "terminal_backend_url": None, "browser_backend_url": None, "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None}
    with ExitStack() as services, openai_chat_stub(port) as model:
        if options.get("terminal"):
            terminal_port = integration_port_block_for_file(Path(request.node.path)).port(40)
            mount_payload["terminal_backend_url"] = f"http://127.0.0.1:{terminal_port}"
            process = start_terminal_backend(backend_port=terminal_port, workspace_root=str(workspace))
            services.callback(terminate_process, process)
        monkeypatch.setenv("BOXTEAM_MOUNTED_WORKSPACES", json.dumps([mount_payload]))
        async with main.lifespan(main.app):
            registry = main.app.state.workspace_container_registry
            container = registry.get(workspace_id)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://workspace.test",
                headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": workspace_id},
                timeout=30,
            ) as client:
                runtime_root = context.runtime_root_for_node(request.node.nodeid)
                artifacts_dir = context.artifacts_dir / runtime_root.parent.name / runtime_root.name
                artifacts_dir.mkdir(parents=True, exist_ok=True)
                case = RuntimeSourceCase(client, container, model, context, artifacts_dir)
                try:
                    yield case
                finally:
                    case.write_actual(request.node.name + "-wire", {"requests": model.requests})


async def _wait_until(predicate: Callable[[], bool], seconds: float = 30) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.05)


async def _job(case: RuntimeSourceCase | RestartRuntimeSourceCase, job_id: str, expected: str = "completed") -> dict[str, object]:
    async with asyncio.timeout(45):
        while True:
            response = await case.client.get(f"/api/v1/jobs/{job_id}")
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            if data["status"] in {"completed", "failed", "cancelled", "timed_out", "execution_lost"}:
                if data["status"] != expected:
                    case.write_actual(f"job-failure-{job_id}", {"job": data, "expected": expected})
                assert data["status"] == expected, data.get("error_message") or data
                return data
            await asyncio.sleep(0.05)


async def _send(case: RuntimeSourceCase, session_id: str, content: str) -> dict[str, object]:
    response = await case.client.post(f"/api/v1/sessions/{session_id}/messages", json={
        "message": {"content": content}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    return response.json()["data"]


def _wire_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _tool_reply(name: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
    return {"tool_calls": [{
        "index": 0, "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }]}


async def _team_child(case: RuntimeSourceCase) -> tuple[dict[str, object], dict[str, object], ThreadExecutionIntent]:
    child_started = False

    def model_reply(payload: dict[str, object]) -> dict[str, object]:
        nonlocal child_started
        messages = payload["messages"]
        assert isinstance(messages, list)
        human = "\n".join(_wire_text(m["content"]) for m in messages
                          if m["role"] == "user" and m["content"])
        if "<generated_session_result" in human:
            return {"content": "parent-result-ack-h1"}
        if "<delegated_task" in human and "private-seed-h1" in human:
            child_started = True
            return {"content": "child-result-h1"}
        if "H1-team-core" not in human:
            return {"content": "核心验证会话"}
        tools = [m for m in messages if m["role"] == "tool"]
        names = {m.get("name") for m in tools}
        if "create_team" not in names:
            return _tool_reply("create_team", {"name": "H1 同会话团队"}, "call_h1_team")
        team_result = json.loads(next(m["content"] for m in tools if m.get("name") == "create_team"))
        if "create_team_member" not in names:
            return _tool_reply("create_team_member", {
                "team_id": team_result["team_id"], "role": "reviewer",
                "startup_prompt": "private-seed-h1，独立确认成员运行，返回 child-result-h1。",
            }, "call_h1_member")
        return {"content": "parent-dispatched-h1"}

    case.model.response_callback = model_reply
    session_id = await create_session(case.client, "H1-team-core")
    accepted = await _send(case, session_id, "H1-team-core：创建团队并启动一个成员。")
    parent_job = await _job(case, str(accepted["job_id"]))
    await _wait_until(lambda: child_started)
    factory = case.container.thread_initial_execution_service._factory
    control = factory.for_owner_session(session_id).control_store
    intents = control.list_initial_execution_intents()
    assert len(intents) == 1
    intent = intents[0]
    assert intent.session_id == session_id and intent.state == "bound"
    child_job = await _job(case, intent.job_id)
    await _wait_until(lambda: case.sql(
        session_id, parent_job["thread_id"],
        "SELECT COUNT(*) FROM executions WHERE execution_origin='internal' AND outcome='completed'",
    )[0][0] == 1)
    return parent_job, child_job, intent


def _assert_generated_notice(
    case: RuntimeSourceCase, session_id: str, thread_id: str, item_id: str,
    *, expected_provenance: dict[str, str], private_result: str,
) -> dict[str, object]:
    storage = case.container.checkpointer._storage
    (notice,) = storage.storage.read_items(session_id, thread_id, item_ids=(item_id,))
    payload = RuntimeNoticePayload.from_dict(notice.payload)
    assert notice.semantic_kind == "runtime_notice" and notice.payload_kind == "structured_content"
    assert notice.turn_id is None and notice.turn_scope == "pending_next_turn"
    assert payload.display_part.provenance.to_dict() == expected_provenance
    assert private_result in payload.prompt
    assert private_result not in canonical_json_bytes(payload.display_part.to_dict()).decode()
    assert private_result not in canonical_json_bytes(notice.extensions).decode()
    assert notice.content_hash == content_hash(notice.payload_kind, notice.payload)
    annotations = notice.extensions["boxteam.checkpoint.annotations"]["value"]
    assert set(annotations) == {"structured_prompt_kind", "structured_prompt_schema_version", "boxteam_session_provider_id"}
    # 从权威持久 item 构造全新 codec 冷投影，再核对封存 selection 和 hash。
    assemblies = storage.assemblies.list_context_assemblies(session_id, thread_id)
    report_assemblies = tuple(a for a in assemblies if a.execution_id == notice.producer_ref["invocation_id"])
    assert report_assemblies
    assert any(entry.included and entry.content_hash == notice.content_hash
               for assembly in report_assemblies for entry in assembly.selection)
    sealed_before = canonical_json_bytes([assembly.to_dict() for assembly in report_assemblies])
    cold_item = CanonicalItemRecord.from_dict(json.loads(canonical_json_bytes(notice.to_dict())))
    codec = LangChainMessageCodec()
    projected = codec.from_dict(codec.project_message((cold_item,)))
    assert projected.content == payload.prompt
    assert projected.response_metadata["internal_display_provenance"] == expected_provenance
    assert private_result not in canonical_json_bytes(projected.response_metadata).decode()
    display = project_message_for_display(projected.content, projected.response_metadata)
    assert display.content == payload.display_part.text
    assert private_result not in canonical_json_bytes(display.metadata).decode()
    assert canonical_json_bytes([storage.assemblies.get_context_assembly(session_id, thread_id, assembly_id=a.assembly_id).to_dict()
                                 for a in report_assemblies]) == sealed_before
    return {"canonical": cold_item.to_dict(), "cold_projection_metadata": projected.response_metadata,
            "sealed_assemblies_hash": sha256_jcs(json.loads(sealed_before))}


@pytest.mark.asyncio
async def test_h1_http_team_child_seed_binder_and_exact_parent_report(source_case: RuntimeSourceCase) -> None:
    case = source_case
    parent_job, child_job, intent = await _team_child(case)
    session_id = parent_job["session_id"]
    child_executions = case.sql(session_id, intent.thread_id,
        "SELECT execution_id,execution_origin,turn_id,outcome FROM executions")
    assert child_executions == [(intent.execution_binding_id, "internal", None, "completed")]
    assert case.sql(session_id, intent.thread_id, "SELECT COUNT(*) FROM turn_records")[0][0] == 0
    assert case.sql(session_id, intent.thread_id,
        "SELECT COUNT(*) FROM item_catalog WHERE semantic_kind='runtime_notice'")[0][0] == 0
    request = case.container.checkpointer._storage.executions.internal_execution_request_input(
        session_id, intent.thread_id, intent.execution_binding_id,
    )
    assert request["kind"] == "seed_reference"
    board_list = await case.container.team_service.list_teams(
        requester_session_id=session_id, requester_thread_id=parent_job["thread_id"],
    )
    board = board_list.items[0]
    assert board.session_id == session_id
    assert {member.thread_id for member in board.members} == {parent_job["thread_id"], intent.thread_id}
    assert all(member.status == "active" for member in board.members)
    control = case.container.thread_initial_execution_service._factory.for_owner_session(session_id).control_store
    persisted_boards = control.collaboration_boards(board.team_id)
    assert len(persisted_boards) == 1
    assert json.loads(persisted_boards[0])["members"] == []
    assert not (control.database_path.parent / "teams").exists()
    seed_wire = [r["json"] for r in case.model.requests if any(
        m["role"] == "user" and "<delegated_task" in _wire_text(m["content"])
        and "private-seed-h1" in _wire_text(m["content"])
        for m in r["json"]["messages"]
    )]
    assert seed_wire
    parent_inputs = case.sql(session_id, parent_job["thread_id"],
        "SELECT item_id FROM item_catalog WHERE semantic_kind='runtime_notice'")
    assert len(parent_inputs) == 1
    child_outputs = case.sql(session_id, intent.thread_id,
        "SELECT item_id FROM item_catalog WHERE semantic_kind='assistant_output' AND execution_id=? ORDER BY item_sequence",
        (intent.execution_binding_id,))
    record = case.container.thread_initial_execution_service._factory.for_owner_session(session_id).control_store.get_thread_creation_record(intent.creation_idempotency_key)
    canonical_report = _assert_generated_notice(
        case, session_id, parent_job["thread_id"], parent_inputs[0][0],
        expected_provenance={"source_session_id": session_id, "source_thread_id": intent.thread_id,
            "delegation_id": record.delegation_id, "source_execution_id": intent.execution_binding_id,
            "source_item_id": child_outputs[-1][0]}, private_result="child-result-h1",
    )
    case.write_actual("h1", {"parent_job": parent_job, "child_job": child_job,
        "request_input": request, "child_executions": child_executions, "team": board.model_dump(mode="json"),
        "parent_runtime_inputs": parent_inputs, "canonical_report": canonical_report,
        "model_request_count": len(case.model.requests)})


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", [{"terminal": True}], indirect=True)
async def test_h2_goal_terminal_and_interrupt_sources_keep_real_execution(source_case: RuntimeSourceCase) -> None:
    case = source_case
    started, release = Event(), Event()

    def reply(payload: dict[str, object]) -> dict[str, object]:
        messages = payload["messages"]
        human = next((_wire_text(m["content"]) for m in reversed(messages)
                      if m["role"] == "user" and m["content"]), "")
        tools = [m for m in messages if m["role"] == "tool"]
        if "H2-interrupt" in human:
            started.set()
            assert release.wait(20), "测试没有解除外部模型等待"
            return {"content": "中断后不可采纳的回复"}
        if "后台终端命令已经结束" in human:
            if not any(m.get("name") == "write_stdin" for m in tools):
                result = json.loads(next(m["content"] for m in reversed(tools) if m.get("name") == "exec_command"))
                return _tool_reply("write_stdin", {"session_id": result["session_id"], "chars": "", "yield_time_ms": 1000}, "call_h2_stdin")
            return {"content": "H2-terminal-result-ack"}
        if "H2-terminal" in human:
            if not any(m.get("name") == "exec_command" for m in tools):
                return _tool_reply("exec_command", {"cmd": "sleep 3; printf 'H2-terminal-completed\\n'", "yield_time_ms": 1000}, "call_h2_terminal")
            return {"content": "H2-terminal-backgrounded"}
        if "H2-goal" in human:
            if not any(m.get("name") == "update_goal" for m in tools):
                return _tool_reply("update_goal", {"status": "complete"}, "call_h2_goal")
            return {"content": "H2-goal-completed"}
        return {"content": "H2 普通回复"}

    case.model.response_callback = reply
    session_id = await create_session(case.client, "H2-source-core")
    thread_id = await case.container.session_service.resolve_main_thread(session_id)
    goal_response = await case.client.put(f"/api/v1/sessions/{session_id}/goal", json={"objective": "H2-goal：完成目标并调用 update_goal。"})
    assert goal_response.status_code == 200, goal_response.text
    await _wait_until(lambda: bool(case.sql(session_id, thread_id, "SELECT job_id FROM executions")))
    goal_job_id = case.sql(session_id, thread_id, "SELECT job_id FROM executions")[0][0]
    goal_job = await _job(case, goal_job_id)
    tracker = case.container.agent_execution_service._residency_tracker
    source_generation = tracker.snapshot(session_id, thread_id).generation
    assert case.sql(session_id, thread_id, "SELECT execution_origin,turn_id FROM executions") == [("internal", None)]
    assert (await case.client.get(f"/api/v1/sessions/{session_id}/goal")).json()["data"]["status"] == "complete"

    terminal_accepted = await _send(case, session_id, "H2-terminal：执行后台命令并读取最终输出。")
    terminal_job = await _job(case, terminal_accepted["job_id"])
    await _wait_until(lambda: case.sql(session_id, thread_id,
        "SELECT COUNT(*) FROM executions WHERE execution_origin='internal'")[0][0] == 2, seconds=45)
    terminal_completion_job_id = case.sql(session_id, thread_id,
        "SELECT e.job_id FROM runtime_execution_wakeups w JOIN executions e ON e.execution_id=w.execution_id WHERE w.source_id LIKE 'terminal:%'")[0][0]
    terminal_completion_job = await _job(case, terminal_completion_job_id)
    assert tracker.snapshot(session_id, thread_id).generation == source_generation
    before_repeat = case.sql(session_id, thread_id, "SELECT source_id,execution_id FROM runtime_execution_wakeups ORDER BY source_id")
    await case.container.terminal_steering_service.scan_once()
    await case.container.terminal_steering_service.scan_once()
    assert case.sql(session_id, thread_id, "SELECT source_id,execution_id FROM runtime_execution_wakeups ORDER BY source_id") == before_repeat
    assert sum(source.startswith("terminal:") for source, _execution in before_repeat) == 1

    accepted = await _send(case, session_id, "H2-interrupt：等待用户中断。")
    try:
        await _wait_until(started.is_set)
        admission = case.container.checkpointer.execution_admission_for_job(session_id, thread_id=thread_id, job_id=accepted["job_id"])
        for _attempt in range(2):
            submit_checkpoint_reminder(checkpointer=case.container.checkpointer,
                session_id=session_id, thread_id=thread_id, reminder="H2 同一真实执行事件提醒",
                response_metadata={"source": "h2-boundary"}, checkpoint_source="h2-boundary",
                event_identity="h2-stable-event", execution_id=admission.execution_id)
        interrupted = await case.client.post(f"/api/v1/sessions/{session_id}/interrupt")
        assert interrupted.status_code == 200, interrupted.text
    finally:
        release.set()
    cancelled_job = await _job(case, accepted["job_id"], "cancelled")
    notices = case.sql(session_id, thread_id, "SELECT item_id,execution_id FROM item_catalog WHERE semantic_kind='runtime_notice' AND execution_id=?", (admission.execution_id,))
    assert len(notices) == 2 and all(row[1] == admission.execution_id for row in notices)
    assert sum("h2-stable-event" in row[0] for row in notices) == 1
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM executions")[0][0] == 4
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM turn_records")[0][0] == 2
    assert len(await case.container.job_service.list(session_id=session_id)) == 4
    case.write_actual("h2", {"goal_job": goal_job, "terminal_job": terminal_job,
        "terminal_completion_job": terminal_completion_job,
        "source_runtime_generation": source_generation,
        "cancelled_job": cancelled_job, "wakeup_identities": before_repeat,
        "pending_reminders": notices, "execution_id": admission.execution_id})


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", [{"delegated_report": True}], indirect=True)
@pytest.mark.parametrize("retry_kind", ["empty", "custom", "delegated", "question"])
async def test_h3_four_retries_share_one_real_execution(source_case: RuntimeSourceCase, retry_kind: str) -> None:
    case = source_case
    calls = 0

    def reply(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if retry_kind == "empty" and calls <= 2:
            return {"content": ""}
        if retry_kind == "custom" and calls == 3:
            return _tool_reply("invoke_extension_tool", {"tool_name": "test_tool_2", "arguments": {}}, "call_h3_custom")
        return {"content": f"H3-{retry_kind}-final"}

    case.model.response_callback = reply
    session_id = await create_session(case.client, f"H3-{retry_kind}")
    thread_id = await case.container.session_service.resolve_main_thread(session_id)
    if retry_kind in {"empty", "custom"}:
        accepted = await _send(case, session_id, "H3 调用 test_tool_2" if retry_kind == "custom" else "H3 空响应恢复")
        job_id = accepted["job_id"]
    else:
        sender_session_id = await create_session(case.client, f"H3-{retry_kind}-sender")
        if retry_kind == "delegated":
            prepared = internal_message_factory.build(kind="delegated_task", control="H3 委派报告恢复",
                sections=[PromptSection("control_context", {"parent_session_id": sender_session_id}),
                          PromptSection("delegated_task", "H3 返回普通文本，验证真实报告限次失败")],
                metadata={"source": "session_subagent_delegation", "parent_session_id": sender_session_id},
                display_content="H3 委派任务")
        else:
            source_address = {"gateway_id": "gw-h3", "workspace_id": case.container.session_service.workspace_id, "session_id": sender_session_id, "thread_id": await case.container.session_service.resolve_main_thread(sender_session_id)}
            prepared = internal_message_factory.build(kind="session_message", control="H3 问题回复恢复",
                sections=[PromptSection("control_context", {"source_address": source_address, "communication_id": "h3-question"}),
                          PromptSection("session_message", "H3 返回普通文本，验证真实定向回复限次失败")],
                metadata={"source": "send_message_to_session", "kind": "question", "reply_required": True,
                          "source_address": source_address, "communication_id": "h3-question"})
        accepted_internal = await case.container.session_orchestrator.submit_runtime_event(
            session_id, prepared, thread_id=thread_id, source_id=f"h3:{retry_kind}", idempotency_key=f"h3:{retry_kind}")
        job_id = accepted_internal.job_id
    result = await _job(case, job_id, "failed" if retry_kind in {"delegated", "question"} else "completed")
    executions = case.sql(session_id, thread_id, "SELECT execution_id,execution_origin,turn_id,outcome FROM executions")
    assert len(executions) == 1
    execution_id, origin, turn_id, outcome = executions[0]
    expected_outcome = "failed" if retry_kind in {"delegated", "question"} else "completed"
    assert outcome == expected_outcome
    assert origin == ("internal" if retry_kind in {"delegated", "question"} else "user_turn")
    assert (turn_id is None) == (origin == "internal")
    assert len(await case.container.job_service.list(session_id=session_id)) == 1
    assert calls == (4 if retry_kind == "custom" else 3)
    retry_items = case.sql(session_id, thread_id,
        "SELECT item_id,execution_id FROM item_catalog WHERE semantic_kind='runtime_notice' AND item_id LIKE '%retry:%' ORDER BY item_sequence")
    assert len(retry_items) == 2 and all(row[1] == execution_id for row in retry_items)
    model_calls = case.sql(session_id, thread_id, "SELECT execution_id,attempt,attempt_ordinal,outcome FROM model_calls ORDER BY created_at")
    assert len(model_calls) == calls and all(row[0] == execution_id for row in model_calls)
    assert model_calls[-1][3] == expected_outcome
    assemblies = case.sql(session_id, thread_id,
        "SELECT a.assembly_id,a.status,a.outcome FROM context_assemblies a "
        "JOIN model_calls m ON m.model_call_id=a.model_call_id ORDER BY m.created_at")
    assert len(assemblies) == calls and all(row[1] == "terminal" for row in assemblies)
    assert assemblies[-1][2] == expected_outcome
    source_kind = {"empty": "empty_response_retry", "custom": "missing_custom_tool_retry",
                   "delegated": "delegated_report_retry", "question": "session_question_reply_retry"}[retry_kind]
    expected_ids = {f"retry:{job_id}:{source_kind}:{attempt}" for attempt in (1, 2)}
    assert {row[0] for row in retry_items} == {f"item-{value.removeprefix('retry:')}" for value in expected_ids}
    saver = case.container.checkpointer
    frozen_items = [value for value in saver._storage.storage.read_items(session_id, thread_id)
                    if value.item_id in {row[0] for row in retry_items}]
    assert {value.producer_ref["producer_id"] for value in frozen_items} == expected_ids
    assert all(value.turn_scope == "pending_next_turn" and value.turn_id is None for value in frozen_items)
    assert all(value.extensions["boxteam.checkpoint.annotations"]["value"]["structured_prompt_kind"] == source_kind
               for value in frozen_items)
    sealed = [saver.get_context_assembly(session_id, thread_id, assembly_id=row[0]) for row in assemblies]
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        assert canonical_json_bytes([value.to_dict() for value in cold._storage.storage.read_items(
            session_id, thread_id, item_ids=tuple(row[0] for row in retry_items))]) == canonical_json_bytes(
                [value.to_dict() for value in frozen_items])
        assert not any(value.source_id in expected_ids for value in cold.load_context_source_control_states(
            ContextSourceOwnerKey(session_id, thread_id)))
        for snapshot in sealed:
            restored = cold.get_context_assembly(session_id, thread_id, assembly_id=snapshot.assembly_id)
            assert canonical_json_bytes(restored.to_dict()) == canonical_json_bytes(snapshot.to_dict())
    case.write_actual(f"h3-{retry_kind}", {"job": result, "executions": executions,
        "retry_items": retry_items, "model_calls": model_calls, "assemblies": assemblies,
        "provider_requests": calls, "retry_source_kind": source_kind, "retry_sources": sorted(expected_ids),
        "sealed_bytes_hash": sha256_jcs([snapshot.to_dict() for snapshot in sealed])})


@pytest.mark.asyncio
async def test_e07_reminder_reasons_commit_once_and_cold_restore_without_checkpoint_mutation(source_case: RuntimeSourceCase) -> None:
    case = source_case
    case.model.response_callback = lambda _payload: {"content": "E07 原执行完成"}
    session_id = await create_session(case.client, "E07-reminder-owner")
    thread_id = await case.container.session_service.resolve_main_thread(session_id)
    accepted = await _send(case, session_id, "E07 建立唯一真实 execution")
    await _job(case, accepted["job_id"])
    saver = case.container.checkpointer
    active = saver.execution_admission_for_job(session_id, thread_id=thread_id, job_id=accepted["job_id"])
    checkpoint_headers = case.sql(session_id, thread_id, "SELECT * FROM checkpoints ORDER BY checkpoint_id")
    checkpoint_channels = case.sql(session_id, thread_id, "SELECT * FROM checkpoint_channels ORDER BY checkpoint_id,channel_name")
    calls = len(case.model.requests)
    reasons = ("interrupt", "job_startup_timeout", "job_timeout", "scope_deadline_exceeded",
               "tool_dispatch_timeout", "execution_lost", "execution_error", "resource_cancel")
    for reason in reasons:
        for _attempt in range(2):
            persist_interrupt_checkpoint(checkpointer=saver, session_id=session_id, thread_id=thread_id,
                active_tool_name="exec_command" if reason in {"tool_dispatch_timeout", "resource_cancel"} else None,
                checkpoint_source=reason, event_identity=f"{active.execution_id}:{reason}", execution_id=active.execution_id)
    source_ids = {f"checkpoint:{reason}:{active.execution_id}:{reason}" for reason in reasons}
    item_ids = {f"item-{source}" for source in source_ids}
    items = saver._storage.storage.read_items(session_id, thread_id, item_ids=item_ids)
    assert len(items) == len(reasons)
    assert all(item.semantic_kind == "runtime_notice" and item.turn_id is None
               and item.turn_scope == "pending_next_turn" for item in items)
    bindings = case.sql(session_id, thread_id,
        "SELECT item_id,execution_id FROM item_catalog WHERE item_id IN (" + ",".join("?" for _item in item_ids) + ")",
        tuple(sorted(item_ids)))
    assert {row[0] for row in bindings} == item_ids and all(row[1] == active.execution_id for row in bindings)
    assert {item.producer_ref["producer_id"] for item in items} == source_ids
    assert {item.extensions["boxteam.checkpoint.annotations"]["value"]["checkpoint_source"] for item in items} == set(reasons)
    assert not any(value.source_id in source_ids for value in saver.load_context_source_control_states(
        ContextSourceOwnerKey(session_id, thread_id)))
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        assert canonical_json_bytes([item.to_dict() for item in cold._storage.storage.read_items(
            session_id, thread_id, item_ids=item_ids)]) == canonical_json_bytes([item.to_dict() for item in items])
        for reason in reasons:
            persist_interrupt_checkpoint(checkpointer=cold, session_id=session_id, thread_id=thread_id,
                active_tool_name="exec_command" if reason in {"tool_dispatch_timeout", "resource_cancel"} else None,
                checkpoint_source=reason, event_identity=f"{active.execution_id}:{reason}", execution_id=active.execution_id)
        assert not any(value.source_id in source_ids for value in cold.load_context_source_control_states(
            ContextSourceOwnerKey(session_id, thread_id)))
        with pytest.raises((RuntimeError, ValueError)):
            submit_checkpoint_reminder(checkpointer=cold, session_id=session_id, thread_id=thread_id,
                reminder="同一事件正文漂移必须拒绝", response_metadata={"source": "interrupt"}, checkpoint_source="interrupt",
                event_identity=f"{active.execution_id}:interrupt", execution_id=active.execution_id)
    assert case.sql(session_id, thread_id, "SELECT * FROM checkpoints ORDER BY checkpoint_id") == checkpoint_headers
    assert case.sql(session_id, thread_id, "SELECT * FROM checkpoint_channels ORDER BY checkpoint_id,channel_name") == checkpoint_channels
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM executions")[0][0] == 1
    assert len(await case.container.job_service.list(session_id=session_id)) == 1
    assert len(case.model.requests) == calls
    assert len(saver._storage.storage.read_items(session_id, thread_id, item_ids=item_ids)) == len(reasons)
    case.write_actual("e07-reminder-reasons", {"execution_id": active.execution_id, "sources": sorted(source_ids),
        "canonical_bytes_hash": sha256_jcs([item.to_dict() for item in items]),
        "unchanged_checkpoint_ids": [row[0] for row in checkpoint_headers], "provider_requests": calls})


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", [{"terminal": True}], indirect=True)
async def test_e05_terminal_completion_rehydrates_exact_cold_child_owner(source_case: RuntimeSourceCase, monkeypatch) -> None:
    case = source_case
    parent, _child, intent = await _team_child(case)
    session_id, thread_id, main_thread = parent["session_id"], intent.thread_id, parent["thread_id"]
    steering = case.container.terminal_steering_service
    await steering.shutdown()
    tracker = case.container.agent_execution_service._residency_tracker
    clock = [tracker._clock()]
    monkeypatch.setattr(tracker, "_clock", lambda: clock[0])
    original = tracker._states[(session_id, thread_id)].runtime
    assert original is not None and tracker.snapshot(session_id, thread_id).blockers == ()
    clock[0] += 1800
    await tracker.sweep()
    assert tracker.snapshot(session_id, thread_id).residency == "cold"
    assert original.lifetime_scope.state == "closed"
    main_count = case.sql(session_id, main_thread, "SELECT COUNT(*) FROM executions")[0][0]
    client = steering._terminal_client
    async with client.operation(session_id=session_id, thread_id=thread_id,
        holder_id="e05-cold-terminal", operation_id="e05-command") as operation:
        created = await operation.create_terminal(retention_scope="workspace", title="E05 精确 cold child")
        terminal_id = created["terminal_id"]
        await operation.write_terminal(terminal_id=terminal_id, source="agent", command="printf E05-final",
            data="printf '\\n__BOXTEAM_CMD_START_e05__\\n'; printf 'E05-final\\n'; printf '\\n__BOXTEAM_CMD_DONE_e05__:0\\n'\r")
        await operation.mark_terminal_backgrounded(terminal_id)
    assert tracker.snapshot(session_id, thread_id).residency == "cold"
    async with asyncio.timeout(20):
        while True:
            terminal = next(value for value in await client.list_resources() if value["terminal_id"] == terminal_id)
            if terminal["last_command_status"] == "completed":
                break
            await asyncio.sleep(0.05)
    assert terminal["source_thread_id"] == thread_id and terminal["completion_event_id"]

    def reply(payload):
        tools = [value for value in payload["messages"] if value["role"] == "tool" and value.get("name") == "write_stdin"]
        if not tools:
            return _tool_reply("write_stdin", {"session_id": terminal_id, "chars": "", "yield_time_ms": 1000}, "call_e05_stdin")
        assert "E05-final" in tools[-1]["content"]
        return {"content": "E05 cold child 已读取真实终端输出"}

    case.model.response_callback = reply
    await steering.scan_once()
    wakeups = case.sql(session_id, thread_id,
        "SELECT w.source_id,w.source_item_id,w.admission_idempotency_key,e.job_id FROM runtime_execution_wakeups w JOIN executions e ON e.execution_id=w.execution_id WHERE w.source_id=?",
        (f"terminal:{terminal_id}:completion",))
    assert len(wakeups) == 1
    wakeup = case.container.checkpointer.runtime_wakeup_for_admission(session_id, thread_id, wakeups[0][2])
    event_digest = hashlib.sha256(
        f"runtime-source\0{session_id}\0{thread_id}\0{terminal['completion_event_id']}".encode()).hexdigest()
    assert wakeup.admission_idempotency_key == f"radm_{event_digest}"
    result = await _job(case, wakeups[0][3])
    assert result["thread_id"] == thread_id
    assert tracker.snapshot(session_id, thread_id).generation > original.generation
    assert not tracker.is_current_generation(session_id, thread_id, original.generation)
    assert case.sql(session_id, main_thread, "SELECT COUNT(*) FROM executions")[0][0] == main_count
    notices = case.container.checkpointer._storage.storage.read_items(session_id, thread_id, item_ids=(wakeups[0][1],))
    assert len(notices) == 1 and notices[0].turn_id is None
    assert notices[0].semantic_kind == "runtime_notice" and notices[0].turn_scope == "pending_next_turn"
    await steering.scan_once()
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM runtime_execution_wakeups WHERE source_id=?",
                    (f"terminal:{terminal_id}:completion",))[0][0] == 1
    case.write_actual("e05-cold-terminal", {"terminal_id": terminal_id, "source_thread_id": thread_id,
        "old_generation": original.generation, "new_generation": tracker.snapshot(session_id, thread_id).generation,
        "wakeup": wakeups, "job": result, "canonical_notice": notices[0].to_dict()})


@pytest.mark.asyncio
async def test_h4_generated_branch_returns_to_frozen_child_thread(source_case: RuntimeSourceCase) -> None:
    case = source_case
    parent_job, _child_job, intent = await _team_child(case)
    session_id, child_thread_id = parent_job["session_id"], intent.thread_id
    main_thread_id = parent_job["thread_id"]
    generated_contexts: list[str] = []

    def reply(payload: dict[str, object]) -> dict[str, object]:
        human = "\n".join(_wire_text(m["content"]) for m in payload["messages"]
                          if m["role"] == "user" and m["content"])
        if "<generated_session_result" in human:
            return {"content": "H4-exact-child-origin-ack"}
        assert "H4-private-generator-prompt" in human
        generated_contexts.append(_wire_text(payload["messages"]))
        return {"content": "H4-generated-result"}

    case.model.response_callback = reply
    workspace_id = case.client.headers["X-BoxTeam-Workspace-Id"]
    payload = {
        "run_id": "h4-run", "generator_id": "h4-generator", "idempotency_key": "h4-stable-key",
        "generator_type": {"type_id": "builtin.agent_prompt", "version": "1"}, "name": "H4 生成器",
        "config": {"prompt": "H4-private-generator-prompt：返回 H4-generated-result。"},
        "placement": {"kind": "workspace", "workspace_id": workspace_id},
        "execution_workspace_id": workspace_id, "context_source": {"kind": "fresh"},
        "session_strategy": {"mode": "fork_new_and_report_back", "concurrency": "queue",
            "report_back": "continue_agent", "target": {"workspace_id": workspace_id,
                "session_id": session_id, "thread_id": child_thread_id}},
        "title": "H4 生成分支", "navigation_path": [],
    }
    response = await case.client.post("/api/v1/session-generations/execute", json=payload)
    assert response.status_code == 200, response.text
    accepted = response.json()["data"]
    generated_session_id = accepted["outputs"][0]["session_id"]
    generated_job = await _job(case, accepted["job_id"])
    async with asyncio.timeout(45):
        while True:
            status_response = await case.client.get("/api/v1/session-generations/status", params={
                "generator_id": "h4-generator", "idempotency_key": "h4-stable-key"})
            assert status_response.status_code == 200, status_response.text
            result = status_response.json()["data"]
            if result["status"] in {"completed", "failed"}:
                assert result["status"] == "completed", result
                break
            await asyncio.sleep(0.05)
    report_job = await _job(case, result["report_back_job_id"])
    assert report_job["session_id"] == session_id and report_job["thread_id"] == child_thread_id
    assert case.sql(session_id, child_thread_id, "SELECT COUNT(*) FROM turn_records")[0][0] == 0
    assert case.sql(session_id, main_thread_id, "SELECT COUNT(*) FROM item_catalog WHERE semantic_kind='runtime_notice'")[0][0] == 1
    child_notices = case.sql(session_id, child_thread_id, "SELECT item_id FROM item_catalog WHERE semantic_kind='runtime_notice'")
    assert len(child_notices) == 1
    generated_thread_id = generated_job["thread_id"]
    generated_execution = case.sql(generated_session_id, generated_thread_id,
        "SELECT execution_id FROM executions WHERE job_id=?", (generated_job["job_id"],))
    assert len(generated_execution) == 1
    generated_outputs = case.sql(generated_session_id, generated_thread_id,
        "SELECT item_id FROM item_catalog WHERE semantic_kind='assistant_output' AND execution_id=? ORDER BY item_sequence",
        (generated_execution[0][0],))
    canonical_report = _assert_generated_notice(
        case, session_id, child_thread_id, child_notices[0][0],
        expected_provenance={"source_session_id": generated_session_id, "source_thread_id": generated_thread_id,
            "source_execution_id": generated_execution[0][0], "source_item_id": generated_outputs[-1][0]},
        private_result="H4-generated-result",
    )
    repeat = await case.client.post("/api/v1/session-generations/execute", json=payload)
    assert repeat.status_code == 200 and repeat.json()["data"] == result, repeat.text
    assert case.sql(session_id, child_thread_id, "SELECT COUNT(*) FROM executions")[0][0] == 2
    case.write_actual("h4", {"generated_session_id": generated_session_id, "generated_job": generated_job,
        "report_job": report_job, "result": result, "exact_origin_thread_id": child_thread_id,
        "child_runtime_inputs": child_notices, "canonical_report": canonical_report,
        "private_seed_in_generated_context": any("private-seed-h1" in value for value in generated_contexts),
        "child_output_in_generated_context": any("child-result-h1" in value for value in generated_contexts)})
    assert generated_contexts and all("private-seed-h1" in value and "child-result-h1" in value for value in generated_contexts)


_COLD_FORK_PROCESS = """
import asyncio
import json
import os
import sys
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from tests.support.session_catalog_owner import fixture_catalog_scope

sessions, source_session, source_thread, target_session, phase = sys.argv[1:]
with fixture_catalog_scope(), RolloutCheckpointSaver(sessions) as saver:
    storage = saver._storage
    if phase != 'read':
        target_thread = storage.storage.main_thread_id(target_session)
        if phase == 'publication_crash':
            publish = saver._detail_store.publish
            def crash_after_publication(*args, **kwargs):
                publish(*args, **kwargs)
                os._exit(73)
            saver._detail_store.publish = crash_after_publication
        if phase == 'finish_crash':
            def crash_before_completion(*, materialization_id, target_session_id,
                                        target_thread_id, checkpoint_ns):
                with storage.storage._connect(target_thread_id, checkpoint_ns,
                                      session_id=target_session_id, read_only=True) as connection:
                    journal = connection.execute(
                        'SELECT status FROM fork_materializations WHERE materialization_id=?',
                        (materialization_id,),
                    ).fetchone()
                assert journal == ('target_committed',), journal
                print(json.dumps({'pid': os.getpid(), 'phase': phase,
                                  'journal': journal}), flush=True)
                os._exit(74)
            storage.forks.complete_fork_materialization = crash_before_completion
        result = asyncio.run(saver.forks.afork(
            source_session_id=source_session, target_session_id=target_session,
            source_owner=MutationIntentOwner(session_id=source_session, thread_id=source_thread),
            target_owner=MutationIntentOwner(session_id=target_session, thread_id=target_thread),
        ))
        assert result.journal_state == 'committed', result.journal_state
        session, thread = target_session, target_thread
    else:
        session, thread = source_session, source_thread
    items = storage.storage.read_items(session, thread)
    assemblies = storage.assemblies.list_context_assemblies(session, thread)
    with storage.storage.open_read_snapshot(session, thread) as snapshot:
        connection = snapshot.connection
        executions = connection.execute(
            'SELECT execution_id,execution_origin,turn_id,outcome FROM executions'
        ).fetchall()
        counts = {table: connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for table in ('turn_records','context_plans','context_plan_details',
                                'checkpoint_context_source_snapshots','context_epoch_transitions')}
        journals = connection.execute('SELECT status FROM fork_materializations').fetchall()
        checkpoints = connection.execute('SELECT metadata_json FROM checkpoints').fetchall()
    seed = None
    request = None
    if executions:
        assert len(executions) == 1 and executions[0][1:] == ('internal', None, 'completed')
        request = storage.executions.internal_execution_request_input(session, thread, executions[0][0])
        seed = saver.internal_seed_source(session, thread, executions[0][0]).content
    detail_owners = []
    for assembly in assemblies:
        for entry in assembly.selection:
            if entry.included and entry.detail_ref is not None:
                entry.detail_ref.require_owner(session, thread, assembly.assembly_id)
                saver.read_context_plan_detail(session, thread, detail_ref=entry.detail_ref)
                detail_owners.append(entry.detail_ref.to_dict())
    print(json.dumps({
        'pid': os.getpid(), 'session_id': session, 'thread_id': thread,
        'executions': executions, 'counts': counts, 'journals': journals,
        'request': request, 'seed': seed, 'detail_owners': detail_owners,
        'items': [item.to_dict() for item in items],
        'prefixes': [assembly.to_dict()['seal_binding'] for assembly in assemblies],
        'checkpoint_bindings': [json.loads(row[0]).get('boxteam_execution_binding') for row in checkpoints],
    }, ensure_ascii=False))
"""


@pytest.fixture
def cold_fork_process(source_case: RuntimeSourceCase):
    case = source_case

    async def invoke(session_id: str, thread_id: str, target_session_id: str = "", *, phase: str = "read"):
        command = [sys.executable, "-c", _COLD_FORK_PROCESS,
                   str(case.container.checkpointer._storage.storage.sessions_dir), session_id,
                   thread_id, target_session_id, phase]
        result = await asyncio.to_thread(
            subprocess.run, command, cwd=Path.cwd(), capture_output=True, text=True, timeout=90,
        )
        name = f"cold-{phase}-{target_session_id or session_id}"
        (case.artifacts_dir / f"{name}.log").write_text(result.stdout + result.stderr)
        expected_code = {"publication_crash": 73, "finish_crash": 74}.get(phase, 0)
        assert result.returncode == expected_code, result.stdout + result.stderr
        if expected_code:
            return None
        actual = json.loads(result.stdout.splitlines()[-1])
        assert actual["pid"] != os.getpid()
        case.write_actual(name, actual)
        return actual

    return invoke


@pytest.mark.asyncio
async def test_h5_cold_refork_and_hard_exit_recovery(source_case: RuntimeSourceCase, cold_fork_process) -> None:
    case = source_case
    parent_job, _child_job, intent = await _team_child(case)
    source_session, source_thread = parent_job["session_id"], intent.thread_id
    first_session = await create_session(case.client, "H5 冷 fork")
    first = await cold_fork_process(source_session, source_thread, first_session, phase="fork")
    first_thread = first["thread_id"]
    second_session = await create_session(case.client, "H5 再次 fork")
    second = await cold_fork_process(first_session, first_thread, second_session, phase="fork")
    for actual in (first, second):
        assert actual["request"]["kind"] == "seed_reference"
        assert "private-seed-h1" in actual["seed"]
        assert "private-seed-h1" in _wire_text(actual["prefixes"])
        assert "child-result-h1" in _wire_text(actual["items"])
        assert actual["counts"]["turn_records"] == 0
        assert all(binding is not None and binding["execution_id"] == actual["executions"][0][0]
                   for binding in actual["checkpoint_bindings"])
        assert actual["journals"] == [["committed"]]
        assert actual["detail_owners"]
    assert {d["detail_id"] for d in first["detail_owners"]}.isdisjoint(
        d["detail_id"] for d in second["detail_owners"])

    aborted_session = await create_session(case.client, "H5 publication 硬退出")
    aborted_thread = await case.container.session_service.resolve_main_thread(aborted_session)
    await cold_fork_process(source_session, source_thread, aborted_session, phase="publication_crash")
    mapped = case.sql(aborted_session, aborted_thread,
        "SELECT target_local_id FROM fork_identity_mappings WHERE entity_type='detail'")
    node = case.container.session_service.path_resolver.resolve_session_node(aborted_session)
    paths = [node / detail_relative_path(detail_ref_from_key(row[0])) for row in mapped]
    assert paths and any(path.is_file() for path in paths)
    aborted = await cold_fork_process(aborted_session, aborted_thread)
    assert aborted["executions"] == [] and aborted["items"] == []
    assert aborted["journals"] == [["aborted"]]
    assert all(count == 0 for count in aborted["counts"].values())
    assert all(not path.exists() for path in paths)

    finish_session = await create_session(case.client, "H5 finish 硬退出")
    finish_thread = await case.container.session_service.resolve_main_thread(finish_session)
    await cold_fork_process(source_session, source_thread, finish_session, phase="finish_crash")
    assert case.sql(finish_session, finish_thread, "SELECT status FROM fork_materializations") == [("target_committed",)]
    finished = await cold_fork_process(finish_session, finish_thread)
    assert finished["journals"] == [["committed"]]
    assert "private-seed-h1" in finished["seed"] and finished["counts"]["turn_records"] == 0
    repeated = await cold_fork_process(finish_session, finish_thread)
    assert repeated["executions"] == finished["executions"]
    assert repeated["detail_owners"] == finished["detail_owners"]


@dataclass
class RestartRuntimeSourceCase:
    client: httpx.AsyncClient
    model: HTTPStubState
    workspace: Path
    artifacts_dir: Path
    backends: list[BackendProcess]
    release_report: Event

    def write_actual(self, name: str, value: dict[str, object]) -> None:
        (self.artifacts_dir / f"{name}-actual.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        )

    async def restart(self, *, hard: bool) -> None:
        previous = self.backends[-1]
        if hard:
            previous.process.kill()
            return_code = await asyncio.to_thread(previous.process.wait, timeout=10)
            assert return_code < 0
        await asyncio.to_thread(close_backend_process, previous)
        self.release_report.set()
        backend = await asyncio.to_thread(
            start_backend_process, workspace_root=str(self.workspace), port=previous.port,
            log_name=f"runtime-source-restart-{len(self.backends)}",
        )
        assert backend.process.pid != previous.process.pid
        self.backends.append(backend)

    def snapshot(self, session_dir: Path) -> dict[str, object]:
        with closing(SessionControlStore(session_dir / CONTROL_DATABASE_NAME)) as control:
            intents = control.list_initial_execution_intents()
            assert len(intents) == 1
            intent = intents[0]
            record = control.get_thread_creation_record(intent.creation_idempotency_key)
            identity = {
                field: getattr(intent, field) for field in (
                    "session_id", "thread_id", "creation_idempotency_key", "state",
                    "execution_binding_id", "job_id", "admission_idempotency_key", "binding_preimage_hash",
                )
            }
            main_thread_id = str(control.get_main_thread()["thread_id"])
        result: dict[str, object] = {
            "intent": identity, "frozen_seed": record.task_seed, "main_thread_id": main_thread_id,
        }
        for name, owner_dir in (("parent", session_dir), ("child", session_dir / record.final_relative_locator)):
            database = owner_dir / "rollout/index.sqlite"
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                result[name] = {
                    "executions": connection.execute(
                        "SELECT execution_id,execution_origin,turn_id,outcome,job_id FROM executions ORDER BY created_at",
                    ).fetchall(),
                    "turns": connection.execute("SELECT turn_id,status FROM turn_records").fetchall(),
                    "notices": connection.execute(
                        "SELECT item_id,execution_id FROM item_catalog WHERE semantic_kind='runtime_notice' ORDER BY item_sequence",
                    ).fetchall(),
                    "wakeups": connection.execute(
                        "SELECT source_id,source_item_id,execution_id,job_id,admission_idempotency_key,state,"
                        "dispatch_receipt_json FROM runtime_execution_wakeups ORDER BY message_created_at",
                    ).fetchall(),
                    "assemblies": connection.execute(
                        "SELECT assembly_id,status,outcome FROM context_assemblies ORDER BY created_at",
                    ).fetchall(),
                    "models": connection.execute(
                        "SELECT model_call_id,outcome,execution_id FROM model_calls ORDER BY created_at",
                    ).fetchall(),
                }
        return result


@pytest.fixture
async def restart_source_case(request: pytest.FixtureRequest) -> AsyncIterator[RestartRuntimeSourceCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    workspace = _prepare_source_workspace(context, ports.port(10))
    runtime_root = context.runtime_root_for_node(request.node.nodeid)
    artifacts_dir = context.artifacts_dir / runtime_root.parent.name / runtime_root.name
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    release = Event()
    with openai_chat_stub(ports.port(10)) as model:
        backend = await asyncio.to_thread(
            start_backend_process, workspace_root=str(workspace), port=ports.port(0),
            log_name="runtime-source-restart-0",
        )
        backends = [backend]
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{backend.port}",
                headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": backend.workspace_id},
                timeout=30,
            ) as client:
                case = RestartRuntimeSourceCase(client, model, workspace, artifacts_dir, backends, release)
                yield case
        finally:
            release.set()
            for handle in reversed(backends):
                await asyncio.to_thread(close_backend_process, handle)
            (artifacts_dir / "h6-wire-actual.json").write_text(
                json.dumps({"requests": model.requests, "backend_pids": [handle.process.pid for handle in backends]},
                           ensure_ascii=False, indent=2) + "\n",
            )


@pytest.mark.asyncio
async def test_h6_backend_hard_restart_keeps_child_and_runtime_report_identity(
    restart_source_case: RestartRuntimeSourceCase,
) -> None:
    case = restart_source_case
    report_started = Event()
    report_requests: list[dict[str, object]] = []

    def reply(payload: dict[str, object]) -> dict[str, object]:
        messages = payload["messages"]
        # 团队 facet 指引也使用 user 角色，不能把最后一帧当成唯一请求正文。
        human = "\n".join(_wire_text(message["content"]) for message in messages
                          if message["role"] == "user" and message["content"])
        if "<generated_session_result" in human:
            report_requests.append(payload)
            report_started.set()
            assert case.release_report.wait(90), "H6 未在已开始的 parent report 模型请求窗口硬杀后端"
            return {"content": "h6-parent-ack"}
        if "<delegated_task" in human and "h6-private-seed" in human:
            return {"content": "h6-child-result"}
        if "h6-team-trigger" not in human:
            return {"content": "H6 重启验证会话"}
        tools = [message for message in messages if message["role"] == "tool"]
        if not any(message.get("name") == "create_team" for message in tools):
            return _tool_reply("create_team", {"name": "H6 团队"}, "call_h6_team")
        team = json.loads(next(message["content"] for message in tools if message.get("name") == "create_team"))
        if not any(message.get("name") == "create_team_member" for message in tools):
            return _tool_reply("create_team_member", {
                "team_id": team["team_id"], "role": "reviewer", "startup_prompt": "h6-private-seed，返回 h6-child-result。",
            }, "call_h6_member")
        return {"content": "h6-parent-dispatched"}

    case.model.response_callback = reply
    session_id = await create_session(case.client, "H6 同会话 child 服务重启")
    response = await case.client.post(f"/api/v1/sessions/{session_id}/messages", json={
        "message": {"content": "h6-team-trigger：创建团队并启动成员。"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    await _job(case, response.json()["data"]["job_id"])
    await _wait_until(report_started.is_set, seconds=45)
    catalog = await case.client.get("/api/v1/session-catalog/export")
    assert catalog.status_code == 200, catalog.text
    node = next(node for node in catalog.json()["data"]["items"] if node["node_id"] == session_id)
    session_dir = case.workspace / ".boxteam/sessions" / node["storage_relative_path"]
    before = case.snapshot(session_dir)
    assert before["intent"]["state"] == "bound"
    assert "h6-private-seed" in before["frozen_seed"]
    assert before["child"]["turns"] == []
    assert len(before["child"]["executions"]) == 1
    assert before["child"]["executions"][0][1:4] == ("internal", None, "completed")
    assert len(before["parent"]["turns"]) == 1 and before["parent"]["turns"][0][1] == "completed"
    assert len(before["parent"]["notices"]) == len(before["parent"]["wakeups"]) == 1
    assert before["parent"]["executions"][-1][1:4] == ("internal", None, "unknown")
    assert all(model[2] != before["parent"]["executions"][-1][0] for model in before["parent"]["models"])
    case.write_actual("h6-before-kill", before)

    for hard in (True, False):
        await case.restart(hard=hard)
        after = case.snapshot(session_dir)
        case.write_actual(f"h6-after-{'hard' if hard else 'second'}-restart", after)
        assert after["intent"] == before["intent"] and after["frozen_seed"] == before["frozen_seed"]
        assert after["child"] == before["child"]
        assert after["parent"]["turns"] == before["parent"]["turns"]
        assert after["parent"]["notices"] == before["parent"]["notices"]
        expected_executions = [*before["parent"]["executions"]]
        expected_executions[-1] = (*expected_executions[-1][:3], "execution_lost", expected_executions[-1][4])
        assert after["parent"]["executions"] == expected_executions
        assert len(after["parent"]["wakeups"]) == 1
        wakeup = after["parent"]["wakeups"][0]
        assert wakeup[:5] == before["parent"]["wakeups"][0][:5]
        assert wakeup[5] == "dispatched"
        receipt = json.loads(wakeup[6])
        assert (receipt["job_id"], receipt["thread_id"], receipt["status"]) == (wakeup[3], before["main_thread_id"], "failed")
        assert all(assembly[1] == "terminal" for assembly in after["parent"]["assemblies"])
        assert after["parent"]["assemblies"][-1][2] == "execution_lost"
        assert after["parent"]["models"] == before["parent"]["models"]
        assert len(report_requests) == 1


@pytest.mark.parametrize("source_case", [{"workspace_template": "model_tool_test_workspace"}], indirect=True)
@pytest.mark.asyncio
async def test_replay_and_manual_compaction_http_keep_catalog_main_owner(
    source_case: RuntimeSourceCase,
) -> None:
    case = source_case

    def reply(payload: dict[str, object]) -> dict[str, object]:
        human = next((_wire_text(message["content"]) for message in reversed(payload["messages"])
                      if message["role"] == "user" and message["content"]), "")
        return {"content": f"真实 owner 回答：{human}"}

    case.model.response_callback = reply
    session_id = await create_session(case.client, "Replay 与手动压缩归属")
    first = await _send(case, session_id, "replay-first-private")
    await _job(case, first["job_id"])
    second = await _send(case, session_id, "replay-second-private")
    await _job(case, second["job_id"])
    registry = case.container.workspace_file_resource_registry
    agents_snapshot = registry.snapshot(workspace_agent_spec_display_uri(
        load_or_create_workspace_id(registry.workspace_root),
    ))
    assert agents_snapshot.available and agents_snapshot.revision
    assert agents_snapshot.content == (registry.workspace_root / "AGENTS.md").read_text()
    case.write_actual("replay-published-agents-source", {
        "uri": agents_snapshot.uri, "available": agents_snapshot.available,
        "revision": agents_snapshot.revision,
        "fixture": "model_tool_test_workspace", "workspace_root": str(registry.workspace_root),
    })
    saver = case.container.checkpointer
    owner = saver.forks.source.resolve_main_thread_owner(session_id)
    assert owner.thread_id != session_id
    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
        original = await saver.aget_tuple(build_checkpoint_config(owner.thread_id))
    assert original is not None
    original_messages = original.checkpoint["channel_values"]["messages"]

    edited = await case.client.post(
        f"/api/v1/sessions/{session_id}/messages/{first['message_id']}/replay",
        headers={"X-Request-ID": "req-replay-catalog-owner"},
        json={"action": "edit_and_continue", "content": "replay-edited-private", "acknowledge_context_only": True},
    )
    assert edited.status_code == 200, edited.text
    assert edited.headers["X-Request-ID"] == edited.json()["request_id"] == "req-replay-catalog-owner"
    accepted = edited.json()["data"]
    assert accepted["thread_id"] == accepted["dispatch"]["thread_id"] == owner.thread_id
    await _job(case, accepted["job_id"])
    (turn_id,) = case.sql(session_id, owner.thread_id,
        "SELECT turn_id FROM turn_acceptances WHERE accepted_ingress_id=?", (accepted["message_id"],))[0]

    regenerated = await case.client.post(
        f"/api/v1/sessions/{session_id}/turns/{turn_id}/replay",
        json={"action": "regenerate", "acknowledge_context_only": True},
    )
    assert regenerated.status_code == 200, regenerated.text
    replayed = regenerated.json()["data"]
    assert replayed["thread_id"] == replayed["dispatch"]["thread_id"] == owner.thread_id
    assert replayed["message_id"] != accepted["message_id"]
    await _job(case, replayed["job_id"])
    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
        latest = await saver.aget_tuple(build_checkpoint_config(owner.thread_id))
        frozen = await saver.aget_tuple(original.config)
    assert latest is not None and frozen is not None
    messages = latest.checkpoint["channel_values"]["messages"]
    assert [message.content for message in messages if message.type == "human"] == ["replay-edited-private"]
    assert frozen.checkpoint["channel_values"]["messages"] == original_messages
    assemblies = [saver.get_context_assembly(session_id, owner.thread_id, assembly_id=row[0])
                  for row in case.sql(session_id, owner.thread_id,
                      "SELECT assembly_id FROM context_assemblies ORDER BY created_at,assembly_id")]
    assert [snapshot.seal_binding.prefix.state.epoch.ordinal for snapshot in assemblies] == [1, 1, 2, 3]
    guidance_revision = None
    for ordinal, snapshot in enumerate(assemblies):
        guidance = [frame for frame in snapshot.seal_binding.prefix.state.frames
                    if frame.item_ref.source_identity == MCP_GUIDANCE_SOURCE_ID]
        assert len(guidance) == 1
        if ordinal == 0:
            guidance_revision = guidance[0].item_ref.source_revision
        assert guidance[0].item_ref.source_revision == guidance_revision
        applied = [source for source in snapshot.seal_binding.source_revisions
                   if source.source_id == MCP_GUIDANCE_SOURCE_ID]
        assert len(applied) == (1 if ordinal == 0 else 0)
    assert case.sql(session_id, owner.thread_id,
        "SELECT COUNT(*) FROM context_source_control_states WHERE source_id=?", (MCP_GUIDANCE_SOURCE_ID,))[0][0] == 0
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        for snapshot in assemblies:
            restored = cold.get_context_assembly(session_id, owner.thread_id, assembly_id=snapshot.assembly_id)
            assert canonical_json_bytes(restored.to_dict()) == canonical_json_bytes(snapshot.to_dict())
    before_execution_count = case.sql(session_id, owner.thread_id, "SELECT COUNT(*) FROM executions")[0][0]

    compact = await case.client.post(f"/api/v1/sessions/{session_id}/compact")
    assert compact.status_code == 200, compact.text
    assert compact.json()["data"]["status"] == "scheduled"
    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
        scheduled = await saver.aget_tuple(build_checkpoint_config(owner.thread_id))
    assert scheduled is not None
    assert scheduled.checkpoint["channel_values"]["_force_cache_compaction"] is True
    assert scheduled.checkpoint["channel_values"]["messages"] == messages
    assert case.sql(session_id, owner.thread_id, "SELECT COUNT(*) FROM executions")[0][0] == before_execution_count
    assert case.sql(session_id, owner.thread_id,
        "SELECT DISTINCT session_id,thread_id FROM executions") == [(session_id, owner.thread_id)]
    case.write_actual("replay-and-compaction-main-owner", {
        "session_id": session_id, "thread_id": owner.thread_id,
        "edited": accepted, "regenerated": replayed, "compact": compact.json(),
        "execution_count": before_execution_count, "original_checkpoint_id": original.checkpoint["id"],
        "scheduled_checkpoint_id": scheduled.checkpoint["id"],
        "sealed_assemblies": [{
            "assembly_id": snapshot.assembly_id,
            "epoch": snapshot.seal_binding.prefix.state.epoch.ordinal,
            "source_revisions": [source.source_id for source in snapshot.seal_binding.source_revisions],
        } for snapshot in assemblies],
    })
