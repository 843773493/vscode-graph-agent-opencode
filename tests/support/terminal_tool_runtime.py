"""用正式服务图、catalog 与真实 acceptance 装配 Terminal 工具测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
)
from app.container import AppContainer, WorkspaceContainerRegistry
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_prefixed_id
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import catalog_provider_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.core.turn_execution_scope import (
    TurnExecutionScope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.main import _workspace_lifespan
from app.schemas.internal_v2.session import SessionCreateRequest

TerminalToolRuntime = tuple[AppContainer, str, ToolInvocationContext]


@asynccontextmanager
async def terminal_tool_runtime(
    workspace_root: Path,
) -> AsyncIterator[TerminalToolRuntime]:
    workspace_id = load_or_create_workspace_id(workspace_root)
    with (
        WorkspaceContainerRegistry(
            mounted_workspaces=MountedWorkspaceRegistry(
                [MountedWorkspace(workspace_id, workspace_root)]
            ),
            project_root=Path.cwd(),
        ) as registry,
        catalog_provider_scope(registry),
    ):
        container = registry.get(workspace_id)
        try:
            async with _workspace_lifespan(container):
                session = await container.session_service.create(
                    SessionCreateRequest(title="真实 Terminal 工具调用")
                )
                thread_id = container.session_catalog.resolver.main_thread_id(
                    session.session_id
                )
                context = ToolInvocationContext(
                    thread_binding=ThreadRuntimeBinding(session.session_id, thread_id)
                )
                with session_thread_owner_scope(
                    MutationIntentOwner(session.session_id, thread_id),
                    langgraph_thread_id=thread_id,
                ):
                    message_id = create_prefixed_id("msg")
                    accepted = container.checkpointer.accept_turn(
                        session.session_id,
                        thread_id=thread_id,
                        accepted_ingress_id=message_id,
                        acceptance_idempotency_key=f"terminal-tool:{message_id}",
                        job_id=create_prefixed_id("job"),
                        admission_idempotency_key=create_prefixed_id("adm"),
                        payload="运行真实 Terminal 工具",
                        payload_kind="text",
                        turn_id=create_prefixed_id("turn"),
                        root_item_id=f"item-{message_id}",
                    )
                    admission = container.checkpointer.execution_admission(
                        session.session_id,
                        thread_id=thread_id,
                        execution_id=accepted["execution_id"],
                    )
                    scope = TurnExecutionScope(admission.execution_id)
                    scope_token = set_current_turn_execution_scope(scope)
                    call_token = context.set_tool_call_id("terminal-real-tool")
                    with execution_admission_scope(
                        admission, langgraph_thread_id=thread_id
                    ):
                        try:
                            yield container, session.session_id, context
                        finally:
                            context.reset_tool_call_id(call_token)
                            reset_current_turn_execution_scope(scope_token)
                            await scope.close()
                            await container.terminal_resource_owner.cleanup_session(
                                session.session_id
                            )
        finally:
            container.workspace_activity_service.store.close()
            container.session_catalog.close()
