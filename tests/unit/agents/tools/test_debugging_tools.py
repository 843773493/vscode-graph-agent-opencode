from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.debug_redaction import REDACTION_PLACEHOLDER
from app.agents.tools.debugging import create_debugging_tools
from app.schemas.public_v2.node_debug import (
    NodeDebugEvaluationDTO,
    NodeDebugStackFrameDTO,
    NodeDebugStateDTO,
    NodeDebugVariableDTO,
)


def _build_tools(tmp_path: Path):
    return create_debugging_tools(
        session_id="ses_debug_schema",
        workspace_root=tmp_path,
        node_debug_service=MagicMock(),
        invocation_context=ToolInvocationContext(),
    )


def test_debug_tool_names_and_model_schemas_match_debug_mcp_shape(
    tmp_path: Path,
) -> None:
    tools = _build_tools(tmp_path)
    by_name = {tool.name: tool for tool in tools}

    assert list(by_name) == [
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
    ]
    assert set(by_name["start_debugging"].args) == {
        "fileFullPath",
        "workingDirectory",
        "testName",
        "configurationName",
        "debugConfigurationId",
    }
    assert set(by_name["add_breakpoint"].args) == {
        "fileFullPath",
        "line",
        "condition",
        "hitCondition",
    }
    assert set(by_name["add_logpoint"].args) == {
        "fileFullPath",
        "line",
        "logMessage",
        "condition",
        "hitCondition",
    }
    assert set(by_name["remove_breakpoint"].args) == {
        "fileFullPath",
        "line",
    }
    assert set(by_name["get_variables_values"].args) == {
        "variableNames",
        "scope",
    }
    assert set(by_name["evaluate_expression"].args) == {"expression"}
    hidden_fields = {
        "session_id",
        "job_id",
        "adapter",
        "launch",
        "runtime",
        "program",
        "inspectorPort",
        "debugpyPort",
        "vscodeSessionId",
        "threadId",
        "frameId",
    }
    for tool in tools:
        assert hidden_fields.isdisjoint(tool.args)
    start_schema = by_name["start_debugging"].tool_call_schema.model_json_schema()
    path_description = start_schema["properties"]["fileFullPath"]["description"]
    assert "工作区相对" in path_description
    assert "不能以 / 开头" in path_description


@pytest.mark.asyncio
async def test_logpoint_maps_to_non_pausing_breakpoint_definition(
    tmp_path: Path,
) -> None:
    state = NodeDebugStateDTO(session_id="ses_debug_logpoint", status="idle")
    service = MagicMock()
    service.get_state = AsyncMock(return_value=state)
    service.record_tool_action = AsyncMock()
    service.apply_action = AsyncMock(return_value=state)
    tools = create_debugging_tools(
        session_id="ses_debug_logpoint",
        workspace_root=tmp_path,
        node_debug_service=service,
        invocation_context=ToolInvocationContext(),
    )

    result = await next(tool for tool in tools if tool.name == "add_logpoint").ainvoke(
        {
            "fileFullPath": "fixture.mjs",
            "line": 2,
            "logMessage": "value={value}",
            "condition": "value > 0",
            "hitCondition": 3,
        }
    )
    payload = json.loads(result)

    assert payload["ok"] is True
    service.apply_action.assert_awaited_once_with(
        session_id="ses_debug_logpoint",
        action="set_breakpoint",
        params={
            "path": "fixture.mjs",
            "line": 2,
            "condition": "value > 0",
            "hit_condition": 3,
            "log_message": "value={value}",
        },
        actor="ai",
        tool_name="add_logpoint",
        tool_call_id="direct-backend-test",
    )


@pytest.fixture
def paused_debug_state() -> NodeDebugStateDTO:
    return NodeDebugStateDTO(
        session_id="ses_debug_redaction",
        status="paused",
        pid=43210,
        call_stack=[
            NodeDebugStackFrameDTO(
                call_frame_id="frame-1",
                function_name="main",
                url="file:///workspace/fixture.mjs",
                path="fixture.mjs",
                line=3,
                column=1,
                variables=[
                    NodeDebugVariableDTO(name="password", value="hunter2"),
                    NodeDebugVariableDTO(name="tokenCount", value="42"),
                    NodeDebugVariableDTO(
                        name="renamed",
                        value="ghp_1234567890abcdefghijklmnopqrstuv",
                    ),
                ],
            )
        ],
    )


