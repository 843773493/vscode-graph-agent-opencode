"""验证 Responses 默认配置的 reasoning、工具循环和最终文本。"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.testing.model_stream import load_scenario
from tests.support.api_waiters import wait_for_job_done
from tests.support.messages import last_assistant_message
from tests.support.paths import output_root_for_test
from tests.support.sse import read_sse_events_until
from tests.support.trace import get_trace_payload

FIXTURE_ROOT = Path.cwd() / "tests" / "fixtures" / "model_stream"
SCENARIO_ID = "responses-reasoning-tool"


@pytest.fixture(scope="module")
def e2e_model_stream_config_path() -> str:
    return str(Path.cwd() / "configs" / "tests" / "model_stream_responses.jsonc")


def _load_default_cassette_review() -> dict[str, object]:
    scenario = load_scenario(FIXTURE_ROOT, SCENARIO_ID)
    assert scenario.cassette.metadata["source"] == "handwritten"
    assert scenario.cassette.metadata["protocol"] == "openai_responses_sse"
    assert len(scenario.cassette.interactions) == 2
    first, second = scenario.cassette.interactions
    assert first.request.match["input_types"] == ["message", "message"]
    assert second.request.match["input_types"] == [
        "message",
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert first.response.frames[-1].event == "response.completed"
    assert second.response.frames[-1].event == "response.completed"
    assert first.response.frames[5].payload["item"]["type"] == "reasoning"
    assert first.response.frames[10].payload["item"]["type"] == "function_call"
    assert second.response.frames[1].payload["item"]["type"] == "reasoning"
    assert second.response.frames[8].payload["item"]["type"] == "message"
    return {
        "scenario_id": scenario.scenario_id,
        "asset_path": str(scenario.asset_path.relative_to(Path.cwd())),
        "cassette": scenario.cassette.raw,
        "checks": {
            "default_config": True,
            "reasoning": True,
            "tool_call": True,
            "tool_result": True,
            "final_text": True,
        },
    }


def _write_review_artifact(review: dict[str, object]) -> Path:
    output_path = (
        output_root_for_test(Path(__file__), test_layer="e2e")
        / "artifacts"
        / "responses-default-e2e-review.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(review, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


@pytest.mark.asyncio
async def test_default_responses_replay_runs_complete_tool_loop(
    client: httpx.AsyncClient,
    is_debug: bool,
) -> None:
    review = _load_default_cassette_review()

    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Default Responses reasoning tool replay E2E", "agent_id": "default"},
    )
    assert create_response.status_code == 200, create_response.text
    session_id = create_response.json()["data"]["session_id"]

    provider_response = await client.patch(
        f"/api/v1/sessions/{session_id}",
        json={"provider_id": "backup_3"},
    )
    assert provider_response.status_code == 200, provider_response.text
    assert provider_response.json()["data"]["current_provider_id"] == "backup_3"

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
    assert {"tool_call_start", "tool_call_end", "text_delta", "agent_end"} <= set(
        event_types
    )

    tool_start = next(event for event in events if event.get("type") == "tool_call_start")
    tool_end = next(event for event in events if event.get("type") == "tool_call_end")
    tool_start_payload = get_trace_payload(tool_start)
    tool_end_payload = get_trace_payload(tool_end)
    assert tool_start_payload["tool_name"] == "read_file"
    assert tool_start_payload["args"] == {"path": "README.md"}
    assert tool_end_payload["tool_name"] == "read_file"
    assert tool_end_payload["status"] == "success"
    assert "# 统一测试工作区" in str(tool_end_payload["result"])

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
    messages = messages_response.json()["data"]["items"]
    assert last_assistant_message(messages) == "工具调用完成"

    logs_response = await client.get(
        f"/api/v1/sessions/{session_id}/llm-request-logs"
    )
    assert logs_response.status_code == 200, logs_response.text
    logs = logs_response.json()["data"]
    assert len(logs) == 2
    attempts = [log["upstream"]["attempts"][0] for log in logs]
    assert all(attempt["call_type"] == "aresponses" for attempt in attempts)
    assert attempts[0]["response"]["output"][0]["type"] == "reasoning"
    assert attempts[0]["response"]["output"][1]["type"] == "function_call"
    assert attempts[1]["request"]["input"][-1]["type"] == "function_call_output"
    assert "# 统一测试工作区" in str(attempts[1]["request"]["input"][-1]["output"])
    assert attempts[1]["response"]["output"][0]["type"] == "reasoning"
    assert attempts[1]["response"]["output"][1]["type"] == "message"
    assert attempts[1]["response"]["output"][1]["content"][0]["text"] == "工具调用完成"

    review["observed"] = {
        "session_id": session_id,
        "job_id": job_id,
        "job_status": job["status"],
        "trace_event_types": event_types,
        "trace_reasoning": reasoning_text,
        "tool_call_start": tool_start_payload,
        "tool_call_end": {
            "tool_name": tool_end_payload["tool_name"],
            "status": tool_end_payload["status"],
            "result_preview": str(tool_end_payload["result"])[:120],
        },
        "assistant_text": last_assistant_message(messages),
        "upstream_tool_result_preview": str(
            attempts[1]["request"]["input"][-1]["output"]
        )[:120],
    }
    review_path = _write_review_artifact(review)
    print(f"Responses 默认 reasoning/tool E2E 数据审查产物: {review_path}")
