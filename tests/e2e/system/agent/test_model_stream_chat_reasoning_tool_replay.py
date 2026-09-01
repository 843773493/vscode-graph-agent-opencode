"""验证 Chat Completions reasoning、工具调用和最终文本的完整 replay 链路。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.testing.model_stream import load_scenario
from tests.support.api_waiters import wait_for_job_done
from tests.support.messages import last_assistant_message
from tests.support.paths import output_root_for_test
from tests.support.sse import read_sse_events_until
from tests.support.trace import get_trace_payload

FIXTURE_ROOT = Path.cwd() / "tests" / "fixtures" / "model_stream"
SCENARIO_ID = "reasoning-tool"
EXPECTED_FIRST_FINISH_REASON = "tool_calls"
EXPECTED_SECOND_FINISH_REASON = "stop"


@pytest.fixture(scope="module")
def e2e_model_stream_config_path() -> str:
    return str(Path.cwd() / "configs" / "tests" / "model_stream_chat_tool.jsonc")


def _load_and_validate_recorded_data() -> dict[str, object]:
    scenario = load_scenario(FIXTURE_ROOT, SCENARIO_ID)
    assert scenario.cassette.metadata["protocol"] == "openai_chat_sse"
    assert len(scenario.cassette.interactions) == 2

    first, second = scenario.cassette.interactions
    assert first.request.match["message_roles"] == ["system", "user"]
    assert second.request.match["message_roles"] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]

    first_payloads = [frame.payload for frame in first.response.frames]
    second_payloads = [frame.payload for frame in second.response.frames]
    first_deltas = [payload["choices"][0]["delta"] for payload in first_payloads[:-1]]
    second_deltas = [
        payload["choices"][0]["delta"] for payload in second_payloads[:-1]
    ]

    assert "reasoning_content" in first_deltas[0]
    assert "reasoning_content" in first_deltas[1]
    tool_call_parts = [
        delta["tool_calls"][0]
        for delta in first_deltas
        if "tool_calls" in delta
    ]
    assert tool_call_parts[0]["function"]["name"] == "read_file"
    assert "{\"path\":" in tool_call_parts[0]["function"]["arguments"]
    assert "".join(
        part["function"]["arguments"]
        for part in tool_call_parts
    ) == '{"path":"README.md"}'
    assert first_payloads[-2]["choices"][0]["finish_reason"] == EXPECTED_FIRST_FINISH_REASON

    assert "reasoning_content" in second_deltas[0]
    assert "reasoning_content" in second_deltas[1]
    assert [
        delta["content"]
        for delta in second_deltas
        if "content" in delta
    ] == [
        "Chat Completions 会说明检查结果，",
        "再给出 Chat Completions 可复查结论。",
    ]
    assert second_payloads[-2]["choices"][0]["finish_reason"] == EXPECTED_SECOND_FINISH_REASON

    review: dict[str, object] = {
        "scenario_id": scenario.scenario_id,
        "asset_path": str(scenario.asset_path.relative_to(Path.cwd())),
        "interaction_count": len(scenario.cassette.interactions),
        "cassette": scenario.cassette.raw,
        "checks": {
            "message_role_matching": True,
            "reasoning_deltas": True,
            "tool_call_arguments": True,
            "tool_result": True,
            "final_text_deltas": True,
            "terminal_finish_reasons": True,
        },
    }
    return review


def _write_review_artifact(review: dict[str, object]) -> Path:
    output_path = (
        output_root_for_test(Path(__file__), test_layer="e2e")
        / "artifacts"
        / "chat-reasoning-tool-e2e-review.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(review, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def _message_roles(request: Any) -> list[str]:
    messages = request.get("messages") if isinstance(request, dict) else None
    if not isinstance(messages, list):
        return []
    return [
        str(message.get("role"))
        for message in messages
        if isinstance(message, dict)
    ]


@pytest.mark.asyncio
async def test_chat_replay_records_reasoning_and_tool_call_loop(
    client: httpx.AsyncClient,
    is_debug: bool,
) -> None:
    review = _load_and_validate_recorded_data()

    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Chat reasoning tool replay E2E", "agent_id": "default"},
    )
    assert create_response.status_code == 200, create_response.text
    session_id = create_response.json()["data"]["session_id"]

    provider_response = await client.patch(
        f"/api/v1/sessions/{session_id}",
        json={"provider_id": "primary"},
    )
    assert provider_response.status_code == 200, provider_response.text
    assert provider_response.json()["data"]["current_provider_id"] == "primary"

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

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200, messages_response.text
    messages = messages_response.json()["data"]["items"]
    assert last_assistant_message(messages) == (
        "Chat Completions 会说明检查结果，再给出 Chat Completions 可复查结论。"
    )
    assistant_message = next(
        message for message in reversed(messages) if message.get("role") == "assistant"
    )
    assert assistant_message["metadata"]["provider_id"] == "primary"
    assert assistant_message["metadata"]["custom_llm_provider"] == "openai"

    agent_state_response = await client.get(
        f"/api/v1/sessions/{session_id}/agent-state/messages"
    )
    assert agent_state_response.status_code == 200, agent_state_response.text
    agent_state_jsonl = agent_state_response.json()["data"]["jsonl"]
    assert "Chat Completions 先确认要读取的文件，再检查工具返回的证据，" in agent_state_jsonl
    reasoning_deltas = [
        get_trace_payload(event).get("text", "")
        for event in events
        if event.get("type") == "text_delta"
        and get_trace_payload(event).get("kind") == "reasoning"
    ]
    assert "".join(str(text) for text in reasoning_deltas) == (
        "Chat Completions 先确认要读取的文件，再检查工具返回的证据，"
        "确认内容和当前问题一致后，再发起一次明确的 Chat Completions 工具调用。"
        "Chat Completions 工具已经返回，我先核对文件内容，确认结果和请求目标相符，"
        "再整理一份 Chat Completions 可复查的最终答复。"
    )

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
        "acompletion",
        "acompletion",
    ]
    assert all(attempt["provider"] == "openai" for attempt in upstream_attempts)
    assert all(attempt["model"] == "big-pickle" for attempt in upstream_attempts)
    assert all(
        attempt["api_base"] == "https://opencode.ai/zen/v1"
        for attempt in upstream_attempts
    )

    first_request = upstream_attempts[0]["request"]
    second_request = upstream_attempts[1]["request"]
    assert _message_roles(first_request) == ["system", "user"]
    assert _message_roles(second_request) == ["system", "user", "assistant", "tool"]

    first_response = upstream_attempts[0]["response"]
    second_response = upstream_attempts[1]["response"]
    assert first_response["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "read_file"
    assert first_response["choices"][0]["message"]["reasoning_content"] == (
        "Chat Completions 先确认要读取的文件，再检查工具返回的证据，"
        "确认内容和当前问题一致后，再发起一次明确的 Chat Completions 工具调用。"
    )
    assert second_response["choices"][0]["message"]["reasoning_content"] == (
        "Chat Completions 工具已经返回，我先核对文件内容，确认结果和请求目标相符，"
        "再整理一份 Chat Completions 可复查的最终答复。"
    )
    assert second_response["choices"][0]["message"]["content"] == (
        "Chat Completions 会说明检查结果，再给出 Chat Completions 可复查结论。"
    )

    review["observed"] = {
        "session_id": session_id,
        "job_id": job_id,
        "job_status": job["status"],
        "trace_event_types": event_types,
        "tool_call_start": tool_start_payload,
        "tool_call_end": {
            "tool_name": tool_end_payload["tool_name"],
            "status": tool_end_payload["status"],
            "result_preview": str(tool_end_payload["result"])[:120],
        },
        "assistant_text": assistant_message["content"],
        "agent_state_contains_reasoning": (
            "Chat Completions 先确认要读取的文件，再检查工具返回的证据，"
            in agent_state_jsonl
        ),
        "trace_reasoning": "".join(str(text) for text in reasoning_deltas),
        "upstream": [
            {
                "call_type": attempt["call_type"],
                "provider": attempt["provider"],
                "model": attempt["model"],
                "api_base": attempt["api_base"],
                "message_roles": _message_roles(attempt["request"]),
                "response_reasoning": attempt["response"]["choices"][0]["message"][
                    "reasoning_content"
                ],
                "response_tool_names": [
                    item["function"]["name"]
                    for item in attempt["response"]["choices"][0]["message"].get(
                        "tool_calls", []
                    )
                ],
                "response_text": attempt["response"]["choices"][0]["message"].get(
                    "content"
                ),
            }
            for attempt in upstream_attempts
        ],
    }
    review_path = _write_review_artifact(review)
    print(f"Chat reasoning/tool E2E 数据审查产物: {review_path}")
