"""真实消息与 pending reminder 经派发后产生轻量标记，详情仍读原 Item。"""

import asyncio

import httpx
import pytest

from app.api.deps import get_llm_request_log_reader
from app.core.path_utils import workspace_context
from app.services.business.runtime_reminder import submit_runtime_reminder
from tests.integration.backend.sessions.test_cold_rollout_history import (
    history_client as history_client,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    _session,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    recording_runtime_case as recording_runtime_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414
)
from tests.support.api_helpers import with_message_run_operation_id


@pytest.fixture
async def notice_client(history_client: httpx.AsyncClient, recording_runtime_case: RuntimeCase):
    application = history_client._transport.app
    container = recording_runtime_case.runner.container
    application.dependency_overrides[get_llm_request_log_reader] = lambda: container.llm_request_log_reader
    yield history_client


async def test_pending_reminders_are_displayed_once_at_the_consuming_request(
    recording_runtime_case: RuntimeCase, notice_client: httpx.AsyncClient,
) -> None:
    case = recording_runtime_case
    container = case.runner.container
    session_id, thread_id = await _session(case.runner)
    base = f"/api/v1/sessions/{session_id}/threads/{thread_id}"

    async def send(content: str):
        response = await notice_client.post(base + "/messages", json=with_message_run_operation_id({
            "message": {"content": content}, "run": {},
        }))
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        job = container.job_service._jobs[result["job_id"]]
        await asyncio.wait_for(asyncio.shield(job.task), 20)
        assert job.status.value == "completed", job.error_message
        return result

    async def history():
        response = await notice_client.post(base + "/history", json={"direction": "tail"})
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def markers(page):
        return [(turn, part) for turn in page["items"] for part in turn["response_parts"]
                if part["kind"] == "context_notice" and "notice-ui-" in part["context_source_id"]]

    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        await send("先建立真实执行")
        admission = case.model_admissions[-1]
        for index in range(2):
            reminder = f"保留 <system_reminder> 原文 notice-ui-{index}"
            if index == 1:
                # 长提示用于区分聊天有限预览与 Item 全文读取，不截断持久化原文。
                reminder += "".join(f"\n上下文第 {line} 行：保留这条说明的完整原文。" for line in range(1, 81))
                reminder += "\nCOMPLETE_CONTEXT_TAIL"
            submit_runtime_reminder(checkpointer=container.checkpointer, session_id=session_id, thread_id=thread_id,
                reminder=reminder,
                response_metadata={"source": "interrupt"}, checkpoint_source="interrupt",
                event_identity=f"notice-ui-{index}", execution_id=admission.execution_id)
        assert markers(await history()) == []
        second = await send("继续处理")
        second_call = case.model_dispatches[-1].snapshot.model_call_id
        page = await history()
        notices = markers(page)
        assert len(notices) == 2
        assert {turn["turn_id"] for turn, _ in notices} == {second["turn_id"]}
        assert {part["context_model_call_id"] for _, part in notices} == {second_call}
        for _, part in notices:
            assert part["text"] == "" and part["projection"] == "summary"
            response = await notice_client.post(base + "/items/inspect", json={
                "operation": "content", "item_id": part["source"]["item_id"],
            })
            assert response.status_code == 200, response.text
            inspected = response.json()["data"]
            assert inspected["location"]["model_call_id"] == second_call
            assert inspected["location"]["turn_id"] == second["turn_id"]
            assert inspected["contents"][0]["language"] == "plaintext"
            assert "<system_reminder>" in inspected["contents"][0]["content"]
            if "notice-ui-1" in part["context_source_id"]:
                assert "COMPLETE_CONTEXT_TAIL" in inspected["contents"][0]["content"]
        item_id = notices[-1][1]["source"]["item_id"]
        for operation, field in (("request", "request_log"), ("trace", "trace")):
            response = await notice_client.post(base + "/items/inspect", json={
                "operation": operation, "item_id": item_id,
            })
            assert response.status_code == 200, response.text
            inspected = response.json()["data"]
            assert inspected["location"]["model_call_id"] == second_call
            assert inspected[field] is not None
        await send("再次继续，不重复标记旧提示")
        assert len(markers(await history())) == 2
