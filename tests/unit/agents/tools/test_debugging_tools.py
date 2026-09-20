from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
)
from app.agents.tools.debug_redaction import REDACTION_PLACEHOLDER
from app.agents.tools.debugging import create_debugging_tools
from app.schemas.internal_v2.node_debug import (
    NodeDebugBreakpointDTO,
    NodeDebugConfigurationDTO,
    NodeDebugConfigurationSummaryDTO,
    NodeDebugEvaluationDTO,
    NodeDebugStackFrameDTO,
    NodeDebugStateDTO,
    NodeDebugVariableDTO,
)

_MAIN_THREAD_ID = "main"
_MISSING_CONFIGURATION_ID = "dbgcfg_" + "b" * 32


def _build_tools(tmp_path: Path):
    return create_debugging_tools(
        session_id="ses_debug_schema",
        workspace_root=tmp_path,
        node_debug_service=MagicMock(),
        invocation_context=ToolInvocationContext(),
    )


def _debug_configuration(
    *,
    configuration_id: str = "dbgcfg_" + "a" * 32,
    name: str = "调试 debug-fixture.mjs",
    script_path: str = "debug-fixture.mjs",
    working_directory: str = "",
    launch_profile_name: str | None = "node-default",
) -> NodeDebugConfigurationDTO:
    now = datetime.now(UTC)
    return NodeDebugConfigurationDTO(
        configuration_id=configuration_id,
        name=name,
        script_path=script_path,
        working_directory=working_directory,
        launch_profile_name=launch_profile_name,
        created_at=now,
        updated_at=now,
    )


def _configuration_summary(
    configuration: NodeDebugConfigurationDTO,
) -> NodeDebugConfigurationSummaryDTO:
    return NodeDebugConfigurationSummaryDTO(
        configuration_id=configuration.configuration_id,
        name=configuration.name,
        script_path=configuration.script_path,
        launch_profile_name=configuration.launch_profile_name,
        breakpoint_count=len(configuration.breakpoints),
        revision=configuration.revision,
        updated_at=configuration.updated_at,
    )


def _service_for_launch(
    *,
    state: NodeDebugStateDTO,
    configurations: list[NodeDebugConfigurationDTO] | None = None,
) -> MagicMock:
    service = MagicMock()
    service.get_state = AsyncMock(return_value=state)
    service.list_configurations = MagicMock(
        return_value=list(configurations or []),
    )
    service.start = AsyncMock(return_value=state)
    service.record_tool_action = AsyncMock()
    service.resolve_launch_profile_name = MagicMock(
        side_effect=lambda value: value or "node-default",
    )
    return service


def _tool_map(tmp_path: Path, service: MagicMock) -> dict[str, object]:
    return {
        tool.name: tool
        for tool in create_debugging_tools(
            session_id="ses_debug_launch",
            workspace_root=tmp_path,
            node_debug_service=service,
            invocation_context=ToolInvocationContext(),
        )
    }


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
    assert "优先使用工作区相对路径" in path_description
    assert "已有活动方案时以方案保存的入口" in by_name["start_debugging"].description
    assert "失效断点不会阻止继续" in by_name["continue_execution"].description
    assert "重点查看 relocation_status" in by_name["list_breakpoints"].description


@pytest.mark.asyncio
async def test_logpoint_maps_to_non_pausing_breakpoint_definition(
    tmp_path: Path,
) -> None:
    state = NodeDebugStateDTO(
        session_id="ses_debug_logpoint",
        thread_id="main",
        status="idle",
    )
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
        thread_id=_MAIN_THREAD_ID,
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


