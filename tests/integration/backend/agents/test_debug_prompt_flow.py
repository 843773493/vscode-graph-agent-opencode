from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Generator, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import commentjson
import httpx
import pytest

from app.agents.tool_identity import CUSTOM_TOOL_INVOKER_NAME
from tests.support.api_waiters import wait_for_job_done
from tests.support.messages import last_assistant_message
from tests.support.processes import close_backend_process, start_backend_process
from tests.support.trace import get_trace_payload


@dataclass(slots=True)
class ScriptedModelState:
    requests: list[dict[str, object]] = field(default_factory=list)
    fixture_path: str = ""
    worker_path: str = ""
    working_directory: str = ""
    failure: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"模型请求必须是 JSON object: {value!r}")
    return cast(dict[str, object], value)


def _message_tool_count(payload: dict[str, object]) -> int:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise TypeError(f"模型请求缺少 messages 数组: {payload!r}")
    return sum(
        1
        for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"
    )


def _last_tool_content(payload: dict[str, object]) -> str:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise TypeError(f"模型请求缺少 messages 数组: {payload!r}")
    tool_messages = [
        message
        for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"
    ]
    if not tool_messages:
        return ""
    content = tool_messages[-1].get("content")
    return content if isinstance(content, str) else ""


def _tool_call(
    *,
    call_index: int,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    return {
        "id": f"debug-e2e-call-{call_index}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        },
    }


def _stream_response(
    handler: BaseHTTPRequestHandler,
    *,
    request_index: int,
    call: dict[str, object] | None,
    assistant_text: str | None = None,
) -> None:
    if call is None:
        chunks = [
            {
                "id": f"chatcmpl-debug-e2e-{request_index}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "debug-e2e-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": assistant_text or "DEBUG_PROMPT_FLOW_OK",
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": f"chatcmpl-debug-e2e-{request_index}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "debug-e2e-model",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
            },
        ]
    else:
        chunks = [
            {
                "id": f"chatcmpl-debug-e2e-{request_index}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "debug-e2e-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            **(
                                {"content": assistant_text}
                                if assistant_text is not None
                                else {}
                            ),
                            "tool_calls": [call],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": f"chatcmpl-debug-e2e-{request_index}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "debug-e2e-model",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                ],
            },
        ]

    encoded = b"".join(
        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        for chunk in chunks
    ) + b"data: [DONE]\n\n"
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


