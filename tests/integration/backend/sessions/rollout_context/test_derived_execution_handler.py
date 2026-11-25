"""原注册器、冻结 detail、原图和 SDK 回放验证参数化生产 handler。"""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.instruction_producers import freeze_initial_derived_sources
from app.agents.policy import ToolPolicyResolver
from app.core.path_utils import get_session_catalog_components
from app.core.trace_middleware import (
    get_current_gateway_id,
    reset_current_gateway_id,
    set_current_gateway_id,
)
from app.domain.background_work import BackgroundTaskDescriptor
from app.domain.itemized.hashing import sha256_jcs
from app.runtime.derived_tools import build_derived_tools
from app.runtime.network_proxy import ProxyRoutes
from app.schemas.internal_v2.derived_execution import DerivedExecutionSubmitRequest
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.schemas.internal_v2.turn_search import ThreadItemInspectionRequest
from app.services.business.agent_tool_settings.service import AgentToolSettingsService
from app.services.business.derived_execution_service import DerivedExecutionService
from app.services.business.session_service import SessionService
from app.services.business.session_turn_history.service import SessionTurnHistoryService
from app.services.infrastructure.background_work_store import BackgroundWorkStore
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.rollout_context.activation.body import (
    ResourceActivationBodyStore,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    DetailUnavailableError,
)
from app.services.infrastructure.rollout_history.errors import (
    HistoryItemUnavailableError,
)
from app.services.infrastructure.rollout_history.reader import RolloutHistoryReader
from app.services.infrastructure.session_trace.store import TraceEventStore
from app.services.infrastructure.tool_output_store import ToolOutputStore
from app.services.infrastructure.tool_selection_store import ToolSelectionStore
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)
from app.services.mapping.tool_catalog import tools_to_catalog_definitions
from app.services.orchestration.background_work.derived_execution import (
    DerivedExecutionHandler,
)
from app.services.orchestration.background_work.registry import BackgroundTaskRegistry
from app.services.orchestration.resource_activation import ResourceActivationCoordinator
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from app.testing.model_stream import chat_completion_frames
from configs.network import DirectProxy
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_context as production_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_derived_summary_request import (
    summary_model as summary_model,  # noqa: PLC0414
)
from tests.support.rollout.context_registry import (
    registry_db as registry_db,  # noqa: PLC0414
)
from tests.unit.services.business.session_service_test_support import (
    SessionServiceConfigStub,
)

pytestmark = pytest.mark.parametrize("registry_db", ["protected"], indirect=True)


@pytest.fixture
async def derived_trace(production_context):
    saver = production_context[0]
    store = TraceEventStore(sessions_dir=saver._storage.storage.sessions_dir)
    yield store
    await store.close()


@pytest.fixture
def derived_history(production_context, derived_trace):
    saver = production_context[0]
    catalog = get_session_catalog_components(saver._storage.storage.sessions_dir)
    sessions = SessionService(config_service=SessionServiceConfigStub(), trace_event_store=derived_trace,
        workspace_id=catalog.resolver.workspace_id, path_resolver=catalog.resolver, creation_service=None)
    return SessionTurnHistoryService(checkpointer=saver, session_service=sessions, job_service=None,
        trace_event_store=derived_trace)


