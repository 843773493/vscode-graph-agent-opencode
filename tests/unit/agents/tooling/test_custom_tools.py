from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langgraph.prebuilt.tool_node import ToolRuntime

from app.agents.custom_tools import (
    CustomToolFactoryContext,
    build_custom_tools,
)
from app.agents.policy import custom_tool_spec_names, parse_custom_tool_specs
from app.agents.tool_invocation_context import ToolInvocationContext

TEST_TOOL_SPEC = {
    "name": "test_tool_2",
    "factory": "app.agents.tools.testing:create_test_tool_2",
}


def _context() -> CustomToolFactoryContext:
    return CustomToolFactoryContext(
        session_id="ses_test",
        agent_id="default",
        sender_agent_id="default",
        workspace_root=Path.cwd(),
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


def test_build_custom_tools_loads_factory_from_config_spec() -> None:
    tools = build_custom_tools([TEST_TOOL_SPEC], context=_context())

    assert [tool.name for tool in tools] == ["test_tool_2"]
    assert tools[0].invoke({}) == "4568"


def test_product_skill_associations_require_actual_browser_or_debug_producer() -> None:
    from app.agents.policy.tool_capability import metadata_for_tool
    from app.schemas.gateway.service_plugin import PluginToolBinding
    from app.schemas.gateway.skill_sources import PluginSkillSourceDTO
    from app.services.infrastructure.mcp.tool_definitions import (
        SkillToolSource,
        _tool_skill_associations,
        definition_from_tool,
    )

    tools = build_custom_tools([
        {"tool_id": "openBrowserPage"},
        {"name": "evaluate_expression", "factory": "app.agents.tools.debugging:create_debugging_tool",
         "options": {"tool_name": "evaluate_expression"}},
    ], context=replace(_context(), node_debug_service=MagicMock()))
    for tool, plugin_id in zip(tools, ("browser", "debugging"), strict=True):
        source = SkillToolSource(
            name=f"{plugin_id}-guide", source_id="gateway-source", entry_identity=f"plugin:{plugin_id}",
            display_uri="boxteam://gateway/target/resources/skills/guide/SKILL.md",
            plugin=PluginSkillSourceDTO(
                package_id="boxteam.developer-tools", package_version="0.1.0", plugin_id=plugin_id,
                package_digest="a" * 64, contribution_id="guide",
                tool_bindings=[PluginToolBinding(tool_id=tool.name, contract_version=1)],
            ),
        )
        metadata = metadata_for_tool(tool, origin="custom")
        definition = definition_from_tool(tool, metadata)
        available, = _tool_skill_associations(tool.name, definition, (source,), (), gateway_id="target")
        assert available.status == "available"
        assert available.entry_identity == source.entry_identity
        # 同名但并非原业务工厂发布的工具，不能借名称继承产品合同。
        unverified, = _tool_skill_associations(
            tool.name, replace(definition, product_tool_contract=None), (source,), (), gateway_id="target",
        )
        assert (unverified.status, unverified.reason) == ("unavailable", "product-tool-source-unverified")
        assert source.plugin is not None
        incompatible_source = replace(source, plugin=source.plugin.model_copy(update={
            "tool_bindings": [PluginToolBinding(tool_id=tool.name, contract_version=2)],
        }))
        incompatible, = _tool_skill_associations(tool.name, definition, (incompatible_source,), (), gateway_id="target")
        assert incompatible.status == "unavailable"


def test_build_custom_tools_passes_each_spec_options_to_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received_options: list[dict[str, object]] = []

    def fake_factory(context: CustomToolFactoryContext):
        received_options.append(dict(context.tool_options))
        from app.agents.tools.testing import create_test_tool_2

        return create_test_tool_2(context)

    monkeypatch.setattr(
        "app.agents.custom_tools.load_custom_tool_factory",
        lambda _factory_path: fake_factory,
    )

    build_custom_tools(
        [{**TEST_TOOL_SPEC, "options": {"mode": "strict"}}],
        context=_context(),
    )

    assert received_options == [{"mode": "strict"}]


def test_custom_tool_spec_names_reads_names_from_config_spec() -> None:
    assert custom_tool_spec_names([TEST_TOOL_SPEC]) == {"test_tool_2"}


def test_builtin_tool_id_resolves_current_factory_and_preserves_stable_config() -> None:
    specs = parse_custom_tool_specs([{"tool_id": "read_context"}])

    assert specs[0].name == "read_context"
    assert specs[0].factory_path.endswith(":create_read_context_tool")
    assert specs[0].to_config() == {"tool_id": "read_context"}


def test_custom_tool_spec_parser_strips_fields_and_copies_options() -> None:
    options = {"mode": "strict"}

    specs = parse_custom_tool_specs(
        [
            {
                "name": "  test_tool_2  ",
                "factory": "  app.agents.tools.testing:create_test_tool_2  ",
                "options": options,
                "description": "  测试工具  ",
            }
        ]
    )
    options["mode"] = "changed"

    assert specs[0].name == "test_tool_2"
    assert specs[0].factory_path == ("app.agents.tools.testing:create_test_tool_2")
    assert specs[0].options == {"mode": "strict"}
    assert specs[0].description == "测试工具"


def test_custom_tool_spec_parser_rejects_duplicate_normalized_names() -> None:
    with pytest.raises(ValueError, match="重复扩展工具名: test_tool_2"):
        parse_custom_tool_specs(
            [
                TEST_TOOL_SPEC,
                {
                    **TEST_TOOL_SPEC,
                    "name": " test_tool_2 ",
                },
            ]
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "   ", r"\.name 必须是非空字符串"),
        ("factory", "   ", r"\.factory 必须是非空字符串"),
        ("factory", "missing_colon", "必须使用"),
        ("options", [], "options 必须是对象"),
    ],
)
def test_custom_tool_spec_parser_rejects_invalid_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        parse_custom_tool_specs([{**TEST_TOOL_SPEC, field: value}])


def test_build_custom_tools_rejects_name_only_config() -> None:
    with pytest.raises(ValueError, match="不支持只写工具名"):
        build_custom_tools(["test_tool_2"], context=_context())


def test_build_custom_tools_rejects_non_object_options() -> None:
    with pytest.raises(TypeError, match="options 必须是对象"):
        build_custom_tools(
            [{**TEST_TOOL_SPEC, "options": "invalid"}],
            context=_context(),
        )


def test_build_custom_tools_rejects_hidden_runtime_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.tools import tool

    @tool
    def test_tool_2(value: str, runtime: ToolRuntime) -> str:
        """测试错误地使用 LangGraph 隐藏运行时参数的扩展工具。"""
        del runtime
        return value

    monkeypatch.setattr(
        "app.agents.custom_tools.load_custom_tool_factory",
        lambda _factory_path: lambda _context: test_tool_2,
    )

    with pytest.raises(
        TypeError,
        match="必须由 CustomToolFactoryContext 容器注入",
    ):
        build_custom_tools([TEST_TOOL_SPEC], context=_context())
