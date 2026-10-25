"""用正式服务图、catalog 与真实 acceptance 装配 工作区资源工具测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI

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
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.message import MessageDTO
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.job.service import JobService
from app.workspace_lifecycle import WorkspaceLifespan

WorkspaceToolRuntime = tuple[AppContainer, str, ToolInvocationContext]


@asynccontextmanager
async def workspace_tool_runtime(
    workspace_root: Path,
    *,
    application: FastAPI | None = None,
    startup_mount: MountedWorkspace | None = None,
) -> AsyncIterator[WorkspaceToolRuntime]:
    workspace_id = load_or_create_workspace_id(workspace_root)
    with (
        WorkspaceContainerRegistry(
            mounted_workspaces=MountedWorkspaceRegistry(
                [startup_mount or MountedWorkspace(workspace_id, workspace_root)]
            ),
            project_root=Path.cwd(),
        ) as registry,
        catalog_provider_scope(registry),
    ):
        container = registry.get(workspace_id)
        with ExitStack() as runtime_cleanup:
            runtime_cleanup.callback(container.session_catalog.close)
            runtime_cleanup.callback(container.workspace_activity_service.store.close)
            if application is not None:
                runtime_cleanup.callback(setattr, application.state, "workspace_container_registry", None)
                application.state.workspace_container_registry = registry
            async with WorkspaceLifespan(container):
                session = await container.session_service.create(
                    SessionCreateRequest(title="真实 工作区资源工具调用")
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
                    identity = JobService.reserve_user_execution_identity()
                    now = datetime.now(UTC)
                    container.message_service.accept_user_message(
                        MessageDTO(
                            message_id=message_id, session_id=session.session_id,
                            thread_id=thread_id, role="user", content="运行真实 工作区资源工具",
                            created_at=now, updated_at=now,
                        ),
                        execution_identity=identity,
                        queue_input=ExecutionQueueInput(
                            execution_binding=identity, message_id=message_id,
                            agent_id=session.current_agent_id, provider_id=session.current_provider_id,
                            delivery_policy="after_turn", gateway_id=None, request_id=None,
                            created_at=now.isoformat(),
                        ),
                    )
                    def cancel_accepted_execution() -> None:
                        if not container.checkpointer.mark_job_terminal_status(
                            session_id=session.session_id, thread_id=thread_id,
                            job_id=identity.job_id, status="cancelled",
                        ):
                            raise RuntimeError(
                                f"工作区工具装具未结算自身执行: job_id={identity.job_id}"
                            )

                    async with (
                        AsyncExitStack() as ownership_cleanup,
                        AsyncExitStack() as thread_cleanup,
                    ):
                        # 准入成功即承担结算责任；始终先取消执行，再释放holder和admission。
                        thread_cleanup.callback(cancel_accepted_execution)
                        admission = container.checkpointer.execution_admission_for_job(
                            session.session_id, thread_id=thread_id, job_id=identity.job_id,
                        )
                        ownership_cleanup.enter_context(execution_admission_scope(
                            admission, langgraph_thread_id=thread_id,
                        ))
                        record = container.checkpointer.execution_queue_record(
                            session.session_id, thread_id, identity.job_id,
                        )
                        claim = await container.pending_request_store.claim(record)
                        if claim is None:
                            raise RuntimeError("工作区工具装具未取得新建Thread的执行权")
                        ownership_cleanup.push_async_callback(claim.close)
                        await claim.start()
                        thread_cleanup.push_async_callback(container.terminal_resource_owner.cleanup_session, session.session_id)
                        thread_cleanup.push_async_callback(container.browser_resource_owner.cleanup_session, session.session_id)
                        scope = TurnExecutionScope(admission.execution_id)
                        thread_cleanup.push_async_callback(scope.close)
                        scope_token = set_current_turn_execution_scope(scope)
                        thread_cleanup.callback(reset_current_turn_execution_scope, scope_token)
                        call_token = context.set_tool_call_id("workspace-real-tool")
                        thread_cleanup.callback(context.reset_tool_call_id, call_token)
                        yield container, session.session_id, context
