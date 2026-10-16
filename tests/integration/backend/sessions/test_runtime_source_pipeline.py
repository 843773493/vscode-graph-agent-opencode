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
from app.abstractions.job_executor import JobRuntimeStateProtocol
from app.agents.graph_binding import compute_capability_profile_hash
from app.agents.skill_runtime import WORKSPACE_AGENTS_SOURCE_ID
from app.container import AppContainer
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore, ThreadExecutionIntent
from app.core.session_thread_context import session_thread_owner_scope
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.execution_admission import InternalExecutionIntent
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    content_hash,
    sha256_jcs,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord, RuntimeNoticePayload
from app.domain.itemized.serialization import normalize_wire_request
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
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_relative_path,
)
from app.services.orchestration.initial_execution_binding_worker import (
    InitialExecutionBindingWorker,
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
                headers={
                    "X-Local-Token": "local-dev-token",
                    "X-BoxTeam-Workspace-Id": workspace_id,
                    "X-BoxTeam-Gateway-Id": "0197d9a3-7d2a-7c29-8d76-58b3cf3f8a21",
                },
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


async def _send(case: RuntimeSourceCase | RestartRuntimeSourceCase, session_id: str, content: str) -> dict[str, object]:
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
async def test_runtime_notice_plain_text_containing_display_part_stays_text(
    source_case: RuntimeSourceCase,
) -> None:
    case = source_case
    session_id = await create_session(case.client, "普通 runtime notice 文本分类")
    thread_id = await case.container.session_service.resolve_main_thread(session_id)
    marker = "R1520 普通通知正文包含 display_part 字面量。"
    prepared = internal_message_factory.build(
        kind="session_message",
        control="保留普通 runtime notice 文本。",
        sections=[
            PromptSection("control_context", {"source_id": "r1520-plain-text"}),
            PromptSection("session_message", marker),
        ],
        metadata={"source": "send_message_to_session", "kind": "result"},
    )
    case.model.response_callback = lambda _request: {"content": "R1520 provider ack"}

    accepted = await case.container.session_orchestrator.submit_runtime_event(
        session_id,
        prepared,
        thread_id=thread_id,
        source_id="r1520-plain-text",
        idempotency_key="r1520-plain-text-display-part",
    )
    await _job(case, accepted.job_id)

    execution_rows = case.sql(
        session_id,
        thread_id,
        "SELECT execution_id FROM executions WHERE job_id = ?",
        (accepted.job_id,),
    )
    assert len(execution_rows) == 1
    execution_id = execution_rows[0][0]
    notice_rows = case.sql(
        session_id,
        thread_id,
        "SELECT item_id FROM item_catalog "
        "WHERE execution_id = ? AND semantic_kind = 'runtime_notice'",
        (execution_id,),
    )
    assert len(notice_rows) == 1
    notice_id = notice_rows[0][0]
    (notice,) = case.container.checkpointer._storage.storage.read_items(
        session_id,
        thread_id,
        item_ids=(notice_id,),
    )
    assert notice.payload_kind == "text"
    assert notice.payload == prepared.content
    assert case.sql(
        session_id,
        thread_id,
        "SELECT COUNT(*) FROM context_view_items "
        "WHERE item_id = ? AND source_kind = 'display_only'",
        (notice_id,),
    )[0][0] > 0

    assert len(case.model.requests) == 1
    provider_messages = case.model.requests[0]["json"]["messages"]
    assert any(
        message["role"] == "user" and marker in _wire_text(message["content"])
        for message in provider_messages
    )

    history_response = await case.client.post(
        f"/api/v1/sessions/{session_id}/threads/{thread_id}/history",
        json={"execution_id": execution_id, "include": ["internal"]},
    )
    assert history_response.status_code == 200, history_response.text
    assert history_response.headers["X-Request-ID"] == history_response.json()["request_id"]
    page = history_response.json()["data"]
    assert page["thread_id"] == thread_id
    execution_history = next(
        entry for entry in page["internal_executions"]
        if entry["execution_id"] == execution_id
    )
    assert execution_history["display_parts"] == []


@pytest.mark.asyncio
async def test_runtime_wakeup_before_provider_preserves_started_job_and_frozen_admission(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    session_id = await create_session(case.client, "原 runtime 准入首次派发恢复")
    thread_id = await case.container.session_service.resolve_main_thread(session_id)
    entered, release = asyncio.Event(), asyncio.Event()
    executor = case.container.job_service._job_executor
    original_run = executor.run

    async def hold_before_provider(job: JobRuntimeStateProtocol) -> str:
        if job.session_id == session_id:
            entered.set()
            await release.wait()
        return await original_run(job)

    monkeypatch.setattr(executor, "run", hold_before_provider)
    case.model.response_callback = lambda _request: {"content": "同一原准入完成"}
    prepared = internal_message_factory.build(
        kind="session_message", control="原 runtime 准入恢复验证",
        sections=[
            PromptSection("control_context", {"source_id": "before-provider-source"}),
            PromptSection("session_message", "保持冻结正文与原身份。"),
        ],
        metadata={"source": "send_message_to_session", "kind": "result"},
    )
    dispatcher = case.container.runtime_source_dispatcher
    try:
        accepted = await case.container.session_orchestrator.submit_runtime_event(
            session_id, prepared, thread_id=thread_id,
            source_id="before-provider-source", idempotency_key="before-provider-admission",
        )
        await asyncio.wait_for(entered.wait(), 30)
        job = case.container.job_service._jobs[accepted.job_id]
        original_task = job.task
        before = case.sql(session_id, thread_id,
            "SELECT execution_id,job_id,admission_idempotency_key,admission_payload_hash,outcome "
            "FROM executions")
        assert len(before) == 1 and before[0][-1] == "unknown"
        assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM model_calls") == [(0,)]
        assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM context_assemblies") == [(0,)]
        assert await dispatcher.can_restart_before_provider(job)
        assert await case.container.runtime_service.reconcile_stale_executions() == 0
        assert case.sql(session_id, thread_id,
            "SELECT execution_id,job_id,admission_idempotency_key,admission_payload_hash,outcome "
            "FROM executions") == before
        wakeup = case.container.checkpointer.runtime_wakeup_for_admission(
            session_id, thread_id, before[0][2],
        )
        assert wakeup is not None
        repeated = await dispatcher.dispatch(wakeup)
        assert repeated.job_id == accepted.job_id and job.task is original_task
        trace = case.container.trace_event_store.read_events(session_id)
        own_trace = [event for event in trace if event.job_id == accepted.job_id]
        assert [event.type for event in own_trace].count("job_created") == 1
        assert [event.type for event in own_trace].count("job_started") == 1
        assert all(event.type != "session_interrupted" for event in own_trace)
    finally:
        release.set()
    await _job(case, accepted.job_id)
    assert not await dispatcher.can_restart_before_provider(job)
    assert case.sql(session_id, thread_id,
        "SELECT execution_id,job_id,admission_idempotency_key,admission_payload_hash FROM executions",
    ) == [before[0][:-1]]
    assert case.sql(session_id, thread_id,
        "SELECT COUNT(*) FROM item_catalog WHERE semantic_kind='runtime_notice'",
    ) == [(1,)]
    assert len(case.model.requests) == 1


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
import hashlib
import json
import os
import sys
import pytest
from app.core.resource_uri import thread_resource_uri
from app.domain.itemized.execution_admission import InternalExecutionIntent
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from tests.support.session_catalog_owner import fixture_catalog_scope

sessions, source_session, source_thread, target_session, phase, mode = sys.argv[1:]
with fixture_catalog_scope(), RolloutCheckpointSaver(sessions) as saver:
    storage = saver._storage
    if phase != 'read':
        target_thread = storage.storage.main_thread_id(target_session)
        operation_id = 'h5-' + target_session
        catalog = storage.forks._catalog_store()
        capture = catalog.get_fork_source_capture(copy_operation_id=operation_id)
        source_preflight = None
        if capture is None:
            assert phase != 'resume'
            with storage.storage.read_connection(source_session, source_thread, '') as connection:
                source_view, source_revision = connection.execute(
                    'SELECT branch.head_view_id,meta.history_view_revision FROM checkpoint_namespace_state namespace '
                    'JOIN branches branch ON branch.branch_id=namespace.active_branch_id '
                    'JOIN database_meta meta ON meta.singleton_id=1 WHERE namespace.checkpoint_ns=?', ('',),
                ).fetchone()
            source_preflight = asyncio.run(saver.forks.source.preflight_fork(
                source_session_id=source_session,
                source_owner=MutationIntentOwner(session_id=source_session, thread_id=source_thread),
                mode=mode, source_view_id=source_view, source_view_revision=source_revision,
            ))
            frozen = {
                'source_uri': thread_resource_uri(catalog.get_node(source_session).workspace_id,
                                                  source_session, source_thread),
                'source_view_id': source_preflight.source_view_id,
                'source_view_revision': source_preflight.source_view_revision,
                'anchor_item_id': source_preflight.anchor_item_id,
            }
        else:
            assert phase == 'resume'
            frozen = json.loads(capture.request_json)
        if phase == 'publication_crash':
            from app.services.infrastructure.rollout_context.checkpoint import fork_copy
            publish = fork_copy.publish_target_snapshot
            def crash_after_publication(*args, **kwargs):
                publish(*args, **kwargs)
                with storage.storage._connect(target_thread, '',
                                              session_id=target_session, read_only=True) as connection:
                    journals = connection.execute(
                        'SELECT status FROM fork_materializations WHERE target_session_id=? AND target_thread_id=?',
                        (target_session, target_thread),
                    ).fetchall()
                assert journals == [('prepared',)], journals
                print(json.dumps({'pid': os.getpid(), 'phase': phase,
                                  'journals': journals}), flush=True)
                os._exit(73)
            fork_copy.publish_target_snapshot = crash_after_publication
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
            mode=mode, fork_id=operation_id, source_preflight=source_preflight,
            source_uri=frozen['source_uri'], source_view_id=frozen['source_view_id'],
            source_view_revision=frozen['source_view_revision'], anchor_item_id=frozen['anchor_item_id'],
        ))
        assert result.journal_state == 'committed', result.journal_state
        session, thread = target_session, target_thread
    else:
        session, thread = source_session, source_thread
    storage.storage.initialize(session, thread_id=thread)
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
        journals = connection.execute(
            'SELECT materialization_id,source_checkpoint_id,status FROM fork_materializations '
            'WHERE target_session_id=? AND target_thread_id=?',
            (session, thread),
        ).fetchall()
        checkpoints = []
        for checkpoint in connection.execute('SELECT * FROM checkpoints ORDER BY checkpoint_id'):
            channels = connection.execute(
                'SELECT * FROM checkpoint_channels WHERE checkpoint_id=? ORDER BY channel_name',
                (checkpoint[0],),
            ).fetchall()
            pending = connection.execute(
                'SELECT * FROM pending_writes WHERE checkpoint_id=? ORDER BY task_id,task_path,write_index',
                (checkpoint[0],),
            ).fetchall()
            raw = json.dumps((checkpoint, channels, pending), ensure_ascii=False,
                             default=lambda value: {'bytes_hex': value.hex()}).encode('utf-8')
            checkpoints.append({'checkpoint_id': checkpoint[0], 'metadata': json.loads(checkpoint[13]),
                                'facts_sha256': hashlib.sha256(raw).hexdigest()})
        head = connection.execute(
            'SELECT cp.checkpoint_id,cp.metadata_json,cp.pending_sends_type,cp.pending_sends_blob '
            'FROM checkpoint_namespace_state namespace JOIN branches branch '
            'ON branch.branch_id=namespace.active_branch_id JOIN checkpoints cp '
            'ON cp.checkpoint_id=branch.head_checkpoint_id WHERE namespace.checkpoint_ns=?', ('',),
        ).fetchone()
        head_checkpoint = None if head is None else {
            'checkpoint_id': head[0], 'metadata': json.loads(head[1]),
            'pending_sends': storage.storage.decode_value(tuple(head[2:])),
            'pending_write_count': connection.execute(
                'SELECT COUNT(*) FROM pending_writes WHERE checkpoint_id=?', (head[0],),
            ).fetchone()[0],
        }
        current_execution_count = connection.execute(
            'SELECT COUNT(*) FROM executions WHERE session_id=? AND thread_id=?', (session, thread),
        ).fetchone()[0]
        parts = connection.execute(
            'SELECT p.item_id,p.part_id,p.part_ordinal,i.jsonl_offset FROM item_parts p '
            'JOIN item_catalog i ON i.item_id=p.item_id ORDER BY i.item_sequence,p.part_ordinal'
        ).fetchall()
        model_calls = connection.execute(
            'SELECT model_call_id,execution_id,assembly_id,outcome,dispatch_state '
            'FROM model_calls ORDER BY created_at'
        ).fetchall()
    seed = None
    request = None
    accepted_seed = None
    cursor_fact = None
    admission_hash = None
    execution_scope = None
    historical_execution_rejected = False
    if executions:
        assert len(executions) == 1 and executions[0][1:3] == ('internal', None)
        assert executions[0][3] in {'completed', 'completed_empty'}
        request = storage.executions.internal_execution_request_input(session, thread, executions[0][0])
        seed = saver.internal_seed_source(session, thread, executions[0][0]).content
        with storage.storage.open_read_snapshot(session, thread) as snapshot:
            execution_scope = snapshot.connection.execute(
                'SELECT session_id,thread_id FROM executions WHERE execution_id=?', (executions[0][0],),
            ).fetchone()
            admission = storage.executions._read_execution_admission_connection(
                snapshot.connection, session_id=execution_scope[0], thread_id=execution_scope[1],
                execution_id=executions[0][0],
            )
            cursor = storage.executions.execution_admission_cursor(
                admission, connection=snapshot.connection,
            )
            commit = snapshot.connection.execute(
                'SELECT commit_mode,jsonl_record_count FROM storage_commits WHERE commit_id=?',
                (cursor.commit_id,),
            ).fetchone()
            admission_hash = snapshot.connection.execute(
                'SELECT admission_payload_hash FROM executions WHERE execution_id=?',
                (admission.execution_id,),
            ).fetchone()[0]
        cursor_fact = {'commit_id': cursor.commit_id, 'commit_kind': cursor.commit_kind,
                       'commit_mode': commit[0], 'jsonl_record_count': commit[1]}
        binding = InternalExecutionIntent(
            message_id=f'seed-{admission.execution_id}', execution_id=admission.execution_id,
            admission_idempotency_key=admission.admission_idempotency_key,
            input_reference_json=json.dumps(request['reference']),
        )
        if execution_scope == (session, thread):
            message = saver.accepted_internal_message(
                session, thread_id=thread, job_id=admission.job_id,
                message_id=binding.message_id, intent=binding,
            )
            accepted_seed = {'id': message.id, 'content': message.content,
                             'metadata': message.response_metadata['message_metadata']}
        else:
            with pytest.raises(KeyError, match='execution admission 不存在'):
                storage.executions.execution_admission(session, thread_id=thread, execution_id=admission.execution_id)
            with pytest.raises(KeyError, match='Job execution admission 不存在'):
                saver.accepted_internal_message(
                    session, thread_id=thread, job_id=admission.job_id,
                    message_id=binding.message_id, intent=binding,
                )
            historical_execution_rejected = True
    detail_owners = []
    for assembly in assemblies:
        for entry in assembly.selection:
            if entry.included and entry.detail_ref is not None:
                entry.detail_ref.require_owner(assembly.session_id, assembly.thread_id, assembly.assembly_id)
                saver.read_context_plan_detail(session, thread, detail_ref=entry.detail_ref)
                detail_owners.append(entry.detail_ref.to_dict())
    print(json.dumps({
        'pid': os.getpid(), 'session_id': session, 'thread_id': thread,
        'executions': executions, 'counts': counts, 'journals': [row[2:] for row in journals],
        'head_journals': [dict(zip(('materialization_id', 'source_checkpoint_id', 'status'), row))
                          for row in journals],
        'request': request, 'seed': seed, 'detail_owners': detail_owners,
        'accepted_seed': accepted_seed, 'admission_cursor': cursor_fact,
        'admission_payload_hash': admission_hash,
        'parts': parts, 'execution_scope': execution_scope,
        'historical_execution_rejected': historical_execution_rejected,
        'model_calls': model_calls,
        'items': [item.to_dict() for item in items],
        'prefixes': [assembly.to_dict()['seal_binding'] for assembly in assemblies],
        'checkpoints': checkpoints, 'head_checkpoint': head_checkpoint,
        'current_execution_count': current_execution_count,
    }, ensure_ascii=False))
