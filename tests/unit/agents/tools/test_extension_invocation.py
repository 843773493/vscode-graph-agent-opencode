from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool, ToolException
from langgraph.prebuilt.tool_node import ToolCallRequest
from pydantic import ValidationError

from app.agents.custom_tools import CustomToolFactoryContext, build_custom_tools
from app.agents.policy import DEBUGGING_TOOL_GROUP, parse_custom_tool_specs
from app.agents.policy.tool_groups import DEBUGGING_TOOL_NAMES
from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)
from app.agents.tool_node_adapter import extract_agent_tools_by_name
from app.agents.tools.debugging import create_debugging_tools
from app.agents.tools.extension_invocation import (
    create_extension_tool_invoker_tool,
    seal_extension_catalog_binding_from_tools,
)
from app.agents.tools.testing import create_test_tool_2
from app.schemas.internal_v2.node_debug import NodeDebugStateDTO
from app.services.business.agent_tool_settings.catalog import AgentToolCatalog
from app.services.infrastructure.config_service import ConfigService

#: 16 个 DebugMCP 风格内层目标（不含 4 个方案管理目标）。
DEBUG_TARGET_NAMES = frozenset(
    {
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
)
DEBUG_CONFIGURATION_TOOL_NAMES = frozenset(
    {
        "list_debug_configurations",
        "create_debug_configuration",
        "activate_debug_configuration",
        "delete_debug_configuration",
    }
)
#: 模型绝不能提供的运行时字段：session/thread、Inspector 端口、DAP/VS Code 内部字段。
FORBIDDEN_RUNTIME_ARGUMENT_FIELDS = (
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
)


@pytest.mark.parametrize("async_target", [False, True])
async def test_envelope_uses_target_result_contract_without_nested_events(async_target):
    def output():
        return [{"type": "text", "text": "完整结果"}], {"structured_content": {"count": 2}}

    async def async_output():
        return output()

    target = StructuredTool.from_function(
        func=None if async_target else output, coroutine=async_output if async_target else None,
        name="protocol_result", description="完整结果", response_format="content_and_artifact",
    )
    envelope = create_extension_tool_invoker_tool(
        [target], catalog_binding_resolver=seal_extension_catalog_binding_from_tools([target]),
    )
    events = [event async for event in envelope.astream_events({
        "type": "tool_call", "id": "result-contract-call", "name": envelope.name,
        "args": {"tool_name": target.name, "arguments": {}},
    })]
    assert [(event["event"], event["name"]) for event in events] == [
        ("on_tool_start", envelope.name), ("on_tool_end", envelope.name),
    ]
    message = events[-1]["data"]["output"]
    assert isinstance(message, ToolMessage)
    assert message.tool_call_id == "result-contract-call" and message.name == envelope.name
    assert message.content == output()[0] and message.artifact == output()[1]


async def test_envelope_preserves_target_error_policy_and_original_call_identity():
    def fail():
        raise ToolException("真实目标失败")

    target = StructuredTool.from_function(fail, name="failing_target", description="错误策略", handle_tool_error=lambda error: f"目标回执：{error}")
    envelope = create_extension_tool_invoker_tool(
        [target], catalog_binding_resolver=seal_extension_catalog_binding_from_tools([target]),
    )
    result = await envelope.ainvoke({
        "type": "tool_call", "id": "error-contract-call", "name": envelope.name,
        "args": {"tool_name": target.name, "arguments": {}},
    })
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert result.tool_call_id == "error-contract-call" and result.name == envelope.name
    assert result.content == "目标回执：真实目标失败"


class _RuntimeCatalog:
    """只提供目录聚合所需的运行时工具投影。"""

    def get_available_tools(self, agent_id: str = "default") -> list[dict]:
        return [
            {
                "id": "read_file",
                "name": "read_file",
                "description": "读取文件",
                "parameters": {"type": "object"},
            },
            {
                "id": EXTENSION_TOOL_INVOKER_NAME,
                "name": EXTENSION_TOOL_INVOKER_NAME,
                "description": "调用扩展工具",
                "parameters": {"type": "object"},
            },
        ]


def _idle_state() -> NodeDebugStateDTO:
    return NodeDebugStateDTO(
        session_id="ses_envelope",
        thread_id="thr_00000000400070008000000000000002",
        status="idle",
    )


def _debugging_tools() -> list:
    service = MagicMock()
    service.get_state = AsyncMock(return_value=_idle_state())
    service.record_tool_action = AsyncMock()
    return create_debugging_tools(
        session_id="ses_envelope",
        node_debug_service=service,
        invocation_context=ToolInvocationContext(),
    )


def test_debugging_group_matches_production_catalog() -> None:
    # 16 个内层目标与 4 个方案管理目标共同构成 debugging 分组。
    assert DEBUG_TARGET_NAMES | DEBUG_CONFIGURATION_TOOL_NAMES == DEBUGGING_TOOL_NAMES
    assert len(DEBUG_TARGET_NAMES) == 16
    assert DEBUGGING_TOOL_GROUP.kind == "debugging"
    assert DEBUGGING_TOOL_GROUP.group_id == "debugging"


@pytest.mark.asyncio
async def test_envelope_only_accepts_tool_name_and_arguments() -> None:
    debug_tools = _debugging_tools()
    envelope = create_extension_tool_invoker_tool(
        debug_tools,
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(debug_tools),
    )
    schema = envelope.tool_call_schema.model_json_schema()

    assert set(schema["properties"]) == {"tool_name", "arguments"}
    assert schema["required"] == ["tool_name", "arguments"]
    # LangChain 的 tool_call_schema 是 subset model；真正执行校验的 args_schema
    # 必须对额外字段 fail-closed。
    assert envelope.args_schema.model_config["extra"] == "forbid"
    assert envelope.args_schema.model_json_schema()["additionalProperties"] is False

    with pytest.raises(ValidationError, match="session_id"):
        await envelope.ainvoke(
            {
                "tool_name": "list_breakpoints",
                "arguments": {},
                "session_id": "ses_envelope",
            }
        )
    with pytest.raises(ValidationError, match="threadId"):
        await envelope.ainvoke(
            {
                "tool_name": "list_breakpoints",
                "arguments": {},
                "threadId": "child",
            }
        )


@pytest.mark.asyncio
async def test_envelope_rejects_unknown_and_runtime_arguments_before_target() -> None:
    service = MagicMock()
    service.get_state = AsyncMock(return_value=_idle_state())
    service.record_tool_action = AsyncMock()
    tools = create_debugging_tools(
        session_id="ses_envelope",
        node_debug_service=service,
        invocation_context=ToolInvocationContext(thread_binding=ThreadRuntimeBinding(
            "ses_envelope", "thr_01a100c4a1897a2d8d40b568d23fcbe6",
        )),
    )
    envelope = create_extension_tool_invoker_tool(
        tools,
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(tools),
    )

    for field_name in FORBIDDEN_RUNTIME_ARGUMENT_FIELDS:
        result = await envelope.ainvoke(
            {
                "tool_name": "list_breakpoints",
                "arguments": {field_name: "model-supplied"},
            }
        )
        assert isinstance(result, str), field_name
        assert "参数校验失败" in result, result
        assert field_name in result, result
        assert "list_breakpoints" in result, result

    result = await envelope.ainvoke(
        {
            "tool_name": "list_breakpoints",
            "arguments": {"unexpected": 1},
        }
    )
    assert "参数校验失败" in result
    assert "unexpected" in result
    service.get_state.assert_not_awaited()

    ok_result = await envelope.ainvoke(
        {"tool_name": "list_breakpoints", "arguments": {}}
    )
    assert json.loads(ok_result)["ok"] is True


def test_envelope_description_and_schema_do_not_expose_target_names() -> None:
    debug_tools = _debugging_tools()
    envelope = create_extension_tool_invoker_tool(
        debug_tools,
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(debug_tools),
    )
    exposed_text = envelope.description + json.dumps(
        envelope.tool_call_schema.model_json_schema(),
        ensure_ascii=False,
    )

    for target_name in sorted(DEBUGGING_TOOL_NAMES):
        assert target_name not in exposed_text, target_name
    assert "session" not in envelope.description
    assert "thread" not in envelope.description


def test_provider_tool_list_contains_only_fixed_envelope_for_debugging() -> None:
    debug_tools = _debugging_tools()
    envelope = create_extension_tool_invoker_tool(
        debug_tools,
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(debug_tools),
    )
    agent = create_agent(
        FakeListChatModel(responses=["ok"]),
        tools=[envelope],
    )

    provider_tool_names = set(extract_agent_tools_by_name(agent))

    assert EXTENSION_TOOL_INVOKER_NAME in provider_tool_names
    assert provider_tool_names.isdisjoint(DEBUGGING_TOOL_NAMES)


def test_envelope_schema_is_stable_when_debugging_targets_change() -> None:
    tools = _debugging_tools()
    empty_envelope = create_extension_tool_invoker_tool(
        [],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools([]),
    )
    single_envelope = create_extension_tool_invoker_tool(
        tools[:1],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(tools[:1]),
    )
    full_envelope = create_extension_tool_invoker_tool(
        tools,
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(tools),
    )

    assert empty_envelope.name == full_envelope.name == EXTENSION_TOOL_INVOKER_NAME
    assert empty_envelope.description == full_envelope.description
    assert single_envelope.description == full_envelope.description
    assert (
        empty_envelope.tool_call_schema.model_json_schema()
        == single_envelope.tool_call_schema.model_json_schema()
        == full_envelope.tool_call_schema.model_json_schema()
    )


def test_catalog_keeps_debugging_targets_in_inner_directory(
    tmp_path: Path,
) -> None:
    # 只覆盖无关字段，保留发行包 workspace_inline.jsonc 的 tools.custom 数组。
    config_path = tmp_path / "workspace.jsonc"
    config_path.write_text(
        json.dumps({"agents": {"default": {"name": "信封目录检查"}}}),
        encoding="utf-8",
    )
    catalog = AgentToolCatalog(
        runtime_catalog=_RuntimeCatalog(),
        config_service=ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
        ),
    )

    definitions = {item["id"]: item for item in catalog.get_available_tools()}

    assert DEBUGGING_TOOL_NAMES <= definitions.keys()
    for target_name in DEBUGGING_TOOL_NAMES:
        definition = definitions[target_name]
        assert definition["group_id"] == DEBUGGING_TOOL_GROUP.group_id
        assert definition["kind"] == DEBUGGING_TOOL_GROUP.kind
        assert definition["parameters"]["type"] == "object"
        assert definition["parameters"]["additionalProperties"] is False
    assert set(definitions["start_debugging"]["parameters"]["properties"]) == {
        "fileFullPath",
        "workingDirectory",
        "testName",
        "configurationName",
        "debugConfigurationId",
    }
    assert EXTENSION_TOOL_INVOKER_NAME not in definitions


