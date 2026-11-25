"""公共提交沿真实冻结、SDK 与工具入口验证摘要选择及每步完整容量。"""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.agents.policy import ToolPolicyResolver
from app.core.path_utils import get_session_catalog_components
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.domain.itemized.hashing import sha256_jcs
from app.runtime.derived_tools import build_derived_tools
from app.runtime.network_proxy import ProxyRoutes
from app.schemas.internal_v2.derived_execution import (
    DerivedExecutionSourceDTO,
    DerivedExecutionSubmitRequest,
)
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.business.agent_tool_settings.service import AgentToolSettingsService
from app.services.business.derived_execution_service import DerivedExecutionService
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
from tests.integration.backend.sessions.rollout_context.test_derived_summary_request import (
    summary_runner as summary_runner,  # noqa: PLC0414
)
from tests.support.rollout.context_registry import (
    registry_db as registry_db,  # noqa: PLC0414
)

pytestmark = [
    pytest.mark.parametrize("registry_db", ["protected"], indirect=True),
    pytest.mark.parametrize("summary_model", [("basic-text", "chat_completions", "openai", "/chat/completions")], indirect=True),
]


@pytest.fixture
async def derived_preparation_case(production_context, summary_model, request, monkeypatch):
    saver, session, thread, connection, manager, prepare = production_context
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    catalog = get_session_catalog_components(saver._storage.storage.sessions_dir)
    paths = WorkspaceFilePaths(workspace=context.workspace_root, workspace_id=catalog.resolver.workspace_id, bindings=None)
    navigation = ResourceNavigation(workspace_root=context.workspace_root, workspace_id=catalog.resolver.workspace_id, file_paths=paths)
    mutations = WorkspaceTextMutationOwner(paths=paths)
    outputs = ToolOutputStore(workspace_root=context.workspace_root)
    gateway = "gateway_" + "1" * 32

    def no_artifact(*_args, **_kwargs):
        raise AssertionError("普通文件读取不应访问 artifact")

    def tool_runtime(frozen):
        return build_derived_tools(frozen, workspace_root=context.workspace_root, resource_navigation=navigation,
            text_mutations=mutations, artifact_reader=no_artifact, output_store=outputs)

    tools, _ = tool_runtime(SimpleNamespace(allowed_tool_ids=("read_file",), gateway_id=gateway, session_id=session, thread_id=thread))
    definitions = tools_to_catalog_definitions(tools)
    controller = summary_model[1]
    base = controller.scenario.cassette.interactions[0]
    model_name = base.request.match["model"]
    recipe = {"provider_id": "summary", "model_id": "summary", "model": model_name,
        "endpoint": "https://opencode.ai/zen/v1", "api_key": "${DERIVED_TEST_KEY}", "custom_llm_provider": "openai",
        "api_mode": {"protocol": "chat_completions", "model_info": {}, "supports_reasoning": {}}}
    config = SimpleNamespace(
        get_snapshot=lambda: SimpleNamespace(revision="first-config"), use_snapshot=lambda _: nullcontext(),
        validate_agent_id=lambda value: value,
        get_tool_policy_resolver=lambda _: ToolPolicyResolver(
            policy_defaults={"execution_enabled": True, "model_visible": True, "confirmation_required": False},
            policy_rules={"by_origin": {}, "by_kind": {}, "by_group": {}, "by_tool": {}}, restrictions={}),
        resolve_agent_model=lambda *_: ModelReferenceDTO(provider_id="summary", model_id="summary"),
        freeze_model_recipe=lambda _: recipe,
        get_agent_runtime_config=lambda *_, **__: {"system_prompt": "初始冻结规则", "runtime_identity_description": "独立派生"},
    )

    async def prepare_recipe(frozen):
        return {**frozen, "api_key": "test-key", "proxy_routes": ProxyRoutes.from_configuration(DirectProxy())}

    config.prepare_model_recipe = prepare_recipe
    settings = AgentToolSettingsService(
        tool_catalog=SimpleNamespace(list_tool_definitions=lambda _: (config.get_snapshot(), definitions)),
        selection_store=ToolSelectionStore(boxteam_root=context.workspace_root / ".boxteam"),
        config_service=config, test_supported_tools={"read_file"})
    activation = ResourceActivationCoordinator(registry=ResourceRegistry(), saver=saver,
        body_store=ResourceActivationBodyStore(saver._storage, saver._detail_store))
    trace = TraceEventStore(sessions_dir=saver._storage.storage.sessions_dir)
    handler = DerivedExecutionHandler(saver=saver, config=config, activation=activation, registry=lambda: registry,
        workspace_id=catalog.resolver.workspace_id, workspace_root=context.workspace_root, session_catalog=catalog,
        trace_sink=trace, tool_settings=lambda: settings, tool_runtime=tool_runtime)
    store = BackgroundWorkStore(sessions_dir=saver._storage.storage.sessions_dir)
    registry = BackgroundTaskRegistry(store=store, residency_tracker=ThreadResidencyTracker(), handlers=(handler,))

    async def get_session(_):
        return SimpleNamespace(current_agent_id="default")

    service = DerivedExecutionService(registry=registry, handler=handler, store=store, saver=saver,
        sessions=SimpleNamespace(get=get_session), config=config, tool_settings=lambda: settings)
    requests = []
    overflow = [False]

    def responses(use_tool=False, *, denied_then_answer=False):
        delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "read-large", "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": "large.txt"})}}]} if use_tool else {
                "role": "assistant", "content": "完整派生答案。"}
        match = {"model": model_name, "stream": True}
        if denied_then_answer:
            match["message_roles"] = ["system", "user", "user"]
        interaction = replace(base, request=replace(base.request, match=match),
            response=replace(base.response, frames=chat_completion_frames(response_id="derived-preparation",
                model=model_name, deltas=(delta,), finish_reason="tool_calls" if use_tool else "stop")))
        interactions = (interaction,)
        if denied_then_answer:
            interactions += (replace(base, index=1, request=replace(base.request, match={"model": model_name,
                "stream": True, "message_roles": ["system", "user", "user", "assistant", "tool"]}),
                response=replace(base.response, frames=chat_completion_frames(response_id="denied-answer", model=model_name,
                    deltas=({"role": "assistant", "content": "拒绝未授权工具后完成回答。"},), finish_reason="stop"))),)
        controller.scenario = replace(controller.scenario, cassette=replace(controller.scenario.cassette, path=None, interactions=interactions))
        # transport 在首次安装时冻结 scenario，必须先配置真实响应，再接入请求观察。
        controller.ensure_installed()
        dispatch = controller.transport.handle_async_request

        async def capture(incoming, **kwargs):
            payload = json.loads(incoming.content)
            requests.append(payload)
            if overflow[0]:
                return httpx.Response(400, json={"error": {"message": "maximum context length exceeded",
                    "type": "invalid_request_error", "code": "context_length_exceeded", "param": "messages"}}, request=incoming)
            return await dispatch(incoming, **kwargs)

        monkeypatch.setattr(controller.transport, "handle_async_request", capture)

    def append(key, text):
        saver.accept_turn(session, thread_id=thread, accepted_ingress_id=key, acceptance_idempotency_key=f"accept:{key}",
            job_id=f"job:{key}", admission_idempotency_key=f"admit:{key}", payload=text, payload_kind="text")

    async def submit(key, *, allowed=(), source=None, calls=1):
        token = set_current_gateway_id(gateway)
        try:
            return await service.submit(DerivedExecutionSubmitRequest(session_id=session, thread_id=thread,
                idempotency_key=key, instruction="只分析首次选定资料", source=source, allowed_tool_ids=list(allowed),
                timeout_seconds=30, max_model_calls=calls))
        finally:
            reset_current_gateway_id(token)

    try:
        yield SimpleNamespace(**locals())
    finally:
        await registry.stop()
        await trace.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_state", ["none", "generating", "ready", "inapplicable"])
