"""验证 Responses reasoning、工具调用和最终文本的完整 replay 链路。"""
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
EXPECTED_FINAL_TEXT = (
    "OpenAI Responses 工具已经返回，我核对了 OpenAI Responses 文件内容，"
    "现在给出 OpenAI Responses 可复查的结论。"
)
EXPECTED_FIRST_EVENTS = [
    "response.created",
    "response.output_item.added",
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_part.done",
    "response.output_item.done",
    "response.output_item.added",
    "response.function_call_arguments.delta",
    "response.function_call_arguments.delta",
    "response.function_call_arguments.delta",
    "response.function_call_arguments.done",
    "response.output_item.done",
    "response.completed",
]
EXPECTED_SECOND_EVENTS = [
    "response.created",
    "response.output_item.added",
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_part.done",
    "response.output_item.done",
    "response.output_item.added",
    "response.content_part.added",
    "response.output_text.delta",
    "response.output_text.delta",
    "response.output_text.delta",
    "response.output_text.done",
    "response.content_part.done",
    "response.output_item.done",
    "response.completed",
]


@pytest.fixture(scope="module")
def e2e_model_stream_config_path() -> str:
    return str(
        Path.cwd() / "configs" / "tests" / "model_stream_responses_tool.jsonc"
    )


def _load_and_validate_recorded_data() -> dict[str, object]:
    scenario = load_scenario(FIXTURE_ROOT, SCENARIO_ID)
    assert scenario.cassette.metadata["protocol"] == "openai_responses_sse"
    assert len(scenario.cassette.interactions) == 2

    first, second = scenario.cassette.interactions
    assert [frame.event for frame in first.response.frames] == EXPECTED_FIRST_EVENTS
    assert [frame.event for frame in second.response.frames] == EXPECTED_SECOND_EVENTS

    reasoning_item = next(
        frame.payload["item"]
        for frame in first.response.frames
        if frame.event == "response.output_item.done"
        and frame.payload["item"]["type"] == "reasoning"
    )
    assert reasoning_item["type"] == "reasoning"
    assert reasoning_item["summary"][0]["text"] == (
        "OpenAI Responses 先确认待读取的文件，再检查工具返回的证据，"
        "确认内容和当前问题一致后，再发起一次明确的 OpenAI Responses 工具调用。"
    )
    assert reasoning_item["encrypted_content"] == "encrypted-reasoning-tool"

    argument_deltas = [
        frame.payload["delta"]
        for frame in first.response.frames
        if frame.event == "response.function_call_arguments.delta"
    ]
    assert json.loads("".join(argument_deltas)) == {"path": "README.md"}
    function_call_item = next(
        frame.payload["item"]
        for frame in first.response.frames
        if frame.event == "response.output_item.done"
        and frame.payload["item"]["type"] == "function_call"
    )
    assert function_call_item == {
        "id": "fc_reasoning_tool",
        "type": "function_call",
        "status": "completed",
        "call_id": "call_reasoning_tool",
        "name": "read_file",
        "arguments": "{\"path\":\"README.md\"}",
    }

    final_message = next(
        frame.payload["item"]
        for frame in second.response.frames
        if frame.event == "response.output_item.done"
        and frame.payload["item"]["type"] == "message"
    )
    assert final_message["type"] == "message"
    assert final_message["content"][0]["text"] == EXPECTED_FINAL_TEXT
    second_reasoning_item = next(
        frame.payload["item"]
        for frame in second.response.frames
        if frame.event == "response.output_item.done"
        and frame.payload["item"]["type"] == "reasoning"
    )
    assert second_reasoning_item["type"] == "reasoning"
    assert second_reasoning_item["summary"][0]["text"] == (
        "OpenAI Responses 先核对工具结果，确认它和请求目标相符，"
        "再整理一份 OpenAI Responses 最终答复。"
    )
    assert second_reasoning_item["encrypted_content"] == "encrypted-reasoning-tool-2"
    terminal_payload = second.response.frames[-1].payload
    assert terminal_payload["type"] == "response.completed"
    assert terminal_payload["response"]["output"][1]["type"] == "message"

    review: dict[str, object] = {
        "scenario_id": scenario.scenario_id,
        "asset_path": str(scenario.asset_path.relative_to(Path.cwd())),
        "interaction_count": len(scenario.cassette.interactions),
        "cassette": scenario.cassette.raw,
        "checks": {
            "reasoning_summary": True,
            "encrypted_reasoning": True,
            "tool_call_arguments": True,
            "tool_result": True,
            "tool_call_event_sequence": True,
            "final_message": True,
        },
    }
    return review