@pytest.fixture(scope="module")
def scripted_model_server() -> Iterator[tuple[ScriptedModelState, str]]:
    state = ScriptedModelState()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:
            if not self.path.endswith("/chat/completions"):
                self.send_error(404, f"未知模型路径: {self.path}")
                return
            if self.headers.get("Authorization") != "Bearer debug-e2e-key":
                self.send_error(401, "模型 API key 无效")
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            payload = _json_object(
                json.loads(self.rfile.read(content_length).decode("utf-8"))
            )
            with state.lock:
                state.requests.append(payload)
                request_index = len(state.requests) - 1
                tool_count = _message_tool_count(payload)
                skill_load_failed = False

                if tool_count == 1:
                    skill_content = _last_tool_content(payload)
                    if (
                        not skill_content
                        or skill_content.startswith("Error")
                        or "# 源码调试工具" not in skill_content
                        or "invoke_custom_tool" not in skill_content
                    ):
                        state.failure = (
                            "模型在进入调试动作前没有收到有效 debugging Skill 内容: "
                            f"{skill_content[:240]!r}"
                        )
                        skill_load_failed = True

            if skill_load_failed:
                _stream_response(self, request_index=request_index, call=None)
                return

            assistant_text: str | None = None
            scripted_calls = (
                (
                    "glob",
                    {
                        "path": state.working_directory,
                        "pattern": "**/*.mjs",
                    },
                    None,
                ),
                (
                    "read_file",
                    {"path": state.fixture_path},
                    None,
                ),
                (
                    "read_file",
                    {"path": state.worker_path},
                    None,
                ),
                ("list_debug_configurations", {}, None),
                ("list_breakpoints", {}, None),
                (
                    "create_debug_configuration",
                    {
                        "name": "计数流程调试",
                        "fileFullPath": state.fixture_path,
                        "workingDirectory": state.working_directory,
                        "configurationName": "node-default",
                        "arguments": [],
                    },
                    None,
                ),
                (
                    "add_breakpoint",
                    {
                        "fileFullPath": state.fixture_path,
                        "line": 3,
                    },
                    None,
                ),
                (
                    "add_breakpoint",
                    {
                        "fileFullPath": state.worker_path,
                        "line": 2,
                    },
                    None,
                ),
                (
                    "start_debugging",
                    {
                        "fileFullPath": state.fixture_path,
                        "workingDirectory": state.working_directory,
                    },
                    None,
                ),
                (
                    "evaluate_expression",
                    {"expression": "state.counter += 1"},
                    "入口断点停在 state.counter += 2 之前，这里负责累加入口计数；先把当前计数加一。",
                ),
                ("continue_execution", {}, None),
                (
                    "evaluate_expression",
                    {"expression": "state.counter += 1"},
                    "worker 断点停在 state.counter += 10 之前，这里负责被调模块的增量；先把共享计数加一。",
                ),
                ("continue_execution", {}, None),
                ("list_breakpoints", {}, None),
            )
            if tool_count == 0:
                call = _tool_call(
                    call_index=tool_count,
                    name="read_file",
                    arguments={
                        "path": ".boxteam/bundled-skills/debugging/SKILL.md"
                    },
                )
            elif tool_count - 1 < len(scripted_calls):
                name, arguments, assistant_text = scripted_calls[tool_count - 1]
                if name in {"glob", "read_file"}:
                    call = _tool_call(
                        call_index=tool_count,
                        name=name,
                        arguments=arguments,
                    )
                else:
                    call = _tool_call(
                        call_index=tool_count,
                        name="invoke_custom_tool",
                        arguments={"tool_name": name, "arguments": arguments},
                    )
            else:
                call = None
                assistant_text = "两个断点均已解释并完成 counter 加一，目标程序已正常退出。DEBUG_PROMPT_FLOW_OK"
            _stream_response(
                self,
                request_index=request_index,
                call=call,
                assistant_text=assistant_text if tool_count > 0 else None,
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = cast(int, server.server_address[1])
    try:
        yield state, f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def scripted_backend_process(
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    integration_backend_port: int,
    scripted_model_server: tuple[ScriptedModelState, str],
) -> Generator[subprocess.Popen[str], None, None]:
    _state, endpoint = scripted_model_server
    config_path = Path(integration_workspace_config_path)
    config = _json_object(commentjson.loads(config_path.read_text(encoding="utf-8")))
    providers = config["llm"]["providers"]
    if not isinstance(providers, list):
        raise TypeError("测试配置 llm.providers 必须是数组")
    config["llm"]["providers"] = [
        *providers,
        {
            "id": "debug-e2e",
            "endpoint": endpoint,
            "model": "debug-e2e-model",
            "api_key": "debug-e2e-key",
            "custom_llm_provider": "openai",
            "api_mode": "chat_completions",
        }
    ]
    agents = config["agents"]
    if not isinstance(agents, dict):
        raise TypeError("测试配置 agents 必须是对象")
    default_agent = agents["default"]
    if not isinstance(default_agent, dict):
        raise TypeError("测试配置 agents.default 必须是对象")
    model = default_agent["model"]
    if not isinstance(model, dict):
        raise TypeError("测试配置 agents.default.model 必须是对象")
    model["primary_provider"] = "debug-e2e"
    model["fallback_providers"] = []
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    handle = start_backend_process(
        workspace_root=integration_workspace_root_path,
        port=integration_backend_port,
        log_name="debug-prompt-e2e",
        env_overrides={"BOXTEAM_DEFAULT_SKILL_GROUPS": '["debugging"]'},
    )
    try:
        yield handle.process
    finally:
        close_backend_process(handle)


@pytest.fixture
async def scripted_client(
    scripted_backend_process: subprocess.Popen[str],
    integration_backend_port: int,
) -> AsyncIterator[httpx.AsyncClient]:
    del scripted_backend_process
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{integration_backend_port}",
        timeout=60,
        headers={"X-Local-Token": "local-dev-token"},
    ) as client:
        yield client


def _write_debug_fixture(workspace_root: Path) -> tuple[Path, Path]:
    worker_path = workspace_root / "debug-prompt-worker.mjs"
    worker_path.write_text(
        """export function compute(state) {
  state.counter += 10;
  return state.counter;
}
""",
        encoding="utf-8",
    )
    fixture_path = workspace_root / "debug-prompt-fixture.mjs"
    fixture_path.write_text(
        """import { compute } from './debug-prompt-worker.mjs';
const state = { counter: 1 };
state.counter += 2;
const result = compute(state);
console.log(JSON.stringify({ result }));
""",
        encoding="utf-8",
    )
    return fixture_path, worker_path