"""


@pytest.fixture
def cold_fork_process(source_case: RuntimeSourceCase):
    case = source_case

    async def invoke(session_id: str, thread_id: str, target_session_id: str = "", *, phase: str = "read", mode: str = "context_fork"):
        command = [sys.executable, "-c", _COLD_FORK_PROCESS,
                   str(case.container.checkpointer._storage.storage.sessions_dir), session_id,
                   thread_id, target_session_id, phase, mode]
        result = await asyncio.to_thread(
            subprocess.run, command, cwd=Path.cwd(), capture_output=True, text=True, timeout=90,
        )
        name = f"cold-{phase}-{target_session_id or session_id}"
        (case.artifacts_dir / f"{name}.log").write_text(result.stdout + result.stderr)
        expected_code = {"publication_crash": 73, "finish_crash": 74}.get(phase, 0)
        if result.returncode != expected_code:
            pytest.fail(
                f"cold fork 退出码不符: phase={phase}, source={session_id}/{thread_id}, "
                f"target={target_session_id}, actual={result.returncode}, expected={expected_code}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
                pytrace=False,
            )
        if expected_code:
            return None
        actual = json.loads(result.stdout.splitlines()[-1])
        assert actual["pid"] != os.getpid()
        case.write_actual(name, actual)
        return actual

    return invoke


def _assert_cold_fork_checkpoints(source: dict[str, object], target: dict[str, object]) -> None:
    """原 checkpoint 全事实按 ID 保真；新增空闲 head 独立核验零准入。"""
    expected = {fact["checkpoint_id"]: fact for fact in source["checkpoints"]}
    actual = {fact["checkpoint_id"]: fact for fact in target["checkpoints"]}
    assert expected and len(expected) == len(source["checkpoints"])
    assert len(actual) == len(target["checkpoints"])
    assert {checkpoint_id: actual[checkpoint_id] for checkpoint_id in expected} == expected
    head = target["head_checkpoint"]
    assert head is not None and head["checkpoint_id"] not in expected
    assert set(actual) == set(expected) | {head["checkpoint_id"]}
    assert actual[head["checkpoint_id"]]["metadata"] == head["metadata"]
    assert head["metadata"]["source"] == "fork"
    assert "boxteam_execution_binding" not in head["metadata"]
    assert len(target["head_journals"]) == 1
    journal = target["head_journals"][0]
    assert journal["status"] == "committed"
    assert journal["materialization_id"] == head["metadata"]["fork_materialization_id"]
    assert journal["source_checkpoint_id"] == head["metadata"]["fork_context_checkpoint_id"]
    assert journal["source_checkpoint_id"] in expected
    assert head["pending_sends"] == [] and head["pending_write_count"] == 0
    assert target["current_execution_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "full_rollout_copy"])
async def test_h5_cold_refork_and_hard_exit_recovery(source_case: RuntimeSourceCase, cold_fork_process, mode: str) -> None:
    case = source_case
    parent_job, _child_job, intent = await _team_child(case)
    source_session, source_thread = parent_job["session_id"], intent.thread_id
    admission = case.container.checkpointer._storage.executions.execution_admission(
        source_session, thread_id=source_thread, execution_id=intent.execution_binding_id,
    )
    request = case.container.checkpointer._storage.executions.internal_execution_request_input(
        source_session, source_thread, admission.execution_id,
    )
    binding = InternalExecutionIntent(
        message_id=f"seed-{admission.execution_id}", execution_id=admission.execution_id,
        admission_idempotency_key=admission.admission_idempotency_key,
        input_reference_json=json.dumps(request["reference"]),
    )
    source_message = case.container.checkpointer.accepted_internal_message(
        source_session, thread_id=source_thread, job_id=admission.job_id,
        message_id=binding.message_id, intent=binding,
    )
    source_admission_hash = case.sql(
        source_session, source_thread,
        "SELECT admission_payload_hash FROM executions WHERE execution_id=?",
        (admission.execution_id,),
    )[0][0]
    source = await cold_fork_process(source_session, source_thread)
    assert source["checkpoints"]
    assert all(fact["metadata"]["boxteam_execution_binding"]["execution_id"] == admission.execution_id
               and fact["metadata"]["boxteam_execution_binding"]["admission_payload_hash"] == source_admission_hash
               for fact in source["checkpoints"])
    first_session = await create_session(case.client, "H5 冷 fork")
    first = await cold_fork_process(source_session, source_thread, first_session, phase="fork", mode=mode)
    first_thread = first["thread_id"]
    second_session = await create_session(case.client, "H5 再次 fork")
    second = await cold_fork_process(first_session, first_thread, second_session, phase="fork", mode=mode)
    for actual in (first, second):
        assert actual["request"]["kind"] == "seed_reference"
        assert actual["request"] == request
        assert actual["admission_payload_hash"] == source_admission_hash
        assert actual["admission_cursor"]["commit_kind"] == "execution_admission"
        assert actual["admission_cursor"]["commit_mode"] == "metadata_only"
        assert actual["admission_cursor"]["jsonl_record_count"] == 0
        assert actual["accepted_seed"] is None
        assert actual["historical_execution_rejected"] is True
        assert actual["execution_scope"] == [source_session, source_thread]
        assert actual["executions"][0][0] == admission.execution_id
        assert actual["seed"] == source_message.content
        assert "private-seed-h1" in actual["seed"]
        assert "private-seed-h1" in _wire_text(actual["prefixes"])
        assert "child-result-h1" in _wire_text(actual["items"])
        assert actual["counts"]["turn_records"] == 0
        assert actual["journals"] == [["committed"]]
        assert actual["detail_owners"]
    _assert_cold_fork_checkpoints(source, first)
    _assert_cold_fork_checkpoints(first, second)
    assert source["accepted_seed"]["content"] == source_message.content
    assert source["accepted_seed"]["metadata"] == source_message.response_metadata["message_metadata"]
    for copied in (first, second):
        assert copied["detail_owners"] == source["detail_owners"]
        assert copied["parts"] == source["parts"]
        assert copied["items"] == source["items"]
        assert copied["model_calls"] == source["model_calls"]
        assert copied["admission_cursor"] == source["admission_cursor"]

    aborted_session = await create_session(case.client, "H5 publication 硬退出")
    aborted_thread = await case.container.session_service.resolve_main_thread(aborted_session)
    await cold_fork_process(source_session, source_thread, aborted_session, phase="publication_crash", mode=mode)
    details = case.sql(aborted_session, aborted_thread,
        "SELECT detail_ref FROM context_plan_details WHERE availability='available'")
    node = case.container.session_service.path_resolver.resolve_thread_node(aborted_session, aborted_thread)
    paths = [node / detail_relative_path(detail_ref_from_key(row[0])) for row in details]
    assert paths and any(path.is_file() for path in paths)
    assert case.sql(aborted_session, aborted_thread, "SELECT status FROM fork_materializations") == [("prepared",)]
    recovered = await cold_fork_process(source_session, source_thread, aborted_session, phase="resume", mode=mode)
    assert recovered["journals"] == [["committed"]]
    assert recovered["seed"] == source_message.content
    assert recovered["request"] == request and recovered["admission_payload_hash"] == source_admission_hash
    assert recovered["counts"]["turn_records"] == 0 and recovered["detail_owners"] == source["detail_owners"]
    assert recovered["historical_execution_rejected"] is True
    assert recovered["items"] == source["items"]
    _assert_cold_fork_checkpoints(source, recovered)
    assert all(path.is_file() for path in paths)
    repeated_publication = await cold_fork_process(source_session, source_thread, aborted_session, phase="resume", mode=mode)
    assert repeated_publication["executions"] == recovered["executions"]
    assert repeated_publication["detail_owners"] == recovered["detail_owners"]
    assert repeated_publication["checkpoints"] == recovered["checkpoints"]

    finish_session = await create_session(case.client, "H5 finish 硬退出")
    finish_thread = await case.container.session_service.resolve_main_thread(finish_session)
    await cold_fork_process(source_session, source_thread, finish_session, phase="finish_crash", mode=mode)
    assert case.sql(finish_session, finish_thread, "SELECT status FROM fork_materializations") == [("target_committed",)]
    finished = await cold_fork_process(source_session, source_thread, finish_session, phase="resume", mode=mode)
    assert finished["journals"] == [["committed"]]
    assert "private-seed-h1" in finished["seed"] and finished["counts"]["turn_records"] == 0
    assert finished["historical_execution_rejected"] is True
    _assert_cold_fork_checkpoints(source, finished)
    repeated = await cold_fork_process(source_session, source_thread, finish_session, phase="resume", mode=mode)
    assert repeated["executions"] == finished["executions"]
    assert repeated["detail_owners"] == finished["detail_owners"]
    assert repeated["checkpoints"] == finished["checkpoints"]


@pytest.fixture
async def completed_empty_seed_source(
    source_case: RuntimeSourceCase, frozen_provider_wire, frozen_provider_activation,
):
    """正式 creation、准入、封存与收敛组成无输出来源，不伪造 notice/Turn。"""
    case = source_case
    session_id = await create_session(case.client, "H5 completed_empty seed")
    creation = case.container.thread_initial_execution_service._factory.for_owner_session(session_id)
    profile = {"thread_role": "child", "goal_enabled": False}
    created = await creation.create(
        idempotency_key="h5-completed-empty-creation", session_id=session_id, thread_id=None,
        delegation_id=None, initial_state="running", artifact_manifest={}, session_metadata={
            "graph_binding": {"graph_id": "deep-agent", "graph_revision": 1,
                "graph_schema_hash": "sha256:" + "a" * 64,
                "capability_profile_hash": compute_capability_profile_hash(capability_profile=profile)},
            "capability_profile": profile,
            "task_seed": {"description": "H5 合法无输出冻结任务"},
            "task_reference": {"parent_session_id": session_id,
                "parent_thread_id": await case.container.session_service.resolve_main_thread(session_id)},
        },
    )
    thread_id = created.child_thread_id
    async with case.container.session_service.session_write_admission(session_id):
        worker = InitialExecutionBindingWorker(
            store=creation.control_store, claim_owner="initial-execution",
            binder=case.container.thread_initial_execution_service.binder,
        )
        worker.start()
        try:
            attempts = worker.bind_pending_once()
        finally:
            worker.stop()
    assert len(attempts) == 1 and attempts[0].outcome == "bound"
    intent = creation.control_store.list_initial_execution_intents()[0]
    assert intent.state == "bound" and intent.thread_id == thread_id
    saver = case.container.checkpointer
    admission = saver._storage.executions.execution_admission(
        session_id, thread_id=thread_id, execution_id=intent.execution_binding_id,
    )
    seed = saver.internal_seed_source(session_id, thread_id, admission.execution_id)
    assert seed is not None
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(session_id, thread_id), control_state_port=saver,
    )
    with (session_thread_owner_scope(MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id),
          execution_admission_scope(admission, langgraph_thread_id=thread_id)):
        creation_key = "h5-completed-empty-plan-create"
        prepared = saver.prepare_context_for_provider(
            session_id, thread_id, turn_id=None, execution_id=admission.execution_id,
            plan_creation_idempotency_key=creation_key,
            seal_idempotency_key="h5-completed-empty-seal", request_messages=(),
            context_source_manager=manager,
            tool_set_supplier=lambda: ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
            **frozen_provider_wire("e2e-stub-model", "chat_completions"),
            **frozen_provider_activation(saver, plan_creation_idempotency_key=creation_key),
        )
        snapshot = prepared["snapshot"]
        assert snapshot.seal_binding is not None and snapshot.model_call_id is not None
        saver.register_model_call(
            session_id, thread_id=thread_id, execution_id=admission.execution_id,
            model_call_id=snapshot.model_call_id, assembly_id=snapshot.assembly_id,
            attempt=1, provider="e2e-stub-model", dispatch_state="dispatched",
        )
        await saver.aput(build_checkpoint_config(thread_id), {
            "id": "h5-completed-empty-checkpoint", "channel_values": {"messages": []},
            "channel_versions": {"messages": "1"}, "updated_channels": ["messages"], "pending_sends": [],
        }, {"source": "input", "step": 1, "parents": {}}, {"messages": "1"})
    saver.converge_execution(
        session_id, thread_id=thread_id, turn_id=None, execution_id=admission.execution_id,
        outcome="completed_empty", turn_status=None,
    )
    return session_id, thread_id


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "full_rollout_copy"])
async def test_h5_completed_empty_seed_preserves_metadata_only_admission(
    source_case: RuntimeSourceCase, completed_empty_seed_source, cold_fork_process, mode: str,
) -> None:
    case = source_case
    session_id, thread_id = completed_empty_seed_source
    source = await cold_fork_process(session_id, thread_id)
    assert source["checkpoints"]
    assert all(fact["metadata"]["boxteam_execution_binding"]["execution_id"] == source["executions"][0][0]
               and fact["metadata"]["boxteam_execution_binding"]["admission_payload_hash"] == source["admission_payload_hash"]
               for fact in source["checkpoints"])
    first_session = await create_session(case.client, "H5 无输出冷 fork")
    first = await cold_fork_process(session_id, thread_id, first_session, phase="fork", mode=mode)
    second_session = await create_session(case.client, "H5 无输出再次 fork")
    second = await cold_fork_process(first_session, first["thread_id"], second_session, phase="fork", mode=mode)
    for actual in (source, first, second):
        assert actual["items"] == [] and actual["parts"] == []
        assert actual["counts"]["turn_records"] == 0
        assert actual["executions"][0][1:] == ["internal", None, "completed_empty"]
        assert len(actual["model_calls"]) == 1
        assert actual["model_calls"][0][1] == actual["executions"][0][0]
        assert actual["model_calls"][0][3:] == ["completed_empty", "completed"]
        assert actual["model_calls"][0][2] == actual["prefixes"][0]["prefix"]["assembly_id"]
        assert actual["request"] == source["request"]
        assert actual["admission_payload_hash"] == source["admission_payload_hash"]
        assert actual["admission_cursor"]["commit_kind"] == "execution_admission"
        assert actual["admission_cursor"]["commit_mode"] == "metadata_only"
        assert actual["admission_cursor"]["jsonl_record_count"] == 0
        assert actual["seed"] == source["seed"] == source["accepted_seed"]["content"]
        assert actual["execution_scope"] == [session_id, thread_id]
    _assert_cold_fork_checkpoints(source, first)
    _assert_cold_fork_checkpoints(first, second)
    for actual in (first, second):
        assert actual["accepted_seed"] is None and actual["historical_execution_rejected"] is True
        assert actual["executions"] == source["executions"]
        assert actual["model_calls"] == source["model_calls"]
        assert actual["detail_owners"] == source["detail_owners"]
    third_session = await create_session(case.client, "H5 本地冻结空上下文再 fork")
    third = await cold_fork_process(
        second_session, second["thread_id"], third_session, phase="fork", mode="context_fork",
    )
    _assert_cold_fork_checkpoints(second, third)
    assert third["executions"] == source["executions"]
    assert third["seed"] == source["seed"] and third["detail_owners"] == source["detail_owners"]
    assert third["historical_execution_rejected"] is True


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

    async def restart(self, *, hard: bool, while_stopped: Callable[[], None] | None = None) -> None:
        previous = self.backends[-1]
        if hard:
            previous.process.kill()
            return_code = await asyncio.to_thread(previous.process.wait, timeout=10)
            assert return_code < 0
        await asyncio.to_thread(close_backend_process, previous)
        self.release_report.set()
        if while_stopped is not None:
            assert previous.process.poll() is not None
            while_stopped()
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
    params = getattr(request, "param", {})
    if "agents_content" in params:
        (workspace / "AGENTS.md").write_text(params["agents_content"], encoding="utf-8")
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
    # Provider 已实际开始且必经 assembly 封存；model-start 消费是否已完成不决定派发事实。
    assert before["parent"]["assemblies"][-1][1:] == ("sealed", None)
    assert all(model[1] == "unknown" for model in before["parent"]["models"]
               if model[2] == before["parent"]["executions"][-1][0])
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


@pytest.mark.parametrize(
    "agents_after,restart_source_case",
    [(variant, {"agents_content": "CIL_OLD_AGENTS 已完成首帧。\n"})
     for variant in ("unchanged", "changed", "absent")],
    indirect=["restart_source_case"], ids=["unchanged", "changed", "absent"],
)
@pytest.mark.asyncio
async def test_cold_agents_restart_reconciles_before_second_provider(
    restart_source_case: RestartRuntimeSourceCase, agents_after: str,
) -> None:
    case = restart_source_case
    first_body = "CIL_OLD_AGENTS 已完成首帧。\n"
    current_body = "CIL_NEW_AGENTS 停机期间的新指令。\n" if agents_after == "changed" else (
        "" if agents_after == "absent" else first_body
    )
    first_prompt, second_prompt = "cil-first-private", "cil-second-private"
    case.model.response_callback = lambda _payload: {"content": "CIL 同 owner 回复"}
    session_id = await create_session(case.client, "CIL 冷 AGENTS 首次绑定")
    first = await _send(case, session_id, first_prompt)
    await _job(case, first["job_id"])
    catalog = await case.client.get("/api/v1/session-catalog/export")
    assert catalog.status_code == 200, catalog.text
    node = next(item for item in catalog.json()["data"]["items"] if item["node_id"] == session_id)
    session_dir = case.workspace / ".boxteam/sessions" / node["storage_relative_path"]
    sessions = case.workspace / ".boxteam/sessions"
    with RolloutCheckpointSaver(sessions) as cold:
        owner = cold.forks.source.resolve_main_thread_owner(session_id)
        owner_key = ContextSourceOwnerKey(owner.session_id, owner.thread_id)
        assert owner.thread_id != session_id
        assemblies = cold.list_context_assemblies(session_id, owner.thread_id)
        first_candidates = [snapshot for snapshot in assemblies
                            if first_prompt in _wire_text(snapshot.request_hash_preimage["messages"])]
        assert len(first_candidates) == 1
        first_assembly = first_candidates[0]
        assert first_assembly.seal_binding is not None
        applied = [revision for revision in first_assembly.seal_binding.source_revisions
                   if revision.source_id == WORKSPACE_AGENTS_SOURCE_ID]
        assert len(applied) == 1 and applied[0].previous_revision is None
        old_revision = applied[0].revision
        before_control = next(state for state in cold.load_context_source_control_states(owner_key)
                              if state.source_id == WORKSPACE_AGENTS_SOURCE_ID)
        assert before_control.latest_revision == before_control.latest_visible_committed_revision == old_revision
        assert before_control.tracking_status == "tracked"
        assert first_body in first_assembly.seal_binding.root.content
        first_bytes = canonical_json_bytes(first_assembly.to_dict())
    first_requests = [request["json"] for request in case.model.requests
                      if first_prompt in _wire_text(request["json"]["messages"])]
    assert len(first_requests) == 1
    assert normalize_wire_request(first_requests[0]) == first_assembly.request_hash_preimage

    def change_agents_while_stopped() -> None:
        path = case.workspace / "AGENTS.md"
        if agents_after == "absent":
            path.unlink()
        else:
            path.write_text(current_body, encoding="utf-8")

    await case.restart(hard=True, while_stopped=change_agents_while_stopped)
    # 服务重建不能自行推进 desired 或 visible；第二次真实 send 才观察并 seal。
    with RolloutCheckpointSaver(sessions) as cold:
        assert cold.forks.source.resolve_main_thread_owner(session_id) == owner
        restored_control = next(state for state in cold.load_context_source_control_states(owner_key)
                                if state.source_id == WORKSPACE_AGENTS_SOURCE_ID)
        assert restored_control == before_control
        restored_first = cold.get_context_assembly(session_id, owner.thread_id, assembly_id=first_assembly.assembly_id)
        assert canonical_json_bytes(restored_first.to_dict()) == first_bytes
    request_boundary = len(case.model.requests)
    second = await _send(case, session_id, second_prompt)
    assert second["thread_id"] == owner.thread_id
    await _job(case, second["job_id"])
    second_requests = [request["json"] for request in case.model.requests[request_boundary:]
                       if second_prompt in _wire_text(request["json"]["messages"])]
    assert len(second_requests) == 1
    with RolloutCheckpointSaver(sessions) as cold:
        assert cold.forks.source.resolve_main_thread_owner(session_id) == owner
        second_candidates = [snapshot for snapshot in cold.list_context_assemblies(session_id, owner.thread_id)
                             if second_prompt in _wire_text(snapshot.request_hash_preimage["messages"])]
        assert len(second_candidates) == 1
        second_assembly = second_candidates[0]
        assert second_assembly.seal_binding is not None
        assert (second_assembly.session_id, second_assembly.thread_id) == (session_id, owner.thread_id)
        assert normalize_wire_request(second_requests[0]) == second_assembly.request_hash_preimage
        current = next(state for state in cold.load_context_source_control_states(owner_key)
                       if state.source_id == WORKSPACE_AGENTS_SOURCE_ID)
        assert current.latest_revision == current.latest_visible_committed_revision
        revisions = [revision for revision in second_assembly.seal_binding.source_revisions
                     if revision.source_id == WORKSPACE_AGENTS_SOURCE_ID]
        new_frames = [frame for frame in second_assembly.seal_binding.prefix.state.frames
                      if frame.item_ref.source_identity == WORKSPACE_AGENTS_SOURCE_ID
                      and frame.item_ref.source_revision == current.latest_revision]
        if agents_after == "unchanged":
            assert revisions == [] and new_frames == []
            assert current == before_control
            assert any(first_body in _wire_text(message["content"])
                       for message in second_requests[0]["messages"] if message["role"] == "system")
        else:
            assert current.latest_revision != old_revision
            assert [(revision.previous_revision, revision.revision) for revision in revisions] == [
                (old_revision, current.latest_revision),
            ]
            assert len(new_frames) == 1 and new_frames[0].role == "user"
            assert new_frames[0].content == current_body
            assert {"role": "user", "content": current_body} in second_requests[0]["messages"]
            assert current.state_revision > before_control.state_revision
        restored_first = cold.get_context_assembly(session_id, owner.thread_id, assembly_id=first_assembly.assembly_id)
        assert canonical_json_bytes(restored_first.to_dict()) == first_bytes
    with sqlite3.connect((session_dir / "rollout/index.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        model_owner = connection.execute(
            "SELECT session_id,thread_id,execution_id,assembly_id FROM model_calls WHERE model_call_id=?",
            (second_assembly.model_call_id,),
        ).fetchall()
    assert model_owner == [(session_id, owner.thread_id, second_assembly.execution_id, second_assembly.assembly_id)]
    case.write_actual(f"cold-agents-{agents_after}", {
        "session_id": session_id, "thread_id": owner.thread_id, "source_id": WORKSPACE_AGENTS_SOURCE_ID,
        "backend_pids": [handle.process.pid for handle in case.backends],
        "previous_revision": old_revision, "current_revision": current.latest_revision,
        "visible_revision": current.latest_visible_committed_revision,
        "first_assembly_id": first_assembly.assembly_id, "second_assembly_id": second_assembly.assembly_id,
        "first_assembly_hash": hashlib.sha256(first_bytes).hexdigest(),
        "source_receipts": [{"previous_revision": revision.previous_revision, "revision": revision.revision}
                            for revision in revisions],
        "second_wire": second_requests[0], "model_owner": model_owner,
    })


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