async def test_public_freeze_selects_ready_once_and_preserves_prefix_tail_and_concurrent_retry(
    derived_preparation_case, summary_runner, summary_state,
):
    case = derived_preparation_case
    saver, session, thread = case.saver, case.session, case.thread
    case.recipe["api_mode"]["model_info"] = {"max_input_tokens": 20_000}
    case.append("covered-source", "早期唯一约束：" + "original source " * 80)
    _, tool_set = case.settings.derived_tools("default", ("read_file",))
    parent = case.prepare("derived-prefix", supplier=lambda: tool_set)
    covered = tuple(saver.read_canonical_items(session, thread))
    candidate = None
    if summary_state != "none":
        candidate = saver._storage.compactions.freeze(session, thread, candidate_id="ready-source", task_id="summary-source",
            item_ids=[item.item_id for item in covered], model_config_hash=sha256_jcs({"model": "frozen"}))
    case.responses()
    summary = None
    if summary_state in {"ready", "inapplicable"}:
        generated = await summary_runner(candidate=candidate, items=covered)
        saver._storage.compactions.publish_result(candidate, output_refs=generated.output_refs, dispatch_ref=generated.dispatch_ref)
        summary = saver._storage.compactions.read_result(session, thread, candidate.candidate_id)
    case.append("closed-tail", "来源 seal 之后的完整尾部")
    source = None if summary_state != "inapplicable" else DerivedExecutionSourceDTO(
        item_ids=[saver.read_canonical_items(session, thread)[-1].item_id])
    first, concurrent = await asyncio.gather(case.submit("same-key", source=source), case.submit("same-key", source=source))
    assert first.task_id == concurrent.task_id
    handle = case.registry.get_handle(session, first.task_id)
    frozen = case.handler.read_input(handle)
    cutoff = frozen.refs[-1]
    deadline = frozen.deadline
    case.append("later-append", "受理之后不加入本次派生")
    expected_view = case.connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchone()
    case.recipe["model"] = "hot-updated-default"
    again = await case.submit("same-key", source=source)
    receipt = await case.service.get(session, thread, first.task_id, wait_seconds=20)
    assert receipt.status == "completed", receipt.error
    assert again.task_id == first.task_id
    assert case.handler.read_input(handle) == frozen and frozen.deadline == deadline
    assert frozen.model_recipe["model"] == case.model_name
    assert (frozen.summary_result_ref is not None) == (summary_state == "ready")
    result = case.handler.completed_result(frozen)
    snapshot = saver.get_context_assembly(session, thread, assembly_id=result["dispatch_ref"]["assembly_id"])
    wire = snapshot.request_hash_preimage
    assert wire == case.requests[-1]
    assert "来源 seal 之后的完整尾部" in str(wire)
    assert "受理之后不加入本次派生" not in str(wire)
    assert wire["tools"] == parent["snapshot"].request_hash_preimage["tools"]
    assert wire["messages"][0] == parent["snapshot"].request_hash_preimage["messages"][0]
    assert ("早期唯一约束" in str(wire)) == (summary_state in {"none", "generating"})
    if summary_state == "ready":
        assert summary.text in str(wire)
        assert case.connection.execute("SELECT applied_view_id FROM context_compaction_candidates WHERE candidate_id=?",
            (candidate.candidate_id,)).fetchone() == (None,)
    elif source is None:
        assert wire["messages"][:len(parent["snapshot"].request_hash_preimage["messages"])] == parent["snapshot"].request_hash_preimage["messages"]
    assert len(case.requests) == (2 if summary is not None else 1)
    assert case.controller.transport.hit_counts == {0: len(case.requests)}
    with case.store.control(session, read_only=True) as control:
        assert len(control.list_background_tasks()) == 1
    assert case.connection.execute("SELECT COUNT(*) FROM context_compaction_candidates").fetchone() == (int(candidate is not None),)
    events = case.trace.read_events(session)
    metadata = events[0].payload.message_metadata
    assert metadata["source_cutoff"] == cutoff.to_dict()
    assert metadata["model"] == case.model_name
    assert metadata["summary_selection"] == ("ready_replacement" if summary_state == "ready" else "no_applicable_ready")
    assert expected_view == case.connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity_case", ["unknown", "known-overflow", "upstream-overflow", "tool-overflow"])
