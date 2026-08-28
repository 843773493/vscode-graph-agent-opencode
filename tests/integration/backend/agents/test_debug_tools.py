from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.debugging import create_debugging_tools
from app.schemas.internal_v2.node_debug import NodeDebugConfigurationCreateRequest
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.node_debug_service import NodeDebugService
from app.services.infrastructure.node_debug_session_store import NodeDebugSessionStore

# 本文件中的 catalog 检查走真实后端 HTTP；下面直接调用工具 factory 的用例是
# Node Inspector 适配器集成检查，不把它们当作“提示词驱动的完整 E2E”。


class _SessionPathResolverStub:
    def __init__(self, root: Path) -> None:
        self._root = root

    def resolve_session_node(self, session_id: str) -> Path:
        path = self._root / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path


def _write_debug_fixture(workspace_root: Path) -> tuple[Path, int]:
    source = """const globalValue = 11;
function compute(input) {
  const localValue = input + 1;
  const user = { name: 'Ada' };
  return { localValue, user };
}
const result = compute(41);
console.log(JSON.stringify(result));"""
    fixture_path = workspace_root / "debug-fixture.mjs"
    fixture_path.write_text(source + "\n", encoding="utf-8")
    return fixture_path, 3


def _write_secret_debug_fixture(workspace_root: Path) -> tuple[Path, int]:
    source = """function inspectSecrets() {
  const password = 'hunter2';
  const renamed = 'ghp_1234567890abcdefghijklmnopqrstuv';
  const tokenCount = 42;
  return { password, renamed, tokenCount };
}
console.log(inspectSecrets());"""
    fixture_path = workspace_root / "debug-secret-fixture.mjs"
    fixture_path.write_text(source + "\n", encoding="utf-8")
    return fixture_path, 5


def _write_cross_file_debug_fixture(
    workspace_root: Path,
) -> tuple[Path, Path, int, int]:
    worker_path = workspace_root / "debug-worker.mjs"
    worker_path.write_text(
        """export function transform(value) {
  const doubled = value * 2;
  return { value, doubled };
}
""",
        encoding="utf-8",
    )
    entry_path = workspace_root / "debug-entry.mjs"
    entry_path.write_text(
        """import { transform } from './debug-worker.mjs';
const input = 23;
const snapshot = transform(input);
console.log(JSON.stringify(snapshot));
""",
        encoding="utf-8",
    )
    return entry_path, worker_path, 3, 2


def _write_special_breakpoint_fixture(workspace_root: Path) -> tuple[Path, int]:
    fixture_path = workspace_root / "debug-special-breakpoints.mjs"
    fixture_path.write_text(
        """let total = 0;
for (let index = 1; index <= 4; index += 1) {
  total += index;
}
console.log(`total=${total}`);
""",
        encoding="utf-8",
    )
    return fixture_path, 3


async def _wait_for_debug_state(
    service: NodeDebugService,
    session_id: str,
    *,
    expected_status: str,
    output_fragment: str | None = None,
):
    state = await service.get_state(session_id)
    for _ in range(300):
        if state.status == expected_status and (
            output_fragment is None
            or any(output_fragment in line for line in state.output)
        ):
            return state
        await asyncio.sleep(0.01)
        state = await service.get_state(session_id)
    raise AssertionError(
        f"等待调试状态失败: status={state.status}, output={state.output!r}"
    )


def _tool_map(
    workspace_root: Path,
    service: NodeDebugService,
) -> dict[str, object]:
    return {
        tool.name: tool
        for tool in create_debugging_tools(
            session_id="ses_e2e_debug",
            workspace_root=workspace_root,
            node_debug_service=service,
            invocation_context=ToolInvocationContext(),
        )
    }


def _payload(result: object) -> dict[str, object]:
    assert isinstance(result, str)
    return json.loads(result)


def _relative_path(workspace_root: Path, file_path: Path) -> str:
    return file_path.relative_to(workspace_root).as_posix()


