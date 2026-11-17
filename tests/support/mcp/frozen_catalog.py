"""MCP 发布目录的隔离工作区与真实 Provider 上下文封存前提。"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.agents.policy import ToolMetadata, ToolPolicyResolver
from app.agents.tool_set_producer import RegisteredToolSetProducer
from app.agents.tools.extension_invocation import create_extension_tool_invoker_tool
from app.core.execution_admission_context import execution_admission_scope
from app.core.path_utils import workspace_context
from app.core.session_thread_context import session_thread_owner_scope
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.resource_activation import (
    ResourceActivationBoundary,
    ResourceActivationPolicySnapshot,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
    ContextSourceOwnerKey,
)
from app.services.infrastructure.tool_selection_store import ToolSelectionStore
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture
def catalog_workspace(request: pytest.FixtureRequest) -> Iterator[Path]:
    context = TestRunContext.from_test_file(Path(request.node.path))
    root = context.runtime_root_for_node(request.node.nodeid) / "workspace"
    prepare_test_workspace(
        workspace_root=root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(),
    )
    workspace_id = load_or_create_workspace_id(root)
    with workspace_context(workspace_id, root):
        yield root


@pytest.fixture
def prepare_tool_context(catalog_runtime, registry_db, frozen_provider_activation, frozen_provider_wire, extension_catalog_policy_resolver):
    owner, _factory, registry, _scope = catalog_runtime
    saver, session, _connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    admission = saver.execution_admission(
        session, thread_id=thread, execution_id=accepted["initial_execution_id"],
    )
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(session, thread), control_state_port=saver,
    )
    envelope = create_extension_tool_invoker_tool(
        owner.get_tools(), catalog_binding_resolver=owner.binding_snapshot,
    )
    producer = RegisteredToolSetProducer(
        agent_id="default",
        selection_reader=ToolSelectionStore(boxteam_root=saver._storage.storage.sessions_dir.parent),
        policy_resolver=ToolPolicyResolver(policy_defaults={}, policy_rules={}, restrictions={}),
        tools=(envelope,), metadata={envelope.name: ToolMetadata(
            tool_id=envelope.name, origin="builtin", kind="default", group_id="extensions",
        )},
    )
    producer.freeze_structured_output_tools({})

    def prepare(
        key, *, context_owner=None, boundary: ResourceActivationBoundary = "model_call",
        checkpoint_ns: str = "", request_messages=(),
        policies_for=extension_catalog_policy_resolver,
        context_source_manager=None,
    ):
        target = saver if context_owner is None else context_owner
        source_manager = context_source_manager
        if source_manager is None:
            source_manager = manager if target is saver else ContextSourceManager(
                owner=ContextSourceOwnerKey(session, thread), control_state_port=target,
            )
        with (
            session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            activation = frozen_provider_activation(
                target, plan_creation_idempotency_key=key, registry=registry,
                checkpoint_ns=checkpoint_ns,
                policy=ResourceActivationPolicySnapshot(
                    default_boundary="turn", boundaries={"mcp_tool_catalog": boundary},
                ),
            )
            result = target.prepare_context_for_provider(
                session, thread, turn_id=admission.turn_id, execution_id=admission.execution_id,
                plan_creation_idempotency_key=key, seal_idempotency_key="seal:" + key,
                request_messages=request_messages, context_source_manager=source_manager,
                extension_policy_resolver=policies_for,
                tool_set_supplier=producer.snapshot,
                checkpoint_ns=checkpoint_ns,
                **frozen_provider_wire("fixture-model", "chat_completions"), **activation,
            )
        return result, activation["activation_snapshot"]

    return prepare