def _write_review_artifact(review: dict[str, object]) -> Path:
    output_path = (
        output_root_for_test(Path(__file__), test_layer="e2e")
        / "artifacts"
        / "responses-reasoning-tool-e2e-review.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(review, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


@pytest.mark.asyncio
async def test_responses_replay_records_reasoning_and_tool_call_loop(
    client: httpx.AsyncClient,
    is_debug: bool,
) -> None:
    review = _load_and_validate_recorded_data()

    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Responses reasoning tool replay E2E", "agent_id": "default"},
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
                        "请先读取当前工作区的 README.md，然后只回复最终结论，"
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
    assert "tool_call_start" in event_types
    assert "tool_call_end" in event_types
    assert "text_delta" in event_types
    assert "agent_end" in event_types
    tool_start = next(event for event in events if event.get("type") == "tool_call_start")
    tool_end = next(event for event in events if event.get("type") == "tool_call_end")
    tool_start_payload = get_trace_payload(tool_start)
    tool_end_payload = get_trace_payload(tool_end)
    assert tool_start_payload["tool_name"] == "read_file"
    assert tool_start_payload["args"] == {"path": "README.md"}
    assert tool_end_payload["tool_name"] == "read_file"
    assert tool_end_payload["status"] == "success"
    assert "# 统一测试工作区" in str(tool_end_payload["result"])
    reasoning_trace_texts = [
        str(get_trace_payload(event).get("text"))
        for event in events
        if event.get("type") == "text_delta"
        and get_trace_payload(event).get("kind") == "reasoning"
        and get_trace_payload(event).get("text")
    ]
    reasoning_trace_text = "".join(reasoning_trace_texts)
    assert (
        "OpenAI Responses 先确认待读取的文件，再检查工具返回的证据，"
        "确认内容和当前问题一致后，再发起一次明确的 OpenAI Responses 工具调用。"
        in reasoning_trace_text
    )
    assert (
        "OpenAI Responses 先核对工具结果，确认它和请求目标相符，"
        "再整理一份 OpenAI Responses 最终答复。"
        in reasoning_trace_text
    )

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200, messages_response.text
    messages = messages_response.json()["data"]["items"]
    assert last_assistant_message(messages) == EXPECTED_FINAL_TEXT
    assistant_message = next(
        message for message in reversed(messages) if message.get("role") == "assistant"
    )
    assert assistant_message["metadata"]["provider_id"] == "backup_3"
    assert assistant_message["metadata"]["custom_llm_provider"] == "openai"

    logs_response = await client.get(
        f"/api/v1/sessions/{session_id}/llm-request-logs"
    )
    assert logs_response.status_code == 200, logs_response.text
    logs = logs_response.json()["data"]
    assert len(logs) == 2
    assert all(log["job_id"] == job_id for log in logs)
    upstream_attempts = [
        log["upstream"]["attempts"][0]
        for log in logs
    ]
    assert [attempt["call_type"] for attempt in upstream_attempts] == [
        "aresponses",
        "aresponses",
    ]
    assert all(attempt["provider"] == "openai" for attempt in upstream_attempts)
    assert all(attempt["model"] == "gpt-5.6-luna" for attempt in upstream_attempts)
    assert all(
        attempt["api_base"] == "https://www.cctq.ai/v1/responses"
        for attempt in upstream_attempts
    )
    assert upstream_attempts[0]["request"]["input"][-1]["type"] == "message"
    assert upstream_attempts[1]["request"]["input"][-1]["type"] == "function_call_output"
    assert "# 统一测试工作区" in str(upstream_attempts[1]["request"]["input"][-1]["output"])
    assert upstream_attempts[0]["response"]["output"][0]["type"] == "reasoning"
    assert upstream_attempts[0]["response"]["output"][1]["type"] == "function_call"
    assert upstream_attempts[0]["response"]["output"][1]["name"] == "read_file"
    assert upstream_attempts[0]["response"]["output"][1]["arguments"] == (
        "{\"path\":\"README.md\"}"
    )
    assert upstream_attempts[1]["response"]["output"][1]["type"] == "message"
    assert upstream_attempts[1]["response"]["output"][1]["content"][0]["text"] == (
        EXPECTED_FINAL_TEXT
    )
    assert upstream_attempts[1]["response"]["output"][0]["type"] == "reasoning"
    assert upstream_attempts[1]["response"]["output"][0]["summary"][0]["text"] == (
        "OpenAI Responses 工具已经返回，我核对了 OpenAI Responses 文件内容，"
        "现在整理一份 OpenAI Responses 可复查的最终答复。"
    )
    assert upstream_attempts[1]["response"]["output"][0]["encrypted_content"] == (
        "encrypted-reasoning-tool-2"
    )
    assert [
        item["type"] for item in upstream_attempts[1]["request"]["input"]
    ] == ["message", "message", "reasoning", "function_call", "function_call_output"]

    review["observed"] = {
        "session_id": session_id,
        "job_id": job_id,
        "job_status": job["status"],
        "trace_event_types": event_types,
        "trace_reasoning": reasoning_trace_texts,
        "assistant_text": assistant_message["content"],
        "tool_call_start": tool_start_payload,
        "tool_call_end": {
            "tool_name": tool_end_payload["tool_name"],
            "status": tool_end_payload["status"],
            "result_preview": str(tool_end_payload["result"])[:120],
        },
        "upstream_tool_result_preview": str(
            upstream_attempts[1]["request"]["input"][-1]["output"]
        )[:120],
        "trace_contains_tool_call": (
            "tool_call_start" in event_types and "tool_call_end" in event_types
        ),
        "llm_request_log_count": len(logs),
        "upstream": [
            {
                "call_type": attempt["call_type"],
                "provider": attempt["provider"],
                "model": attempt["model"],
                "api_base": attempt["api_base"],
                "input_last_type": attempt["request"]["input"][-1]["type"],
                "response_output_types": [
                    item["type"] for item in attempt["response"]["output"]
                ],
                "reasoning_summary": attempt["response"]["output"][0]["summary"][0][
                    "text"
                ],
            }
            for attempt in upstream_attempts
        ],
    }
    review_path = _write_review_artifact(review)
    print(f"Responses reasoning/tool E2E 数据审查产物: {review_path}")