@pytest.mark.asyncio
async def test_input_release_preserves_cold_receipt_and_unaccepted_discard_allows_retry(production_context, request, monkeypatch, derived_trace):
    saver, session, thread, connection, _manager, _prepare = production_context
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    catalog = get_session_catalog_components(saver._storage.storage.sessions_dir)
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    arguments = {"task_id": "failed-runtime-task", "plan_id": "failed-runtime-plan", "instruction": "冻结输入",
        "allowed_tool_ids": (), "deadline": deadline, "max_model_calls": 1,
        "submission_hash": sha256_jcs({"input": "failed-runtime"}), "model_recipe": {}}
    ref = saver.freeze_derived_input(session, thread, **arguments)
    handle = SimpleNamespace(task_id=arguments["task_id"], session_id=session, thread_id=thread,
        descriptor=BackgroundTaskDescriptor(kind=DerivedExecutionHandler.kind, version=DerivedExecutionHandler.version,
            payload={"input_ref": ref.to_dict(), "checkpoint_ns": "", "deadline": deadline.isoformat()}))

    def fail_runtime(_):
        raise RuntimeError("实际工具装配失败")

    handler = DerivedExecutionHandler(saver=saver, config=None, activation=None, registry=lambda: None,
        workspace_id=catalog.resolver.workspace_id, workspace_root=context.workspace_root, session_catalog=catalog,
        trace_sink=derived_trace, tool_settings=lambda: None, tool_runtime=fail_runtime)
    with pytest.raises(RuntimeError, match="实际工具装配失败"):
        await handler.step(handle)
    assert connection.execute("SELECT status FROM retention_refs WHERE retention_id=?",
        (f"derived-input:{arguments['plan_id']}",)).fetchone() == ("released",)
    assert saver.freeze_derived_input(session, thread, **arguments) == ref
    from app.services.infrastructure.rollout_context.runtime import detail_store

    class FutureDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=60)

    with monkeypatch.context() as clock:
        clock.setattr(detail_store, "datetime", FutureDateTime)
        assert handler.read_input(handle).task_id == handle.task_id
    body_path = saver._detail_store._files.path(ref)
    body = body_path.read_bytes()
    body_path.unlink()
    with pytest.raises(DetailUnavailableError, match="正文缺失|路径访问失败"):
        handler.read_input(handle)
    with pytest.raises(DetailUnavailableError, match="正文缺失|路径访问失败"):
        saver.freeze_derived_input(session, thread, **arguments)
    body_path.write_bytes(body)
    assert handler.read_input(handle).task_id == handle.task_id
    saver.discard_derived_input(session, thread, plan_id=arguments["plan_id"])
    assert not saver._storage.assemblies.find_referenced_context_details(session, thread, detail_refs=(ref,))
    assert ref in saver.gc_context_plan_details(session, thread, expired_before=datetime.now(UTC) + timedelta(days=60))
    with pytest.raises(DetailUnavailableError, match="不可用"):
        handler.read_input(handle)
    retried = saver.freeze_derived_input(session, thread, **arguments)
    assert retried != ref
    assert saver.read_derived_input(session, thread, retried).submission_hash == arguments["submission_hash"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sealed", [True, False], ids=["sealed-source", "initial-config"])
@pytest.mark.parametrize("summary_model", [("basic-text", "chat_completions", "openai", "/chat/completions")], indirect=True)
async def test_parameterized_handler_publishes_complete_result_and_reconcile_never_resends(
    production_context, summary_model, request, sealed, derived_trace, derived_history,
):
    saver, session, thread, connection, _manager, prepare = production_context
    parent = prepare("parent-derived-api") if sealed else None
    original_parent = None if parent is None else connection.execute(
        "SELECT * FROM executions WHERE execution_id=?", (parent["execution_id"],)).fetchone()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    recipe = {"provider_id": "summary", "model_id": "summary", "model": "big-pickle",
              "endpoint": "https://opencode.ai/zen/v1", "api_key": "${DERIVED_TEST_KEY}", "custom_llm_provider": "openai",
              "api_mode": {"protocol": "chat_completions", "model_info": {}, "supports_reasoning": {}}}
    frozen_ref = saver.freeze_derived_input(session, thread, task_id="derived-handler-task", plan_id="derived-handler-plan",
        instruction="归纳已提交资料", allowed_tool_ids=(), deadline=deadline, max_model_calls=1,
        submission_hash=sha256_jcs({"input": "handler"}), model_recipe=recipe,
        initial_sources=freeze_initial_derived_sources(agent_id="default",
            runtime_config={"system_prompt": "初始冻结规则", "runtime_identity_description": "独立派生"},
            config_revision="config-first", provider_candidates=(("summary", "summary"),)))
    preparations = []

    async def prepare_model_recipe(frozen):
        preparations.append(dict(frozen))
        assert frozen == recipe
        return {**frozen, "api_key": "test-key", "proxy_routes": ProxyRoutes.from_configuration(DirectProxy())}

    activation = ResourceActivationCoordinator(registry=ResourceRegistry(), saver=saver,
        body_store=ResourceActivationBodyStore(saver._storage, saver._detail_store))
    catalog = get_session_catalog_components(saver._storage.storage.sessions_dir)
    handler = DerivedExecutionHandler(saver=saver, config=SimpleNamespace(prepare_model_recipe=prepare_model_recipe),
        activation=activation, registry=lambda: registry,
        workspace_id=catalog.resolver.workspace_id, workspace_root=context.workspace_root, session_catalog=catalog,
        trace_sink=derived_trace, tool_settings=lambda: None, tool_runtime=lambda _: ((), ()))
    registry = BackgroundTaskRegistry(store=BackgroundWorkStore(sessions_dir=saver._storage.storage.sessions_dir),
        residency_tracker=ThreadResidencyTracker(), handlers=(handler,))
    try:
        handle = await registry.spawn_derived(session_id=session, thread_id=thread, agent_id="default",
            task_id="derived-handler-task", descriptor=BackgroundTaskDescriptor(kind=handler.kind, version=handler.version,
                payload={"input_ref": frozen_ref.to_dict(), "checkpoint_ns": "", "deadline": deadline.isoformat()}))
        async with asyncio.timeout(20):
            while (current := registry.get_handle(session, handle.task_id)).status in {"pending", "running"}:
                await asyncio.sleep(0.01)
        assert current.status == "completed", current.metadata
        frozen = handler.read_input(current)
        result = handler.completed_result(frozen)
        assert result["execution_id"] == "derived-execution:derived-handler-plan"
        assert result["output_refs"]
        public_result = DerivedExecutionService._result_dto(result).model_dump(mode="json")
        reference = {"kind": "execution_output", "execution_id": public_result["execution_id"],
            **public_result["output_refs"][0]}
        inspection = await derived_history.inspect_item(session, thread,
            ThreadItemInspectionRequest.model_validate_json(json.dumps({"operation": "content", "content_ref": reference})),
            request_logs=None)
        content = json.loads(inspection.model_dump_json())
        assert content["content_ref"] == reference
        assert [part["content"] for part in content["contents"]] == ["基础回放"]
        assert content["location"]["model_call_id"] == public_result["dispatch_ref"]["model_call_id"]
        assert (content["location"]["item_id"], content["location"]["execution_id"], content["location"]["content_hash"]) == (
            reference["item_id"], reference["execution_id"], reference["content_hash"],
        )
        history = RolloutHistoryReader(saver._context_reader)
        page = history.load(session, thread, TurnHistoryLoadRequest())
        assert result["execution_id"] not in {execution.execution_id for execution in page.internal_executions}
        assert history.load(session, thread, TurnHistoryLoadRequest(
            execution_id=result["execution_id"],
        )).internal_executions == []
        with pytest.raises(HistoryItemUnavailableError):
            history.load(session, thread, TurnHistoryLoadRequest(
                execution_id=result["execution_id"], item_ids=[result["output_refs"][0]["ref_id"]],
                source_view_id=page.source_view_id, source_view_revision=page.source_view_revision,
            ))
        # 原件仍可沿独立结果引用读取，隐藏聊天展示不删除执行事实。
        with saver._context_reader.open_snapshot(session, thread) as source:
            assert {item.item_id for item in saver._context_reader.read_canonical_items(
                source, item_ids=[ref["ref_id"] for ref in result["output_refs"]],
            )} == {ref["ref_id"] for ref in result["output_refs"]}
        if parent is not None:
            assert connection.execute("SELECT * FROM executions WHERE execution_id=?", (parent["execution_id"],)).fetchone() == original_parent
        snapshot = saver.get_context_assembly(session, thread, assembly_id=result["dispatch_ref"]["assembly_id"])
        events = derived_trace.read_events(session)
        admission = saver.execution_admission(session, thread_id=thread, execution_id=result["execution_id"])
        assert [event.type for event in events] == ["job_created", "agent_start", "llm_request", "job_completed"]
        assert {event.job_id for event in events} == {admission.job_id}
        assert (events[0].payload.session_id, events[0].payload.thread_id) == (session, thread)
        assert events[0].payload.message_metadata == {
            "execution_id": admission.execution_id, "task_id": frozen.task_id,
            "plan_id": frozen.plan_id, "checkpoint_ns": "",
            "model": recipe["model"], "source_cutoff": frozen.refs[-1].to_dict(),
            "summary_selection": "no_applicable_ready",
        }
        assert events[2].payload.model == recipe["model"]
        assert ("基础规则第一版" if sealed else "初始冻结规则") in str(snapshot.request_hash_preimage["messages"][0]["content"])
        assert summary_model[1].transport.hit_counts == {0: 1}
        recovered = await handler.reconcile(current)
        assert recovered.completed and recovered.messages == ()
        assert recovered.result == result
        assert derived_trace.read_events(session) == events
        assert len(preparations) == 1
        assert summary_model[1].transport.hit_counts == {0: 1}
    finally:
        await registry.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("sealed", [True, False], ids=["sealed-source", "initial-config"])
@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "same-batch"])
@pytest.mark.parametrize("summary_model", [("basic-text", "chat_completions", "openai", "/chat/completions")], indirect=True)
async def test_public_submission_runs_original_file_tools_and_preserves_source(
    production_context, summary_model, request, monkeypatch, sealed, parallel, derived_trace,
):
    saver, session, thread, connection, _manager, prepare = production_context
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    catalog = get_session_catalog_components(saver._storage.storage.sessions_dir)
    gateway_id = "gateway_" + "1" * 32
    paths = WorkspaceFilePaths(workspace=context.workspace_root, workspace_id=catalog.resolver.workspace_id, bindings=None)
    navigation = ResourceNavigation(workspace_root=context.workspace_root, workspace_id=catalog.resolver.workspace_id, file_paths=paths)
    mutations = WorkspaceTextMutationOwner(paths=paths)
    outputs = ToolOutputStore(workspace_root=context.workspace_root)
    for name, text in (("one.txt", "第一份文件的实际内容"), ("two.txt", "第二份文件的实际内容")):
        (context.workspace_root / name).write_text(text, encoding="utf-8")

    def no_artifact(*_args, **_kwargs):
        raise AssertionError("普通文件读取不应访问 artifact")

    def tool_runtime(frozen):
        return build_derived_tools(frozen, workspace_root=context.workspace_root, resource_navigation=navigation,
            text_mutations=mutations, artifact_reader=no_artifact, output_store=outputs)

    tools, _ = tool_runtime(SimpleNamespace(allowed_tool_ids=("read_file",), gateway_id=gateway_id, session_id=session, thread_id=thread))
    definitions = tools_to_catalog_definitions(tools)
    config = SimpleNamespace(
        get_snapshot=lambda: SimpleNamespace(revision="tools-first"), use_snapshot=lambda _: nullcontext(),
        validate_agent_id=lambda agent_id: agent_id,
        get_tool_policy_resolver=lambda _: ToolPolicyResolver(
            policy_defaults={"execution_enabled": True, "model_visible": True, "confirmation_required": False},
            policy_rules={"by_origin": {}, "by_kind": {}, "by_group": {}, "by_tool": {}}, restrictions={}),
        resolve_agent_model=lambda *_: ModelReferenceDTO(provider_id="summary", model_id="summary"),
        get_agent_runtime_config=lambda *_, **__: {"system_prompt": "初始冻结规则", "runtime_identity_description": "独立派生"},
    )
    settings = AgentToolSettingsService(
        tool_catalog=SimpleNamespace(list_tool_definitions=lambda _: (config.get_snapshot(), definitions)),
        selection_store=ToolSelectionStore(boxteam_root=context.workspace_root / ".boxteam"),
        config_service=config, test_supported_tools={"read_file"})
    _, tool_set = settings.derived_tools("default", ("read_file",))
    parent = prepare("parent-file-tools", supplier=lambda: tool_set) if sealed else None
    original_parent = None if parent is None else connection.execute(
        "SELECT * FROM executions WHERE execution_id=?", (parent["execution_id"],)).fetchone()
    with saver._context_reader.open_snapshot(session, thread, "") as source:
        source_view = saver._storage.checkpoints.active_view_id(source)
    original_view = connection.execute("SELECT * FROM context_views WHERE view_id=?", (source_view,)).fetchone()
    read_calls = []
    navigate = navigation.navigate

    async def recorded_navigation(*args, **kwargs):
        read_calls.append((args[0].uri, get_current_gateway_id()))
        return await navigate(*args, **kwargs)

    monkeypatch.setattr(navigation, "navigate", recorded_navigation)
    controller = summary_model[1]
    scenario = controller.scenario
    base = scenario.cassette.interactions[0]
    model_name = base.request.match["model"]
    recipe = {"provider_id": "summary", "model_id": "summary", "model": model_name,
        "endpoint": "https://opencode.ai/zen/v1", "api_key": "${DERIVED_TEST_KEY}", "custom_llm_provider": "openai",
        "api_mode": {"protocol": "chat_completions", "model_info": {}, "supports_reasoning": {}}}
    config.freeze_model_recipe = lambda _: recipe

    async def prepare_recipe(frozen):
        assert frozen == recipe
        return {**frozen, "api_key": "test-key", "proxy_routes": ProxyRoutes.from_configuration(DirectProxy())}

    config.prepare_model_recipe = prepare_recipe
    roles = ["system", "user", "user"]
    interactions = []
    batches = (("one", "two"),) if parallel else (("one",), ("two",))
    for index, names in enumerate((*batches, ())):
        delta = {"role": "assistant", "tool_calls": [{"index": offset, "id": f"read-{name}", "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": f"{name}.txt"})}}
            for offset, name in enumerate(names)]} if names else {"role": "assistant", "content": "两份文件读取完成。"}
        interactions.append(replace(base, index=index,
            request=replace(base.request, match={**base.request.match, "message_roles": list(roles)}),
            response=replace(base.response, frames=chat_completion_frames(response_id=f"file-tools-{index}", model=model_name,
                deltas=(delta,), finish_reason="tool_calls" if names else "stop"))))
        roles.extend(("assistant", *("tool" for _ in names)))
    controller.scenario = replace(scenario, cassette=replace(scenario.cassette, path=None, interactions=tuple(interactions)))
    activation = ResourceActivationCoordinator(registry=ResourceRegistry(), saver=saver,
        body_store=ResourceActivationBodyStore(saver._storage, saver._detail_store))
    handler = DerivedExecutionHandler(saver=saver, config=config, activation=activation, registry=lambda: registry,
        workspace_id=catalog.resolver.workspace_id, workspace_root=context.workspace_root, session_catalog=catalog,
        trace_sink=derived_trace, tool_settings=lambda: settings, tool_runtime=tool_runtime)
    store = BackgroundWorkStore(sessions_dir=saver._storage.storage.sessions_dir)
    registry = BackgroundTaskRegistry(store=store, residency_tracker=ThreadResidencyTracker(), handlers=(handler,))

    async def get_session(_):
        return SimpleNamespace(current_agent_id="default")

    service = DerivedExecutionService(registry=registry, handler=handler, store=store, saver=saver,
        sessions=SimpleNamespace(get=get_session), config=config, tool_settings=lambda: settings)
    try:
        token = set_current_gateway_id(gateway_id)
        try:
            accepted = await service.submit(DerivedExecutionSubmitRequest(session_id=session, thread_id=thread,
                idempotency_key="read-files", instruction="读取两份文件后回答", allowed_tool_ids=["read_file"],
                timeout_seconds=30, max_model_calls=len(interactions)))
        finally:
            reset_current_gateway_id(token)
        receipt = await service.get(session, thread, accepted.task_id, wait_seconds=20)
        assert receipt.status == "completed", receipt.error
        handle = registry.get_handle(session, accepted.task_id)
        frozen = handler.read_input(handle)
        assert frozen.gateway_id == gateway_id
        assert len(read_calls) == 2 and all(value == gateway_id for _, value in read_calls)
        assert {uri.rsplit("/", 1)[-1] for uri, _ in read_calls} == {"one.txt", "two.txt"}
        with saver._context_reader.open_snapshot(session, thread, "") as source:
            assert saver._storage.checkpoints.active_view_id(source) == source_view
        assert connection.execute("SELECT * FROM context_views WHERE view_id=?", (source_view,)).fetchone() == original_view
        if parent is not None:
            assert connection.execute("SELECT * FROM executions WHERE execution_id=?", (parent["execution_id"],)).fetchone() == original_parent
        result = handler.completed_result(frozen)
        snapshot = saver.get_context_assembly(session, thread, assembly_id=result["dispatch_ref"]["assembly_id"])
        messages = snapshot.request_hash_preimage["messages"]
        assert [message["tool_call_id"] for message in messages if message["role"] == "tool"] == ["read-one", "read-two"]
        assert "第一份文件的实际内容" in str(messages) and "第二份文件的实际内容" in str(messages)
        assert "实际内容" not in receipt.model_dump_json() and "两份文件读取完成" not in receipt.model_dump_json()
        assert connection.execute("SELECT DISTINCT status FROM retention_refs WHERE retention_id=?",
            (f"derived-input:{frozen.plan_id}",)).fetchall() == [("released",)]
        assert saver._storage.assemblies.find_referenced_context_details(session, thread, detail_refs=(handler.descriptor_input(handle.descriptor)[0],))
        assert connection.execute("SELECT attempt FROM model_calls WHERE execution_id=? ORDER BY attempt",
            (result["execution_id"],)).fetchall() == [(index,) for index in range(1, len(interactions) + 1)]
        assert (await handler.reconcile(handle)).result == result
        assert controller.transport.hit_counts == {index: 1 for index in range(len(interactions))}
    finally:
        await registry.stop()
