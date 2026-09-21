from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
)
from app.agents.tools.debugging import create_debugging_tools
from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.node_debug.session.thread_owner import (
    MAIN_THREAD_ID,
    NodeDebugThreadOwner,
    resolve_node_debug_owner,
)
from tests.support.node_debug_dependencies import (
    permissive_node_debug_session_admission,
)


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
    owner: NodeDebugThreadOwner,
) -> dict[str, object]:
    binding = ThreadRuntimeBinding(
        session_id=owner.session_id,
        thread_id=owner.thread_id,
    )
    return {
        tool.name: tool
        for tool in create_debugging_tools(
            session_id=owner.session_id,
            workspace_root=workspace_root,
            node_debug_service=service,
            invocation_context=ToolInvocationContext(thread_binding=binding),
            thread_binding=binding,
        )
    }


async def _create_catalog_debug_owners(
    client: httpx.AsyncClient,
    workspace_root: Path,
) -> tuple[SessionCatalogPathResolver, NodeDebugThreadOwner, NodeDebugThreadOwner]:
    """通过真实 Session API 创建 main/child，再由 catalog resolver 解析 owner。"""
    main_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Debug main session"},
    )
    assert main_response.status_code == 200, main_response.text
    main_session_id = main_response.json()["data"]["session_id"]

    child_response = await client.post(
        "/api/v1/sessions",
        json={
            "title": "Debug child session",
            "folder_id": main_session_id,
        },
    )
    assert child_response.status_code == 200, child_response.text
    child_session_id = child_response.json()["data"]["session_id"]
    assert child_response.json()["data"]["parent_session_id"] == main_session_id

    resolver = get_session_path_resolver(workspace_root / ".boxteam" / "sessions")
    assert isinstance(resolver, SessionCatalogPathResolver)
    main_owner = resolve_node_debug_owner(
        resolver,
        session_id=main_session_id,
        thread_id=MAIN_THREAD_ID,
    )
    child_owner = resolve_node_debug_owner(
        resolver,
        session_id=child_session_id,
        thread_id=MAIN_THREAD_ID,
    )
    assert main_owner.key == (main_session_id, MAIN_THREAD_ID)
    assert child_owner.key == (child_session_id, MAIN_THREAD_ID)
    assert main_owner.thread_node != child_owner.thread_node
    assert child_owner.thread_node == resolver.resolve_session_node(child_session_id)
    return resolver, main_owner, child_owner


def _build_debug_service(
    workspace_root: Path,
    config_path: Path,
    resolver: SessionCatalogPathResolver,
) -> tuple[NodeDebugService, NodeDebugSessionStore]:
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=workspace_root,
    )
    config_service.validate_workspace_config()
    store = NodeDebugSessionStore(resolver)
    return NodeDebugService(
        workspace_root=workspace_root,
        config_service=config_service,
        session_store=store,
        session_admission=permissive_node_debug_session_admission(),
        external_resource_leases=ExternalResourceLeaseLedger(),
    ), store


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
        "debugConfigurationId",
    }
    assert tools["start_debugging"]["test_supported"] is False


@pytest.mark.asyncio
async def test_agent_debug_tools_drive_real_node_inspector_session(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
) -> None:
    workspace_root = Path(e2e_workspace_root_path).resolve()
    resolver, _main_owner, child_owner = await _create_catalog_debug_owners(
        client,
        workspace_root,
    )
    fixture_path, breakpoint_line = _write_debug_fixture(workspace_root)
    service, _store = _build_debug_service(
        workspace_root,
        Path(e2e_workspace_config_path),
        resolver,
    )
    tools = _tool_map(workspace_root, service, child_owner)

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
async def test_debug_tools_keep_sessions_isolated_and_support_node_logpoints(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
) -> None:
    workspace_root = Path(e2e_workspace_root_path).resolve()
    resolver, main_owner, child_owner = await _create_catalog_debug_owners(
        client,
        workspace_root,
    )
    fixture_path, breakpoint_line = _write_debug_fixture(workspace_root)
    service, store = _build_debug_service(
        workspace_root,
        Path(e2e_workspace_config_path),
        resolver,
    )
    by_name = _tool_map(workspace_root, service, main_owner)
    other_tools = _tool_map(workspace_root, service, child_owner)

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
        assert logpoint_result["ok"] is True
        assert logpoint_result["state"]["status"] == "idle"
        assert logpoint_result["state"]["breakpoints"][0]["log_message"] == (
            "localValue={localValue}"
        )
        assert "inspector_url" not in logpoint_result["state"]

        removed_logpoint = _payload(
            await by_name["remove_breakpoint"].ainvoke(
                {"fileFullPath": str(fixture_path), "line": breakpoint_line}
            )
        )
        assert removed_logpoint["ok"] is True

        other_session = await service.get_state(
            child_owner.session_id,
            child_owner.thread_id,
        )
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
        assert add_result["state"]["status"] == "idle"
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
        state = await service.get_state(main_owner.session_id, main_owner.thread_id)
        other_state = await service.get_state(
            child_owner.session_id,
            child_owner.thread_id,
        )
        assert state.pid is not None
        assert other_state.pid is not None
        assert state.pid != other_state.pid
        main_claim = store.read_launch_claim(
            main_owner.session_id,
            main_owner.thread_id,
        )
        child_claim = store.read_launch_claim(
            child_owner.session_id,
            child_owner.thread_id,
        )
        assert main_claim is not None
        assert child_claim is not None
        assert main_claim.inspector_port > 0
        assert child_claim.inspector_port > 0
        assert main_claim.inspector_port != child_claim.inspector_port
        stop_result = _payload(await by_name["stop_debugging"].ainvoke({}))
        assert stop_result["ok"] is True
        other_stop_result = _payload(await other_tools["stop_debugging"].ainvoke({}))
        assert other_stop_result["ok"] is True
    finally:
        await service.close()
