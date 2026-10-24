"""恢复验收的来源历史只由产品消息准入和真实 Agent 执行生成。"""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import pytest

from app.container import AppContainer
from app.core.path_utils import workspace_context
from app.core.trace_middleware import (
    reset_current_gateway_id,
    set_current_gateway_id,
)
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.schemas.internal_v2.attachment import AttachmentRef
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.message import (
    MessageCreateRequest,
    MessageRunAccepted,
    MessageRunRequest,
    RunOptions,
)
from app.schemas.internal_v2.session import SessionCreateRequest, SessionDTO
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext


def _source_interactions(*, tool_loop: bool) -> list[dict[str, object]]:
    replies: list[dict[str, object]] = []
    if tool_loop:
        for identifiers in (("fork-read-a", "fork-read-b"), ("fork-read-c",)):
            replies.append({"tool_calls": [{
                "index": index, "id": identifier, "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "fork-source.txt"})},
            } for index, identifier in enumerate(identifiers)]})
    replies.append({"content": "回答一"})
    return [build_cassette(
        asset_id="fork-recovery-source", model="big-pickle",
        url="http://127.0.0.1:1/v1/chat/completions",
        frames=chat_completion_frames(
            response_id=f"chatcmpl-fork-recovery-{index}", model="big-pickle",
            deltas=({"role": "assistant", **reply},),
            finish_reason="tool_calls" if "tool_calls" in reply else "stop",
            usage={"prompt_tokens": 32, "completion_tokens": 8, "total_tokens": 40},
        ),
    ).raw["interactions"][0] for index, reply in enumerate(replies)]


@pytest.fixture
async def fork_execution_message(
    inprocess_model_replay, request: pytest.FixtureRequest,
):
    """按需启动真实执行面；来源和副本续写共用 model_stream 与产品消息入口。"""
    replay, controller = inprocess_model_replay
    controller.ensure_installed()

    async def execute(
        container: AppContainer, session_id: str, *, content: str,
        attachments: list[AttachmentRef] | None = None, gateway_id: str | None = None,
        tool_loop: bool = False, start_fanout: bool = True,
    ) -> MessageRunAccepted:
        async with AsyncExitStack() as cleanup:
            cleanup.callback(reset_current_gateway_id, set_current_gateway_id(gateway_id))
            if gateway_id is not None:
                # 仅隔离无关 Skill HTTP 传输；发布与接受仍走原 Gateway/Workspace owner。
                context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
                skills = GatewaySkillSourceOwner(gateway_id=gateway_id, skills_root=context.boxteam_home / "skills")
                await skills.start()
                cleanup.push_async_callback(skills.close)
                sources = container.resource_platform.gateway_skill_sources
                assert sources is not None

                def skill_response(request: httpx.Request) -> httpx.Response:
                    assert request.url.path == f"/api/gateway/skill-source-connections/{gateway_id}/workspaces/{container.workspace_id}"
                    request_id = request.headers.get("X-Request-ID", "cold-fork-independent-skill-poll")
                    return httpx.Response(200, headers={"X-Request-ID": request_id}, json={
                        "code": 0, "data": skills.snapshot(workspace_id=container.workspace_id).model_dump(mode="json"),
                        "request_id": request_id,
                    })

                sources._state(gateway_id).client = httpx.AsyncClient(transport=httpx.MockTransport(skill_response))
                cleanup.push_async_callback(sources.close)
            # 每个成功启动都有逆序释放；失败不留下半初始化的执行面。
            await type(container.mcp_catalog_owner).start(container.mcp_catalog_owner)
            cleanup.push_async_callback(type(container.mcp_catalog_owner).shutdown, container.mcp_catalog_owner)
            await type(container.workspace_file_resource_registry).start(container.workspace_file_resource_registry)
            cleanup.push_async_callback(type(container.workspace_file_resource_registry).stop, container.workspace_file_resource_registry)
            if start_fanout:
                await container.team_fanout_service.start()
                cleanup.push_async_callback(container.team_fanout_service.stop)
            cleanup.push_async_callback(type(container.agent_execution_service).shutdown, container.agent_execution_service)
            thread = container.session_service.path_resolver.main_thread_id(session_id)
            calls_before = controller.transport.call_count
            interactions = _source_interactions(tool_loop=tool_loop)
            replay.control.plan_model_calls(session_id, thread, interactions)
            with workspace_context(container.workspace_id, container.workspace_root):
                accepted = await container.session_orchestrator.create_message(session_id, MessageRunRequest(
                    message=MessageCreateRequest(content=content, attachments=attachments or []), run=RunOptions(),
                ))
                async with asyncio.timeout(45):
                    while True:
                        job = await container.job_service.get(accepted.job_id)
                        if job.status in {JobStatus.completed, JobStatus.failed, JobStatus.cancelled}:
                            assert job.status == JobStatus.completed, job.error_message
                            break
                        await asyncio.sleep(0.05)
                await container.job_service.drain_finalizations()
            record = container.checkpointer.execution_queue_record(session_id, thread, accepted.job_id)
            assert record.phase == "terminal" and record.outcome == "completed"
            assert controller.transport.call_count == calls_before + len(interactions)
            return accepted

    return execute


@pytest.fixture
async def fork_execution_source(
    production_fork_container: AppContainer, fork_execution_message, request: pytest.FixtureRequest,
) -> SessionDTO:
    source = await production_fork_container.session_service.create(SessionCreateRequest(title="真实执行的 fork 恢复来源"))
    with_attachment = getattr(request, "param", False) is True
    tool_loop = getattr(request, "param", False) == "tools"
    if tool_loop:
        (production_fork_container.workspace_root / "fork-source.txt").write_text("fork-source-body\n")
    attachments = [AttachmentRef(
        file_id="inline:cold-fork.txt", name="cold-fork.txt", content_type="text/plain",
        data_url="data:text/plain;base64," + base64.b64encode("冷恢复附件正文".encode()).decode("ascii"),
    )] if with_attachment else []
    await fork_execution_message(
        production_fork_container, source.session_id, content="问题一", attachments=attachments, tool_loop=tool_loop,
        gateway_id="gateway_bc453186347b952303da1c0634d46150" if with_attachment or tool_loop else None,
    )
    return source