def _debugging_custom_specs(tmp_path: Path) -> list[object]:
    config_path = tmp_path / "workspace.jsonc"
    config_path.write_text(
        json.dumps({"agents": {"default": {"name": "注册路径检查"}}}),
        encoding="utf-8",
    )
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
    )
    raw_specs = config_service.get_agent_tool_config("default")["custom"]
    return [
        spec
        for spec in raw_specs
        if spec.get("name") in DEBUGGING_TOOL_NAMES
    ]


@pytest.fixture
def custom_tool_factory_context(tmp_path: Path) -> CustomToolFactoryContext:
    return CustomToolFactoryContext(
        session_id="ses_test",
        agent_id="default",
        sender_agent_id="default",
        workspace_root=tmp_path,
        background_task_registry=MagicMock(),
        background_message_bus=MagicMock(),
        job_event_bus=MagicMock(),
        job_service=MagicMock(),
        session_context_query_service=MagicMock(),
        workspace_session_context_client=MagicMock(),
        session_orchestrator=MagicMock(),
        config_service=MagicMock(),
        terminal_resource_owner=MagicMock(),
        browser_resource_owner=MagicMock(),
        invocation_context=ToolInvocationContext(),
    )


def test_configured_debugging_specs_resolve_into_envelope_targets(
    tmp_path: Path,
    custom_tool_factory_context: CustomToolFactoryContext,
) -> None:
    raw_specs = _debugging_custom_specs(tmp_path)
    specs = parse_custom_tool_specs(raw_specs, context="测试 tools.custom")
    assert {spec.name for spec in specs} == DEBUGGING_TOOL_NAMES
    assert all(
        spec.options.get("tool_name") == spec.name for spec in specs
    )
    assert all(
        spec.factory_path == "app.agents.tools.debugging:create_debugging_tool"
        for spec in specs
    )

    service = MagicMock()
    service.get_state = AsyncMock(return_value=_idle_state())
    service.record_tool_action = AsyncMock()
    tools = build_custom_tools(
        raw_specs,
        context=replace(
            custom_tool_factory_context, session_id="ses_envelope", node_debug_service=service,
        ),
    )

    assert {tool.name for tool in tools} == DEBUGGING_TOOL_NAMES
    envelope = create_extension_tool_invoker_tool(
        tools,
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(tools),
    )
    provider_tool_names = set(extract_agent_tools_by_name(
        create_agent(FakeListChatModel(responses=["ok"]), tools=[envelope])
    ))
    assert provider_tool_names == {EXTENSION_TOOL_INVOKER_NAME}