@pytest.mark.asyncio
async def test_start_and_final_control_result_include_invalid_breakpoints(
    tmp_path: Path,
) -> None:
    invalid_breakpoint = NodeDebugBreakpointDTO(
        breakpoint_id="node-bp-invalid",
        path="fixture.mjs",
        line=3,
        original_line=3,
        created_at=datetime.now(UTC),
        relocation_status="pending_update",
        relocation_message="源码已变化，断点未自动重定位",
    )
    started_state = NodeDebugStateDTO(
        session_id="ses_debug_invalid_breakpoint",
        thread_id="main",
        status="paused",
        breakpoints=[invalid_breakpoint],
    )
    finished_state = started_state.model_copy(update={"status": "exited"})
    service = MagicMock()
    service.start = AsyncMock(return_value=started_state)
    service.apply_action = AsyncMock(return_value=finished_state)
    service.get_state = AsyncMock(return_value=started_state)
    service.record_tool_action = AsyncMock()
    tools = create_debugging_tools(
        session_id=started_state.session_id,
        workspace_root=tmp_path,
        node_debug_service=service,
        invocation_context=ToolInvocationContext(),
    )
    by_name = {tool.name: tool for tool in tools}

    start_result = json.loads(
        await by_name["start_debugging"].ainvoke(
            {"fileFullPath": "fixture.mjs", "workingDirectory": "."}
        )
    )
    assert start_result["invalid_breakpoints"] == [
        {
            "path": "fixture.mjs",
            "line": 3,
            "column": 1,
            "original_line": 3,
            "relocation_status": "pending_update",
            "relocation_message": "源码已变化，断点未自动重定位",
        }
    ]

    service.get_state = AsyncMock(return_value=finished_state)
    finish_result = json.loads(
        await by_name["continue_execution"].ainvoke({})
    )
    assert len(finish_result["invalid_breakpoints"]) == 1

    query_result = json.loads(
        await by_name["list_breakpoints"].ainvoke({})
    )
    assert "invalid_breakpoints" not in query_result


