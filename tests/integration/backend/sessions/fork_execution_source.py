"""恢复验收的来源历史只由产品消息准入和真实 Agent 执行生成。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import pytest

from app.container import AppContainer
from app.core.path_utils import workspace_context
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.session import SessionCreateRequest, SessionDTO
from app.testing.model_stream import build_cassette, chat_completion_frames


@pytest.fixture
async def fork_execution_source(
    production_fork_container: AppContainer, inprocess_model_replay,
) -> SessionDTO:
    container = production_fork_container
    replay, controller = inprocess_model_replay
    controller.ensure_installed()
    # 来源由工作区原生入口执行；fork 的 Gateway 路由身份在装具返回后恢复。
    async with AsyncExitStack() as cleanup:
        cleanup.callback(reset_current_gateway_id, set_current_gateway_id(None))
        # 只登记已成功启动的服务，任何后续失败都按逆序释放。
        await type(container.mcp_catalog_owner).start(container.mcp_catalog_owner)
        cleanup.push_async_callback(type(container.mcp_catalog_owner).shutdown, container.mcp_catalog_owner)
        await type(container.workspace_file_resource_registry).start(container.workspace_file_resource_registry)
        cleanup.push_async_callback(type(container.workspace_file_resource_registry).stop, container.workspace_file_resource_registry)
        await container.team_fanout_service.start()
        cleanup.push_async_callback(container.team_fanout_service.stop)
        # 冷恢复不能依赖 resident graph；先释放执行面，再释放辅助服务。
        cleanup.push_async_callback(type(container.agent_execution_service).shutdown, container.agent_execution_service)
        source = await container.session_service.create(SessionCreateRequest(title="真实执行的 fork 恢复来源"))
        thread = container.session_service.path_resolver.main_thread_id(source.session_id)
        cassette = build_cassette(
            asset_id="fork-recovery-source", model="big-pickle",
            url="http://127.0.0.1:1/v1/chat/completions",
            frames=chat_completion_frames(
                response_id="chatcmpl-fork-recovery", model="big-pickle",
                deltas=({"role": "assistant", "content": "回答一"},), finish_reason="stop",
                usage={"prompt_tokens": 32, "completion_tokens": 8, "total_tokens": 40},
            ),
        )
        replay.control.plan_model_calls(source.session_id, thread, [cassette.raw["interactions"][0]])
        with workspace_context(container.workspace_id, container.workspace_root):
            accepted = await container.session_orchestrator.create_and_run(source.session_id, "问题一")
            async with asyncio.timeout(45):
                while True:
                    job = await container.job_service.get(accepted.job_id)
                    if job.status in {JobStatus.completed, JobStatus.failed, JobStatus.cancelled}:
                        assert job.status == JobStatus.completed, job.error_message
                        break
                    await asyncio.sleep(0.05)
            await container.job_service.drain_finalizations()
        record = container.checkpointer.execution_queue_record(source.session_id, thread, accepted.job_id)
        assert record.phase == "terminal" and record.outcome == "completed"
        assert controller.transport.call_count == 1
        return source