@pytest.mark.asyncio
async def test_prompt_drives_agent_debug_tools_through_real_backend(
    scripted_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
    scripted_model_server: tuple[ScriptedModelState, str],
) -> None:
    state, _endpoint = scripted_model_server
    workspace_root = Path(integration_workspace_root_path).resolve()
    fixture_path, worker_path = _write_debug_fixture(workspace_root)
    state.fixture_path = fixture_path.relative_to(workspace_root).as_posix()
    state.worker_path = worker_path.relative_to(workspace_root).as_posix()
    state.working_directory = "."

    skill_path = Path.cwd() / "resources" / "skills" / "debugging" / "SKILL.md"
    assert skill_path.is_file()
    assert "start_debugging" in skill_path.read_text(encoding="utf-8")

    create_response = await scripted_client.post(
        "/api/v1/sessions",
        json={"title": "Debug prompt E2E"},
    )
    assert create_response.status_code == 200, create_response.text
    session_id = create_response.json()["data"]["session_id"]

    prompt = (
        "请在工作区里找到刚创建的计数程序入口和它依赖的相关 JavaScript 文件。"
        "先读取 debugging Skill 和可用调试方案；如果没有匹配的具名方案就自己创建。"
        "在入口累加和相关模块累加处设置断点并启动。每次真实停住后，先说明当前代码作用，"
        "再通过调试表达式把当前帧里的 state.counter 加一，然后继续到下一个断点，"
        "最后重新读取状态确认程序正常结束。不要修改源码来伪造计数变化。"
    )
    message_response = await scripted_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": prompt},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert message_response.status_code == 200, message_response.text
    job_id = message_response.json()["data"]["job_id"]
    job = await wait_for_job_done(scripted_client, job_id, max_attempts=120)
    with state.lock:
        assert state.failure is None, state.failure
    assert job["status"] in {"completed", "succeeded"}

    messages_response = await scripted_client.get(
        f"/api/v1/sessions/{session_id}/messages"
    )
    assert messages_response.status_code == 200, messages_response.text
    message_items = messages_response.json()["data"]["items"]
    assert "DEBUG_PROMPT_FLOW_OK" in last_assistant_message(message_items)

    traces_response = await scripted_client.get(
        f"/api/v1/sessions/{session_id}/traces"
    )
    assert traces_response.status_code == 200, traces_response.text
    traces = traces_response.json()["data"]["items"]
    rendered_traces = json.dumps(traces, ensure_ascii=False)
    assert "入口断点停在 state.counter += 2 之前" in rendered_traces
    assert "worker 断点停在 state.counter += 10 之前" in rendered_traces
    tool_names = [
        str(get_trace_payload(trace).get("tool_name"))
        for trace in traces
        if trace.get("type") == "tool_call_start"
    ]
    expected_order = [
        "read_file",
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
    assert tool_names == expected_order
    debug_tool_names = {
        "list_debug_configurations",
        "list_breakpoints",
        "create_debug_configuration",
        "add_breakpoint",
        "start_debugging",
        "evaluate_expression",
        "continue_execution",
    }
    debug_start_traces = [
        trace
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") in debug_tool_names
    ]
    assert debug_start_traces
    assert all(
        get_trace_payload(trace).get("invocation_tool_name")
        == CUSTOM_TOOL_INVOKER_NAME
        for trace in debug_start_traces
    )

    read_file_traces = [
        trace
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") == "read_file"
    ]
    assert len(read_file_traces) == 3
    assert (
        get_trace_payload(read_file_traces[0]).get("args", {}).get("path")
        == ".boxteam/bundled-skills/debugging/SKILL.md"
    )
    assert [
        get_trace_payload(trace).get("args", {}).get("path")
        for trace in read_file_traces[1:]
    ] == [state.fixture_path, state.worker_path]
    read_file_ends = [
        trace
        for trace in traces
        if trace.get("type") == "tool_call_end"
        and get_trace_payload(trace).get("tool_name") == "read_file"
    ]
    assert len(read_file_ends) == 3
    read_file_result = get_trace_payload(read_file_ends[0])
    assert read_file_result.get("status") == "success"
    assert "# 源码调试工具" in str(read_file_result.get("result"))
    assert "invoke_custom_tool" in str(read_file_result.get("result"))

    debug_end_traces = [
        trace
        for trace in traces
        if trace.get("type") == "tool_call_end"
        and get_trace_payload(trace).get("tool_name") in debug_tool_names
    ]
    assert debug_end_traces
    successful_results = [
        str(get_trace_payload(trace)["result"])
        for trace in debug_end_traces
        if get_trace_payload(trace).get("status") != "error"
    ]
    assert successful_results
    assert any(
        '"status":"paused"' in result
        for result in successful_results
    )
    assert any(
        '"status":"exited"' in result
        for result in successful_results
    )

    debug_state_response = await scripted_client.get(
        "/api/v1/debug/node",
        params={"session_id": session_id},
    )
    assert debug_state_response.status_code == 200, debug_state_response.text
    debug_state = debug_state_response.json()["data"]
    assert debug_state["status"] == "exited"
    assert debug_state["active_configuration_name"] == "计数流程调试"
    assert [item["value"] for item in debug_state["evaluations"]] == ["2", "5"]
    assert debug_state["last_stopped_frame"]["path"] == worker_path.name
    assert debug_state["last_stopped_frame"]["line"] == 2
    assert any('{"result":15}' in line for line in debug_state["output"])

    with state.lock:
        model_requests = list(state.requests)
    assert len(model_requests) >= len(expected_order) + 1
    first_request_text = json.dumps(model_requests[0], ensure_ascii=False)
    assert prompt in first_request_text
    assert "debugging" in first_request_text
    first_tools = model_requests[0].get("tools")
    assert isinstance(first_tools, list)
    model_tool_names = {
        tool["function"]["name"]
        for tool in first_tools
        if isinstance(tool, dict)
        and isinstance(tool.get("function"), dict)
        and isinstance(tool["function"].get("name"), str)
    }
    assert "invoke_custom_tool" in model_tool_names
    assert {"glob", "read_file"} <= model_tool_names
    assert not debug_tool_names & model_tool_names
    request_history = json.dumps(model_requests, ensure_ascii=False)
    assert "入口断点停在 state.counter += 2 之前" in request_history
    assert "worker 断点停在 state.counter += 10 之前" in request_history

    configuration_id = debug_state["active_configuration_id"]
    assert isinstance(configuration_id, str)
    human_start = await scripted_client.post(
        "/api/v1/debug/node/start",
        json={
            "session_id": session_id,
            "configuration_id": configuration_id,
            "path": fixture_path.name,
        },
    )
    assert human_start.status_code == 200, human_start.text
    human_state = human_start.json()["data"]
    assert human_state["status"] == "paused"
    assert human_state["call_stack"][0]["path"] == fixture_path.name
    assert human_state["call_stack"][0]["line"] == 3

    for expected_path, expected_line in (
        (fixture_path.name, 3),
        (worker_path.name, 2),
    ):
        assert human_state["call_stack"][0]["path"] == expected_path
        assert human_state["call_stack"][0]["line"] == expected_line
        evaluation_response = await scripted_client.post(
            "/api/v1/debug/node/action",
            json={
                "session_id": session_id,
                "action": "evaluate",
                "params": {"expression": "state.counter += 1"},
            },
        )
        assert evaluation_response.status_code == 200, evaluation_response.text
        human_state = evaluation_response.json()["data"]
        continue_response = await scripted_client.post(
            "/api/v1/debug/node/action",
            json={
                "session_id": session_id,
                "action": "continue",
                "params": {},
            },
        )
        assert continue_response.status_code == 200, continue_response.text
        human_state = continue_response.json()["data"]

    assert human_state["status"] == "exited"
    assert [item["value"] for item in human_state["evaluations"]] == ["2", "5"]
    assert human_state["last_stopped_frame"]["path"] == worker_path.name
    assert any('{"result":15}' in line for line in human_state["output"])
    human_debug_actions = [
        action
        for action in human_state["actions"]
        if action["action"] in {"start", "evaluate", "continue"}
        and action["actor"] == "human"
    ]
    assert [action["action"] for action in human_debug_actions[-5:]] == [
        "start",
        "evaluate",
        "continue",
        "evaluate",
        "continue",
    ]
