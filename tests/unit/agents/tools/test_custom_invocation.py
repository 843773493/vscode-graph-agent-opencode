from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import ToolMessage
from pydantic import ValidationError

from app.agents.custom_tools import CustomToolFactoryContext, build_custom_tools
from app.agents.graph_tool_adapter import extract_agent_tools_by_name
from app.agents.policy import DEBUGGING_TOOL_GROUP, parse_custom_tool_specs
from app.agents.policy.tool_groups import DEBUGGING_TOOL_NAMES
from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.agents.tool_invocation_context import (
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)
from app.agents.tools.custom_invocation import (
    create_extension_tool_invoker_tool,
    seal_extension_catalog_binding_from_tools,
    sealed_extension_catalog_binding_resolver,
)
from app.agents.tools.debugging import create_debugging_tools
from app.schemas.internal_v2.node_debug import NodeDebugStateDTO
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionTargetBindingInput,
    build_extension_catalog_binding,
)
from app.services.infrastructure.tool_catalog_service import ToolCatalogService

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
        thread_id="main",
        status="idle",
    )


def _debug_tools(tmp_path: Path) -> list:
    service = MagicMock()
    service.get_state = AsyncMock(return_value=_idle_state())
    service.record_tool_action = AsyncMock()
    return create_debugging_tools(
        session_id="ses_envelope",
        workspace_root=tmp_path,
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
async def test_envelope_only_accepts_tool_name_and_arguments(tmp_path: Path) -> None:
    debug_tools = _debug_tools(tmp_path)
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
async def test_envelope_rejects_unknown_and_runtime_arguments_before_target(
    tmp_path: Path,
) -> None:
    service = MagicMock()
    service.get_state = AsyncMock(return_value=_idle_state())
    service.record_tool_action = AsyncMock()
    tools = create_debugging_tools(
        session_id="ses_envelope",
        workspace_root=tmp_path,
        node_debug_service=service,
        invocation_context=ToolInvocationContext(),
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
        assert "未知参数" in result, result
        assert field_name in result, result
        assert "list_breakpoints" in result, result

    result = await envelope.ainvoke(
        {
            "tool_name": "list_breakpoints",
            "arguments": {"unexpected": 1},
        }
    )
    assert "未知参数" in result
    assert "unexpected" in result
    service.get_state.assert_not_awaited()

    ok_result = await envelope.ainvoke(
        {"tool_name": "list_breakpoints", "arguments": {}}
    )
    assert json.loads(ok_result)["ok"] is True


def test_envelope_description_and_schema_do_not_expose_target_names(
    tmp_path: Path,
) -> None:
    debug_tools = _debug_tools(tmp_path)
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


def test_provider_tool_list_contains_only_fixed_envelope_for_debugging(
    tmp_path: Path,
) -> None:
    debug_tools = _debug_tools(tmp_path)
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


def test_envelope_schema_is_stable_when_debugging_targets_change(
    tmp_path: Path,
) -> None:
    tools = _debug_tools(tmp_path)
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
    catalog = ToolCatalogService(
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


def _factory_context(tmp_path: Path, service: MagicMock) -> CustomToolFactoryContext:
    return CustomToolFactoryContext(
        session_id="ses_envelope",
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
        terminal_manager_client=MagicMock(),
        browser_manager_client=MagicMock(),
        invocation_context=ToolInvocationContext(),
        node_debug_service=service,
    )


def test_configured_debugging_specs_resolve_into_envelope_targets(
    tmp_path: Path,
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
    tools = build_custom_tools(raw_specs, context=_factory_context(tmp_path, service))

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
async def test_envelope_preserves_original_tool_call_id_pairing(
    tmp_path: Path,
) -> None:
    service = MagicMock()
    service.get_state = AsyncMock(return_value=_idle_state())
    service.record_tool_action = AsyncMock()
    # 生产装配把同一个 ToolInvocationContext 交给工具与 middleware；
    # 工具身份配对依赖这条唯一上下文。
    context = ToolInvocationContext()
    tools = create_debugging_tools(
        session_id="ses_envelope",
        workspace_root=tmp_path,
        node_debug_service=service,
        invocation_context=context,
    )
    envelope = create_extension_tool_invoker_tool(
        tools,
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(tools),
        invocation_context=context,
    )
    middleware = ToolInvocationContextMiddleware(context)
    request = type(
        "Request",
        (),
        {
            "tool_call": {
                "id": "call_debug_envelope",
                "name": EXTENSION_TOOL_INVOKER_NAME,
                "args": {"tool_name": "list_breakpoints", "arguments": {}},
            }
        },
    )()

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
        call_args.kwargs["thread_id"] == "main"
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
    assert recorded_binding.binding_id.startswith("ext-catalog:v1:")
    assert recorded_binding.binding_hash.startswith("sha256:")
    assert recorded_binding.target_schema_hash.startswith("sha256:")


# ---------------------------------------------------------------------------
# E4 第三段：sealed ExtensionCatalogBindingRef resolve 端口合同
# ---------------------------------------------------------------------------


def _extension_binding_inputs(tools):
    """从工具列表构建 sealed binding 输入；origin/server_id 与注册元数据一致。"""
    inputs = []
    for tool_item in tools:
        metadata = getattr(tool_item, "metadata", None)
        server_id = (
            metadata.get("mcp_server_id") if isinstance(metadata, dict) else None
        )
        inputs.append(
            ExtensionTargetBindingInput(
                target_id=tool_item.name,
                origin="mcp" if server_id else "custom",
                args={},
                server_id=server_id,
            )
        )
    return inputs


def _sealed_binding_resolver(tools):
    """按工具快照封存 binding ref，并适配为 invoker 的 typed resolve 端口。"""
    binding = build_extension_catalog_binding(
        catalog_revision="sha256:"
        + hashlib.sha256(b"test-catalog-rev").hexdigest(),
        generation=1,
        targets=_extension_binding_inputs(tools),
    )
    return sealed_extension_catalog_binding_resolver(binding), binding


@pytest.mark.asyncio
async def test_sealed_binding_resolver_resolves_sealed_custom_target():
    from langchain_core.tools import tool

    @tool
    def sealed_custom(value: str) -> str:
        """sealed binding 端口下的 custom target。"""
        return f"sealed:{value}"

    resolver, _binding = _sealed_binding_resolver([sealed_custom])
    invoker = create_extension_tool_invoker_tool(
        [sealed_custom],
        catalog_binding_resolver=resolver,
    )

    result = await invoker.ainvoke(
        {"tool_name": "sealed_custom", "arguments": {"value": "ok"}},
    )

    assert result == "sealed:ok"


@pytest.mark.asyncio
async def test_sealed_resolution_is_isolated_from_live_catalog_changes():
    """live 目录新增 target 不影响封存解析；未封存 target 显式失败。"""
    from langchain_core.tools import tool

    @tool
    def sealed_alpha() -> str:
        """封存时存在的 target。"""
        return "alpha"

    @tool
    def live_beta() -> str:
        """封存后才加入 live 目录的 target。"""
        return "beta"

    # binding 只封存 sealed_alpha 快照；live 注册表随后包含额外 target。
    resolver, binding = _sealed_binding_resolver([sealed_alpha])
    invoker = create_extension_tool_invoker_tool(
        [sealed_alpha, live_beta],
        catalog_binding_resolver=resolver,
    )

    alpha_result = await invoker.ainvoke(
        {"tool_name": "sealed_alpha", "arguments": {}},
    )
    assert alpha_result == "alpha"
    assert binding.resolve("sealed_alpha").target_id == "sealed_alpha"
    live_beta_result = await invoker.ainvoke(
        {"tool_name": "live_beta", "arguments": {}},
    )
    assert isinstance(live_beta_result, str)
    assert "live_beta" in live_beta_result
    assert "不存在 target" in live_beta_result


@pytest.mark.asyncio
async def test_sealed_target_missing_registration_fails_closed():
    """sealed target 在注册表中已不存在时 fail closed，不回退 live 目录。"""
    from langchain_core.tools import tool

    @tool
    def sealed_gamma() -> str:
        """只在封存 ref 中存在的 target。"""
        return "gamma"

    resolver, _binding = _sealed_binding_resolver([sealed_gamma])
    invoker = create_extension_tool_invoker_tool(
        [],
        catalog_binding_resolver=resolver,
    )

    missing_result = await invoker.ainvoke(
        {"tool_name": "sealed_gamma", "arguments": {}},
    )
    assert isinstance(missing_result, str)
    assert "缺少可执行注册" in missing_result


@pytest.mark.asyncio
async def test_sealed_mcp_target_requires_matching_server_identity():
    """封存 server_id 与注册元数据不一致时显式失败；一致时正常执行。"""
    from langchain_core.tools import tool

    @tool
    def sealed_mcp_status(value: str) -> str:
        """MCP target stub。"""
        return f"status:{value}"

    registered = sealed_mcp_status.model_copy(
        update={"metadata": {"mcp_server_id": "server-a"}}
    )
    resolver, _binding = _sealed_binding_resolver([registered])
    matching_invoker = create_extension_tool_invoker_tool(
        [registered],
        catalog_binding_resolver=resolver,
    )
    drifted_invoker = create_extension_tool_invoker_tool(
        [
            sealed_mcp_status.model_copy(
                update={"metadata": {"mcp_server_id": "server-b"}}
            )
        ],
        catalog_binding_resolver=resolver,
    )

    matching_result = await matching_invoker.ainvoke(
        {"tool_name": "sealed_mcp_status", "arguments": {"value": "ready"}},
    )
    assert matching_result == "status:ready"
    drifted_result = await drifted_invoker.ainvoke(
        {"tool_name": "sealed_mcp_status", "arguments": {"value": "ready"}},
    )
    assert isinstance(drifted_result, str)
    assert "身份不一致" in drifted_result


@pytest.mark.asyncio
async def test_sealed_resolution_failure_keeps_tool_call_id_pairing():
    """tool_call 输入下，封存解析失败返回按 tool_call_id 配对的错误 ToolMessage。"""
    from langchain_core.tools import tool

    @tool
    def sealed_only() -> str:
        """唯一封存 target。"""
        return "only"

    resolver, _binding = _sealed_binding_resolver([sealed_only])
    invoker = create_extension_tool_invoker_tool(
        [sealed_only],
        catalog_binding_resolver=resolver,
    )

    result = await invoker.ainvoke(
        {
            "type": "tool_call",
            "id": "call_sealed_miss",
            "name": invoker.name,
            "args": {"tool_name": "not_in_sealed_ref", "arguments": {}},
        }
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call_sealed_miss"
    assert "not_in_sealed_ref" in result.text


@pytest.mark.asyncio
async def test_execution_revocation_fails_at_sealed_target_execution_point():
    """封存调用在执行点重验最新策略，撤权时返回配对失败。"""
    from langchain_core.tools import tool

    @tool
    def revocable_target() -> str:
        """可撤权的 sealed target。"""
        return "should-not-run"

    resolver, _binding = _sealed_binding_resolver([revocable_target])
    enabled = True
    invoker = create_extension_tool_invoker_tool(
        [revocable_target],
        catalog_binding_resolver=resolver,
        is_tool_execution_enabled=lambda _target: enabled,
    )

    enabled_result = await invoker.ainvoke(
        {"tool_name": "revocable_target", "arguments": {}}
    )
    assert enabled_result == "should-not-run"

    enabled = False
    revoked_result = await invoker.ainvoke(
        {
            "type": "tool_call",
            "id": "call_revoked_target",
            "name": invoker.name,
            "args": {"tool_name": "revocable_target", "arguments": {}},
        }
    )
    assert isinstance(revoked_result, ToolMessage)
    assert revoked_result.status == "error"
    assert revoked_result.tool_call_id == "call_revoked_target"
    assert "已被策略禁用" in revoked_result.text
