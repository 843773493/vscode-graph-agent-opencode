"""真实 Agent 工具执行、普通回复和 fork 的留言归属。"""
from __future__ import annotations

import json

import pytest

from app.schemas.internal_v2.session import SessionForkRequest
from tests.integration.backend.sessions import test_thread_runtime_runner as runner
from tests.integration.backend.sessions.test_tool_definition_recovery import _reply

runner_case = runner.runner_case
runtime_case = runner.runtime_case
runner_gateway_context = runner.runner_gateway_context
runner_compaction_gateway_context = runner.runner_compaction_gateway_context
gateway_skill_publication = runner.gateway_skill_publication


async def test_main_agent_leaves_question_without_waiting_and_child_has_no_tool(runtime_case, runner_compaction_gateway_context, gateway_skill_publication, fork_deadlines):
    case = runtime_case
    session, thread = await runner._session(case.runner)
    asked = False

    def reply(payload):
        nonlocal asked
        tool_names = {item["function"]["name"] for item in payload.get("tools", [])}
        if not asked:
            assert "ask_user_async" in tool_names
            asked = True
            return _reply("", tool="ask_user_async", arguments={"markdown": "**需要确认**\n\n部署应使用哪个环境？"})
        return _reply("我继续可独立完成的工作。")

    case.response_chunks = reply
    service = case.runner.container.user_question_service
    channel = service.channel(session, thread)
    subscription = channel.subscribe(label="async-user-questions-test")
    await runner._complete_job(case, session, thread, timeout_seconds=20)
    assert (await subscription.next()).event is True
    page = await service.page(session, thread, before=None, limit=20)
    assert page.count == 1 and "**需要确认**" in page.items[0].markdown
    # 普通消息不绑定答案，也不清除留言。
    await runner._complete_job(case, session, thread, timeout_seconds=20)
    assert (await service.summary(session, thread)).count == 1
    forks = case.runner.container.session_context_fork_service
    point = await forks.source_point(session_id=session, thread_id=thread, mode="last_completed_turn_fork")
    request = SessionForkRequest(idempotency_key="async-question-fork", source_uri=point.source_uri,
        source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
        mode="last_completed_turn_fork", creation_kind="new_child_thread", target_container_uri=point.child_container_uri)
    deadline = fork_deadlines.for_operation(target=f"{case.runner.container.workspace_service.workspace_id}/{request.target_container_uri}", operation_key=request.idempotency_key)
    child = await deadline.wait(forks.fork(request), "SessionContextForkService.fork")
    fork_deadlines.complete(deadline)
    assert (await service.summary(session, child.thread_id)).count == 0
    with pytest.raises(ValueError, match="仅主 Agent"):
        await service.create(session, child.thread_id, question_id="child-question", markdown="child",
            model_call_id="child-call", turn_id=None)
    await service.delete(session, thread, page.items[0].question_id)
    assert (await subscription.next()).event is True
    channel.unsubscribe(subscription)
    service.events.remove_channel_if_unused(channel.name)
    assert (await service.summary(session, thread)).count == 0
    results = [message for message in case.model_requests[1]["messages"] if message.get("role") == "tool"]
    assert json.loads(results[-1]["content"])["status"] == "saved"