@pytest.mark.asyncio
async def test_envelope_preserves_original_tool_call_id_pairing() -> None:
    service = MagicMock()
    service.get_state = AsyncMock(return_value=_idle_state())
    service.record_tool_action = AsyncMock()
    # 生产装配把同一个 ToolInvocationContext 交给工具与 middleware；
    # 工具身份配对依赖这条唯一上下文。
    context = ToolInvocationContext(thread_binding=ThreadRuntimeBinding(
        "ses_envelope", "thr_01a100c4a1897a2d8d40b568d23fcbe6",
    ))
    tools = create_debugging_tools(
        session_id="ses_envelope",
        node_debug_service=service,
        invocation_context=context,
    )
    envelope = create_extension_tool_invoker_tool(
        tools,
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(tools),
        invocation_context=context,
    )
    middleware = ToolInvocationContextMiddleware(context)
    request = ToolCallRequest(
        tool_call={
            "id": "call_debug_envelope", "name": EXTENSION_TOOL_INVOKER_NAME,
            "args": {"tool_name": "list_breakpoints", "arguments": {}},
            "type": "tool_call",
        },
        tool=envelope, state={}, runtime=None,
    )

    async def handler(request_value):
        content = await envelope.ainvoke(request_value.tool_call["args"])
        return ToolMessage(
            content=content,
            tool_call_id=request_value.tool_call["id"],
        )

    result = await middleware.awrap_tool_call(request, handler)

    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == "call_debug_envelope"
    assert json.loads(result.content)["ok"] is True
    recorded_call_ids = {
        call_args.kwargs["tool_call_id"]
        for call_args in service.record_tool_action.await_args_list
    }
    assert recorded_call_ids == {"call_debug_envelope"}
    assert all(
        call_args.kwargs["thread_id"] == "thr_01a100c4a1897a2d8d40b568d23fcbe6"
        for call_args in service.record_tool_action.await_args_list
    )
    recorded_bindings = [
        call_args.kwargs["extension_catalog_binding"]
        for call_args in service.record_tool_action.await_args_list
    ]
    assert len(recorded_bindings) == 1
    recorded_binding = recorded_bindings[0]
    assert recorded_binding is not None
    assert recorded_binding.target_id == "list_breakpoints"
    assert recorded_binding.binding_id.startswith("ext-catalog:v3:")
    assert recorded_binding.binding_hash.startswith("sha256:")
    assert recorded_binding.target_schema_hash.startswith("sha256:")


