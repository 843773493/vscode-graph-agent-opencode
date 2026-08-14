from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from tests.support.api_waiters import wait_for_job_done
from tests.support.messages import last_assistant_message
from tests.support.trace import get_trace_payload


def _decode_debug_result(payload: dict[str, object]) -> dict[str, object]:
    result = payload.get("result")
    assert isinstance(result, str), payload
    decoded = json.loads(result)
    assert isinstance(decoded, dict), decoded
    return decoded


@pytest.mark.skipif(
    os.getenv("BOXTEAM_RUN_LIVE_DEBUG_E2E") != "1",
    reason="设置 BOXTEAM_RUN_LIVE_DEBUG_E2E=1 后才连接配置中的真实模型",
)
@pytest.mark.live_model
@pytest.mark.asyncio
async def test_live_model_drives_node_debugging_from_user_prompt(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    workspace_root = Path(e2e_workspace_root_path).resolve()
    worker_path = workspace_root / "debug-live-worker.mjs"
    worker_path.write_text(
        """export function compute(value) {
  const localValue = value + 1;
  return { localValue };
}
""",
        encoding="utf-8",
    )
    fixture_path = workspace_root / "debug-live-fixture.mjs"
    fixture_path.write_text(
        """import { compute } from './debug-live-worker.mjs';
const input = 41;
const result = compute(input);
console.log(JSON.stringify(result));
""",
        encoding="utf-8",
    )

    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Live debug prompt E2E"},
    )
    assert create_response.status_code == 200, create_response.text
    session_id = create_response.json()["data"]["session_id"]
    prompt = (
        f"请调试文件 {fixture_path}，工作目录是 {workspace_root}。"
        f"请先了解当前工作区适用的调试能力，再对入口第 3 行和 {worker_path} 第 2 行设置断点，"
        "调用 start_debugging，在入口暂停后继续到 worker，求值 value * 2，调用 step_over，"
        "再调用 continue_execution 和 stop_debugging，直到调试结束。"
        "不要只解释，必须真实调用工具。最后只回复 LIVE_DEBUG_PROMPT_FLOW_OK。"
    )
    message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": prompt},
            "run": {
                "mode": "single_agent",
                "agent_id": "default",
                "max_steps": 20,
                "timeout_seconds": 240,
            },
        },
    )
    assert message_response.status_code == 200, message_response.text
    job_id = message_response.json()["data"]["job_id"]
    job = await wait_for_job_done(client, job_id, max_attempts=180)
    assert job["status"] in {"completed", "succeeded"}

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200, messages_response.text
    assert "LIVE_DEBUG_PROMPT_FLOW_OK" in last_assistant_message(
        messages_response.json()["data"]["items"]
    )

    traces_response = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert traces_response.status_code == 200, traces_response.text
    traces = traces_response.json()["data"]["items"]
    start_payloads = [
        get_trace_payload(trace)
        for trace in traces
        if trace.get("type") == "tool_call_start"
    ]
    names = [str(payload.get("tool_name")) for payload in start_payloads]
    expected = [
        "read_file",
        "add_breakpoint",
        "start_debugging",
        "continue_execution",
        "evaluate_expression",
        "step_over",
        "stop_debugging",
    ]
    assert all(names.count(name) >= 1 for name in expected), names
    assert names.count("add_breakpoint") >= 2, names
    assert names.count("continue_execution") >= 2, names
    assert [names.index(name) for name in expected] == sorted(
        names.index(name) for name in expected
    )

    debug_start_payloads = [
        payload
        for payload in start_payloads
        if payload.get("tool_name") in expected[1:]
    ]
    assert debug_start_payloads
    assert all(
        payload.get("invocation_tool_name") == "invoke_custom_tool"
        for payload in debug_start_payloads
    ), debug_start_payloads

    read_file_starts = [
        payload for payload in start_payloads if payload.get("tool_name") == "read_file"
    ]
    assert len(read_file_starts) == 1, read_file_starts
    assert (
        read_file_starts[0].get("args", {}).get("path")
        == ".boxteam/bundled-skills/debugging/SKILL.md"
    )

    end_payloads = [
        get_trace_payload(trace)
        for trace in traces_response.json()["data"]["items"]
        if trace.get("type") == "tool_call_end"
    ]
    end_payloads_by_name = {
        name: [payload for payload in end_payloads if payload.get("tool_name") == name]
        for name in expected
    }
    assert all(end_payloads_by_name[name] for name in expected), end_payloads_by_name
    assert all(
        payload.get("status") == "success"
        for payloads in end_payloads_by_name.values()
        for payload in payloads
    ), end_payloads_by_name

    skill_result = end_payloads_by_name["read_file"][0].get("result")
    assert "# 源码调试工具" in str(skill_result)
    assert "invoke_custom_tool" in str(skill_result)

    decoded_results = {
        name: _decode_debug_result(payloads[-1])
        for name, payloads in end_payloads_by_name.items()
        if name != "read_file"
    }
    assert all(result.get("ok") is True for result in decoded_results.values()), (
        decoded_results
    )
    for name, result in decoded_results.items():
        serialized = json.dumps(result, ensure_ascii=False)
        for internal_field in (
            "inspector_url",
            "inspector_id",
            "call_frame_id",
            "object_id",
            '"pid"',
            '"session_id"',
            '"tool_call_id"',
        ):
            assert internal_field not in serialized, (name, internal_field)

    start_state = decoded_results["start_debugging"].get("state")
    assert isinstance(start_state, dict), start_state
    assert start_state.get("status") == "paused", start_state
    assert start_state.get("call_stack"), start_state

    evaluation_state = decoded_results["evaluate_expression"].get("state")
    assert isinstance(evaluation_state, dict), evaluation_state
    assert evaluation_state.get("status") == "paused", evaluation_state
    evaluation = evaluation_state.get("last_evaluation")
    assert isinstance(evaluation, dict), evaluation_state
    assert evaluation.get("expression") == "value * 2", evaluation
    assert evaluation.get("value") == "82", evaluation

    step_state = decoded_results["step_over"].get("state")
    assert isinstance(step_state, dict), step_state
    assert step_state.get("status") == "paused", step_state
    assert step_state.get("call_stack"), step_state

    continue_state = decoded_results["continue_execution"].get("state")
    assert isinstance(continue_state, dict), continue_state
    assert continue_state.get("status") == "exited", continue_state

    stop_state = decoded_results["stop_debugging"].get("state")
    assert isinstance(stop_state, dict), stop_state
    assert stop_state.get("status") == "exited", stop_state

    logs_response = await client.get(f"/api/v1/sessions/{session_id}/llm-request-logs")
    assert logs_response.status_code == 200, logs_response.text
    logs = logs_response.json()["data"]
    assert isinstance(logs, list) and logs, logs
    model_names = {
        log.get("request", {}).get("model_name")
        for log in logs
        if isinstance(log, dict)
    }
    assert model_names
    assert "debug-e2e-model" not in model_names, model_names

    model_requests = [
        log.get("request")
        for log in logs
        if isinstance(log, dict) and isinstance(log.get("request"), dict)
    ]
    assert model_requests, logs
    exposed_tool_names = [
        {
            tool.get("name")
            for tool in request.get("tools", [])
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        }
        for request in model_requests
    ]
    assert any("invoke_custom_tool" in names for names in exposed_tool_names)
    assert all(not set(expected[1:]) & names for names in exposed_tool_names), (
        exposed_tool_names
    )
