"""冷上下文读取不借用执行准入的私有 notice 过滤。"""

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.services.business.message_service import MessageService
from tests.integration.backend.sessions.rollout_context.test_runtime_notice_consumption import (
    notice_case as notice_case,  # noqa: PLC0414 - 工作区按当前正式测试文件隔离
)


@pytest.mark.asyncio
async def test_cold_internal_history_survives_restart_while_user_execution_excludes_it(notice_case):
    case = notice_case
    internal, intent = case.internal("cold-history")
    prepared = case.prepare(internal, "cold-history-internal")
    notice = case.saver.accepted_internal_message(
        case.session, thread_id=case.thread, job_id="job-cold-history",
        message_id=intent.message_id, intent=intent,
    )
    response = AIMessage(content="已完成的内部回复", id="cold-internal-response",
                         response_metadata={"model_call_id": prepared.model_call_id})
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "checkpoint-cold-internal-history"
    checkpoint["channel_values"] = {"messages": [notice, response]}
    checkpoint["channel_versions"] = {"messages": checkpoint["id"]}
    with case.scope(internal):
        case.saver.put(
            {"configurable": {"thread_id": case.thread, "checkpoint_ns": ""}},
            checkpoint, {"source": "loop", "step": 1}, checkpoint["channel_versions"],
        )
    case.restart()
    service = MessageService(main_thread_resolver=lambda _: case.thread, checkpointer=case.saver)
    path = case.saver._storage.storage.jsonl_path(case.session, thread_id=case.thread)
    before = path.read_bytes()
    state = await service.get_agent_context_state(case.session, case.thread)
    assert state["checkpoint_id"] == checkpoint["id"]
    assert state["raw_message_count"] == 2
    assert [record["content"] for record in state["records"]] == [notice.content, response.content]
    assert path.read_bytes() == before
    assert case.rows() == []

    accepted, user = case.user("cold-history-user")
    frozen = case.metadata(accepted)
    with case.scope(user):
        executing = await service.get_agent_context_state(case.session, case.thread)
    assert [record["content"] for record in executing["records"]] == ["真正的用户输入"]
    assert case.rows() == []
    assert case.metadata(accepted) == frozen
    after_acceptance = path.read_bytes()
    case.restart()
    cold = MessageService(main_thread_resolver=lambda _: case.thread, checkpointer=case.saver)
    reread = await cold.get_agent_context_state(case.session, case.thread)
    assert [record["content"] for record in reread["records"]][:2] == [notice.content, response.content]
    assert reread["records"][-1]["content"] == "真正的用户输入"
    assert path.read_bytes() == after_acceptance
    assert case.metadata(accepted) == frozen
    assert case.rows() == []