@pytest.mark.asyncio
async def test_backend_catalog_exposes_debug_custom_tool_group(
    integration_client: httpx.AsyncClient,
) -> None:
    response = await integration_client.get(
        "/api/v1/tools", params={"agent_id": "default"}
    )

    assert response.status_code == 200, response.text
    tools = {item["tool_id"]: item for item in response.json()["data"]}
    expected_names = {
        "list_debug_configurations",
        "create_debug_configuration",
        "activate_debug_configuration",
        "delete_debug_configuration",
        "start_debugging",
        "stop_debugging",
        "step_over",
        "step_into",
        "step_out",
        "continue_execution",
        "pause_execution",
        "restart_debugging",
        "add_breakpoint",
        "add_logpoint",
        "remove_breakpoint",
        "clear_all_breakpoints",
        "list_breakpoints",
        "list_variable_names",
        "get_variables_values",
        "evaluate_expression",
    }

    assert expected_names <= tools.keys()
    assert tools["start_debugging"]["group_id"] == "debugging"
    assert tools["start_debugging"]["kind"] == "debugging"
    assert tools["start_debugging"]["parameters"] == {}
    assert tools["start_debugging"]["description"].endswith(
        "必须通过 invoke_custom_tool 调用。"
    )


@pytest.mark.asyncio
async def test_node_inspector_tool_adapter_integration_session(
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> None:
    workspace_root = Path(integration_workspace_root_path).resolve()
    fixture_path, breakpoint_line = _write_debug_fixture(workspace_root)
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=Path(integration_workspace_config_path),
        workspace_root=workspace_root,
    )
    config_service.validate_workspace_config()
    service = NodeDebugService(
        workspace_root=workspace_root,
        config_service=config_service,
    )
    tools = _tool_map(workspace_root, service)

    try:
        breakpoint_result = _payload(
            await tools["add_breakpoint"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, fixture_path),
                    "line": breakpoint_line,
                }
            )
        )
        assert breakpoint_result["ok"] is True
        assert breakpoint_result["state"]["status"] == "idle"

        start_result = _payload(
            await tools["start_debugging"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, fixture_path),
                    "workingDirectory": ".",
                }
            )
        )
        assert start_result["ok"] is True, start_result
        assert start_result["state"]["status"] == "paused"
        assert start_result["state"]["error_message"] is None
        assert "inspector_url" not in start_result["state"]
        assert "session_id" not in start_result["state"]
        assert "pid" not in start_result["state"]
        assert "call_frame_id" not in start_result["state"]["call_stack"][0]
        assert "tool_call_id" not in start_result["state"]["actions"][-1]
        assert "action_id" not in start_result["state"]["actions"][-1]
        assert start_result["state"]["call_stack"][0]["path"] == (
            fixture_path.relative_to(workspace_root).as_posix()
        )
        assert start_result["state"]["call_stack"][0]["line"] == breakpoint_line
        assert start_result["state"]["actions"][-1]["tool_name"] == ("start_debugging")
        assert start_result["state"]["actions"][-1]["actor"] == "ai"
        assert not any(
            action["action"] == "start" and action["tool_name"] is None
            for action in start_result["state"]["actions"]
        )

        names_result = _payload(
            await tools["list_variable_names"].ainvoke({"scope": "local"})
        )
        names = {item["name"] for item in names_result["variables"]}
        assert "input" in names

        values_result = _payload(
            await tools["get_variables_values"].ainvoke(
                {"variableNames": ["input"], "scope": "local"}
            )
        )
        assert values_result["variables"][0]["value"] == "41"

        evaluation_result = _payload(
            await tools["evaluate_expression"].ainvoke({"expression": "input + 1"})
        )
        assert evaluation_result["ok"] is True
        assert evaluation_result["state"]["last_evaluation"]["value"] == "42"

        step_result = _payload(await tools["step_over"].ainvoke({}))
        assert step_result["ok"] is True
        assert step_result["state"]["status"] == "paused"
        assert step_result["state"]["error_message"] is None
        stepped_names = _payload(
            await tools["get_variables_values"].ainvoke(
                {"variableNames": ["localValue"], "scope": "local"}
            )
        )
        assert stepped_names["variables"][0]["value"] == "42"

        continue_result = _payload(await tools["continue_execution"].ainvoke({}))
        assert continue_result["ok"] is True
        assert continue_result["state"]["status"] in {"running", "exited"}

        listed_result = _payload(await tools["list_breakpoints"].ainvoke({}))
        assert listed_result["ok"] is True
        assert listed_result["state"]["breakpoints"][0]["condition"] is None
        assert listed_result["state"]["actions"][-1]["tool_name"] == "list_breakpoints"

        restarted_from_start = _payload(
            await tools["start_debugging"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, fixture_path),
                    "workingDirectory": ".",
                }
            )
        )
        assert restarted_from_start["ok"] is True
        assert restarted_from_start["state"]["status"] == "paused"
        assert len(restarted_from_start["state"]["breakpoints"]) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_node_inspector_tool_adapter_integration_isolation(
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> None:
    workspace_root = Path(integration_workspace_root_path).resolve()
    fixture_path, breakpoint_line = _write_debug_fixture(workspace_root)
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=Path(integration_workspace_config_path),
        workspace_root=workspace_root,
    )
    config_service.validate_workspace_config()
    service = NodeDebugService(
        workspace_root=workspace_root,
        config_service=config_service,
    )
    tools = create_debugging_tools(
        session_id="ses_e2e_debug_isolated",
        workspace_root=workspace_root,
        node_debug_service=service,
        invocation_context=ToolInvocationContext(),
    )
    by_name = {tool.name: tool for tool in tools}
    other_tools = {
        tool.name: tool
        for tool in create_debugging_tools(
            session_id="ses_other_debug_session",
            workspace_root=workspace_root,
            node_debug_service=service,
            invocation_context=ToolInvocationContext(),
        )
    }

    try:
        virtual_root_configuration = _payload(
            await by_name["create_debug_configuration"].ainvoke(
                {
                    "name": "Agent 虚拟根路径",
                    "fileFullPath": fixture_path.name,
                    "workingDirectory": ".",
                }
            )
        )
        assert virtual_root_configuration["ok"] is True
        assert virtual_root_configuration["state"]["script_path"] == fixture_path.name
        assert virtual_root_configuration["state"]["working_directory"] == ""

        unsupported_test_result = _payload(
            await by_name["start_debugging"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, fixture_path),
                    "workingDirectory": ".",
                    "testName": "compute",
                }
            )
        )
        assert unsupported_test_result["ok"] is False
        assert unsupported_test_result["error"]["code"] == "UNSUPPORTED_TEST_TARGET"

        invalid_path_result = _payload(
            await by_name["start_debugging"].ainvoke(
                {
                    "fileFullPath": "../outside.mjs",
                    "workingDirectory": ".",
                }
            )
        )
        assert invalid_path_result["ok"] is False
        assert invalid_path_result["error"]["code"] == "INVALID_DEBUG_ARGUMENT"

        logpoint_result = _payload(
            await by_name["add_logpoint"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, fixture_path),
                    "line": breakpoint_line,
                    "logMessage": "localValue={localValue}",
                }
            )
        )
        assert logpoint_result["ok"] is True
        assert logpoint_result["state"]["status"] == "idle"
        assert logpoint_result["state"]["breakpoints"][0]["log_message"] == (
            "localValue={localValue}"
        )
        assert "inspector_url" not in logpoint_result["state"]

        removed_logpoint = _payload(
            await by_name["remove_breakpoint"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, fixture_path),
                    "line": breakpoint_line,
                }
            )
        )
        assert removed_logpoint["ok"] is True

        other_session = await service.get_state("ses_other_debug_session")
        assert other_session.status == "idle"
        assert other_session.breakpoints == []
        assert other_session.actions == []

        add_result = _payload(
            await by_name["add_breakpoint"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, fixture_path),
                    "line": breakpoint_line,
                    "condition": "input === 41",
                }
            )
        )
        assert add_result["state"]["breakpoints"][0]["path"] == (
            fixture_path.relative_to(workspace_root).as_posix()
        )
        assert "session_id" not in add_result["state"]
        assert "breakpoint_id" not in add_result["state"]["breakpoints"][0]
        assert "inspector_id" not in add_result["state"]["breakpoints"][0]
        start_result = _payload(
            await by_name["start_debugging"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, fixture_path),
                    "workingDirectory": ".",
                }
            )
        )
        assert start_result["ok"] is True
        assert start_result["state"]["status"] == "paused"
        assert start_result["state"]["breakpoints"][0]["condition"] == "input === 41"
        other_add_result = _payload(
            await other_tools["add_breakpoint"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, fixture_path),
                    "line": breakpoint_line,
                }
            )
        )
        assert other_add_result["ok"] is True
        other_start_result = _payload(
            await other_tools["start_debugging"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, fixture_path),
                    "workingDirectory": ".",
                }
            )
        )
        assert other_start_result["ok"] is True
        assert other_start_result["state"]["status"] == "paused"
        state = await service.get_state("ses_e2e_debug_isolated")
        other_state = await service.get_state("ses_other_debug_session")
        assert state.pid is not None
        assert other_state.pid is not None
        assert state.pid != other_state.pid
        assert "pid" not in start_result["state"]
        assert "pid" not in other_start_result["state"]
        stop_result = _payload(await by_name["stop_debugging"].ainvoke({}))
        assert stop_result["ok"] is True
        other_stop_result = _payload(await other_tools["stop_debugging"].ainvoke({}))
        assert other_stop_result["ok"] is True
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_node_inspector_hit_count_and_logpoint_runtime_semantics(
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> None:
    workspace_root = Path(integration_workspace_root_path).resolve()
    fixture_path, breakpoint_line = _write_special_breakpoint_fixture(workspace_root)
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=Path(integration_workspace_config_path),
        workspace_root=workspace_root,
    )
    service = NodeDebugService(
        workspace_root=workspace_root,
        config_service=config_service,
    )
    tools = _tool_map(workspace_root, service)

    try:
        added = _payload(
            await tools["add_breakpoint"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, fixture_path),
                    "line": breakpoint_line,
                    "hitCondition": 3,
                }
            )
        )
        assert added["ok"] is True
        assert added["state"]["breakpoints"][0]["hit_condition"] == 3

        started = _payload(
            await tools["start_debugging"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, fixture_path),
                    "workingDirectory": ".",
                }
            )
        )
        assert started["state"]["status"] == "paused"
        values = _payload(
            await tools["get_variables_values"].ainvoke(
                {"variableNames": ["index"], "scope": "local"}
            )
        )
        assert values["variables"][0]["value"] == "3"

        await tools["continue_execution"].ainvoke({})
        await _wait_for_debug_state(
            service,
            "ses_e2e_debug",
            expected_status="exited",
        )
        await tools["clear_all_breakpoints"].ainvoke({})
        logpoint = _payload(
            await tools["add_logpoint"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, fixture_path),
                    "line": breakpoint_line,
                    "logMessage": "index={index}",
                    "hitCondition": 2,
                }
            )
        )
        assert logpoint["ok"] is True

        restarted = _payload(await tools["restart_debugging"].ainvoke({}))
        assert restarted["state"]["status"] in {"running", "exited"}
        completed = await _wait_for_debug_state(
            service,
            "ses_e2e_debug",
            expected_status="exited",
            output_fragment="[日志点] index=2",
        )
        assert completed.call_stack == []
        assert any(line == "total=10" for line in completed.output)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_node_inspector_agent_results_enforce_least_privilege_and_redaction(
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> None:
    workspace_root = Path(integration_workspace_root_path).resolve()
    fixture_path, breakpoint_line = _write_secret_debug_fixture(workspace_root)
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=Path(integration_workspace_config_path),
        workspace_root=workspace_root,
    )
    config_service.validate_workspace_config()
    service = NodeDebugService(
        workspace_root=workspace_root,
        config_service=config_service,
    )
    tools = _tool_map(workspace_root, service)

    try:
        await tools["add_breakpoint"].ainvoke(
            {
                "fileFullPath": _relative_path(workspace_root, fixture_path),
                "line": breakpoint_line,
            }
        )
        start_result = _payload(
            await tools["start_debugging"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, fixture_path),
                    "workingDirectory": ".",
                }
            )
        )
        assert start_result["state"]["status"] == "paused"
        assert start_result["state"]["call_stack"][0]["variables"] == []
        assert "hunter2" not in json.dumps(start_result)
        assert "ghp_" not in json.dumps(start_result)

        values_result = _payload(
            await tools["get_variables_values"].ainvoke(
                {
                    "variableNames": ["password", "renamed", "tokenCount"],
                    "scope": "local",
                }
            )
        )
        values = {item["name"]: item["value"] for item in values_result["variables"]}
        assert values == {
            "password": "<redacted: possible secret>",
            "renamed": "<redacted: possible secret>",
            "tokenCount": "42",
        }
        assert values_result["redaction_notice"]

        evaluation_result = _payload(
            await tools["evaluate_expression"].ainvoke({"expression": "password"})
        )
        assert evaluation_result["state"]["last_evaluation"]["value"] == (
            "<redacted: possible secret>"
        )
        assert evaluation_result["redaction_notice"]
        assert "hunter2" not in json.dumps(evaluation_result)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_human_and_agent_share_cross_file_debug_runtime(
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> None:
    workspace_root = Path(integration_workspace_root_path).resolve()
    entry_path, worker_path, entry_line, worker_line = _write_cross_file_debug_fixture(
        workspace_root
    )
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=Path(integration_workspace_config_path),
        workspace_root=workspace_root,
    )
    config_service.validate_workspace_config()
    service = NodeDebugService(
        workspace_root=workspace_root,
        config_service=config_service,
    )
    tools = _tool_map(workspace_root, service)

    try:
        await tools["add_breakpoint"].ainvoke(
            {
                "fileFullPath": _relative_path(workspace_root, entry_path),
                "line": entry_line,
            }
        )
        await tools["add_breakpoint"].ainvoke(
            {
                "fileFullPath": _relative_path(workspace_root, worker_path),
                "line": worker_line,
            }
        )
        started = _payload(
            await tools["start_debugging"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, entry_path),
                    "workingDirectory": ".",
                }
            )
        )
        assert started["state"]["status"] == "paused"
        assert started["state"]["call_stack"][0]["path"] == "debug-entry.mjs"
        assert started["state"]["call_stack"][0]["line"] == entry_line

        # 人类通过 Web API 使用的同一服务直接继续，不需要接管或交接。
        human_state = await service.apply_action(
            session_id="ses_e2e_debug",
            action="continue",
            params={},
        )
        assert human_state.status == "paused"
        assert human_state.call_stack[0].path == "debug-worker.mjs"
        assert human_state.call_stack[0].line == worker_line
        assert human_state.actions[-1].actor == "human"

        values = _payload(
            await tools["get_variables_values"].ainvoke(
                {"variableNames": ["value"], "scope": "local"}
            )
        )
        assert values["variables"][0]["value"] == "23"
        completed = _payload(await tools["continue_execution"].ainvoke({}))
        assert completed["state"]["status"] in {"running", "exited"}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_active_debug_session_invalidates_changed_source_without_blocking_controls(
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> None:
    workspace_root = Path(integration_workspace_root_path).resolve()
    entry_path, worker_path, _entry_line, worker_line = _write_cross_file_debug_fixture(
        workspace_root
    )
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=Path(integration_workspace_config_path),
        workspace_root=workspace_root,
    )
    config_service.validate_workspace_config()
    service = NodeDebugService(
        workspace_root=workspace_root,
        config_service=config_service,
    )
    tools = _tool_map(workspace_root, service)

    try:
        configured = _payload(
            await tools["create_debug_configuration"].ainvoke(
                {
                    "name": "跨文件失效提醒",
                    "fileFullPath": _relative_path(workspace_root, entry_path),
                    "workingDirectory": ".",
                    "configurationName": "node-default",
                    "arguments": [],
                }
            )
        )
        assert configured["ok"] is True
        await tools["add_breakpoint"].ainvoke(
            {
                "fileFullPath": _relative_path(workspace_root, worker_path),
                "line": worker_line,
            }
        )
        started = _payload(
            await tools["start_debugging"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, entry_path),
                    "workingDirectory": ".",
                }
            )
        )
        assert started["state"]["call_stack"][0]["path"] == "debug-worker.mjs"

        original = worker_path.read_text(encoding="utf-8")
        worker_path.write_text("// 已插入说明\n" + original, encoding="utf-8")
        changed = await service.get_state("ses_e2e_debug")
        assert changed.requires_restart is True
        assert changed.source_changed_paths == ["debug-worker.mjs"]
        assert changed.breakpoints[0].line == worker_line
        assert changed.breakpoints[0].relocation_status == "pending_update"

        # 源码变化只让断点失效，不阻止人类继续当前已经加载的 Node 代码。
        continued = await service.apply_action(
            session_id="ses_e2e_debug",
            action="continue",
            params={},
        )
        if continued.status == "running":
            continued = await _wait_for_debug_state(
                service,
                "ses_e2e_debug",
                expected_status="exited",
            )
        assert continued.status == "exited"
        assert continued.breakpoints[0].relocation_status == "pending_update"

        restarted = _payload(await tools["restart_debugging"].ainvoke({}))
        assert restarted["state"]["status"] == "exited"
        assert restarted["state"]["requires_restart"] is False
        assert restarted["state"]["breakpoints"][0]["relocation_status"] == "pending_update"
        assert restarted["invalid_breakpoints"][0]["path"] == "debug-worker.mjs"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_start_and_finish_report_invalid_breakpoints_at_both_boundaries(
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> None:
    workspace_root = Path(integration_workspace_root_path).resolve()
    entry_path, worker_path, entry_line, worker_line = _write_cross_file_debug_fixture(
        workspace_root
    )
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=Path(integration_workspace_config_path),
        workspace_root=workspace_root,
    )
    config_service.validate_workspace_config()
    service = NodeDebugService(
        workspace_root=workspace_root,
        config_service=config_service,
    )
    tools = _tool_map(workspace_root, service)

    try:
        configured = _payload(
            await tools["create_debug_configuration"].ainvoke(
                {
                    "name": "开始结束失效反馈",
                    "fileFullPath": _relative_path(workspace_root, entry_path),
                    "workingDirectory": ".",
                    "configurationName": "node-default",
                    "arguments": [],
                }
            )
        )
        assert configured["ok"] is True
        await tools["add_breakpoint"].ainvoke(
            {
                "fileFullPath": _relative_path(workspace_root, entry_path),
                "line": entry_line,
            }
        )
        await tools["add_breakpoint"].ainvoke(
            {
                "fileFullPath": _relative_path(workspace_root, worker_path),
                "line": worker_line,
            }
        )

        # 启动前只修改 worker，让它的断点先失效；entry 断点仍应真实命中。
        worker_path.write_text(
            "// 启动前新增说明\n" + worker_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        started = _payload(
            await tools["start_debugging"].ainvoke(
                {
                    "fileFullPath": _relative_path(workspace_root, entry_path),
                    "workingDirectory": ".",
                }
            )
        )
        assert started["ok"] is True
        assert started["state"]["status"] == "paused"
        assert started["state"]["call_stack"][0]["path"] == "debug-entry.mjs"
        assert started["state"]["call_stack"][0]["line"] == entry_line
        assert started["invalid_breakpoints"] == [
            {
                "path": "debug-worker.mjs",
                "line": worker_line,
                "column": 1,
                "original_line": worker_line,
                "relocation_status": "pending_update",
                "relocation_message": (
                    "源码已变化，断点未自动重定位；请检查后重新设置 "
                    f"debug-worker.mjs:{worker_line}"
                ),
            }
        ]

        # 运行中再修改 entry，让第二个原本有效的断点也失效。
        entry_path.write_text(
            "// 运行中新增说明\n" + entry_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        changed = await service.get_state("ses_e2e_debug")
        assert changed.status == "paused"
        assert {breakpoint.relocation_status for breakpoint in changed.breakpoints} == {
            "pending_update"
        }
        assert {breakpoint.path for breakpoint in changed.breakpoints} == {
            "debug-entry.mjs",
            "debug-worker.mjs",
        }

        finished = _payload(await tools["continue_execution"].ainvoke({}))
        if finished["state"]["status"] == "running":
            # 目标进程可能在 continue 返回后才完成退出；停止动作作为最终
            # 控制调用仍必须返回同一组失效断点提醒。
            finished = _payload(await tools["stop_debugging"].ainvoke({}))
        assert finished["state"]["status"] == "exited"
        assert {
            item["path"] for item in finished["invalid_breakpoints"]
        } == {"debug-entry.mjs", "debug-worker.mjs"}
        assert all(
            item["relocation_status"] == "pending_update"
            for item in finished["invalid_breakpoints"]
        )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_session_launch_configuration_is_restored_after_service_restart(
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> None:
    workspace_root = Path(integration_workspace_root_path).resolve()
    entry_path, worker_path, _entry_line, worker_line = _write_cross_file_debug_fixture(
        workspace_root
    )
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=Path(integration_workspace_config_path),
        workspace_root=workspace_root,
    )
    config_service.validate_workspace_config()
    resolver = _SessionPathResolverStub(
        workspace_root / ".boxteam" / "test-debug-session-nodes"
    )
    store = NodeDebugSessionStore(resolver)
    session_id = "ses_persisted_debug_configuration"
    first = NodeDebugService(
        workspace_root=workspace_root,
        config_service=config_service,
        session_store=store,
    )

    try:
        configured = await first.create_configuration(
            NodeDebugConfigurationCreateRequest(
                session_id=session_id,
                name="可恢复方案",
                script_path="debug-entry.mjs",
                working_directory="",
                launch_profile_name="node-default",
                args=["session-argument"],
                breakpoints=[
                    {
                        "path": "debug-worker.mjs",
                        "line": worker_line,
                    }
                ],
            )
        )
        configuration_id = configured.active_configuration_id
        assert configuration_id is not None
        started = await first.start(
            session_id=session_id,
            configuration_id=configuration_id,
            path=str(worker_path),
            working_directory=str(workspace_root),
            launch_profile_name=None,
            args=["ignored-argument"],
            breakpoints=[],
        )
        assert started.status == "paused"
        assert started.script_path == entry_path.relative_to(workspace_root).as_posix()
        assert started.args == ["session-argument"]
    finally:
        await first.close()

    restored = NodeDebugService(
        workspace_root=workspace_root,
        config_service=config_service,
        session_store=store,
    )
    restored_state = await restored.get_state(session_id)
    assert restored_state.status == "idle"
    assert restored_state.script_path == "debug-entry.mjs"
    assert restored_state.working_directory == ""
    assert restored_state.launch_profile_name == "node-default"
    assert restored_state.args == ["session-argument"]
    assert restored_state.breakpoints[0].path == "debug-worker.mjs"
    assert restored_state.breakpoints[0].inspector_id is None


@pytest.mark.asyncio
async def test_node_debug_config_integration_profile_override_and_legacy_compatibility(
    integration_workspace_root_path: str,
) -> None:
    workspace_root = (
        Path(integration_workspace_root_path).resolve() / "config-profile-fixture"
    )
    boxteam_root = workspace_root / ".boxteam"
    boxteam_root.mkdir(parents=True, exist_ok=True)
    base_config_path = workspace_root / "legacy.jsonc"
    base_config_path.write_text(json.dumps({"runtime": {}}), encoding="utf-8")
    (boxteam_root / "workspace.jsonc").write_text(
        json.dumps(
            {
                "runtime": {
                    "debug": {
                        "command_timeout_seconds": 2,
                        "launch_profiles": {
                            "node-test": {
                                "adapter": "node_inspector",
                                "runtime": "node",
                                "program": "debug-fixture.mjs",
                                "working_directory": "",
                                "args": ["--from-profile"],
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=base_config_path,
        workspace_root=workspace_root,
    )

    config = service.get_debug_runtime_config()

    assert config["command_timeout_seconds"] == 2.0
    assert config["launch_profiles"]["node-test"]["args"] == ["--from-profile"]
    assert config["node"]["inspector_port"] == 0

    legacy_config_path = workspace_root / "legacy-without-debug.jsonc"
    legacy_config_path.write_text(
        json.dumps({"runtime": {}}),
        encoding="utf-8",
    )
    legacy_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=legacy_config_path,
    )
    legacy_config = legacy_service.get_debug_runtime_config()
    assert legacy_config["default_adapter"] == "node_inspector"
    assert legacy_config["node"]["inspector_port"] == 0