async def test_public_capacity_checks_first_and_tool_requests_without_compaction_or_hidden_retry(
    derived_preparation_case, capacity_case,
):
    case = derived_preparation_case
    _, tool_set = case.settings.derived_tools("default", ("read_file",))
    case.prepare("capacity-parent", supplier=lambda: tool_set)
    case.responses(capacity_case == "tool-overflow")
    if capacity_case == "known-overflow":
        case.recipe["api_mode"]["model_info"] = {"max_input_tokens": 1}
    elif capacity_case == "tool-overflow":
        case.recipe["api_mode"]["model_info"] = {"max_input_tokens": 3000}
        (case.context.workspace_root / "large.txt").write_text("实际工具返回的完整载荷 " * 1400, encoding="utf-8")
    case.overflow[0] = capacity_case == "upstream-overflow"
    accepted = await case.submit("capacity", allowed=("read_file",) if capacity_case == "tool-overflow" else (), calls=2)
    handle = case.registry.get_handle(case.session, accepted.task_id)
    frozen = case.handler.read_input(handle)
    receipt = await case.service.get(case.session, case.thread, accepted.task_id, wait_seconds=20)
    events = case.trace.read_events(case.session)
    assert events[0].payload.message_metadata["source_cutoff"] == frozen.refs[-1].to_dict()
    assert events[0].payload.message_metadata["model"] == case.model_name
    assert events[0].payload.message_metadata["summary_selection"] == "no_applicable_ready"
    assert case.connection.execute("SELECT COUNT(*) FROM context_compaction_candidates").fetchone() == (0,)
    with case.store.control(case.session, read_only=True) as control:
        assert len(control.list_background_tasks()) == 1
    assert len(case.requests) == (0 if capacity_case == "known-overflow" else 1)
    if capacity_case == "unknown":
        assert receipt.status == "completed", receipt.error
        assert [event.type for event in events] == ["job_created", "agent_start", "llm_request", "job_completed"]
    else:
        assert receipt.status == "failed", receipt.error
        assert events[-1].type == "job_failed"
        if capacity_case == "upstream-overflow":
            assert "maximum context length exceeded" in events[-1].payload.error
            assert "'max_input_tokens': None" in events[-1].payload.error and "'context_window': None" in events[-1].payload.error
            assert "input_estimate=not_recorded; count_quality=not_recorded" in events[-1].payload.error
        else:
            assert "context-capacity-exceeded" in events[-1].payload.error
            assert "input_estimate=" in events[-1].payload.error and "count_quality=" in events[-1].payload.error
            assert "not_recorded" not in events[-1].payload.error
        if capacity_case == "tool-overflow":
            assert case.connection.execute("SELECT COUNT(*) FROM item_catalog WHERE semantic_kind='tool_result'").fetchone() == (1,)
            assert case.connection.execute("SELECT COUNT(*) FROM model_calls WHERE execution_id=?",
                (case.handler.execution_id(frozen),)).fetchone() == (1,)
    assert (case.handler.completed_result(frozen) is not None) == (capacity_case == "unknown")


