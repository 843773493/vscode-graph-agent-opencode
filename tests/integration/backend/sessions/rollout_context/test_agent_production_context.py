"""真实 factory、Bridge、Saver 的消费者闭包；模型响应是显式集成桩。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain.agents.factory import _chain_model_call_handlers
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.runtime import ExecutionInfo, Runtime

from app.agents import agent_factory
from app.agents.graph_tool_adapter import extract_agent_tools_by_name
from app.agents.instruction_producers import runtime_instruction_producers
from app.agents.itemized_context_middleware import SealedAssemblyDispatchBridge
from app.agents.policy import ToolPolicyResolver
from app.agents.skill_runtime import (
    WorkspaceSkillsMiddleware,
    build_workspace_skill_catalog,
)
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.tool_selection_store import ToolSelectionStore
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 由当前测试路径隔离真实工作区
)


@pytest.fixture
def factory_context(registry_db, request, monkeypatch):
    saver, session, connection, accepted = registry_db
    thread = saver._storage.main_thread_id(session)
    workspace = TestRunContext.from_test_file(Path(request.node.path)).workspace_root
    skill_file = workspace / ".boxteam" / "skills" / "example" / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text("---\nname: example\ndescription: 集成测试 Skill\n---\nSkill 正文\n")
    catalog = build_workspace_skill_catalog(workspace, registry=ResourceRegistry(), bundled_skill_groups=())
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
    model = FakeListChatModel(responses=["集成桩响应"])
    tools = [StructuredTool.from_function(
        lambda value: value, name=name, description="受信注册工具",
    ) for name in ("create_team", "send_session_message")]
    graph = agent_factory.create_my_deep_agent(
        model=model, instruction_producers=runtime_instruction_producers(
            agent_id="default", content="配置基础说明", config_revision="config:production:1",
            workspace_root=str(workspace), provider_candidates=(("fixture", "model"),),
        ), tool_selection_reader=selection, checkpointer=saver,
        session_id=session, thread_id=thread, agent_id="default", tools=tools,
        background_task_registry=MagicMock(), background_message_bus=MagicMock(),
        job_event_bus=MagicMock(), job_service=MagicMock(), message_service=MagicMock(),
        session_service=MagicMock(), session_orchestrator=MagicMock(),
        session_subagent_service=MagicMock(), session_context_query_service=MagicMock(),
        workspace_session_context_client=MagicMock(), config_service=config,
        workspace_root=workspace, skill_catalog=catalog, middleware=[],
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
            model_chain(incoming, provider_handler)
        return outbound[0]

    return saver, session, thread, connection, selection, manager, captured, registered, dispatch


def test_real_factory_bridge_registers_all_seven_sources_and_uses_sealed_request(factory_context):
    saver, session, thread, connection, _selection, manager, captured, registered, dispatch = factory_context
    assert captured["system_prompt"] is None
    before = manager.prepare_pending()
    assert before is not None
    outbound = dispatch("initial")
    assert outbound.system_message is None
    assert isinstance(outbound.messages[0], SystemMessage)
    assert sum(isinstance(message, SystemMessage) for message in outbound.messages) == 1
    snapshot = saver._storage.list_context_assemblies(session, thread)[-1]
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
    saver, session, thread, _connection, selection, manager, captured, registered, dispatch = factory_context
    first = dispatch("first")
    original = saver._storage.list_context_assemblies(session, thread)[-1]
    selection.apply_changes(agent_id="default", changes={"write_todos": (False, False)})

    def no_live_desired():
        raise AssertionError("sealed retry 不得重读 source/ToolSet desired")

    bridge = captured["middleware"][-1]
    with monkeypatch.context() as retry_patch:
        retry_patch.setattr(manager, "prepare_sources", no_live_desired)
        retry_patch.setattr(bridge, "_tool_set_supplier", no_live_desired)
        retry = dispatch("first")
    assert retry.messages == first.messages and retry.tools == first.tools
    second = dispatch("second")
    changed = saver._storage.list_context_assemblies(session, thread)[-1]
    assert "write_todos" in registered
    assert "write_todos" not in {schema["function"]["name"] for schema in second.tools}
    assert changed.seal_binding.prefix.state.epoch.ordinal == original.seal_binding.prefix.state.epoch.ordinal + 1
    assert "instruction:todo-list" not in {source.source_identity for source in changed.seal_binding.root.sources}
    assert original.tool_set_refs[0].tools == tuple(first.tools)


def test_unregistered_system_prompt_is_rejected_before_seal(factory_context):
    _saver, _session, _thread, connection, _selection, manager, _captured, _registered, dispatch = factory_context
    before = manager.prepare_pending()
    with pytest.raises(RuntimeError, match="instruction-producer-unregistered"):
        dispatch("unregistered", system_message=SystemMessage(content="未登记说明"))
    assert manager.prepare_pending() == before
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (0,)