@pytest.mark.asyncio
async def test_extension_invoker_dispatches_configured_tool_without_skill_activation(
    custom_tool_factory_context: CustomToolFactoryContext,
):
    custom_tool = create_test_tool_2(custom_tool_factory_context)
    invoker = create_extension_tool_invoker_tool(
        [custom_tool],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
            [custom_tool]
        ),
    )

    result = await invoker.ainvoke(
        {
            "tool_name": "test_tool_2",
            "arguments": {},
        }
    )

    assert invoker.name == EXTENSION_TOOL_INVOKER_NAME
    assert set(invoker.args) == {"tool_name", "arguments"}
    assert result == "4568"


@pytest.mark.asyncio
async def test_extension_invoker_rechecks_effective_execution_policy(
    custom_tool_factory_context: CustomToolFactoryContext,
):
    custom_tool = create_test_tool_2(custom_tool_factory_context)
    invoker = create_extension_tool_invoker_tool(
        [custom_tool],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
            [custom_tool]
        ),
        is_tool_execution_enabled=lambda _tool: False,
    )

    # 权限撤销在执行点拒绝，并按 envelope 合同返回真实 paired 错误结果。
    denied_result = await invoker.ainvoke(
        {"tool_name": "test_tool_2", "arguments": {}},
    )
    assert isinstance(denied_result, str)
    assert "已被策略禁用" in denied_result


