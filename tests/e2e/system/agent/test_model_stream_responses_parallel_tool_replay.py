"""验证 Responses 交错双工具调用的完整 replay 链路。"""
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
SCENARIO_ID = "responses-reasoning-parallel-tool"
EXPECTED_TOOL_RESULTS = {
    "README.md": "# 统一测试工作区",
    "test.md": "# test.md",
}


@pytest.fixture(scope="module")
def e2e_model_stream_config_path() -> str:
    return str(
        Path.cwd()
        / "configs"
        / "tests"
        / "model_stream_responses_parallel_tool.jsonc"
    )


def _load_parallel_cassette_review() -> dict[str, object]:
    scenario = load_scenario(FIXTURE_ROOT, SCENARIO_ID)
    assert scenario.cassette.metadata["protocol"] == "openai_responses_sse"
    assert len(scenario.cassette.interactions) == 2
    first, second = scenario.cassette.interactions

    first_events = [frame.event for frame in first.response.frames]
    assert first_events[6:12] == [
        "response.output_item.added",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.delta",
    ]
    assert [
        first.response.frames[index].payload["item_id"]
        for index in (8, 9, 10, 11)
    ] == [
        "fc_parallel_one",
        "fc_parallel_two",
        "fc_parallel_one",
        "fc_parallel_two",
    ]
    assert first.response.frames[-1].event == "response.completed"
    assert second.response.frames[-1].event == "response.completed"

    first_output = first.response.frames[-1].payload["response"]["output"]
    assert [item["type"] for item in first_output] == [
        "reasoning",
        "function_call",
        "function_call",
    ]
    assert {
        item["call_id"]: item["arguments"]
        for item in first_output
        if item["type"] == "function_call"
    } == {
        "call_parallel_one": '{"path":"README.md"}',
        "call_parallel_two": '{"path":"test.md"}',
    }
    assert second.response.frames[12].payload["item"]["type"] == "message"
    return {
        "scenario_id": scenario.scenario_id,
        "asset_path": str(scenario.asset_path.relative_to(Path.cwd())),
        "cassette": scenario.cassette.raw,
        "checks": {
            "interleaved_argument_deltas": True,
            "two_function_calls": True,
            "two_function_call_outputs": True,
            "final_message": True,
        },
    }


def _write_review_artifact(review: dict[str, object]) -> Path:
    output_path = (
        output_root_for_test(Path(__file__), test_layer="e2e")
        / "artifacts"
        / "responses-reasoning-parallel-tool-e2e-review.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(review, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


@pytest.mark.asyncio
async def test_responses_replay_runs_interleaved_parallel_tool_loop(
    client: httpx.AsyncClient,
    is_debug: bool,
) -> None:
    review = _load_parallel_cassette_review()

    create_response = await client.post(
        "/api/v1/sessions",
        json={
            "title": "Responses interleaved parallel tool replay E2E",
            "agent_id": "default",
        },
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
                            "请并行读取当前工作区的 README.md 和 test.md，"
                            "两个工具都完成后只回复两个工具都已完成，不要补充其它解释。"
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
    start_events = [
        event for event in events if event.get("type") == "tool_call_start"
    ]
    end_events = [event for event in events if event.get("type") == "tool_call_end"]
    assert len(start_events) == 2
    assert len(end_events) == 2
    assert event_types[-1] == "agent_end"
    start_positions = [event_types.index("tool_call_start")]
    start_positions.append(
        event_types.index("tool_call_start", start_positions[0] + 1)
    )
    end_positions = [event_types.index("tool_call_end")]
    end_positions.append(event_types.index("tool_call_end", end_positions[0] + 1))
    assert max(start_positions) < min(end_positions)

    starts_by_execution_id = {
        str(get_trace_payload(event)["execution_id"]): get_trace_payload(event)
        for event in start_events
    }
    starts_by_path = {
        str(payload["args"]["path"]): payload
        for payload in starts_by_execution_id.values()
    }
    ends_by_path: dict[str, dict[str, object]] = {}
    for event in end_events:
        payload = get_trace_payload(event)
        start_payload = starts_by_execution_id[str(payload["execution_id"])]
        ends_by_path[str(start_payload["args"]["path"])] = payload
    assert set(starts_by_path) == set(EXPECTED_TOOL_RESULTS)
    assert set(ends_by_path) == set(EXPECTED_TOOL_RESULTS)
    for path, expected_marker in EXPECTED_TOOL_RESULTS.items():
        assert starts_by_path[path]["tool_name"] == "read_file"
        assert ends_by_path[path]["tool_name"] == "read_file"
        assert ends_by_path[path]["status"] == "success"
        assert expected_marker in str(ends_by_path[path]["result"])

    reasoning_text = "".join(
        str(get_trace_payload(event).get("text", ""))
        for event in events
        if event.get("type") == "text_delta"
        and get_trace_payload(event).get("kind") == "reasoning"
    )
    assert reasoning_text == "先并行读取两个文件，再根据结果作答。两个文件都已读取，正在汇总结果。"

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200, messages_response.text
    messages = messages_response.json()["data"]["items"]
    assert last_assistant_message(messages) == "两个工具都已完成，文件内容已核对。"

    logs_response = await client.get(
        f"/api/v1/sessions/{session_id}/llm-request-logs"
    )
    assert logs_response.status_code == 200, logs_response.text
    logs = logs_response.json()["data"]
    assert len(logs) == 2
    attempts = [log["upstream"]["attempts"][0] for log in logs]
    assert all(attempt["call_type"] == "aresponses" for attempt in attempts)
    first_response_output = attempts[0]["response"]["output"]
    assert [item["type"] for item in first_response_output] == [
        "reasoning",
        "function_call",
        "function_call",
    ]
    second_request_input = attempts[1]["request"]["input"]
    assert [item["type"] for item in second_request_input] == [
        "message",
        "message",
        "reasoning",
        "function_call",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]
    tool_outputs = {
        item["call_id"]: item["output"]
        for item in second_request_input
        if item["type"] == "function_call_output"
    }
    assert set(tool_outputs) == {"call_parallel_one", "call_parallel_two"}
    assert "# 统一测试工作区" in str(tool_outputs["call_parallel_one"])
    assert "# test.md" in str(tool_outputs["call_parallel_two"])
    assert attempts[1]["response"]["output"][0]["type"] == "reasoning"
    assert attempts[1]["response"]["output"][1]["type"] == "message"

    review["observed"] = {
        "session_id": session_id,
        "job_id": job_id,
        "job_status": job["status"],
        "trace_event_types": event_types,
        "tool_start_paths": sorted(starts_by_path),
        "tool_end_paths": sorted(ends_by_path),
        "trace_reasoning": reasoning_text,
        "assistant_text": last_assistant_message(messages),
        "upstream_tool_output_call_ids": sorted(tool_outputs),
    }
    review_path = _write_review_artifact(review)
    print(f"Responses 交错双工具 E2E 数据审查产物: {review_path}")
