from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Generator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import commentjson
import httpx
import pytest

from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.core.path_utils import get_session_path_resolver
from app.core.workspace_identity import read_workspace_id
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationDTO,
    NodeDebugSessionManifestDTO,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.support.api_waiters import wait_for_job_done
from tests.support.e2e_test_control import E2ETestControlHarness
from tests.support.messages import last_assistant_message
from tests.support.model_stream_config import model_stream_replay
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import (
    close_backend_process,
    start_backend_process,
    workspace_backend_id_for_root,
)
from tests.support.trace import get_trace_payload


@dataclass(slots=True)
class ScriptedModelState:
    control: E2ETestControlHarness
    config_path: Path
    artifacts: Path
    call_ids: list[str] = field(default_factory=list)
    fixture_path: str = ""
    worker_path: str = ""
    working_directory: str = ""

    @property
    def requests(self) -> list[dict[str, object]]:
        return [json.loads((self.artifacts / f"{call_id}.json").read_text())["request"]
                for call_id in self.call_ids]


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"模型请求必须是 JSON object: {value!r}")
    return cast(dict[str, object], value)


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


@pytest.fixture(scope="module")
def scripted_model_replay(integration_workspace_root_path: str) -> Iterator[ScriptedModelState]:
    """普通调试业务复用现有 model_stream，不监听模型端口或绕过真实 SDK。"""
    output = Path(integration_workspace_root_path).parent
    with model_stream_replay(output, runtime_root=output / "runtime", business_assertion="debug-prompt-flow") as replay:
        yield ScriptedModelState(replay.control, replay.config_path, replay.artifacts)