@pytest.mark.asyncio
async def test_extension_invoker_preserves_local_extra_policy_and_rejects_unknown_envelope_arguments(
    custom_tool_factory_context: CustomToolFactoryContext,
):
    custom_tool = create_test_tool_2(custom_tool_factory_context)
    invoker = create_extension_tool_invoker_tool(
        [custom_tool],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
            [custom_tool]
        ),
    )

    result = await invoker.ainvoke(
        {
            "tool_name": "test_tool_2",
            "arguments": {"unexpected": True},
        }
    )
    assert isinstance(result, str)
    # 本地模型的默认 extra=ignore 必须保持；信封自身仍拒绝后端注入字段。
    assert result == "4568"
    assert await custom_tool.ainvoke({"unexpected": True}) == result

    with pytest.raises(ValidationError):
        invoker.args_schema.model_validate(
            {
                "tool_name": "test_tool_2",
                "arguments": {},
                "session_id": "must-not-be-accepted",
            }
        )


def test_extension_invoker_description_is_fixed_and_contains_no_target_schema():
    from langchain_core.tools import tool

    @tool
    def visible_extension(value: str) -> str:
        """可见扩展工具。"""
        return value

    @tool
    def hidden_extension(secret: str) -> str:
        """隐藏扩展工具。"""
        return secret

    invoker = create_extension_tool_invoker_tool(
        [visible_extension, hidden_extension],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
            [visible_extension, hidden_extension]
        ),
    )

    assert "visible_extension" not in invoker.description
    assert "可见扩展工具" not in invoker.description
    assert "hidden_extension" not in invoker.description
    assert "隐藏扩展工具" not in invoker.description
    assert set(invoker.args) == {"tool_name", "arguments"}


@pytest.mark.asyncio
async def test_extension_invoker_executes_mcp_style_target_and_validates_schema():
    from langchain_core.tools import tool

    @tool
    def mcp_status(value: str) -> str:
        """MCP 状态查询 stub。"""
        return f"status:{value}"

    mcp_status = mcp_status.model_copy(
        update={"metadata": {"mcp_server_id": "tui-mcp", "mcp_connection_ref": "mcp-connection:v1:" + "a" * 32 + ":1"}}
    )
    invoker = create_extension_tool_invoker_tool(
        [mcp_status],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
            [mcp_status]
        ),
    )

    result = await invoker.ainvoke(
        {
            "tool_name": "mcp_status",
            "arguments": {"value": "ready"},
        }
    )

    assert result == "status:ready"


@pytest.mark.asyncio
async def test_extension_invoker_uses_ainvoke_for_mcp_style_base_tool():
    from langchain_core.tools import BaseTool

    class AinvokeOnlyMcpTool(BaseTool):
        name: str = "mcp_async_status"
        description: str = "只暴露 ainvoke 的 MCP stub。"

        def _run(self, value: str) -> str:
            raise AssertionError("该 stub 不应走同步 _run")

        async def ainvoke(self, input, config=None, **kwargs):
            return f"async-status:{input['value']}"

    ainvoke_only = AinvokeOnlyMcpTool()
    invoker = create_extension_tool_invoker_tool(
        [ainvoke_only],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
            [ainvoke_only]
        ),
    )

    result = await invoker.ainvoke(
        {
            "tool_name": "mcp_async_status",
            "arguments": {"value": "ready"},
        }
    )

    assert result == "async-status:ready"


@pytest.mark.asyncio
async def test_extension_invoker_preserves_container_injected_invocation_context(
    custom_tool_factory_context: CustomToolFactoryContext,
) -> None:
    from langchain_core.tools import tool

    context = custom_tool_factory_context

    @tool
    def context_aware() -> str:
        """读取由统一执行中间件注入到容器的调用 ID。"""
        return context.invocation_context.require_tool_call_id()

    invoker = create_extension_tool_invoker_tool(
        [context_aware],
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
            [context_aware]
        ),
    )
    token = context.invocation_context.set_tool_call_id("call_from_outer_invoker")
    try:
        result = await invoker.ainvoke(
            {
                "tool_name": "context_aware",
                "arguments": {},
            }
        )
    finally:
        context.invocation_context.reset_tool_call_id(token)

    assert result == "call_from_outer_invoker"