@pytest.mark.asyncio
async def test_public_default_empty_authorization_pairs_denial_without_loading_or_running_file_tools(
    derived_preparation_case, monkeypatch,
):
    case = derived_preparation_case
    _, tool_set = case.settings.derived_tools("default", ("read_file",))
    parent = case.prepare("denied-tools-parent", supplier=lambda: tool_set)
    case.responses(True, denied_then_answer=True)
    navigations = []
    original_view = case.connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchone()

    async def forbidden_navigation(*_args, **_kwargs):
        navigations.append(_args)
        raise AssertionError("空授权不得实际读取文件")

    monkeypatch.setattr(case.navigation, "navigate", forbidden_navigation)
    accepted = await case.submit("default-empty-tools", calls=2)
    handle = case.registry.get_handle(case.session, accepted.task_id)
    frozen = case.handler.read_input(handle)
    assert frozen.allowed_tool_ids == ()
    assert case.tool_runtime(frozen) == ((), ())
    receipt = await case.service.get(case.session, case.thread, accepted.task_id, wait_seconds=20)
    assert receipt.status == "completed", receipt.error
    assert navigations == []
    assert len(case.requests) == 2
    for wire in case.requests:
        assert wire["tools"] == parent["snapshot"].request_hash_preimage["tools"]
    second = case.requests[1]
    tool, = (message for message in second["messages"] if message["role"] == "tool")
    assert tool["tool_call_id"] == "read-large" and "derived-tool-denied" in str(tool["content"])
    assert second["messages"][-2]["tool_calls"][0]["id"] == tool["tool_call_id"]
    assert "完整派生答案" not in str(second)
    assert case.connection.execute("SELECT COUNT(*) FROM item_catalog WHERE semantic_kind='tool_result'").fetchone() == (1,)
    assert case.connection.execute("SELECT attempt FROM model_calls WHERE execution_id=? ORDER BY attempt",
        (case.handler.execution_id(frozen),)).fetchall() == [(1,), (2,)]
    assert case.connection.execute("SELECT COUNT(*) FROM context_compaction_candidates").fetchone() == (0,)
    assert case.connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchone() == original_view
