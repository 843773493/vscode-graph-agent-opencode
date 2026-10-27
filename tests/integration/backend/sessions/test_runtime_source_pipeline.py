"""真实 Job、模型协议与 runtime source/child publication 的核心集成验收。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, closing
from dataclasses import dataclass, field
from pathlib import Path

import commentjson
import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from app import main
from app.abstractions.job_executor import JobRuntimeStateProtocol
from app.agents.graph_binding import compute_capability_profile_hash
from app.container import AppContainer
from app.core import identifier, phase_observer
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.mounted_workspaces import MountedWorkspace
from app.core.path_utils import get_session_path_resolver
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore, ThreadExecutionIntent
from app.core.session_thread_context import session_thread_owner_scope
from app.core.trace_middleware import TraceMiddleware
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
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes.skill_sources import router as gateway_skill_sources_router
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.gateway.workspace_ids import build_managed_local_workspace_id
from app.prompting import PromptSection, internal_message_factory
from app.runtime.model_stream import configure_model_stream_from_environment
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
from app.services.orchestration.execution_step import runner
from app.services.orchestration.initial_execution_binding_worker import (
    InitialExecutionBindingWorker,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.browser_manager import (
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.support.api_helpers import create_session
from tests.support.e2e_control.harness import E2ETestControlHarness, control_request
from tests.support.e2e_control.runtime_ports import (
    ControlledIdentifiers,
    ControlledPhases,
)
from tests.support.e2e_control.tool_observations import ObservedCanonicalItemSink
from tests.support.fork_deadline import ForkDeadlineRegistry
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.workspace import (
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
class ModelReplayCase:
    client: httpx.AsyncClient
    model: ModelStreamReplay
    workspace: Path
    artifacts_dir: Path
    planned_calls: dict[str, dict[str, str]] = field(default_factory=dict, init=False)

    @property
    def control(self) -> E2ETestControlHarness:
        return self.model.control

    async def plan(
        self, session_id: str, responses: list[dict[str, object]], *, thread_id: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> list[str]:
        if thread_id is None:
            main_thread = await self.client.get(f"/api/v1/sessions/{session_id}/main-thread")
            assert main_thread.status_code == 200, main_thread.text
            thread_id = main_thread.json()["data"]["thread_id"]
        config = commentjson.loads((self.workspace / ".boxteam/workspace.jsonc").read_text(encoding="utf-8"))
        endpoint = next(provider["endpoint"] for provider in config["llm"]["providers"] if provider["id"] == "primary")
        interactions = [build_cassette(
            asset_id="runtime-source-restart", model="e2e-stub-model", url=endpoint + "/chat/completions",
            frames=chat_completion_frames(
                response_id=f"chatcmpl-runtime-source-{index}", model="e2e-stub-model",
                deltas=({"role": "assistant", **response},),
                finish_reason="tool_calls" if "tool_calls" in response else "stop",
                usage=usage,
            ),
        ).raw["interactions"][0] for index, response in enumerate(responses)]
        calls = self.control.plan_model_calls(session_id, thread_id, interactions)
        self.planned_calls.update({call: {"session_id": session_id, "thread_id": thread_id, "model_call_id": call} for call in calls})
        return calls

    def request(self, call: str) -> dict[str, object]:
        artifact = json.loads((self.model.artifacts / (call + ".json")).read_text(encoding="utf-8"))
        assert artifact["identity"] == self.planned_calls[call]
        assert artifact["request"]["stream"] is True
        return artifact["request"]

    def call_count(self, session_id: str, thread_id: str) -> int:
        with self.control._condition:
            return self.control._counters.get(f"model_call/{session_id}/{thread_id}", 0)

    def requests(self) -> list[dict[str, object]]:
        calls = [event["operation_id"] for event in self.control.events if event["phase"] == "model.response_ready"]
        assert {path.stem for path in self.model.artifacts.glob("*.json")} == set(calls)
        with self.control._condition:
            actual_calls = sum(count for namespace, count in self.control._counters.items() if namespace.startswith("model_call/"))
        assert actual_calls == len(calls)
        return [self.request(call) for call in calls]

    def tool_result(self, call: str) -> dict[str, object]:
        event, = [event for event in self.control.events
                  if event["phase"] == "test.tool_result.committed" and event["operation_id"] == call]
        assert event["details"]["item"]["payload"]["tool_call_id"] == call
        return json.loads(event["details"]["item"]["payload"]["content"])

    def write_actual(self, name: str, value: dict[str, object]) -> None:
        (self.artifacts_dir / f"{name}-actual.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


@dataclass
class RuntimeSourceCase(ModelReplayCase):
    container: AppContainer
    context: TestRunContext
    gateway_id: str | None
    gateway_skill_source_owner: GatewaySkillSourceOwner | None

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
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
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
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return workspace


@asynccontextmanager
async def _managed_gateway_skill_source(
    *, workspace: Path, workspace_id: str, boxteam_home: Path, port: int,
):
    """为 managed 测试提供真实 owner、已登记本地 Workspace 和认证快照路由。"""
    gateway_root = boxteam_home / "state" / "gateway"
    gateway_id = load_or_create_gateway_id(gateway_root / "identity.json")
    skills_root = boxteam_home / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    owner = GatewaySkillSourceOwner(gateway_id=gateway_id, skills_root=skills_root)
    registry = GatewayWorkspaceRegistry(storage_path=gateway_root / "skill-source-registry.json")
    registry.upsert(WorkspaceTarget(
        workspace_id=build_managed_local_workspace_id(str(workspace.resolve())),
        name="runtime-source-pipeline",
        root_path=str(workspace.resolve()),
        backend_url="http://127.0.0.1:8010",
        backend_workspace_id=workspace_id,
        connection_kind="local",
        owner="system",
        managed=False,
        removable=False,
        system_default=True,
    ))
    api = FastAPI()
    api.include_router(gateway_skill_sources_router)
    api.add_middleware(TraceMiddleware)
    api.state.registry = registry
    api.state.skill_source_owner = owner
    server = uvicorn.Server(uvicorn.Config(
        api, host="127.0.0.1", port=port, log_level="error", access_log=False,
        log_config=None, lifespan="off",
    ))
    server_task: asyncio.Task[None] | None = None
    try:
        await owner.start()
        server_task = asyncio.create_task(asyncio.to_thread(server.run))
        async with asyncio.timeout(10):
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.01)
        yield gateway_id, owner
    finally:
        if server_task is not None:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=10)
        registry.close()
        await owner.close()


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
    boxteam_home = context.boxteam_home_for_node(request.node.nodeid)
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)
    gateway_id = None
    gateway_skill_source_owner = None
    async with AsyncExitStack() as managed_services:
        if options.get("managed_gateway"):
            gateway_id, gateway_skill_source_owner = await managed_services.enter_async_context(
                _managed_gateway_skill_source(
                    workspace=workspace, workspace_id=workspace_id, boxteam_home=boxteam_home,
                    port=integration_port_block_for_file(Path(request.node.path)).port(11),
                ),
            )
            monkeypatch.setenv(
                "BOXTEAM_GATEWAY_URL",
                f"http://127.0.0.1:{integration_port_block_for_file(Path(request.node.path)).port(11)}",
            )
        else:
            monkeypatch.delenv("BOXTEAM_GATEWAY_URL", raising=False)
        mount_payload = {
            "workspace_id": workspace_id, "workspace_root": str(workspace), "terminal_backend_url": None, "browser_backend_url": None, "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None}
        runtime_root = context.runtime_root_for_node(request.node.nodeid)
        artifacts_dir = context.artifacts_dir
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        with ExitStack() as services:
            model = services.enter_context(model_stream_replay(
                runtime_root, runtime_root=runtime_root,
                business_assertion="runtime-source-product-admission-and-context",
            ))
            for key, value in model.environment.items():
                monkeypatch.setenv(key, value)
            address = json.loads(model.environment["BOXTEAM_E2E_TEST_CONTROL"])
            monkeypatch.setattr(identifier, "_identifier_factory", identifier._identifier_factory)
            monkeypatch.setattr(phase_observer, "_observer", phase_observer._observer)
            identifier.bind_identifier_factory(ControlledIdentifiers(address))
            phase_observer.bind_phase_observer(ControlledPhases(address))
            monkeypatch.setattr(runner, "CanonicalItemSink", ObservedCanonicalItemSink)
            if options.get("terminal"):
                terminal_port = integration_port_block_for_file(Path(request.node.path)).port(40)
                mount_payload["terminal_backend_url"] = f"http://127.0.0.1:{terminal_port}"
                process = start_terminal_backend(backend_port=terminal_port, workspace_root=str(workspace), artifacts_dir=artifacts_dir)
                services.callback(terminate_process, process)
            if options.get("browser"):
                browser_port = integration_port_block_for_file(Path(request.node.path)).port(42)
                mount_payload["browser_backend_url"] = f"http://127.0.0.1:{browser_port}"
                browser = start_browser_manager_processes(
                    workspace_root=workspace, backend_port=browser_port,
                    artifacts_dir=artifacts_dir,
                    frontend_port=integration_port_block_for_file(Path(request.node.path)).port(43),
                    backend_host="127.0.0.1",
                )
                services.callback(close_browser_manager_processes, browser)
            monkeypatch.setenv("BOXTEAM_MOUNTED_WORKSPACES", json.dumps([mount_payload]))
            async with AsyncExitStack() as lifespan:
                controller = configure_model_stream_from_environment(project_root=Path.cwd())
                assert controller is not None
                lifespan.push_async_callback(controller.aclose)
                monkeypatch.setattr(main, "_model_stream_controller", controller)
                await lifespan.enter_async_context(main.lifespan(main.app))
                registry = main.app.state.workspace_container_registry
                container = registry.get(workspace_id)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=main.app), base_url="http://workspace.test",
                    headers={
                        "X-Local-Token": "local-dev-token",
                        "X-BoxTeam-Workspace-Id": workspace_id,
                        **({"X-BoxTeam-Gateway-Id": gateway_id} if gateway_id is not None else {}),
                    },
                    timeout=30,
                ) as client:
                    case = RuntimeSourceCase(
                        client=client, model=model, workspace=workspace, artifacts_dir=artifacts_dir,
                        container=container, context=context, gateway_id=gateway_id,
                        gateway_skill_source_owner=gateway_skill_source_owner,
                    )
                    try:
                        yield case
                    finally:
                        case.write_actual("source-wire", {"requests": case.requests()})


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
    session_id = await create_session(case.client, "H1-team-core")
    case.control.arm_match("h1-child", "thread.runtime.acquired", {"session_id": session_id, "execution_origin": "internal"})
    first_call, = await case.plan(session_id, [
        _tool_reply("create_team", {"name": "H1 同会话团队"}, "call_h1_team"),
    ])
    team_call = first_call + ":tool-call:call_h1_team"
    case.control.arm(team_call, "test.tool_result.committed")
    accepted = await _send(case, session_id, "H1-team-core：创建团队并启动一个成员。")
    try:
        await asyncio.to_thread(case.control.acknowledged, team_call, "test.tool_result.committed")
        team = case.tool_result(team_call)
        await case.plan(session_id, [
            _tool_reply("create_team_member", {
                "team_id": team["team_id"], "role": "reviewer",
                "startup_prompt": "private-seed-h1，独立确认成员运行，返回 child-result-h1。",
            }, "call_h1_member"),
            {"content": "parent-dispatched-h1"}, {"content": "parent-result-ack-h1"},
        ])
    finally:
        case.control.release(team_call, "test.tool_result.committed")
    child = await asyncio.to_thread(case.control.acknowledged_match, "h1-child")
    try:
        child_call, = await case.plan(session_id, [{"content": "child-result-h1"}], thread_id=child["details"]["thread_id"])
    finally:
        control_request(json.loads(case.control.environment["BOXTEAM_E2E_TEST_CONTROL"]), {
            "action": "release_match", "barrier_id": "h1-child",
        })
    parent_job = await _job(case, str(accepted["job_id"]))
    factory = case.container.thread_initial_execution_service._factory
    with factory.for_owner_session(session_id) as thread_owner:
        control = thread_owner.control_store
        intents = control.list_initial_execution_intents()
        assert len(intents) == 1
        intent = intents[0]
        assert intent.session_id == session_id and intent.state == "bound"
        child_job = await _job(case, intent.job_id)
        assert "private-seed-h1" in _wire_text(case.request(child_call)["messages"])
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
    assert payload.prompt.count(private_result) == 1
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
    await case.plan(session_id, [{"content": "R1520 provider ack"}])

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

    assert len(case.requests()) == 1
    provider_messages = case.requests()[0]["messages"]
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
    await case.plan(session_id, [{"content": "同一原准入完成"}])
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
        queue_store = case.container.job_service._pending_requests.store
        queue_record = await queue_store.record(session_id, thread_id, accepted.job_id)
        assert await queue_store.before_dispatch(queue_record)
        assert len(before) == 1 and before[0][-1] == "unknown"
        assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM model_calls") == [(0,)]
        assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM context_assemblies") == [(0,)]
        trace_before = case.container.trace_event_store.read_events(session_id)
        with pytest.raises(RuntimeError, match="仍由另一个 runtime 持有"):
            await case.container.runtime_service.reconcile_stale_executions()
        assert case.container.job_service._jobs[accepted.job_id] is job
        assert job.task is original_task and original_task is not None and not original_task.done()
        assert case.sql(session_id, thread_id,
            "SELECT execution_id,job_id,admission_idempotency_key,admission_payload_hash,outcome "
            "FROM executions") == before
        assert await queue_store.record(session_id, thread_id, accepted.job_id) == queue_record
        assert await queue_store.before_dispatch(queue_record)
        assert case.container.trace_event_store.read_events(session_id) == trace_before
        assert case.sql(session_id, thread_id,
            "SELECT execution_id,job_id,admission_idempotency_key,admission_payload_hash,outcome "
            "FROM executions") == before
        wakeup = case.container.checkpointer.runtime_wakeup_for_admission(
            session_id, thread_id, before[0][2],
        )
        assert wakeup is not None
        repeated = await dispatcher.dispatch(wakeup)
        assert repeated.job_id == accepted.job_id and job.task is original_task
        assert await queue_store.record(session_id, thread_id, accepted.job_id) == queue_record
        trace = case.container.trace_event_store.read_events(session_id)
        own_trace = [event for event in trace if event.job_id == accepted.job_id]
        assert [event.type for event in own_trace].count("job_created") == 1
        assert [event.type for event in own_trace].count("job_started") == 1
        assert all(event.type != "session_interrupted" for event in own_trace)
    finally:
        release.set()
    await _job(case, accepted.job_id)
    terminal_record = await queue_store.record(session_id, thread_id, accepted.job_id)
    assert terminal_record.phase == "terminal" and terminal_record.outcome == "completed"
    assert not await queue_store.before_dispatch(terminal_record)
    assert case.sql(session_id, thread_id,
        "SELECT execution_id,job_id,admission_idempotency_key,admission_payload_hash FROM executions",
    ) == [before[0][:-1]]
    assert case.sql(session_id, thread_id,
        "SELECT COUNT(*) FROM item_catalog WHERE semantic_kind='runtime_notice'",
    ) == [(1,)]
    assert len(case.requests()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
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
    with case.container.thread_initial_execution_service._factory.for_owner_session(session_id) as thread_owner:
        control = thread_owner.control_store
        persisted_boards = control.collaboration_boards(board.team_id)
        assert len(persisted_boards) == 1
        assert json.loads(persisted_boards[0])["members"] == []
        assert not (control.database_path.parent / "teams").exists()
        seed_wire = [r for r in case.requests() if any(
            m["role"] == "user" and "<delegated_task" in _wire_text(m["content"])
            and "private-seed-h1" in _wire_text(m["content"])
            for m in r["messages"]
        )]
        assert seed_wire
        parent_inputs = case.sql(session_id, parent_job["thread_id"],
            "SELECT item_id FROM item_catalog WHERE semantic_kind='runtime_notice'")
        assert len(parent_inputs) == 1
        child_outputs = case.sql(session_id, intent.thread_id,
            "SELECT item_id FROM item_catalog WHERE semantic_kind='assistant_output' AND execution_id=? ORDER BY item_sequence",
            (intent.execution_binding_id,))
        with case.container.thread_initial_execution_service._factory.for_owner_session(session_id) as thread_owner:
            record = thread_owner.control_store.get_thread_creation_record(intent.creation_idempotency_key)
        canonical_report = _assert_generated_notice(
            case, session_id, parent_job["thread_id"], parent_inputs[0][0],
            expected_provenance={"source_session_id": session_id, "source_thread_id": intent.thread_id,
                "delegation_id": record.delegation_id, "source_execution_id": intent.execution_binding_id,
                "source_item_id": child_outputs[-1][0]}, private_result="child-result-h1",
        )
        case.write_actual("h1", {"parent_job": parent_job, "child_job": child_job,
            "request_input": request, "child_executions": child_executions, "team": board.model_dump(mode="json"),
            "parent_runtime_inputs": parent_inputs, "canonical_report": canonical_report,
            "model_request_count": len(case.requests())})


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", [{"terminal": True}], indirect=True)
async def test_h2_goal_terminal_and_interrupt_sources_keep_real_execution(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id = await create_session(case.client, "H2-source-core")
    await case.plan(session_id, [
        _tool_reply("update_goal", {"status": "complete"}, "call_h2_goal"),
        {"content": "H2-goal-completed"},
    ])
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

    terminal_call, = await case.plan(session_id, [
        _tool_reply("exec_command", {"cmd": "sleep 3; printf 'H2-terminal-completed\\n'", "yield_time_ms": 1000}, "call_h2_terminal"),
    ])
    terminal_tool = terminal_call + ":tool-call:call_h2_terminal"
    case.control.arm(terminal_tool, "test.tool_result.committed")
    terminal_accepted = await _send(case, session_id, "H2-terminal：执行后台命令并读取最终输出。")
    try:
        await asyncio.to_thread(case.control.acknowledged, terminal_tool, "test.tool_result.committed")
        terminal = case.tool_result(terminal_tool)
        await case.plan(session_id, [
            {"content": "H2-terminal-backgrounded"},
            _tool_reply("write_stdin", {"session_id": terminal["session_id"], "chars": "", "yield_time_ms": 1000}, "call_h2_stdin"),
            {"content": "H2-terminal-result-ack"},
        ])
    finally:
        case.control.release(terminal_tool, "test.tool_result.committed")
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

    interrupted_call, = await case.plan(session_id, [{"content": "中断后不可采纳的回复"}])
    case.control.arm(interrupted_call, "model.response_ready")
    accepted = await _send(case, session_id, "H2-interrupt：等待用户中断。")
    try:
        await asyncio.to_thread(case.control.acknowledged, interrupted_call, "model.response_ready", 20)
        admission = case.container.checkpointer.execution_admission_for_job(session_id, thread_id=thread_id, job_id=accepted["job_id"])
        for _attempt in range(2):
            submit_checkpoint_reminder(checkpointer=case.container.checkpointer,
                session_id=session_id, thread_id=thread_id, reminder="H2 同一真实执行事件提醒",
                response_metadata={"source": "h2-boundary"}, checkpoint_source="h2-boundary",
                event_identity="h2-stable-event", execution_id=admission.execution_id)
        interrupted = await case.client.post(f"/api/v1/sessions/{session_id}/interrupt")
        assert interrupted.status_code == 200, interrupted.text
    finally:
        case.control.release(interrupted_call, "model.response_ready")
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
    calls = 4 if retry_kind == "custom" else 3
    session_id = await create_session(case.client, f"H3-{retry_kind}")
    responses = [{"content": f"H3-{retry_kind}-final"} for _ in range(calls)]
    if retry_kind == "empty":
        responses[:2] = [{"content": ""}, {"content": ""}]
    elif retry_kind == "custom":
        responses[2] = _tool_reply("invoke_extension_tool", {"tool_name": "test_tool_2", "arguments": {}}, "call_h3_custom")
    await case.plan(session_id, responses)
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
    assert len(case.requests()) == calls
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
    retry_prompts = [_wire_text(value.payload) for value in sorted(frozen_items, key=lambda item: item.item_sequence)]
    for request_index, request in enumerate(case.requests()):
        selected_retry_prompts = [_wire_text(message["content"]) for message in request["messages"]
                                  if _wire_text(message.get("content", "")) in retry_prompts]
        assert selected_retry_prompts == retry_prompts[:min(request_index, 2)]
        assert all(message["role"] == "user" for message in request["messages"]
                   if _wire_text(message.get("content", "")) in retry_prompts)
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
    if origin == "user_turn":
        previous_wire = case.requests()[-1]
        await case.plan(session_id, [{"content": f"H3-{retry_kind}-final"}])
        continued = await _send(case, session_id, "H3 后续用户输入：直接完成。")
        await _job(case, continued["job_id"])
        assert continued["turn_id"] != turn_id
        following_wire = case.requests()[-1]
        assert following_wire["messages"][:len(previous_wire["messages"])] == previous_wire["messages"]
        assert following_wire["tools"] == previous_wire["tools"]
        assert [_wire_text(message["content"]) for message in following_wire["messages"]
                if _wire_text(message.get("content", "")) in retry_prompts] == retry_prompts
        assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM executions") == [(2,)]
        assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM turn_records") == [(2,)]
        assert case.sql(session_id, thread_id,
            "SELECT item_id,execution_id FROM item_catalog WHERE semantic_kind='runtime_notice' AND item_id LIKE '%retry:%' ORDER BY item_sequence") == retry_items
    case.write_actual(f"h3-{retry_kind}", {"job": result, "executions": executions,
        "retry_items": retry_items, "model_calls": model_calls, "assemblies": assemblies,
        "provider_requests": calls, "retry_source_kind": source_kind, "retry_sources": sorted(expected_ids),
        "sealed_bytes_hash": sha256_jcs([snapshot.to_dict() for snapshot in sealed])})


@pytest.mark.asyncio
async def test_e07_reminder_reasons_commit_once_and_cold_restore_without_checkpoint_mutation(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id = await create_session(case.client, "E07-reminder-owner")
    thread_id = await case.container.session_service.resolve_main_thread(session_id)
    await case.plan(session_id, [{"content": "E07 原执行完成"}])
    accepted = await _send(case, session_id, "E07 建立唯一真实 execution")
    await _job(case, accepted["job_id"])
    saver = case.container.checkpointer
    active = saver.execution_admission_for_job(session_id, thread_id=thread_id, job_id=accepted["job_id"])
    checkpoint_headers = case.sql(session_id, thread_id, "SELECT * FROM checkpoints ORDER BY checkpoint_id")
    checkpoint_channels = case.sql(session_id, thread_id, "SELECT * FROM checkpoint_channels ORDER BY checkpoint_id,channel_name")
    calls = len(case.requests())
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
    assert len(case.requests()) == calls
    assert len(saver._storage.storage.read_items(session_id, thread_id, item_ids=item_ids)) == len(reasons)
    case.write_actual("e07-reminder-reasons", {"execution_id": active.execution_id, "sources": sorted(source_ids),
        "canonical_bytes_hash": sha256_jcs([item.to_dict() for item in items]),
        "unchanged_checkpoint_ids": [row[0] for row in checkpoint_headers], "provider_requests": calls})


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", [{"terminal": True, "managed_gateway": True}], indirect=True)
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

    _read_call, final_call = await case.plan(session_id, [
        _tool_reply("write_stdin", {"session_id": terminal_id, "chars": "", "yield_time_ms": 1000}, "call_e05_stdin"),
        {"content": "E05 cold child 已读取真实终端输出"},
    ], thread_id=thread_id)
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
    assert "E05-final" in next(message["content"] for message in reversed(case.request(final_call)["messages"])
                               if message["role"] == "tool" and message.get("name") == "write_stdin")
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
@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_h4_generated_branch_returns_to_frozen_child_thread(source_case: RuntimeSourceCase) -> None:
    case = source_case
    parent_job, _child_job, intent = await _team_child(case)
    session_id, child_thread_id = parent_job["session_id"], intent.thread_id
    main_thread_id = parent_job["thread_id"]
    report_call, = await case.plan(session_id, [{"content": "H4-exact-child-origin-ack"}], thread_id=child_thread_id)
    case.control.arm_match("h4-generated", "thread.runtime.acquired", {"execution_origin": "user_turn"})
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
    generated = await asyncio.to_thread(case.control.acknowledged_match, "h4-generated")
    try:
        assert generated["details"]["session_id"] == generated_session_id
        generated_call, = await case.plan(generated_session_id, [{"content": "H4-generated-result"}], thread_id=generated["details"]["thread_id"])
    finally:
        control_request(json.loads(case.control.environment["BOXTEAM_E2E_TEST_CONTROL"]), {
            "action": "release_match", "barrier_id": "h4-generated",
        })
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
        "SELECT execution.execution_id, turn.final_item_id FROM executions AS execution "
        "JOIN turn_records AS turn ON turn.turn_id=execution.turn_id WHERE execution.job_id=?",
        (generated_job["job_id"],))
    assert len(generated_execution) == 1
    assert generated_execution[0][1] is not None
    generated_outputs = case.sql(generated_session_id, generated_thread_id,
        "SELECT item_id FROM item_catalog WHERE semantic_kind='assistant_output' AND execution_id=? ORDER BY item_sequence",
        (generated_execution[0][0],))
    generated_items = case.container.checkpointer._storage.storage.read_items(
        generated_session_id, generated_thread_id, item_ids=tuple(row[0] for row in generated_outputs),
    )
    assert len(generated_items) > 1
    final_item = next(item for item in generated_items if item.item_id == generated_execution[0][1])
    identity = final_item.projection_identity
    assert identity is not None and identity.content_part_refs
    assert any(
        item.projection_identity is not None
        and item.projection_identity.model_call_id == identity.model_call_id
        and (item.projection_identity.block_id, item.projection_identity.block_index)
        == (f"{identity.model_call_id}:block:{ref.id}", ref.index)
        for item in generated_items for ref in identity.content_part_refs
    )
    assert any(
        relation.relation == "replaces" and relation.target_kind == "projection_message"
        and item.projection_identity is not None
        and item.projection_identity.projection_message_id == relation.target_id
        for relation in final_item.provenance_relations for item in generated_items
    )
    canonical_report = _assert_generated_notice(
        case, session_id, child_thread_id, child_notices[0][0],
        expected_provenance={"source_session_id": generated_session_id, "source_thread_id": generated_thread_id,
            "source_execution_id": generated_execution[0][0], "source_item_id": generated_execution[0][1]},
        private_result="H4-generated-result",
    )
    generated_contexts = [_wire_text(case.request(generated_call)["messages"])]
    assert "H4-private-generator-prompt" in generated_contexts[0]
    report_prompts = [_wire_text(message["content"]) for message in case.request(report_call)["messages"]
                      if message["role"] == "user" and "<generated_session_result" in _wire_text(message["content"])]
    assert len(report_prompts) == 1 and report_prompts[0].count("H4-generated-result") == 1
    report_admission = case.container.checkpointer.execution_admission_for_job(
        session_id, thread_id=child_thread_id, job_id=report_job["job_id"],
    )
    history_response = await case.client.post(
        f"/api/v1/sessions/{session_id}/threads/{child_thread_id}/history",
        json={"execution_id": report_admission.execution_id, "include": ["internal"]},
    )
    assert history_response.status_code == 200, history_response.text
    history = history_response.json()["data"]
    report_history = next(entry for entry in history["internal_executions"]
                          if entry["execution_id"] == report_admission.execution_id)
    assert [part["text"] for part in report_history["display_parts"]] == ["生成分支已结束，主会话正在处理返回结果。"]
    assert all(private not in canonical_json_bytes(history).decode()
               for private in ("H4-generated-result", "H4-private-generator-prompt"))
    repeat = await case.client.post("/api/v1/session-generations/execute", json=payload)
    assert repeat.status_code == 200 and repeat.json()["data"] == result, repeat.text
    assert case.sql(session_id, child_thread_id, "SELECT COUNT(*) FROM executions")[0][0] == 2
    case.write_actual("h4", {"generated_session_id": generated_session_id, "generated_job": generated_job,
        "report_job": report_job, "result": result, "exact_origin_thread_id": child_thread_id,
        "child_runtime_inputs": child_notices, "canonical_report": canonical_report,
        "report_provider_prompts": report_prompts, "report_history": report_history,
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

sessions, source_session, source_thread, target_session, phase, mode, commit_signal_fd = sys.argv[1:]
commit_signal_fd = int(commit_signal_fd)
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
        commit_signal = json.dumps({
            'operation_key': operation_id,
            'target_session_id': target_session,
            'journal_state': result.journal_state,
            'phase': phase,
        }, ensure_ascii=False).encode('utf-8') + bytes((10,))
        assert os.write(commit_signal_fd, commit_signal) == len(commit_signal)
        os.close(commit_signal_fd)
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
            'SELECT cp.checkpoint_id,cp.metadata_json,cp.pending_sends_type,cp.pending_sends_blob,view_state.head_message_sequence '
            'FROM checkpoint_namespace_state namespace JOIN branches branch '
            'ON branch.branch_id=namespace.active_branch_id JOIN checkpoints cp '
            'ON cp.checkpoint_id=branch.head_checkpoint_id JOIN context_views view_state '
            'ON view_state.view_id=cp.view_id WHERE namespace.checkpoint_ns=?', ('',),
        ).fetchone()
        head_checkpoint = None if head is None else {
            'checkpoint_id': head[0], 'metadata': json.loads(head[1]),
            'pending_sends': storage.storage.decode_value(tuple(head[2:4])),
            'head_message_sequence': head[4],
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
def cold_fork_deadlines(source_case: RuntimeSourceCase) -> Iterator[ForkDeadlineRegistry]:
    deadlines = ForkDeadlineRegistry(source_case.artifacts_dir)
    try:
        yield deadlines
    finally:
        deadlines.close()


def _read_fork_commit_signal(descriptor: int) -> bytes:
    try:
        chunks = bytearray()
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                return bytes(chunks)
            chunks.extend(chunk)
            if b"\n" in chunk:
                return bytes(chunks).split(b"\n", maxsplit=1)[0]
    finally:
        os.close(descriptor)


@pytest.fixture
def cold_fork_process(source_case: RuntimeSourceCase, cold_fork_deadlines: ForkDeadlineRegistry):
    case = source_case

    async def invoke(session_id: str, thread_id: str, target_session_id: str = "", *, phase: str = "read", mode: str = "context_fork"):
        command = [sys.executable, "-c", _COLD_FORK_PROCESS,
                   str(case.container.checkpointer._storage.storage.sessions_dir), session_id,
                   thread_id, target_session_id, phase, mode]
        deadline = None
        if phase != "read":
            if not target_session_id:
                raise ValueError("fork 子进程必须指定 target session")
            operation_key = "h5-" + target_session_id
            target = f"{case.container.workspace_id}/{target_session_id}"
            deadline = cold_fork_deadlines.for_operation(target, operation_key)
            deadline.set_stage(f"cold_fork_child:{phase}")
        if deadline is None:
            result = await asyncio.to_thread(
                subprocess.run, [*command, "-1"], cwd=Path.cwd(), capture_output=True,
                text=True, timeout=90,
            )
        else:
            read_fd, write_fd = os.pipe()
            command.append(str(write_fd))
            try:
                process = await asyncio.to_thread(
                    subprocess.Popen, command, cwd=Path.cwd(), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, pass_fds=(write_fd,),
                )
            except BaseException:
                os.close(read_fd)
                raise
            finally:
                os.close(write_fd)
            output_task = asyncio.create_task(asyncio.to_thread(process.communicate))
            signal_task = asyncio.create_task(asyncio.to_thread(_read_fork_commit_signal, read_fd))
            signal_bytes = await deadline.wait(
                signal_task, f"cold_fork_child:{phase}:owner_committed",
            )
            if signal_bytes:
                signal = json.loads(signal_bytes)
                assert signal == {
                    "operation_key": operation_key,
                    "target_session_id": target_session_id,
                    "journal_state": "committed",
                    "phase": phase,
                }, signal
                cold_fork_deadlines.complete(deadline)
            stdout, stderr = await output_task
            result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
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
        if deadline is not None:
            assert signal_bytes, "fork 子进程未报告 durable committed owner 结果"
            assert actual["journals"] == [["committed"]], actual["journals"]
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
@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_h5_cold_refork_and_hard_exit_recovery(source_case: RuntimeSourceCase, cold_fork_process, mode: str) -> None:
    case = source_case
    parent_job, _child_job, intent = await _team_child(case)
    source_session, source_thread = parent_job["session_id"], intent.thread_id
    assert case.gateway_id is not None and case.gateway_skill_source_owner is not None
    with case.container.thread_initial_execution_service._factory.for_owner_session(source_session) as creation_owner:
        creation_record = creation_owner.control_store.get_thread_creation_record(intent.creation_idempotency_key)
        assert creation_record.task_seed is not None
        assert json.loads(creation_record.task_seed)["gateway_id"] == case.gateway_id
        gateway_sources = case.container.resource_platform.gateway_skill_sources
        assert gateway_sources is not None
        assert gateway_sources.snapshot(gateway_id=case.gateway_id) == case.gateway_skill_source_owner.snapshot(
            workspace_id=case.container.workspace_id,
        )
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
    """手工准备 completed_empty 历史，仅证明冷存储复制边界，不作为完整执行验收。"""
    case = source_case
    session_id = await create_session(case.client, "H5 completed_empty seed")
    with case.container.thread_initial_execution_service._factory.for_owner_session(session_id) as creation:
        profile = {"thread_role": "child", "goal_enabled": False}
        created = await creation.create(
            idempotency_key="h5-completed-empty-creation", session_id=session_id, thread_id=None,
            delegation_id=None, initial_state="running", artifact_manifest={}, session_metadata={
                "graph_binding": {"graph_id": "deep-agent", "graph_revision": 1,
                    "graph_schema_hash": "sha256:" + "a" * 64,
                    "capability_profile_hash": compute_capability_profile_hash(capability_profile=profile)},
                "capability_profile": profile,
                "task_seed": {"description": "H5 合法无输出冻结任务", "gateway_id": None, "request_id": None},
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
        assert actual["head_checkpoint"]["head_message_sequence"] == 0
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
    assert third["head_checkpoint"]["head_message_sequence"] == 0
    assert third["executions"] == source["executions"]
    assert third["seed"] == source["seed"] and third["detail_owners"] == source["detail_owners"]
    assert third["historical_execution_rejected"] is True


@dataclass
class RestartRuntimeSourceCase(ModelReplayCase):
    backends: list[BackendProcess]
    mount: MountedWorkspace | None = None

    async def restart(self, *, hard: bool, while_stopped: Callable[[], None] | None = None) -> None:
        previous = self.backends[-1]
        if hard:
            previous.process.kill()
            return_code = await asyncio.to_thread(previous.process.wait, timeout=10)
            assert return_code < 0
        await asyncio.to_thread(close_backend_process, previous)
        if while_stopped is not None:
            assert previous.process.poll() is not None
            while_stopped()
        backend = await asyncio.to_thread(
            start_backend_process, workspace_root=str(self.workspace), port=previous.port,
            log_name=f"runtime-source-restart-{len(self.backends)}",
            asgi_app="tests.support.e2e_control.communication_backend_app:app",
            env_overrides=self.model.environment,
            startup_mount=self.mount,
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
        resolver = get_session_path_resolver(self.workspace / ".boxteam/sessions")
        for name, owner_dir in (("parent", resolver.resolve_thread_node(intent.session_id, main_thread_id)),
                                ("child", resolver.resolve_thread_node(intent.session_id, record.child_thread_id))):
            database = owner_dir / "rollout/index.sqlite"
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                result[name] = {
                    "executions": connection.execute(
                        "SELECT execution_id,execution_origin,turn_id,outcome,job_id FROM executions ORDER BY created_at",
                    ).fetchall(),
                    "admissions": connection.execute(
                        "SELECT execution_id,job_id,admission_idempotency_key,admission_payload_hash FROM executions ORDER BY created_at",
                    ).fetchall(),
                    "admission_commits": connection.execute(
                        "SELECT subject_id,idempotency_key,status FROM storage_commits WHERE commit_kind='execution_admission' ORDER BY commit_id",
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
    artifacts_dir = context.artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    with ExitStack() as services, model_stream_replay(
        runtime_root, runtime_root=runtime_root,
        business_assertion="runtime-source-real-backend-restart-and-owner-recovery",
    ) as model:
        mount = None
        if params.get("native_services"):
            terminal_port, browser_port = ports.port(40), ports.port(42)
            terminal = start_terminal_backend(backend_port=terminal_port, workspace_root=str(workspace), artifacts_dir=artifacts_dir)
            services.callback(terminate_process, terminal)
            browser = start_browser_manager_processes(
                workspace_root=workspace, backend_port=browser_port, frontend_port=ports.port(43),
                artifacts_dir=artifacts_dir, backend_host="127.0.0.1",
            )
            services.callback(close_browser_manager_processes, browser)
            mount = MountedWorkspace(load_or_create_workspace_id(workspace), workspace,
                                     f"http://127.0.0.1:{terminal_port}", f"http://127.0.0.1:{browser_port}")
        backend = await asyncio.to_thread(
            start_backend_process, workspace_root=str(workspace), port=ports.port(0),
            log_name="runtime-source-restart-0",
            asgi_app="tests.support.e2e_control.communication_backend_app:app",
            env_overrides=model.environment,
            startup_mount=mount,
        )
        backends = [backend]
        case: RestartRuntimeSourceCase | None = None
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{backend.port}",
                headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": backend.workspace_id},
                timeout=30,
            ) as client:
                case = RestartRuntimeSourceCase(client, model, workspace, artifacts_dir, backends, mount)
                yield case
        finally:
            for handle in reversed(backends):
                await asyncio.to_thread(close_backend_process, handle)
            if case is not None:
                case.write_actual("h6-wire", {
                    "requests": case.requests(), "backend_pids": [handle.process.pid for handle in backends],
                })



@pytest.mark.asyncio
@pytest.mark.parametrize("restart_source_case", [{"controlled_phases": True}], indirect=True)
async def test_initial_binding_durable_admission_crash_before_bound_recovers_same_job(
    restart_source_case: RestartRuntimeSourceCase,
) -> None:
    """真实 binder 已提交 admission、mark_bound 前硬杀，再以原 key 重启两次。"""
    case = restart_source_case
    session_id = await create_session(case.client, "初始绑定 durable admission 恢复")
    first_call, = await case.plan(session_id, [
        _tool_reply("create_team", {"name": "初始绑定恢复团队"}, "call_binding_crash_team"),
    ])
    team_call = first_call + ":tool-call:call_binding_crash_team"
    case.control.arm(team_call, "test.tool_result.committed")
    catalog = await case.client.get("/api/v1/session-catalog/export")
    assert catalog.status_code == 200, catalog.text
    node = next(node for node in catalog.json()["data"]["items"] if node["node_id"] == session_id)
    assert "storage_relative_path" not in node
    session_dir = get_session_path_resolver(case.workspace / ".boxteam/sessions").resolve_session_node(session_id)
    case.control.arm_match("initial-admission", "thread.initial.admitted_before_bound", {"session_id": session_id})
    await _send(case, session_id, "binding-crash-team-trigger：创建团队并启动成员。")
    try:
        await asyncio.to_thread(case.control.acknowledged, team_call, "test.tool_result.committed")
        team = case.tool_result(team_call)
        await case.plan(session_id, [
            _tool_reply("create_team_member", {
                "team_id": team["team_id"], "role": "reviewer",
                "startup_prompt": "binding-crash-private-seed，返回任务结果。",
            }, "call_binding_crash_member"),
            {"content": "binding-crash-parent-dispatched"}, {"content": "binding-crash-parent-ack"},
        ])
    finally:
        case.control.release(team_call, "test.tool_result.committed")
    barrier = await asyncio.to_thread(case.control.acknowledged_match, "initial-admission")
    before = case.snapshot(session_dir)
    intent = before["intent"]
    assert intent["state"] == "pending"
    assert barrier["operation_id"] == intent["admission_idempotency_key"]
    assert (barrier["details"]["execution_binding_id"], barrier["details"]["job_id"]) == (intent["execution_binding_id"], intent["job_id"])
    with closing(SessionControlStore(session_dir / CONTROL_DATABASE_NAME)) as control:
        claimed = control.get_initial_execution_intent(intent["admission_idempotency_key"])
        assert (claimed.claim_owner, claimed.claim_generation) == ("initial-execution", 1)
    assert case.call_count(session_id, intent["thread_id"]) == 0
    assert before["child"]["turns"] == before["child"]["models"] == []
    assert before["child"]["executions"] == [(intent["execution_binding_id"], "internal", None, "unknown", intent["job_id"])]
    assert len(before["child"]["admissions"]) == 1
    admission = before["child"]["admissions"][0]
    assert admission[:3] == (intent["execution_binding_id"], intent["job_id"], intent["admission_idempotency_key"])
    assert admission[3].startswith("sha256:jcs:v1:") and admission[3] != intent["binding_preimage_hash"]
    assert before["child"]["admission_commits"] == [(intent["execution_binding_id"], "internal-admission:" + intent["admission_idempotency_key"], "committed")]
    case.write_actual("initial-binding-before-kill", {"barrier": barrier, "snapshot": before})

    child_call, = await case.plan(session_id, [{"content": "binding-crash-child-result"}], thread_id=intent["thread_id"])
    await case.restart(hard=True, while_stopped=lambda: control_request(
        json.loads(case.control.environment["BOXTEAM_E2E_TEST_CONTROL"]),
        {"action": "release_match", "barrier_id": "initial-admission"},
    ))
    child_job = await _job(case, intent["job_id"])
    assert (child_job["job_id"], child_job["session_id"], child_job["thread_id"]) == (intent["job_id"], session_id, intent["thread_id"])
    after = case.snapshot(session_dir)
    assert after["intent"] == {**intent, "state": "bound"}
    assert after["child"]["admissions"] == before["child"]["admissions"]
    assert after["child"]["admission_commits"] == before["child"]["admission_commits"]
    assert after["child"]["executions"] == [(intent["execution_binding_id"], "internal", None, "completed", intent["job_id"])]
    assert after["child"]["turns"] == []
    assert case.call_count(session_id, intent["thread_id"]) == 1
    assert "binding-crash-private-seed" in _wire_text(case.request(child_call)["messages"])
    case.write_actual("initial-binding-after-restart", {"snapshot": after, "job": child_job})
    await case.restart(hard=False)
    second = case.snapshot(session_dir)
    assert second["intent"] == after["intent"]
    assert second["child"] == after["child"]
    assert case.call_count(session_id, intent["thread_id"]) == 1
    case.write_actual("initial-binding-second-restart", {"snapshot": second, "processes": [
        {"pid": backend.process.pid, "returncode": backend.process.poll()} for backend in case.backends
    ]})

@pytest.mark.asyncio
async def test_h6_backend_hard_restart_keeps_child_and_runtime_report_identity(
    restart_source_case: RestartRuntimeSourceCase,
) -> None:
    case = restart_source_case
    session_id = await create_session(case.client, "H6 同会话 child 服务重启")
    main = await case.client.get(f"/api/v1/sessions/{session_id}/main-thread")
    assert main.status_code == 200, main.text
    main_thread_id = main.json()["data"]["thread_id"]
    case.control.arm_match("h6-child", "thread.initial.admitted_before_bound", {"session_id": session_id})
    first_call, = await case.plan(session_id, [
        _tool_reply("create_team", {"name": "H6 团队"}, "call_h6_team"),
    ])
    team_call = first_call + ":tool-call:call_h6_team"
    case.control.arm(team_call, "test.tool_result.committed")
    response = await case.client.post(f"/api/v1/sessions/{session_id}/messages", json={
        "message": {"content": "h6-team-trigger：创建团队并启动成员。"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    try:
        await asyncio.to_thread(case.control.acknowledged, team_call, "test.tool_result.committed")
        team = case.tool_result(team_call)
        _member_call, _final_call, report_call = await case.plan(session_id, [
            _tool_reply("create_team_member", {
                "team_id": team["team_id"], "role": "reviewer", "startup_prompt": "h6-private-seed，返回 h6-child-result。",
            }, "call_h6_member"), {"content": "h6-parent-dispatched"}, {"content": "h6-parent-ack"},
        ])
        case.control.arm(report_call, "model.response_ready")
    finally:
        case.control.release(team_call, "test.tool_result.committed")
    child = await asyncio.to_thread(case.control.acknowledged_match, "h6-child")
    try:
        assert child["details"]["thread_id"] != main_thread_id
        await case.plan(session_id, [{"content": "h6-child-result"}], thread_id=child["details"]["thread_id"])
    finally:
        control_request(json.loads(case.control.environment["BOXTEAM_E2E_TEST_CONTROL"]), {
            "action": "release_match", "barrier_id": "h6-child",
        })
    await _job(case, response.json()["data"]["job_id"])
    await asyncio.to_thread(case.control.acknowledged, report_call, "model.response_ready", 45)
    assert "<generated_session_result" in _wire_text(case.request(report_call)["messages"])
    catalog = await case.client.get("/api/v1/session-catalog/export")
    assert catalog.status_code == 200, catalog.text
    node = next(node for node in catalog.json()["data"]["items"] if node["node_id"] == session_id)
    assert "storage_relative_path" not in node
    session_dir = get_session_path_resolver(case.workspace / ".boxteam/sessions").resolve_session_node(session_id)
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
        await case.restart(hard=hard, while_stopped=(
            lambda: case.control.release(report_call, "model.response_ready")
        ) if hard else None)
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
        assert sum("<generated_session_result" in _wire_text(wire["messages"]) for wire in case.requests()) == 1


@pytest.mark.parametrize("source_case", [{"workspace_template": "model_tool_test_workspace"}], indirect=True)
@pytest.mark.asyncio
async def test_replay_and_manual_compaction_http_keep_catalog_main_owner(
    source_case: RuntimeSourceCase,
) -> None:
    case = source_case

    session_id = await create_session(case.client, "Replay 与手动压缩归属")
    await case.plan(session_id, [{"content": "真实 owner 回答：" + prompt} for prompt in (
        "replay-first-private", "replay-second-private", "replay-edited-private", "replay-edited-private",
    )])
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
        guidance = [ref for frame in snapshot.seal_binding.prefix.state.frames for ref in frame.item_refs
                    if ref.source_identity == MCP_GUIDANCE_SOURCE_ID]
        assert len(guidance) == 1
        if ordinal == 0:
            guidance_revision = guidance[0].source_revision
        assert guidance[0].source_revision == guidance_revision
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
