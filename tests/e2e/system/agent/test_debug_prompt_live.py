from __future__ import annotations

import json
import subprocess
from collections.abc import Generator
from pathlib import Path

import commentjson
import httpx
import pytest

pytest_plugins = (
    "tests.integration.backend.agents.test_debug_prompt_flow",
)

from tests.integration.backend.agents.test_debug_prompt_flow import (
    ScriptedModelState,
    _write_debug_fixture,
)
from tests.support.api_waiters import wait_for_job_done
from tests.support.messages import last_assistant_message
from tests.support.processes import close_backend_process, start_backend_process
from tests.support.trace import get_trace_payload


def _decode_debug_result(payload: dict[str, object]) -> dict[str, object]:
    result = payload.get("result")
    assert isinstance(result, str), payload
    decoded = json.loads(result)
    assert isinstance(decoded, dict), decoded
    return decoded


@pytest.fixture(scope="module")
def e2e_backend_process(
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
    e2e_backend_port: int,
    scripted_model_server: tuple[ScriptedModelState, str],
) -> Generator[subprocess.Popen[str], None, None]:
    """为本文件注入确定性流式模型，禁止连接外部 Provider。"""
    _state, endpoint = scripted_model_server
    config_path = Path(e2e_workspace_config_path)
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    providers = config["llm"]["providers"]
    providers.append(
        {
            "id": "debug-e2e",
            "endpoint": endpoint,
            "model": "debug-e2e-model",
            "api_key": "debug-e2e-key",
            "custom_llm_provider": "openai",
            "api_mode": {
                "protocol": "chat_completions",
                "model_info": {
                    "supports_function_calling": True,
                    "supports_reasoning": True,
                },
                "supports_reasoning": {"reasoning_content": True},
            },
        }
    )
    model = config["agents"]["default"]["model"]
    model["primary_provider"] = "debug-e2e"
    model["fallback_providers"] = []
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    handle = start_backend_process(
        workspace_root=e2e_workspace_root_path,
        port=e2e_backend_port,
        log_name="deterministic-debug-prompt-e2e",
        env_overrides={"BOXTEAM_DEFAULT_SKILL_GROUPS": '["debugging"]'},
        env_unset=("BOXTEAM_TEST_MODEL_STREAM_CONFIG",),
    )
    try:
        yield handle.process
    finally:
        close_backend_process(handle)


@pytest.mark.asyncio
async def test_deterministic_model_stream_drives_node_debugging_from_user_prompt(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
    scripted_model_server: tuple[ScriptedModelState, str],
) -> None:
    state, _endpoint = scripted_model_server
    workspace_root = Path(e2e_workspace_root_path).resolve()
    fixture_path, worker_path = _write_debug_fixture(workspace_root)
    state.fixture_path = fixture_path.relative_to(workspace_root).as_posix()
    state.worker_path = worker_path.relative_to(workspace_root).as_posix()
    state.working_directory = "."

    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Live debug prompt E2E"},
    )
    assert create_response.status_code == 200, create_response.text
    session_id = create_response.json()["data"]["session_id"]
    prompt = (
        f"请调试文件 {state.fixture_path}，工作目录是 {state.working_directory}。"
        "请先使用 skill_load(name=debugging) 加载当前工作区适用的调试能力，"
        f"再对入口第 3 行和 {state.worker_path} 第 2 行设置断点，"
        "通过固定扩展信封启动并完成两处真实暂停、求值和继续。"
        "不要只解释，必须真实调用工具。"
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
    assert "DEBUG_PROMPT_FLOW_OK" in last_assistant_message(
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
    expected_order = [
        "skill_load",
        "glob",
        "read_file",
        "read_file",
        "list_debug_configurations",
        "list_breakpoints",
        "create_debug_configuration",
        "add_breakpoint",
        "add_breakpoint",
        "start_debugging",
        "evaluate_expression",
        "continue_execution",
        "evaluate_expression",
        "continue_execution",
        "list_breakpoints",
    ]
    assert names == expected_order, names

    debug_start_payloads = [
        payload
        for payload in start_payloads
        if payload.get("tool_name")
        in {
            "list_debug_configurations",
            "list_breakpoints",
            "create_debug_configuration",
            "add_breakpoint",
            "start_debugging",
            "evaluate_expression",
            "continue_execution",
        }
    ]
    assert debug_start_payloads
    assert all(
        payload.get("invocation_tool_name") == "invoke_extension_tool"
        for payload in debug_start_payloads
    ), debug_start_payloads

    skill_load_starts = [
        payload for payload in start_payloads if payload.get("tool_name") == "skill_load"
    ]
    assert len(skill_load_starts) == 1, skill_load_starts
    assert (
        skill_load_starts[0].get("args", {}).get("name") == "debugging"
    )

    end_payloads = [
        get_trace_payload(trace)
        for trace in traces_response.json()["data"]["items"]
        if trace.get("type") == "tool_call_end"
    ]
    expected_names = set(expected_order)
    end_payloads_by_name = {
        name: [payload for payload in end_payloads if payload.get("tool_name") == name]
        for name in expected_names
    }
    assert all(
        end_payloads_by_name[name] for name in expected_names
    ), end_payloads_by_name
    assert all(
        payload.get("status") == "success"
        for payloads in end_payloads_by_name.values()
        for payload in payloads
    ), end_payloads_by_name

    skill_result = end_payloads_by_name["skill_load"][0].get("result")
    assert '"name":"debugging"' in str(skill_result)
    assert '"mode":"snapshot"' in str(skill_result)

    decoded_results = {
        name: _decode_debug_result(payloads[-1])
        for name, payloads in end_payloads_by_name.items()
        if name
        not in {
            "skill_load",
            "glob",
            "read_file",
        }
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
    assert evaluation.get("expression") == "state.counter += 1", evaluation
    assert evaluation.get("value") == "5", evaluation

    continue_state = decoded_results["continue_execution"].get("state")
    assert isinstance(continue_state, dict), continue_state
    assert continue_state.get("status") == "exited", continue_state

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
    assert model_names == {"debug-e2e-model"}, model_names

    model_requests = [
        log.get("request")
        for log in logs
        if isinstance(log, dict) and isinstance(log.get("request"), dict)
    ]
    assert model_requests, logs
    exposed_tool_names: list[set[str]] = []
    for request in model_requests:
        names: set[str] = set()
        for tool in request.get("tools", []):
            if not isinstance(tool, dict):
                continue
            name = tool.get("name")
            function = tool.get("function")
            if isinstance(name, str):
                names.add(name)
            elif isinstance(function, dict) and isinstance(function.get("name"), str):
                names.add(function["name"])
        exposed_tool_names.append(names)
    assert any("invoke_extension_tool" in names for names in exposed_tool_names)
    debug_target_names = {
        "list_debug_configurations",
        "list_breakpoints",
        "create_debug_configuration",
        "add_breakpoint",
        "start_debugging",
        "evaluate_expression",
        "continue_execution",
    }
    assert all(not debug_target_names & names for names in exposed_tool_names), (
        exposed_tool_names
    )
