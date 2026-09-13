from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.debugging import create_debugging_tools
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.node_debug_service import NodeDebugService


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


@pytest.mark.asyncio
async def test_backend_catalog_exposes_debug_tool_group_and_schema(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/tools", params={"agent_id": "default"})

    assert response.status_code == 200, response.text
    tools = {item["tool_id"]: item for item in response.json()["data"]}
    expected_names = {
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
    assert set(tools["start_debugging"]["parameters"]["properties"]) == {
        "fileFullPath",
        "workingDirectory",
        "testName",
        "configurationName",
    }
    assert tools["start_debugging"]["test_supported"] is False


@pytest.mark.asyncio
async def test_agent_debug_tools_drive_real_node_inspector_session(
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
) -> None:
    workspace_root = Path(e2e_workspace_root_path).resolve()
    fixture_path, breakpoint_line = _write_debug_fixture(workspace_root)
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=Path(e2e_workspace_config_path),
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
                {"fileFullPath": str(fixture_path), "line": breakpoint_line}
            )
        )
        assert breakpoint_result["ok"] is True
        assert breakpoint_result["state"]["status"] == "idle"

        start_result = _payload(
            await tools["start_debugging"].ainvoke(
                {
                    "fileFullPath": str(fixture_path),
                    "workingDirectory": str(workspace_root),
                }
            )
        )
        assert start_result["ok"] is True, start_result
        assert start_result["state"]["status"] == "paused"
        assert "inspector_url" not in start_result["state"]
        assert start_result["state"]["call_stack"][0]["path"] == (
            fixture_path.relative_to(workspace_root).as_posix()
        )
        assert start_result["state"]["call_stack"][0]["line"] == breakpoint_line

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
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_debug_tools_keep_sessions_isolated_and_report_unsupported_logpoints(
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
) -> None:
    workspace_root = Path(e2e_workspace_root_path).resolve()
    fixture_path, breakpoint_line = _write_debug_fixture(workspace_root)
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=Path(e2e_workspace_config_path),
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
        unsupported_test_result = _payload(
            await by_name["start_debugging"].ainvoke(
                {
                    "fileFullPath": str(fixture_path),
                    "workingDirectory": str(workspace_root),
                    "testName": "compute",
                }
            )
        )
        assert unsupported_test_result["ok"] is False
        assert unsupported_test_result["error"]["code"] == "UNSUPPORTED_TEST_TARGET"

        invalid_path_result = _payload(
            await by_name["start_debugging"].ainvoke(
                {
                    "fileFullPath": str(workspace_root.parent / "outside.mjs"),
                    "workingDirectory": str(workspace_root),
                }
            )
        )
        assert invalid_path_result["ok"] is False
        assert invalid_path_result["error"]["code"] == "INVALID_DEBUG_ARGUMENT"

        logpoint_result = _payload(
            await by_name["add_logpoint"].ainvoke(
                {
                    "fileFullPath": str(fixture_path),
                    "line": breakpoint_line,
                    "logMessage": "localValue={localValue}",
                }
            )
        )
        assert logpoint_result["ok"] is False
        assert logpoint_result["error"]["code"] == "UNSUPPORTED_DEBUG_FEATURE"
        assert logpoint_result["state"]["status"] == "idle"
        assert "inspector_url" not in logpoint_result["state"]

        other_session = await service.get_state("ses_other_debug_session")
        assert other_session.status == "idle"
        assert other_session.breakpoints == []
        assert other_session.actions == []

        add_result = _payload(
            await by_name["add_breakpoint"].ainvoke(
                {
                    "fileFullPath": str(fixture_path),
                    "line": breakpoint_line,
                    "condition": "input === 41",
                }
            )
        )
        assert add_result["state"]["session_id"] == "ses_e2e_debug_isolated"
        assert add_result["state"]["breakpoints"][0]["path"] == (
            fixture_path.relative_to(workspace_root).as_posix()
        )
        start_result = _payload(
            await by_name["start_debugging"].ainvoke(
                {
                    "fileFullPath": str(fixture_path),
                    "workingDirectory": str(workspace_root),
                }
            )
        )
        assert start_result["ok"] is True
        assert start_result["state"]["status"] == "paused"
        assert start_result["state"]["breakpoints"][0]["condition"] == "input === 41"
        other_add_result = _payload(
            await other_tools["add_breakpoint"].ainvoke(
                {"fileFullPath": str(fixture_path), "line": breakpoint_line}
            )
        )
        assert other_add_result["ok"] is True
        other_start_result = _payload(
            await other_tools["start_debugging"].ainvoke(
                {
                    "fileFullPath": str(fixture_path),
                    "workingDirectory": str(workspace_root),
                }
            )
        )
        assert other_start_result["ok"] is True
        assert other_start_result["state"]["status"] == "paused"
        assert start_result["state"]["pid"] != other_start_result["state"]["pid"]
        stop_result = _payload(await by_name["stop_debugging"].ainvoke({}))
        assert stop_result["ok"] is True
        other_stop_result = _payload(await other_tools["stop_debugging"].ainvoke({}))
        assert other_stop_result["ok"] is True
    finally:
        await service.close()
