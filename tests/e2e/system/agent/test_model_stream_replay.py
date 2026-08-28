"""验证 model stream replay 从子进程配置到业务结果的完整链路。"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.support.api_waiters import wait_for_job_done
from tests.support.messages import last_assistant_message
from tests.support.sse import read_sse_events_until
from tests.support.trace import get_trace_payload


@pytest.fixture(scope="module")
def e2e_model_stream_config_path() -> str:
    return str(Path.cwd() / "configs" / "tests" / "model_stream.jsonc")


@pytest.mark.asyncio
async def test_replay_config_runs_full_business_chain(
    client: httpx.AsyncClient,
    is_debug: bool,
) -> None:
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Model stream replay E2E"},
    )
    assert create_response.status_code == 200, create_response.text
    session_id = create_response.json()["data"]["session_id"]

    async with client.stream(
        "GET",
        f"/api/v1/sessions/{session_id}/traces/stream",
        timeout=None,
    ) as stream_response:
        assert stream_response.status_code == 200, await stream_response.aread()
        message_response = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": {
                    "content": (
                        "请先读取当前工作区的 README.md，然后只回复工具调用完成，"
                        "不要补充其它解释。"
                    )
                },
                "run": {
                    "mode": "single_agent",
                    "agent_id": "default",
                },
            },
        )
        assert message_response.status_code == 200, message_response.text
        job_id = message_response.json()["data"]["job_id"]
        events = await read_sse_events_until(
            stream_response,
            lambda event: event.get("type") == "agent_end",
            timeout_seconds=100000 if is_debug else 60,
        )

    job = await wait_for_job_done(client, job_id)
    assert job["status"] in {"completed", "succeeded"}
    event_types = [event.get("type") for event in events]
    assert "agent_end" in event_types
    assert "text_delta" in event_types
    tool_start = next(event for event in events if event.get("type") == "tool_call_start")
    tool_end = next(event for event in events if event.get("type") == "tool_call_end")
    assert get_trace_payload(tool_start)["tool_name"] == "read_file"
    assert get_trace_payload(tool_start)["args"] == {"path": "README.md"}
    assert get_trace_payload(tool_end)["status"] == "success"
    assert "# 统一测试工作区" in str(get_trace_payload(tool_end)["result"])
    reasoning_text = "".join(
        str(get_trace_payload(event).get("text", ""))
        for event in events
        if event.get("type") == "text_delta"
        and get_trace_payload(event).get("kind") == "reasoning"
    )
    assert reasoning_text == (
        "先读取 README，再根据工具结果作答。"
        "已读取 README，整理最终答复。"
    )

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200, messages_response.text
    assert last_assistant_message(messages_response.json()["data"]["items"]) == "工具调用完成"