@pytest.fixture
def paused_debug_state() -> NodeDebugStateDTO:
    return NodeDebugStateDTO(
        session_id="ses_debug_redaction",
        thread_id="main",
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


@pytest.mark.asyncio
async def test_tools_resolve_owner_from_trusted_thread_binding(tmp_path: Path) -> None:
    state = NodeDebugStateDTO(
        session_id="ses_child_debug",
        thread_id=_MAIN_THREAD_ID,
        status="idle",
    )
    service = MagicMock()
    service.get_state = AsyncMock(return_value=state)
    service.record_tool_action = AsyncMock()
    tools = create_debugging_tools(
        session_id="ses_child_debug",
        workspace_root=tmp_path,
        node_debug_service=service,
        invocation_context=ToolInvocationContext(
            thread_binding=ThreadRuntimeBinding(
                session_id="ses_child_debug",
                thread_id=_MAIN_THREAD_ID,
            )
        ),
    )

    result = json.loads(
        await next(tool for tool in tools if tool.name == "list_breakpoints").ainvoke(
            {}
        )
    )

    assert result["ok"] is True
    assert {
        call_args.args for call_args in service.get_state.await_args_list
    } == {("ses_child_debug", _MAIN_THREAD_ID)}
    assert all(
        call_args.kwargs["thread_id"] == _MAIN_THREAD_ID
        for call_args in service.record_tool_action.await_args_list
    )


@pytest.mark.asyncio
async def test_alias_binding_passes_precise_parent_child_owner_to_service(
    tmp_path: Path,
) -> None:
    state = NodeDebugStateDTO(
        session_id="ses_parent_debug",
        thread_id="ses_child_thread",
        status="idle",
    )
    service = MagicMock()
    service.get_state = AsyncMock(return_value=state)
    service.record_tool_action = AsyncMock()
    tools = create_debugging_tools(
        session_id="ses_parent_debug",
        workspace_root=tmp_path,
        node_debug_service=service,
        invocation_context=ToolInvocationContext(
            thread_binding=ThreadRuntimeBinding(
                session_id="ses_parent_debug",
                thread_id="ses_child_thread",
            )
        ),
    )

    await next(tool for tool in tools if tool.name == "list_breakpoints").ainvoke({})

    # 工具层只传绑定结果；别名折叠仍由服务层唯一实现。
    assert {
        call_args.args for call_args in service.get_state.await_args_list
    } == {("ses_parent_debug", "ses_child_thread")}


@pytest.mark.asyncio
async def test_model_state_and_tool_schemas_hide_runtime_identity(
    tmp_path: Path,
) -> None:
    state = NodeDebugStateDTO(
        session_id="ses_hidden_identity",
        thread_id="ses_child_thread",
        status="idle",
    )
    service = MagicMock()
    service.get_state = AsyncMock(return_value=state)
    service.record_tool_action = AsyncMock()
    tools = create_debugging_tools(
        session_id="ses_hidden_identity",
        workspace_root=tmp_path,
        node_debug_service=service,
        invocation_context=ToolInvocationContext(),
    )
    by_name = {tool.name: tool for tool in tools}

    result = json.loads(await by_name["list_breakpoints"].ainvoke({}))

    assert "thread_id" not in result["state"]
    assert "session_id" not in result["state"]
    runtime_fields = {
        "session_id",
        "sessionId",
        "thread_id",
        "threadId",
        "port",
        "inspectorPort",
        "debugpyPort",
        "frameId",
        "callFrameId",
        "vscodeSessionId",
        "adapter",
        "runtime",
        "program",
        "launch",
    }
    for tool in tools:
        properties = tool.tool_call_schema.model_json_schema()["properties"]
        assert runtime_fields.isdisjoint(properties), tool.name

    with pytest.raises(ValidationError, match="threadId"):
        await by_name["add_breakpoint"].ainvoke(
            {"fileFullPath": "fixture.mjs", "line": 1, "threadId": "child"}
        )


@pytest.mark.asyncio
async def test_start_debugging_reports_missing_explicit_configuration_first(
    tmp_path: Path,
) -> None:
    state = NodeDebugStateDTO(
        session_id="ses_debug_launch",
        thread_id="main",
        status="idle",
    )
    service = _service_for_launch(state=state, configurations=[])
    tools = _tool_map(tmp_path, service)

    payload = json.loads(
        await tools["start_debugging"].ainvoke(
            {
                "fileFullPath": "debug-fixture.mjs",
                "workingDirectory": ".",
                "debugConfigurationId": _MISSING_CONFIGURATION_ID,
            }
        )
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "debug_configuration_not_found"
    assert payload["error"]["configuration_id"] == _MISSING_CONFIGURATION_ID
    assert payload["error"]["available_configuration_ids"] == []
    service.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_debugging_reports_missing_id_before_invalid_paths(
    tmp_path: Path,
) -> None:
    state = NodeDebugStateDTO(
        session_id="ses_debug_launch",
        thread_id="main",
        status="idle",
    )
    service = _service_for_launch(state=state, configurations=[])
    tools = _tool_map(tmp_path, service)

    payload = json.loads(
        await tools["start_debugging"].ainvoke(
            {
                "fileFullPath": "",
                "workingDirectory": "../outside",
                "debugConfigurationId": _MISSING_CONFIGURATION_ID,
            }
        )
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "debug_configuration_not_found"
    assert payload["error"]["configuration_id"] == _MISSING_CONFIGURATION_ID
    service.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_debugging_rejects_launch_parameter_conflict_with_fields(
    tmp_path: Path,
) -> None:
    configuration = _debug_configuration(
        script_path="debug-entry.mjs",
        working_directory="src",
        launch_profile_name="node-test",
    )
    state = NodeDebugStateDTO(
        session_id="ses_debug_launch",
        thread_id="main",
        status="idle",
        active_configuration_id=configuration.configuration_id,
        active_configuration_name=configuration.name,
        configurations=[_configuration_summary(configuration)],
    )
    service = _service_for_launch(state=state, configurations=[configuration])
    tools = _tool_map(tmp_path, service)

    payload = json.loads(
        await tools["start_debugging"].ainvoke(
            {
                "fileFullPath": "other.mjs",
                "workingDirectory": ".",
                "configurationName": "node-default",
            }
        )
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "debug_launch_parameter_conflict"
    assert payload["error"]["fields"] == [
        "configurationName",
        "fileFullPath",
        "workingDirectory",
    ]
    assert payload["error"]["conflicts"]["fileFullPath"] == {
        "provided": "other.mjs",
        "expected": "debug-entry.mjs",
    }
    assert payload["error"]["conflicts"]["workingDirectory"] == {
        "provided": "",
        "expected": "src",
    }
    assert payload["error"]["conflicts"]["configurationName"] == {
        "provided": "node-default",
        "expected": "node-test",
    }
    service.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_debugging_accepts_normalized_paths_matching_active_configuration(
    tmp_path: Path,
) -> None:
    configuration = _debug_configuration()
    state = NodeDebugStateDTO(
        session_id="ses_debug_launch",
        thread_id="main",
        status="idle",
        active_configuration_id=configuration.configuration_id,
        configurations=[_configuration_summary(configuration)],
    )
    service = _service_for_launch(state=state, configurations=[configuration])
    tools = _tool_map(tmp_path, service)

    payload = json.loads(
        await tools["start_debugging"].ainvoke(
            {
                # 工作区内绝对路径与 `.` 必须与方案保存的相对形式归一相等。
                "fileFullPath": str(tmp_path / "debug-fixture.mjs"),
                "workingDirectory": str(tmp_path),
                "configurationName": "node-default",
            }
        )
    )

    assert payload["ok"] is True, payload
    service.start.assert_awaited_once()
    start_kwargs = service.start.await_args.kwargs
    assert start_kwargs["session_id"] == "ses_debug_launch"
    assert start_kwargs["thread_id"] == _MAIN_THREAD_ID
    assert start_kwargs["configuration_id"] == configuration.configuration_id
    assert start_kwargs["path"] == "debug-fixture.mjs"
    assert start_kwargs["working_directory"] == ""


@pytest.mark.asyncio
async def test_start_debugging_prefers_explicit_id_then_active_configuration(
    tmp_path: Path,
) -> None:
    active = _debug_configuration(
        configuration_id="dbgcfg_" + "a" * 32,
        name="活动方案",
    )
    explicit = _debug_configuration(
        configuration_id="dbgcfg_" + "c" * 32,
        name="显式方案",
    )
    state = NodeDebugStateDTO(
        session_id="ses_debug_launch",
        thread_id="main",
        status="idle",
        active_configuration_id=active.configuration_id,
        configurations=[
            _configuration_summary(active),
            _configuration_summary(explicit),
        ],
    )
    service = _service_for_launch(state=state, configurations=[active, explicit])
    tools = _tool_map(tmp_path, service)

    explicit_payload = json.loads(
        await tools["start_debugging"].ainvoke(
            {
                "fileFullPath": "debug-fixture.mjs",
                "workingDirectory": ".",
                "debugConfigurationId": explicit.configuration_id,
            }
        )
    )
    assert explicit_payload["ok"] is True, explicit_payload
    assert (
        service.start.await_args.kwargs["configuration_id"]
        == explicit.configuration_id
    )

    service.start.reset_mock()
    active_payload = json.loads(
        await tools["start_debugging"].ainvoke(
            {"fileFullPath": "debug-fixture.mjs", "workingDirectory": "."}
        )
    )
    assert active_payload["ok"] is True, active_payload
    assert service.start.await_args.kwargs["configuration_id"] == active.configuration_id


@pytest.mark.asyncio
async def test_start_debugging_without_configuration_uses_safe_creation_path(
    tmp_path: Path,
) -> None:
    state = NodeDebugStateDTO(
        session_id="ses_debug_launch",
        thread_id="main",
        status="idle",
    )
    service = _service_for_launch(state=state, configurations=[])
    tools = _tool_map(tmp_path, service)

    payload = json.loads(
        await tools["start_debugging"].ainvoke(
            {"fileFullPath": "debug-fixture.mjs", "workingDirectory": "."}
        )
    )

    assert payload["ok"] is True, payload
    start_kwargs = service.start.await_args.kwargs
    assert start_kwargs["configuration_id"] is None
    assert start_kwargs["thread_id"] == _MAIN_THREAD_ID
    assert start_kwargs["path"] == "debug-fixture.mjs"
    assert start_kwargs["working_directory"] == ""