@pytest.mark.asyncio
async def test_model_state_does_not_leak_unrequested_variable_values(
    tmp_path: Path,
    paused_debug_state: NodeDebugStateDTO,
) -> None:
    service = MagicMock()
    service.get_state = AsyncMock(return_value=paused_debug_state)
    service.get_variables = AsyncMock(
        return_value=paused_debug_state.call_stack[0].variables
    )
    service.record_tool_action = AsyncMock(return_value=paused_debug_state)
    tools = create_debugging_tools(
        session_id=paused_debug_state.session_id,
        workspace_root=tmp_path,
        node_debug_service=service,
        invocation_context=ToolInvocationContext(),
    )

    result = await next(
        tool for tool in tools if tool.name == "list_variable_names"
    ).ainvoke({"scope": "local"})
    payload = json.loads(result)

    assert {item["name"] for item in payload["variables"]} == {
        "password",
        "tokenCount",
        "renamed",
    }
    assert payload["state"]["call_stack"][0]["variables"] == []
    assert "session_id" not in payload["state"]
    assert "pid" not in payload["state"]
    assert "call_frame_id" not in payload["state"]["call_stack"][0]
    assert "hunter2" not in result
    assert "ghp_" not in result


@pytest.mark.asyncio
async def test_requested_variable_values_are_redacted_before_model_response(
    tmp_path: Path,
    paused_debug_state: NodeDebugStateDTO,
) -> None:
    service = MagicMock()
    service.get_state = AsyncMock(return_value=paused_debug_state)
    service.get_variables = AsyncMock(
        return_value=paused_debug_state.call_stack[0].variables
    )
    service.record_tool_action = AsyncMock(return_value=paused_debug_state)
    tools = create_debugging_tools(
        session_id=paused_debug_state.session_id,
        workspace_root=tmp_path,
        node_debug_service=service,
        invocation_context=ToolInvocationContext(),
    )

    result = await next(
        tool for tool in tools if tool.name == "get_variables_values"
    ).ainvoke(
        {
            "variableNames": ["password", "tokenCount", "renamed"],
            "scope": "local",
        }
    )
    payload = json.loads(result)
    values = {item["name"]: item["value"] for item in payload["variables"]}

    assert values == {
        "password": REDACTION_PLACEHOLDER,
        "tokenCount": "42",
        "renamed": REDACTION_PLACEHOLDER,
    }
    assert payload["redaction_notice"]
    assert all("object_id" not in item for item in payload["variables"])
    assert "hunter2" not in result
    assert "ghp_" not in result


@pytest.mark.asyncio
async def test_evaluate_expression_redacts_sensitive_expression_result(
    tmp_path: Path,
    paused_debug_state: NodeDebugStateDTO,
) -> None:
    evaluated_state = paused_debug_state.model_copy(
        update={
            "last_evaluation": NodeDebugEvaluationDTO(
                expression="process.env.PASSWORD",
                value="hunter2",
                description="hunter2",
                evaluated_at="2026-08-12T00:00:00Z",
            )
        }
    )
    service = MagicMock()
    service.apply_action = AsyncMock(return_value=evaluated_state)
    service.get_state = AsyncMock(return_value=evaluated_state)
    service.record_tool_action = AsyncMock(return_value=evaluated_state)
    tools = create_debugging_tools(
        session_id=paused_debug_state.session_id,
        workspace_root=tmp_path,
        node_debug_service=service,
        invocation_context=ToolInvocationContext(),
    )

    result = await next(
        tool for tool in tools if tool.name == "evaluate_expression"
    ).ainvoke({"expression": "process.env.PASSWORD"})
    payload = json.loads(result)

    assert payload["state"]["last_evaluation"]["value"] == REDACTION_PLACEHOLDER
    assert payload["state"]["last_evaluation"]["description"] == (REDACTION_PLACEHOLDER)
    assert payload["redaction_notice"]
    assert "hunter2" not in result
