"""真实 factory、Bridge、Saver 的消费者闭包；模型响应是显式集成桩。"""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain.agents.factory import _chain_model_call_handlers
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.runtime import ExecutionInfo, Runtime

from app.agents import agent_factory
from app.agents.graph_tool_adapter import extract_agent_tools_by_name
from app.agents.instruction_producers import runtime_instruction_producers
from app.agents.itemized_context_middleware import SealedAssemblyDispatchBridge
from app.agents.policy import ToolPolicyResolver
from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.skill_runtime import (
    WorkspaceSkillsMiddleware,
    build_workspace_skill_catalog,
)
from app.agents.tools.skill_loading import create_skill_load_tool
from app.core.execution_admission_context import execution_admission_scope
from app.core.lifecycle import LifetimeScope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.mcp import McpCatalogOwner
from app.services.infrastructure.resource_platform.bootstrap import (
    bootstrap_resource_platform,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.activation.body import (
    ResourceActivationBodyStore,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.skill_catalog import (
    skill_catalog_from_activation,
)
from app.services.infrastructure.tool_selection_store import ToolSelectionStore
from app.services.orchestration.resource_activation import (
    ResourceActivationCoordinator,
    ResourceActivationError,
)
from app.services.orchestration.resource_activation.contracts import (
    reset_current_execution_resource_activation,
    set_current_execution_resource_activation,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 由当前测试路径隔离真实工作区
)


@pytest.fixture
def skill_control_admission():
    """CSM 消费者测试显式隔离 Session 生命周期准入。"""
    return lambda _name, _mode: nullcontext()


@pytest.fixture
def factory_context(registry_db, request, monkeypatch):
    saver, session, connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    workspace = TestRunContext.from_test_file(Path(request.node.path)).workspace_root
    skill_file = workspace / ".boxteam" / "skills" / "example" / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text("---\nname: example\ndescription: 集成测试 Skill\n---\nSkill 正文\n")
    event_service = EventChannelService()
    owner = McpCatalogOwner(
        raw_config={"servers": {}}, workspace_root=workspace,
        event_service=event_service,
    )
    asyncio.run(owner.start())
    platform = bootstrap_resource_platform(
        workspace_root=workspace,
        event_service=event_service,
    )
    scope = LifetimeScope("production-context-fixture")
    registry = ResourceRegistry(
        publication_lock=owner.publication_lock, capture_validator=owner.validate_resource_capture,
    )
    monkeypatch.setenv("BOXTEAM_DEFAULT_SKILL_GROUPS", "[]")
    coordinator = ResourceActivationCoordinator(
        registry=registry, saver=saver,
        body_store=ResourceActivationBodyStore(saver._storage, saver._detail_store),
    )
    config = MagicMock()
    config.get_tool_policy_resolver.return_value = ToolPolicyResolver(
        policy_defaults={}, policy_rules={}, restrictions={},
    )
    selection = ToolSelectionStore(boxteam_root=workspace / ".boxteam")
    captured = {}
    original_create = agent_factory.create_agent

    def capture_create(*args, **kwargs):
        captured.update(kwargs)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(agent_factory, "create_agent", capture_create)
    # 使用正式 Provider 的真实 bind_tools；回答仍由下方显式集成桩提供。
    model = BoxteamLiteLLMChatModel(
        model="openai/gpt-4.1", api_key="factory-fixture-key",
        api_base="http://127.0.0.1:1/v1", custom_llm_provider="openai",
        streaming=False, max_retries=0,
    )
    tools = [StructuredTool.from_function(
        lambda value: value, name=name, description="受信注册工具",
    ) for name in ("create_team", "send_session_message")]
    sessions = MagicMock()
    sessions.session_context_control_admission.side_effect = lambda *_args, **_kwargs: nullcontext()
    graph = agent_factory.create_my_deep_agent(
        model=model, instruction_producers=runtime_instruction_producers(
            agent_id="default", content="配置基础说明", config_revision="config:production:1",
            workspace_root=str(workspace), provider_candidates=(("fixture", "model"),),
        ), tool_selection_reader=selection, checkpointer=saver,
        session_id=session, thread_id=thread, agent_id="default", tools=tools,
        background_task_registry=MagicMock(), background_message_bus=MagicMock(),
        job_event_bus=MagicMock(), job_service=MagicMock(), message_service=MagicMock(),
        session_service=sessions, session_orchestrator=MagicMock(),
        session_subagent_service=MagicMock(), session_context_query_service=MagicMock(),
        workspace_session_context_client=MagicMock(), config_service=config,
        workspace_root=workspace, workspace_file_resource_registry=platform.file_registry, middleware=[],
        resource_registry=registry, resource_activation=coordinator,
        mcp_catalog_owner=owner, reactor_lifetime_scope=scope,
    )
    bridge = captured["middleware"][-1]
    assert isinstance(bridge, SealedAssemblyDispatchBridge)
    for middleware in captured["middleware"]:
        if isinstance(middleware, WorkspaceSkillsMiddleware):
            middleware.before_agent({}, None, {})
            assert middleware.before_model({}, None) is None
    model_chain = _chain_model_call_handlers([
        middleware.wrap_model_call for middleware in captured["middleware"]
        if middleware.__class__.wrap_model_call is not AgentMiddleware.wrap_model_call
        or middleware.__class__.awrap_model_call is not AgentMiddleware.awrap_model_call
    ])
    manager = bridge._context_source_manager
    admission = saver.execution_admission(
        session, thread_id=thread, execution_id=accepted["initial_execution_id"],
    )
    with (
        session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        parent = asyncio.run(coordinator.freeze_execution(
            admission=admission, policy=ResourceActivationPolicySnapshot(
                default_boundary="turn", boundaries={"mcp_tool_catalog": "model_call"},
            ),
        ))
    registered = extract_agent_tools_by_name(graph)

    def dispatch(task, *, system_message=None):
        incoming = ModelRequest(
            model=model, messages=[], state={"messages": []}, tools=list(registered.values()),
            system_message=system_message, runtime=Runtime(execution_info=ExecutionInfo(
                thread_id=thread, checkpoint_ns="", checkpoint_id="factory-checkpoint",
                task_id=task, node_attempt=1,
            )),
        )
        outbound = []

        def provider_handler(actual):
            outbound.append(actual)
            return ModelResponse(result=[AIMessage(content="集成桩响应")])

        with (
            session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            token = set_current_execution_resource_activation(parent, admission=admission)
            try:
                model_chain(incoming, provider_handler)
            finally:
                reset_current_execution_resource_activation(token)
        return outbound[0]

    try:
        yield (
            saver, session, thread, connection, selection, manager, captured,
            registered, dispatch, platform.file_registry,
        )
    finally:
        asyncio.run(scope.close())
        asyncio.run(platform.close())
        asyncio.run(owner.shutdown())


def test_real_factory_bridge_registers_all_seven_sources_and_uses_sealed_request(factory_context):
    saver, session, thread, connection, _selection, manager, captured, registered, dispatch, _file_registry = factory_context
    assert captured["system_prompt"] is None
    before = manager.prepare_pending()
    assert before is not None
    outbound = dispatch("initial")
    assert outbound.system_message is None
    assert isinstance(outbound.messages[0], SystemMessage)
    assert sum(isinstance(message, SystemMessage) for message in outbound.messages) == 1
    snapshot = saver._storage.assemblies.list_context_assemblies(session, thread)[-1]
    sources = snapshot.seal_binding.root.sources
    assert {source.source_identity for source in sources} == {
        "instruction:agent-instructions", "instruction:runtime-identity",
        "instruction:team-coordination-policy", "instruction:todo-list",
        "instruction:skill-catalog", "instruction:filesystem-rules",
        "instruction:compact-tool-usage",
    }
    assert "集成测试 Skill" in outbound.messages[0].content
    assert "/.boxteam/skills/" not in outbound.messages[0].content
    assert manager.prepare_pending() is None
    assert {schema["function"]["name"] for schema in outbound.tools} == set(registered)
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (1,)
    assert len(snapshot.tool_set_refs) == 1


def test_runtime_selection_hard_rebase_preserves_retry_and_graph_registration(factory_context, monkeypatch):
    saver, session, thread, _connection, selection, manager, captured, registered, dispatch, _file_registry = factory_context
    first = dispatch("first")
    original = saver._storage.assemblies.list_context_assemblies(session, thread)[-1]
    selection.apply_changes(agent_id="default", changes={"write_todos": (False, False)})

    def no_live_desired(*_args: object, **_kwargs: object):
        raise AssertionError("sealed retry 不得重读 source/ToolSet desired")

    bridge = captured["middleware"][-1]
    with monkeypatch.context() as retry_patch:
        retry_patch.setattr(manager, "prepare_sources", no_live_desired)
        retry_patch.setattr(bridge._tool_set_producer, "snapshot", no_live_desired)
        retry = dispatch("first")
    assert retry.messages == first.messages and retry.tools == first.tools
    second = dispatch("second")
    changed = saver._storage.assemblies.list_context_assemblies(session, thread)[-1]
    assert "write_todos" in registered
    assert "write_todos" not in {schema["function"]["name"] for schema in second.tools}
    assert changed.seal_binding.prefix.state.epoch.ordinal == original.seal_binding.prefix.state.epoch.ordinal + 1
    assert "instruction:todo-list" not in {source.source_identity for source in changed.seal_binding.root.sources}
    assert original.tool_set_refs[0].tools == tuple(first.tools)


def test_unregistered_system_prompt_is_rejected_before_seal(factory_context):
    _saver, _session, _thread, connection, _selection, manager, _captured, _registered, dispatch, _file_registry = factory_context
    before = manager.prepare_pending()
    with pytest.raises(RuntimeError, match="instruction-producer-unregistered"):
        dispatch("unregistered", system_message=SystemMessage(content="未登记说明"))
    assert manager.prepare_pending() == before
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (0,)


def test_factory_skill_graph_blocks_required_capture_and_recovers_without_semantic_change(
    factory_context, monkeypatch,
):
    saver, session, thread, _connection, _selection, _manager, captured, _registered, dispatch, files = factory_context
    dispatch("skill-graph-initial")
    bridge = captured["middleware"][-1]
    coordinator = bridge._resource_activation
    registry = coordinator._registry
    resource_id = "skill-entry:workspace:example:activation"
    catalog_id = "skill-entry:workspace:catalog:catalog"
    spec = registry.descriptor(resource_id)
    assert spec.loader_version == "skill-frontmatter:v1"
    assert spec.source_ids == ("skill-source:workspace:example",)
    assert resource_id in registry.descriptor(catalog_id).dependency_resource_ids
    original = registry.snapshot(resource_id)
    source = files.workspace_root / ".boxteam/skills/example/SKILL.md"
    valid = source.read_bytes()
    source.write_bytes(b"---\nname: example\n---\ninvalid\n")
    assert spec.display_uri is not None
    files.refresh(spec.display_uri)
    monkeypatch.setattr(coordinator, "_wait_seconds", 0)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="description"):
            build_workspace_skill_catalog(files.workspace_root, registry=registry, file_registry=files, bundled_skill_groups=())
        assert registry.snapshot(resource_id).retained_revision == original.revision
        assert registry.last_valid_snapshot(resource_id) is original
        with pytest.raises(ResourceActivationError, match="required snapshot"):
            asyncio.run(coordinator._await_required_snapshots(ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={}), boundary="turn"))
    # 已 seal 的 assembly 仍只消费自身冻结正文。
    sealed = saver._storage.assemblies.list_context_assemblies(session, thread)[-1]
    frozen = saver.load_sealed_resource_activation(
        session, thread_id=thread, assembly_id=sealed.assembly_id,
        plan_hash=sealed.plan_hash, request_hash=sealed.request_hash,
    )
    restored = skill_catalog_from_activation(frozen, lambda snapshot, binding: saver.read_resource_activation_payload(snapshot, binding, storage_owner=(session, thread)))
    assert restored is not None and restored.entries["example"].body == original.payload["body"]
    source.write_bytes(valid)
    files.refresh(spec.display_uri)
    recovered = build_workspace_skill_catalog(files.workspace_root, registry=registry, file_registry=files, bundled_skill_groups=())
    assert registry.snapshot(resource_id).available
    assert registry.snapshot(resource_id).revision == original.revision
    assert recovered.catalog_snapshot.available
    asyncio.run(coordinator._await_required_snapshots(ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={}), boundary="turn"))


def test_frozen_skill_tool_uses_durable_body_after_live_catalog_changes(
    factory_context, request, skill_control_admission,
):
    saver, session, thread, _connection, _selection, manager, captured, _registered, dispatch, file_registry = factory_context
    dispatch("skill-catalog-initial")
    sealed = saver._storage.assemblies.list_context_assemblies(session, thread)[-1]
    activation = saver.load_sealed_resource_activation(
        session, thread_id=thread, assembly_id=sealed.assembly_id,
        plan_hash=sealed.plan_hash, request_hash=sealed.request_hash,
    )
    frozen = skill_catalog_from_activation(
        activation, lambda snapshot, binding: saver.read_resource_activation_payload(
            snapshot, binding, storage_owner=(session, thread),
        ),
    )
    assert frozen is not None
    original_body = frozen.entries["example"].body
    workspace = TestRunContext.from_test_file(Path(request.node.path)).workspace_root
    source = workspace / ".boxteam" / "skills" / "example" / "SKILL.md"
    source.write_text("---\nname: example\ndescription: 当前已更新目录\n---\n新的 live 正文\n")
    bridge = captured["middleware"][-1]
    activation_descriptor = bridge._resource_activation._registry.descriptor(
        "skill-entry:workspace:example:activation"
    )
    assert activation_descriptor.display_uri is not None
    file_registry.refresh(activation_descriptor.display_uri)
    live = build_workspace_skill_catalog(
        workspace,
        registry=bridge._resource_activation._registry,
        file_registry=file_registry,
        bundled_skill_groups=(),
    )
    assert live.activation_bodies["example"] != original_body

    reads = []

    def sealed_catalog():
        reads.append(sealed.assembly_id)
        restored = saver.load_sealed_resource_activation(
            session, thread_id=thread, assembly_id=sealed.assembly_id,
            plan_hash=sealed.plan_hash, request_hash=sealed.request_hash,
        )
        return skill_catalog_from_activation(
            restored, lambda snapshot, binding: saver.read_resource_activation_payload(
                snapshot, binding, storage_owner=(session, thread),
            ),
        )

    tool = create_skill_load_tool(manager, activation_snapshot_resolver=sealed_catalog, control_admission=skill_control_admission)
    admission = saver.execution_admission(
        session, thread_id=thread, execution_id=sealed.execution_id,
    )
    with (
        session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        receipt = json.loads(asyncio.run(tool.ainvoke({"name": "example", "mode": "tracked"})))
        assert receipt["status"] == "loaded" and receipt["tracked"]
        assert original_body not in json.dumps(receipt, ensure_ascii=False)
        outbound = dispatch("skill-body-selected")
        texts = [message.content for message in outbound.messages if isinstance(message.content, str)]
        assert any(original_body in text for text in texts)
        assert all("新的 live 正文" not in text for text in texts)
        before_reads = tuple(reads)
        result = json.loads(asyncio.run(tool.ainvoke({"name": "example", "mode": "untrack"})))
        assert result["status"] == "loaded" and not result["tracked"]
        assert tuple(reads) == before_reads

    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        restored = restarted.load_sealed_resource_activation(
            session, thread_id=thread, assembly_id=sealed.assembly_id,
            plan_hash=sealed.plan_hash, request_hash=sealed.request_hash,
        )
        catalog = skill_catalog_from_activation(
            restored, lambda snapshot, binding: restarted.read_resource_activation_payload(
                snapshot, binding, storage_owner=(session, thread),
            ),
        )
        assert catalog == frozen