def _plan_debug_calls(state: ScriptedModelState, session_id: str, thread_id: str) -> None:
    scripted_calls = (
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
    interactions: list[dict[str, object]] = []
    for index in range(len(scripted_calls) + 2):
        if index == 0:
            call = _tool_call(call_index=index, name="skill_load", arguments={"name": "debugging"})
            text = None
        elif index <= len(scripted_calls):
            name, arguments, text = scripted_calls[index - 1]
            call = _tool_call(call_index=index, name="invoke_extension_tool",
                              arguments={"tool_name": name, "arguments": arguments})
        else:
            call = None
            text = "两个断点均已解释并完成 counter 加一，目标程序已正常退出。DEBUG_PROMPT_FLOW_OK"
        delta: dict[str, object] = {"role": "assistant"}
        if text is not None:
            delta["content"] = text
        if call is not None:
            delta["tool_calls"] = [call]
        cassette = build_cassette(
            asset_id=f"debug-prompt-{index}", model="debug-e2e-model",
            url="https://opencode.ai/zen/v1/chat/completions",
            frames=chat_completion_frames(
                response_id=f"chatcmpl-debug-e2e-{index}", model="debug-e2e-model",
                deltas=(delta,), finish_reason="tool_calls" if call is not None else "stop",
            ),
        )
        interactions.append(cassette.raw["interactions"][0])
    state.call_ids = state.control.plan_model_calls(session_id, thread_id, interactions)
    # 首次 Skill 真实返回后、下一模型响应前核验请求，保留原来的先验收再执行顺序。
    state.control.arm(state.call_ids[1], "model.response_ready")


@pytest.fixture(scope="module")
def scripted_debug_backend_port(request: pytest.FixtureRequest) -> int:
    # 文件专属端口块内避开本机常驻服务占用的起始端口，不回收未知进程。
    return integration_port_block_for_file(Path(request.node.path)).port(20)


@pytest.fixture(scope="module")
def scripted_backend_process(
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    scripted_debug_backend_port: int,
    scripted_model_replay: ScriptedModelState,
) -> Generator[subprocess.Popen[str], None, None]:
    state = scripted_model_replay
    endpoint = "https://opencode.ai/zen/v1"
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
            "api_mode": {
                "protocol": "chat_completions",
                "model_info": {"supports_function_calling": True, "supports_reasoning": True},
                "supports_reasoning": {"reasoning_content": True},
            },
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
    workspace_root = Path(integration_workspace_root_path)
    config.setdefault("runtime", {}).setdefault("debug", {}).setdefault("launch_profiles", {})[
        "capability-entry"
    ] = {
        "adapter": "node_inspector", "runtime": "node",
        "program": str(workspace_root / "debug-prompt-fixture.mjs"),
        "working_directory": str(workspace_root), "args": [],
    }
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    handle = start_backend_process(
        workspace_root=integration_workspace_root_path,
        port=scripted_debug_backend_port,
        log_name="debug-prompt-e2e",
        asgi_app="tests.support.section8_backend:app",
        env_overrides={
            "BOXTEAM_DEFAULT_SKILL_GROUPS": '["debugging"]',
            "BOXTEAM_TEST_MODEL_STREAM_CONFIG": str(state.config_path),
            **state.control.environment,
        },
    )
    try:
        yield handle.process
    finally:
        close_backend_process(handle)


@pytest.fixture
async def scripted_client(
    scripted_backend_process: subprocess.Popen[str],
    scripted_debug_backend_port: int,
    integration_workspace_root_path: str,
) -> AsyncIterator[httpx.AsyncClient]:
    del scripted_backend_process
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{scripted_debug_backend_port}",
        timeout=60,
        headers={
            "X-Local-Token": "local-dev-token",
            "X-BoxTeam-Workspace-Id": workspace_backend_id_for_root(
                integration_workspace_root_path
            ),
        },
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


async def _session_main_thread_id(
    client: httpx.AsyncClient,
    session_id: str,
) -> str:
    response = await client.get(f"/api/v1/sessions/{session_id}/main-thread")
    assert response.status_code == 200, response.text
    return response.json()["data"]["thread_id"]


@pytest.mark.asyncio
async def test_corrupt_debug_storage_http_errors_keep_owner_without_native_location(
    scripted_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
) -> None:
    """冷入口实际读取损坏原件，错误回执不能复制底层位置或原始输入。"""
    workspace_root = Path(integration_workspace_root_path)
    resolver = get_session_path_resolver(
        workspace_root / ".boxteam/sessions", workspace_id=read_workspace_id(workspace_root),
    )
    store = NodeDebugSessionStore(resolver)
    configuration_id = "dbgcfg_" + "1" * 32
    for artifact, corruption in (
        ("configuration", "json"), ("configuration", "schema"),
        ("configuration", "extra"), ("configuration", "id"),
        ("manifest", "json"), ("manifest", "owner"),
    ):
        created = await scripted_client.post("/api/v1/sessions", json={"title": "损坏调试数据诊断"})
        assert created.status_code == 200, created.text
        session_id = created.json()["data"]["session_id"]
        thread_id = await _session_main_thread_id(scripted_client, session_id)
        now = datetime.now(UTC)
        store.write_configuration(session_id, NodeDebugConfigurationDTO(
            configuration_id=configuration_id, name="主方案", created_at=now, updated_at=now,
        ), thread_id)
        store.write_manifest(NodeDebugSessionManifestDTO(
            session_id=session_id, thread_id=thread_id, updated_at=now,
        ))
        directory = resolver.resolve_thread_node(session_id, thread_id) / "debug/node"
        path = (
            directory / "manifest.json" if artifact == "manifest"
            else directory / "configurations" / f"{configuration_id}.json"
        )
        original = path.read_bytes()
        payload = json.loads(original)
        private_input = str(directory / "private-input")
        if corruption == "json":
            corrupted = b"{"
        else:
            if corruption == "schema":
                payload["updated_at"] = [private_input]
            elif corruption == "extra":
                payload[private_input] = "invalid"
            elif corruption == "id":
                payload["configuration_id"] = "dbgcfg_" + "2" * 32
            else:
                payload["session_id"] = private_input
            corrupted = json.dumps(payload).encode()
        path.write_bytes(corrupted)
        request_id = f"debug-storage-{artifact}-{corruption}"
        endpoint = f"/api/v1/debug/node/configurations/{configuration_id}"
        response = await scripted_client.get(
            endpoint, params={"session_id": session_id, "thread_id": thread_id},
            headers={"X-Request-ID": request_id},
        )
        assert response.status_code == 409, response.text
        body = response.json()
        assert response.headers["X-Request-ID"] == body["request_id"] == request_id
        assert session_id in response.text and thread_id in response.text
        assert str(workspace_root) not in response.text
        assert private_input not in response.text
        assert path.read_bytes() == corrupted
        # 显式修复原件后沿同一真实入口重试，不能缓存失败或伪造空配置。
        path.write_bytes(original)
        repaired = await scripted_client.get(
            endpoint, params={"session_id": session_id, "thread_id": thread_id},
        )
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["data"]["configuration_id"] == configuration_id


@pytest.mark.asyncio
async def test_prompt_drives_agent_debug_tools_through_real_backend(
    scripted_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
    scripted_model_replay: ScriptedModelState,
) -> None:
    state = scripted_model_replay
    workspace_root = Path(integration_workspace_root_path).resolve()
    file_paths = WorkspaceFilePaths(
        workspace=workspace_root, workspace_id=read_workspace_id(workspace_root), bindings=None,
    )
    assert not (workspace_root / "debug-prompt-fixture.mjs").exists()
    capabilities_response = await scripted_client.get("/api/v1/debug/node/capabilities")
    assert capabilities_response.status_code == 200, capabilities_response.text
    capabilities = capabilities_response.json()
    assert capabilities["request_id"] == capabilities_response.headers["X-Request-ID"]
    assert str(workspace_root) not in capabilities_response.text
    launch_profile = next(p for p in capabilities["data"]["launch_profiles"]
                          if p["name"] == "capability-entry")
    assert launch_profile["program"] == file_paths.resolve("debug-prompt-fixture.mjs")[1]
    assert launch_profile["working_directory"] == file_paths.workspace_uri
    fixture_path, worker_path = _write_debug_fixture(workspace_root)
    state.fixture_path = fixture_path.relative_to(workspace_root).as_posix()
    state.worker_path = worker_path.relative_to(workspace_root).as_posix()
    state.working_directory = "."

    skill_path = Path.cwd() / "resources" / "skills" / "debugging" / "SKILL.md"
    assert skill_path.is_file()
    skill_text = skill_path.read_text(encoding="utf-8")
    assert "start_debugging" in skill_text
    assert "先看状态再行动" in skill_text
    assert "invalid_breakpoints" in skill_text
    assert "不需要先交接控制权" in skill_text
    assert "先用 `read_file` 读取 `.boxteam/bundled-skills/debugging/SKILL.md`" not in skill_text
    assert "普通 `read_file` 可读取 Skill 包文件" in skill_text
    assert "读取不会激活或追踪来源，不能代替 `skill_load`" in skill_text

    create_response = await scripted_client.post(
        "/api/v1/sessions",
        json={"title": "Debug prompt E2E"},
    )
    assert create_response.status_code == 200, create_response.text
    session_id = create_response.json()["data"]["session_id"]
    main_thread_id = await _session_main_thread_id(scripted_client, session_id)

    prompt = (
        f"请调试入口文件 {state.fixture_path} 及其依赖文件 {state.worker_path}，工作目录是 .。"
        "先使用 skill_load(name=debugging) 加载 debugging Skill 和可用调试方案；"
        "普通读取不能代替 Skill 激活；如果没有匹配的具名方案就自己创建。"
        "在入口累加和相关模块累加处设置断点并启动。每次真实停住后，先说明当前代码作用，"
        "再通过调试表达式把当前帧里的 state.counter 加一，然后继续到下一个断点，"
        "最后重新读取状态确认程序正常结束。不要修改源码来伪造计数变化。"
    )
    _plan_debug_calls(state, session_id, main_thread_id)
    message_response = await scripted_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": prompt},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert message_response.status_code == 200, message_response.text
    job_id = message_response.json()["data"]["job_id"]
    try:
        await asyncio.to_thread(state.control.acknowledged, state.call_ids[1], "model.response_ready")
        skill_request = json.loads((state.artifacts / f"{state.call_ids[1]}.json").read_text())["request"]
        skill_content = json.dumps(skill_request, ensure_ascii=False)
        assert "# 源码调试工具" in skill_content
        assert "invoke_extension_tool" in skill_content
    finally:
        state.control.release(state.call_ids[1], "model.response_ready")
    job = await wait_for_job_done(scripted_client, job_id, max_attempts=120)
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
        "skill_load",
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
        == EXTENSION_TOOL_INVOKER_NAME
        for trace in debug_start_traces
    )
    assert "read_file" not in tool_names
    assert "invoke_custom_tool" not in json.dumps(traces, ensure_ascii=False)

    skill_load_starts = [
        trace
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") == "skill_load"
    ]
    assert len(skill_load_starts) == 1
    skill_load_args = get_trace_payload(skill_load_starts[0]).get("args")
    assert isinstance(skill_load_args, dict)
    assert skill_load_args.get("name") == "debugging"
    # mode 是可选参数；未传入时由 SkillLoadInput 的默认值生效。
    assert skill_load_args.get("mode", "snapshot") == "snapshot"
    skill_load_ends = [
        trace
        for trace in traces
        if trace.get("type") == "tool_call_end"
        and get_trace_payload(trace).get("tool_name") == "skill_load"
    ]
    assert len(skill_load_ends) == 1
    skill_load_result = get_trace_payload(skill_load_ends[0])
    assert skill_load_result.get("status") == "success"
    skill_load_receipt = json.loads(str(skill_load_result.get("result")))
    assert skill_load_receipt["mode"] == "snapshot"
    assert skill_load_receipt["name"] == "debugging"
    assert skill_load_receipt["queued"] is True
    assert skill_load_receipt["tracked"] is False
    assert skill_load_receipt["revision"]

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
    for result in successful_results:
        assert "file://" not in result
        assert str(workspace_root) not in result
    assert any(file_paths.reference(fixture_path) in result for result in successful_results)
    assert any(file_paths.reference(worker_path) in result for result in successful_results)

    debug_state_response = await scripted_client.get(
        "/api/v1/debug/node",
        params={"session_id": session_id},
    )
    assert debug_state_response.status_code == 200, debug_state_response.text
    debug_state = debug_state_response.json()["data"]
    assert debug_state["thread_id"] == main_thread_id
    assert debug_state["status"] == "exited"
    assert debug_state["active_configuration_name"] == "计数流程调试"
    assert [item["value"] for item in debug_state["evaluations"]] == ["2", "5"]
    assert debug_state["last_stopped_frame"]["path"] == worker_path.name
    assert debug_state["last_stopped_frame"]["url"] == file_paths.reference(worker_path)
    assert debug_state["last_stopped_frame"]["line"] == 2
    assert any('{"result":15}' in line for line in debug_state["output"])

    model_requests = state.requests
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
    assert "invoke_extension_tool" in model_tool_names
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
            "thread_id": main_thread_id,
            "configuration_id": configuration_id,
            "path": launch_profile["program"],
            "working_directory": launch_profile["working_directory"],
        },
    )
    assert human_start.status_code == 200, human_start.text
    human_state = human_start.json()["data"]
    assert human_state["status"] == "paused"
    assert human_state["call_stack"][0]["path"] == fixture_path.name
    assert human_state["call_stack"][0]["url"] == file_paths.reference(fixture_path)
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
                "thread_id": main_thread_id,
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
                "thread_id": main_thread_id,
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
